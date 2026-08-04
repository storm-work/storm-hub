"""Worker phase-machine skeleton (pure) — the flow the live engine plugs into.

This drives the confirmed worker lifecycle (login → warmup → optional ISP swap → hold-until-signal →
discover → apply/book) using the DOMAIN substate transitions (so every hop is validated), branching
on role/no_book/warmup_only. The actual browser/login/book work is injected as `EngineOps`; in
Phase 2b those become real Playwright/solver/pool calls, and the worker runtime wraps this async.

Keeping it pure means the whole control flow — including the scout-vs-apply and dry-run branches —
is unit-tested with stub ops, no browser required.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from libs.domain.enums import WorkerRole, WorkerSubstate
from libs.domain.state_machines import worker_substate_transition

from .config import EngineConfig


@runtime_checkable
class EngineOps(Protocol):
    """The live operations the phase-machine invokes. Phase 2b implements these with the real engine."""

    def do_login(self) -> bool: ...
    def do_warmup(self) -> bool: ...
    def do_swap(self) -> str | None: ...          # returns ISP exit ip if a swap happened, else None
    def await_signal(self) -> str: ...            # blocks until 'slot_open'; returns the signal name
    def do_discover(self) -> list[dict]: ...      # list of free slots [{date, period}, ...]
    def push_slots(self, slots: list[dict]) -> None: ...   # scout -> pool
    def lease_slot(self) -> dict | None: ...      # apply -> lease from pool (None if none available)
    def do_apply(self, slot: dict) -> str: ...    # 'booked' | 'bounced'


@dataclass
class PhaseResult:
    outcome: str                       # booked | discovered | dry_run | warmed | failed
    final_substate: WorkerSubstate
    detail: str = ""
    visited: list[WorkerSubstate] = field(default_factory=list)


class PhaseMachine:
    """Sequences a single worker run. Records every substate for observability/testing and validates
    each transition against the domain state machine (illegal hops raise, catching flow bugs early)."""

    def __init__(self, cfg: EngineConfig, ops: EngineOps, on_transition=None):
        self.cfg = cfg
        self.ops = ops
        # optional callback(substate, detail) fired on every substate change — the worker
        # runtime uses it to emit live status_change events over the bus.
        self._on_transition = on_transition
        self._sub = WorkerSubstate.IDLE
        self.visited: list[WorkerSubstate] = [WorkerSubstate.IDLE]

    def _to(self, nxt: WorkerSubstate, detail: str = "") -> None:
        worker_substate_transition(self._sub, nxt)   # raises IllegalTransition on a bad hop
        self._sub = nxt
        self.visited.append(nxt)
        if self._on_transition is not None:
            self._on_transition(nxt, detail)

    def _fail(self, step: str) -> PhaseResult:
        self._to(WorkerSubstate.FAILED)
        return PhaseResult("failed", self._sub, step, self.visited)

    def run(self) -> PhaseResult:
        # login
        self._to(WorkerSubstate.LOGGING_IN)
        if not self.ops.do_login():
            return self._fail("login")
        self._to(WorkerSubstate.LOGGED_IN)

        # warmup
        self._to(WorkerSubstate.WARMING_UP)
        if not self.ops.do_warmup():
            return self._fail("warmup")
        self._to(WorkerSubstate.WARMED)
        if self.cfg.warmup_only:
            return PhaseResult("warmed", self._sub, "warmup_only", self.visited)

        # optional ISP swap (warm on SOAX -> swap exit to ISP, the confirmed hold+book path)
        exit_ip = self.ops.do_swap()
        if exit_ip:
            self._to(WorkerSubstate.SWAPPED)

        # hold until the slot-open signal
        self._to(WorkerSubstate.HOLDING)
        self.ops.await_signal()

        # discover
        self._to(WorkerSubstate.DISCOVERING)
        found = self.ops.do_discover()

        # scout: capture-only — push to the shared pool and finish; NEVER books.
        if self.cfg.role == WorkerRole.SCOUT or self.cfg.capture_only:
            self.ops.push_slots(found)
            return PhaseResult("discovered", self._sub, f"pushed {len(found)}", self.visited)

        # apply: prefer a pool lease (cross-service handover), else fall back to own discovery.
        slot = self.ops.lease_slot() or (found[0] if found else None)
        if slot is None:
            self._to(WorkerSubstate.HOLDING)
            return PhaseResult("discovered", self._sub, "no bookable slot", self.visited)

        self._to(WorkerSubstate.APPLYING)
        if self.cfg.no_book:
            # dry-run rehearsal (test booking with no_book): reach applying, never submit.
            self._to(WorkerSubstate.HOLDING)
            return PhaseResult("dry_run", self._sub, "no_book: not submitted", self.visited)

        outcome = self.ops.do_apply(slot)
        if outcome == "booked":
            self._to(WorkerSubstate.BOOKED)
            return PhaseResult("booked", self._sub, str(slot), self.visited)

        # bounced (phantom/full slot) — recover to hold rather than churn the session.
        self._to(WorkerSubstate.HOLDING)
        return PhaseResult("discovered", self._sub, "apply bounced", self.visited)

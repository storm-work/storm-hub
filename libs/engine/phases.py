"""Worker phase machine — the confirmed scout/apply control model (operator spec 2026-08-04).

Browser logic is IDENTICAL for scout and apply (one EngineOps implementation); the ONLY difference
is phase sequencing after the shared warm-up:

  common:  login -> warmup -> swap-to-ISP -> hold
  apply :  hold -> (await slot ASSIGNMENT from the orchestrator) -> applying/book
           fallback: if all scouts are invalid/dead -> self-discover -> book
  scout :  hold -> (await slot-open) -> discovering -> push pool (on found) -> polling (loop)
           until booking finishes / all apply workers closed / manual stop

The orchestrator (hub+supervisor) owns: firing slot-open, firing slot-found (when a scout pushes),
assigning pooled slots to holding apply workers, and tracking scout validity for the fallback.

Pure + injectable: the EngineOps calls block on real signals/assignments in production; stubs drive
them in tests, so the whole control flow is unit-tested with no browser.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from libs.domain.enums import WorkerRole, WorkerSubstate
from libs.domain.state_machines import worker_substate_transition

from .config import EngineConfig


@runtime_checkable
class EngineOps(Protocol):
    """Live operations the phase machine invokes (same impl for both roles)."""

    # ── shared warm-up ────────────────────────────────────────────────────────
    def do_login(self) -> bool: ...
    def do_warmup(self) -> bool: ...
    def do_swap(self) -> str | None: ...          # ISP exit ip if swapped, else None
    def await_slot_open(self) -> str: ...         # block until the slot-open signal

    # ── scout ─────────────────────────────────────────────────────────────────
    def do_discover(self) -> list[dict]: ...      # scan calendar -> free slots
    def push_slots(self, slots: list[dict]) -> None: ...   # push to pool (orchestrator fires slot-found)
    def scout_should_continue(self) -> bool: ...  # False when booking done / applies closed / stop
    def poll_gap(self) -> None: ...               # wait between discovery polls

    # ── apply ─────────────────────────────────────────────────────────────────
    def await_assignment(self) -> dict | None: ...  # block until a pooled slot is assigned (None = none)
    def scouts_invalid(self) -> bool: ...           # True when all scouts dead -> apply must self-scout
    def do_apply(self, slot: dict) -> str: ...      # 'booked' | 'bounced'


@dataclass
class PhaseResult:
    outcome: str                       # booked | discovered | dry_run | warmed | failed | held
    final_substate: WorkerSubstate
    detail: str = ""
    visited: list[WorkerSubstate] = field(default_factory=list)


class PhaseMachine:
    def __init__(self, cfg: EngineConfig, ops: EngineOps, on_transition=None):
        self.cfg = cfg
        self.ops = ops
        self._on_transition = on_transition
        self._sub = WorkerSubstate.IDLE
        self.visited: list[WorkerSubstate] = [WorkerSubstate.IDLE]

    def _to(self, nxt: WorkerSubstate, detail: str = "") -> None:
        worker_substate_transition(self._sub, nxt)
        self._sub = nxt
        self.visited.append(nxt)
        if self._on_transition is not None:
            self._on_transition(nxt, detail)

    def _fail(self, step: str) -> PhaseResult:
        self._to(WorkerSubstate.FAILED)
        return PhaseResult("failed", self._sub, step, self.visited)

    # ── shared warm-up: login -> warmup -> swap -> hold ───────────────────────
    def _warmup(self) -> PhaseResult | None:
        self._to(WorkerSubstate.LOGGING_IN)
        if not self.ops.do_login():
            return self._fail("login")
        self._to(WorkerSubstate.LOGGED_IN)
        self._to(WorkerSubstate.WARMING_UP)
        if not self.ops.do_warmup():
            return self._fail("warmup")
        self._to(WorkerSubstate.WARMED)
        if self.cfg.warmup_only:
            return PhaseResult("warmed", self._sub, "warmup_only", self.visited)
        if self.ops.do_swap():
            self._to(WorkerSubstate.SWAPPED)
        self._to(WorkerSubstate.HOLDING)
        return None

    def run(self) -> PhaseResult:
        warm = self._warmup()
        if warm is not None:
            return warm
        if self.cfg.role == WorkerRole.SCOUT:
            return self._run_scout()
        return self._run_apply()

    # ── scout: discover/push/poll loop until told to stop ─────────────────────
    def _run_scout(self) -> PhaseResult:
        self.ops.await_slot_open()
        pushed_total = 0
        while self.ops.scout_should_continue():
            self._to(WorkerSubstate.DISCOVERING)
            found = self.ops.do_discover()
            if found:
                self.ops.push_slots(found)     # orchestrator fires slot-found
                pushed_total += len(found)
            self._to(WorkerSubstate.POLLING)
            self.ops.poll_gap()
        return PhaseResult("discovered", self._sub, f"pushed {pushed_total}", self.visited)

    # ── apply: hold -> await assignment -> book; fallback self-scout ──────────
    def _run_apply(self) -> PhaseResult:
        self.ops.await_slot_open()             # apply holds through slot-open, awaiting assignment
        slot = self.ops.await_assignment()
        if slot is None and self.ops.scouts_invalid():
            # FALLBACK: no valid scouts -> apply self-discovers
            self._to(WorkerSubstate.DISCOVERING)
            found = self.ops.do_discover()
            slot = found[0] if found else None
        if slot is None:
            return PhaseResult("held", self._sub, "no slot assigned", self.visited)
        self._to(WorkerSubstate.APPLYING)
        if self.cfg.no_book:
            self._to(WorkerSubstate.HOLDING)
            return PhaseResult("dry_run", self._sub, "no_book: not submitted", self.visited)
        outcome = self.ops.do_apply(slot)
        if outcome == "booked":
            self._to(WorkerSubstate.BOOKED)
            return PhaseResult("booked", self._sub, str(slot), self.visited)
        self._to(WorkerSubstate.HOLDING)
        return PhaseResult("held", self._sub, "apply bounced", self.visited)

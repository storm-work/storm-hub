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
from datetime import date
from typing import Protocol, runtime_checkable

from libs.domain.enums import WorkerRole, WorkerSubstate
from libs.domain.state_machines import worker_substate_transition

from . import pool_manager
from .config import EngineConfig

# False-slot-open revert (operator spec 2026-08-14): await_discover_task's third possible return,
# distinct from a real date list and from None (stop). The Hub decided the slot-open it fired was
# false (a long window with zero slots found) and is dropping the booking back to holding -- the
# scout returns to await_slot_open with its session alive, ready for a genuinely new window.
# Defined here, next to the EngineOps Protocol that returns it, so apps/worker imports from libs
# (the established direction) rather than libs reaching into apps.
INACTIVATE = object()


def _flatten_found(results: list[dict]) -> list[dict]:
    """do_discover's per-date outcome list -> a flat list of {"date","period"} found slots
    (push_slots/the assignment shape stays scalar-per-period, matching apps/hub/events.py)."""
    return [
        {"date": r["date"], "period": p}
        for r in results
        if r.get("outcome") == "found"
        for p in r.get("periods", [])
    ]


@runtime_checkable
class EngineOps(Protocol):
    """Live operations the phase machine invokes (same impl for both roles)."""

    # ── shared warm-up ────────────────────────────────────────────────────────
    def do_login(self) -> bool: ...
    def do_warmup(self) -> bool: ...
    def do_swap(self) -> str | None: ...          # ISP exit ip if swapped, else None
    def await_slot_open(self) -> str: ...         # block until the slot-open signal

    # ── scout ─────────────────────────────────────────────────────────────────
    # Confirmed 4-phase model (operator spec 2026-08-05): SINGLE-mode probe (one scout at a time)
    # before the first slot is found, then SPLIT-mode polling (date range partitioned across scouts)
    # after. The orchestrator (Hub) owns which dates go to which scout each round; do_discover's
    # per-date outcome must stay 3-way (found/empty/failed) -- bug fix #6: a WAF-blocked/failed scan
    # must never be silently treated as 'empty' and pruned from the pool.
    # Returns a date list, None (stop), or the INACTIVATE sentinel (apps/worker/bus_signals.py) --
    # the Hub's false-slot-open revert: go BACK to await_slot_open and keep holding, don't terminate.
    def await_discover_task(self) -> list[str] | None: ...
    # do_discover: one entry per requested date, {"date": d, "outcome": "found"|"empty"|"failed",
    # "periods": [...]} ("periods" present only when outcome == "found").
    def do_discover(self, dates: list[str]) -> list[dict]: ...
    def push_slots(self, slots: list[dict]) -> None: ...   # push to pool (orchestrator fires slot-found)
    def report_dates(self, outcomes: list[dict]) -> None: ...  # [{"date":..., "outcome":...}, ...]
    def poll_gap(self) -> None: ...               # wait between discovery polls

    # ── apply ─────────────────────────────────────────────────────────────────
    def await_assignment(self) -> dict | None: ...  # block until a pooled slot is assigned (None = none)
    def scouts_invalid(self) -> bool: ...           # True when all scouts dead -> apply must self-scout
    def do_apply(self, slot: dict) -> str: ...      # 'booked' | 'bounced'

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def is_stopped(self) -> bool: ...  # True if the last wait ended because of an explicit stop --
    # distinguishes "operator stopped me" (-> outcome "stopped") from "genuinely no slot assigned"
    # (-> outcome "held") when await_assignment() returns None, which is ambiguous between the two.


@dataclass
class PhaseResult:
    outcome: str                       # booked | discovered | dry_run | warmed | failed | held | stopped
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

    # ── scout: single-probe -> split-poll loop, driven by the orchestrator's task assignment ──
    def _run_scout(self) -> PhaseResult:
        # "stopped" is a distinct outcome from "held" -- an explicit operator stop must map to
        # WorkerStatus.STOPPED (events.py's _TERMINAL_STATUS already has this key; it just wasn't
        # ever emitted here), never to the FAILED the "held" bucket falls back to. Conflating the two
        # was a confirmed bug: the Hub sets the worker STOPPED, the worker then reports "held", the
        # event consumer overwrites STOPPED with FAILED, and hold-respawn relaunches a worker the
        # operator explicitly stopped.
        if self.ops.await_slot_open() == "stop":
            return PhaseResult("stopped", self._sub, "stopped before slot_open", self.visited)
        pushed_total = 0
        while True:
            dates = self.ops.await_discover_task()
            if dates is None:               # orchestrator says stop (booking done / applies closed)
                break
            if dates is INACTIVATE:
                # False slot-open (operator spec 2026-08-14): the Hub decided this open was false --
                # drop back to plain holding and wait for a genuinely new slot-open rather than
                # grinding discovery (which risks rate-limiting for nothing). Not a stop: the
                # session stays alive and this scout is fully reusable in the next window.
                self._to(WorkerSubstate.HOLDING, "slot-open inactivated")
                if self.ops.await_slot_open() == "stop":
                    return PhaseResult("stopped", self._sub, "stopped while re-holding after "
                                       "slot-open inactivation", self.visited)
                continue
            self._to(WorkerSubstate.DISCOVERING)
            results = self.ops.do_discover(dates)
            found = _flatten_found(results)
            if found:
                self.ops.push_slots(found)     # orchestrator fires slot-found on the first push
                pushed_total += len(found)
            self.ops.report_dates([{"date": r["date"], "outcome": r["outcome"]} for r in results])
            self._to(WorkerSubstate.POLLING)
            self.ops.poll_gap()
        # dates is None both on an explicit stop and (in unit-test/no-bus-wired mode) on genuinely
        # running out of configured discover tasks -- is_stopped() disambiguates rather than
        # guessing from the None alone.
        if self.ops.is_stopped():
            return PhaseResult("stopped", self._sub, f"stopped while discovering, pushed {pushed_total}",
                               self.visited)
        return PhaseResult("discovered", self._sub, f"pushed {pushed_total}", self.visited)

    # ── apply: hold -> await assignment -> book; fallback self-scout ──────────
    def _run_apply(self) -> PhaseResult:
        # apply holds through slot-open, awaiting assignment. See _run_scout's comment above --
        # "stopped" (-> WorkerStatus.STOPPED) must never be reported as "held" (-> FAILED, which
        # hold-respawn then relaunches, undoing the operator's stop).
        if self.ops.await_slot_open() == "stop":
            return PhaseResult("stopped", self._sub, "stopped before slot_open", self.visited)
        slot = self.ops.await_assignment()
        if slot is None and self.ops.is_stopped():
            return PhaseResult("stopped", self._sub, "stopped while awaiting assignment", self.visited)
        if slot is None and self.ops.scouts_invalid():
            # FALLBACK: no valid scouts -> apply self-discovers over its own configured date window
            self._to(WorkerSubstate.DISCOVERING)
            d = self.cfg.discovery
            dates = pool_manager.default_date_window(
                start_date=d.start_date, end_date=d.end_date, num_days=d.num_days, today=date.today()
            )
            found = _flatten_found(self.ops.do_discover(dates))
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

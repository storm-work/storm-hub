"""SimEngineOps — deterministic EngineOps with NO browser, for the control-plane loop + tests.

Implements the confirmed scout/apply model: shared warm-up, scout single-probe/split-poll loop driven
by a configurable per-date outcome plan, apply await-assignment with optional fallback self-scout. The
live browser implementation (apps/worker/ops_live.py) satisfies the same protocol.
"""
from __future__ import annotations

import time

from libs.engine.phases import EngineOps

from .bus_signals import TIMEOUT


class SimEngineOps(EngineOps):
    def __init__(
        self,
        *,
        login_ok: bool = True,
        warmup_ok: bool = True,
        swap_ip: str | None = None,
        discover_tasks: list[list[str]] | None = None,
        # date -> {"outcome": "found"|"empty"|"failed", "periods": [...]}  (periods only for "found")
        discover_outcomes: dict[str, dict] | None = None,
        assignment: dict | None = None,   # apply: slot handed by the orchestrator (None = none)
        scouts_invalid: bool = False,     # apply fallback trigger
        apply_outcome: str = "booked",
        step_delay: float = 0.0,
        signals=None,                     # BusSignals: block on real slot-open/assign commands
        emit=None,                        # callable(type, **payload): emit an event (e.g. slots_found)
    ):
        self._login_ok = login_ok
        self._warmup_ok = warmup_ok
        self._swap_ip = swap_ip
        self._discover_tasks = (
            discover_tasks if discover_tasks is not None else [["2026-08-18"]]
        )
        self._discover_outcomes = discover_outcomes if discover_outcomes is not None else {
            "2026-08-18": {"outcome": "found", "periods": [4]},
        }
        self._task_idx = 0
        self._assignment = assignment
        self._scouts_invalid = scouts_invalid
        self._apply_outcome = apply_outcome
        self._delay = step_delay
        self._signals = signals
        self._emit = emit
        self.pushed: list[dict] = []
        self.reported_dates: list[dict] = []

    def _pause(self):
        if self._delay:
            time.sleep(self._delay)

    # shared
    def do_login(self) -> bool:
        self._pause(); return self._login_ok

    def do_warmup(self) -> bool:
        self._pause(); return self._warmup_ok

    def do_swap(self) -> str | None:
        self._pause(); return self._swap_ip

    def await_slot_open(self) -> str:
        if self._signals is None:
            self._pause(); return "slot_open"
        # Loop across as many bounded windows as it takes (BusSignals itself is bounded per-window,
        # see its docstring) -- a sim worker has no real portal session to keep alive, but it must
        # still emit SOMETHING each window so the Hub's stall-reaper doesn't mistake a healthy,
        # long-waiting hold for a dead one (the two are otherwise indistinguishable by last_seen).
        while True:
            result = self._signals.wait_slot_open()
            if result in ("slot_open", "stop"):
                return result
            if self._emit is not None:
                self._emit("log_line", text="keepalive: sim heartbeat (await_slot_open)")

    # scout
    def await_discover_task(self) -> list[str] | None:
        self._pause()
        if self._task_idx >= len(self._discover_tasks):
            return None
        dates = self._discover_tasks[self._task_idx]
        self._task_idx += 1
        return dates

    def do_discover(self, dates: list[str]) -> list[dict]:
        self._pause()
        out = []
        for d in dates:
            info = self._discover_outcomes.get(d, {"outcome": "empty"})
            entry = {"date": d, "outcome": info["outcome"]}
            if "periods" in info:
                entry["periods"] = list(info["periods"])
            out.append(entry)
        return out

    def push_slots(self, slots: list[dict]) -> None:
        self.pushed.extend(slots)
        if self._emit is not None:         # -> Hub event consumer writes them to the DB pool
            self._emit("slots_found", slots=slots)

    def report_dates(self, outcomes: list[dict]) -> None:
        self.reported_dates.extend(outcomes)

    def poll_gap(self) -> None:
        self._pause()

    # apply
    def await_assignment(self) -> dict | None:
        if self._signals is None:
            self._pause(); return self._assignment
        # Same "loop across windows, heartbeat while waiting" contract as await_slot_open above.
        # TIMEOUT (the sentinel) means "keep waiting"; a real None (an `assign slot=None` command,
        # the self-scout fallback signal) or stop must return immediately, not be swallowed.
        while True:
            result = self._signals.wait_assignment()
            if result is not TIMEOUT:
                return result
            if self._emit is not None:
                self._emit("log_line", text="keepalive: sim heartbeat (await_assignment)")

    def scouts_invalid(self) -> bool:
        return self._scouts_invalid

    def is_stopped(self) -> bool:
        return self._signals is not None and self._signals.stopped

    def do_apply(self, slot: dict) -> str:
        self._pause(); return self._apply_outcome

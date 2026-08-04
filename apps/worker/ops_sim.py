"""SimEngineOps — deterministic EngineOps with NO browser, for the control-plane loop + tests.

Implements the confirmed scout/apply model: shared warm-up, scout discover/push/poll loop (bounded
here by `scout_rounds`), apply await-assignment with optional fallback self-scout. The live browser
implementation (Phase 2b) satisfies the same protocol.
"""
from __future__ import annotations

import time

from libs.engine.phases import EngineOps


class SimEngineOps(EngineOps):
    def __init__(
        self,
        *,
        login_ok: bool = True,
        warmup_ok: bool = True,
        swap_ip: str | None = None,
        found: list[dict] | None = None,
        assignment: dict | None = None,   # apply: slot handed by the orchestrator (None = none)
        scouts_invalid: bool = False,     # apply fallback trigger
        apply_outcome: str = "booked",
        scout_rounds: int = 1,            # how many discover/poll loops a scout runs before stopping
        step_delay: float = 0.0,
        signals=None,                     # BusSignals: block on real slot-open/assign commands
        emit=None,                        # callable(type, **payload): emit an event (e.g. slots_found)
    ):
        self._login_ok = login_ok
        self._warmup_ok = warmup_ok
        self._swap_ip = swap_ip
        self._found = found if found is not None else [{"date": "2026-08-18", "period": 4}]
        self._assignment = assignment
        self._scouts_invalid = scouts_invalid
        self._apply_outcome = apply_outcome
        self._scout_rounds = scout_rounds
        self._delay = step_delay
        self._signals = signals
        self._emit = emit
        self.pushed: list[dict] = []
        self._rounds_done = 0

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
        if self._signals is not None:      # block on the real scheduler-fired signal
            return self._signals.wait_slot_open()
        self._pause(); return "slot_open"

    # scout
    def do_discover(self) -> list[dict]:
        self._pause(); return list(self._found)

    def push_slots(self, slots: list[dict]) -> None:
        self.pushed.extend(slots)
        if self._emit is not None:         # -> Hub event consumer writes them to the DB pool
            self._emit("slots_found", slots=slots)

    def scout_should_continue(self) -> bool:
        cont = self._rounds_done < self._scout_rounds
        self._rounds_done += 1
        return cont

    def poll_gap(self) -> None:
        self._pause()

    # apply
    def await_assignment(self) -> dict | None:
        if self._signals is not None:      # block until the scheduler assigns a pooled slot
            return self._signals.wait_assignment()
        self._pause(); return self._assignment

    def scouts_invalid(self) -> bool:
        return self._scouts_invalid

    def do_apply(self, slot: dict) -> str:
        self._pause(); return self._apply_outcome

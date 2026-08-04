"""SimEngineOps — a deterministic EngineOps implementation with NO browser.

Used to (a) exercise the whole control plane loop end-to-end offline (Hub -> bus -> worker ->
events) and (b) drive test bookings with `no_book`. The live browser/login/book implementation
(Phase 2b) is a separate EngineOps that talks to Playwright/solver/proxy; both satisfy the same
protocol the PhaseMachine calls.
"""
from __future__ import annotations

from libs.engine.phases import EngineOps


class SimEngineOps(EngineOps):
    def __init__(
        self,
        *,
        login_ok: bool = True,
        warmup_ok: bool = True,
        swap_ip: str | None = None,
        found: list[dict] | None = None,
        pool_slot: dict | None = None,
        apply_outcome: str = "booked",
    ):
        self._login_ok = login_ok
        self._warmup_ok = warmup_ok
        self._swap_ip = swap_ip
        self._found = found if found is not None else [{"date": "2026-08-18", "period": 4}]
        self._pool_slot = pool_slot
        self._apply_outcome = apply_outcome
        self.pushed: list[dict] = []

    def do_login(self) -> bool:
        return self._login_ok

    def do_warmup(self) -> bool:
        return self._warmup_ok

    def do_swap(self) -> str | None:
        return self._swap_ip

    def await_signal(self) -> str:
        return "slot_open"

    def do_discover(self) -> list[dict]:
        return list(self._found)

    def push_slots(self, slots: list[dict]) -> None:
        self.pushed.extend(slots)

    def lease_slot(self) -> dict | None:
        return self._pool_slot

    def do_apply(self, slot: dict) -> str:
        return self._apply_outcome

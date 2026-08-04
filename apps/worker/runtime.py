"""WorkerRuntime — runs one worker's phase machine and reports over the bus (spec §7).

A worker process: emits `started`, streams `status_change` on every substate hop, and emits a
`terminal` event with the outcome. The EngineOps passed in decides what actually happens — SimEngineOps
for the control-plane loop / test bookings, the live browser ops for Phase 2b. The Hub's event consumer
turns these events into DB status updates + dashboard WS pushes.
"""
from __future__ import annotations

from libs.bus import Bus
from libs.engine.config import EngineConfig
from libs.engine.phases import EngineOps, PhaseMachine, PhaseResult


class WorkerRuntime:
    def __init__(self, bus: Bus, worker_id: str):
        self.bus = bus
        self.worker_id = worker_id

    def _emit(self, type: str, **payload) -> None:
        self.bus.emit_event(self.worker_id, type, **payload)

    def run(self, cfg: EngineConfig, ops: EngineOps) -> PhaseResult:
        self._emit("started", role=str(cfg.role), no_book=cfg.no_book)

        def on_transition(substate, detail):
            self._emit("status_change", substate=str(substate), detail=detail)
            self._emit("log_line", text=f"-> {substate}" + (f" ({detail})" if detail else ""))

        pm = PhaseMachine(cfg, ops, on_transition=on_transition)
        try:
            res = pm.run()
        except Exception as e:  # pragma: no cover - defensive; a live op blew up
            self._emit("terminal", outcome="failed", detail=f"{e.__class__.__name__}: {e}")
            raise
        self._emit(
            "terminal",
            outcome=res.outcome,
            detail=res.detail,
            substate=str(res.final_substate),
        )
        return res

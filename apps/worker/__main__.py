"""Worker process entrypoint:  python -m apps.worker <worker_id>

Connects to the bus, waits for its `launch` command (carrying the frozen effective settings), runs
the phase machine via WorkerRuntime, and exits when it reaches a terminal state or is told to stop.

Engine selection is driven by `_engine` ("sim" | "live") in the launch payload -- a Hub-controlled,
per-worker, explicit decision (Phase C6/B3), never an env var or a default-to-live: live execution
must always be an auditable choice made by whoever launched the worker, not an accident of how the
process happened to start.

The worker needs only the bus (REDIS_URL) — no DB — matching the decoupled architecture (§2). For a
live-engine launch, every input LiveEngineOps needs (account, proxy, quest_steps, form_defaults,
capsolver_keys) travels through the launch command payload for the same reason: the worker never
reaches into the DB itself.
"""
from __future__ import annotations

import os
import sys

from libs.bus import Bus, connect
from libs.engine.config import EngineConfig

from .bus_signals import BusSignals
from .ops_sim import SimEngineOps
from .runtime import WorkerRuntime


def _build_ops(engine_kind: str, cfg: EngineConfig, payload: dict, bus: Bus, worker_id: str,
               signals: BusSignals):
    emit = lambda t, **p: bus.emit_event(worker_id, t, **p)  # noqa: E731
    if engine_kind == "live":
        from .ops_live import LiveEngineOps
        redis_client = bus.r if hasattr(bus, "r") else None
        ops = LiveEngineOps(
            cfg,
            account=payload["account"],
            posto_id=payload["posto_id"],
            nationality=payload["nationality"],
            residence=payload["residence"],
            quest_steps=payload["quest_steps"],
            form_defaults=payload["form_defaults"],
            capsolver_keys=payload["capsolver_keys"],
            warm_proxy=payload["warm_proxy"],
            hold_proxy=payload.get("hold_proxy"),
            sub_account_id=payload["sub_account_id"],
            identity_id=payload.get("identity_id"),
            booking_id=payload.get("booking_id"),
            headless=bool(payload.get("headless", True)),
            signals=signals,
            emit=emit,
            redis_client=redis_client,
        )
        # Regression (confirmed live, 2026-08-13): LiveEngineOps._log defaults to a no-op
        # (`lambda t: None`) and nothing here ever overrode it, so every self._log(...) call inside
        # ops_live.py -- do_swap()'s own failure diagnostics ("no hold_proxy provided", "get_all_
        # cookies failed", "new session launch failed", "new session failed health check"), do_apply,
        # do_discover, keepalive, etc. -- was silently discarded. Confirmed the hard way: two apply
        # workers in the same booking/template diverged (one swapped to its hold proxy, one didn't),
        # and there was NO way to find out why from any log the Hub or journalctl retained. Wired to
        # the same `log_line` event WorkerRuntime's own transition logging already uses, so the
        # dashboard's existing per-worker log view picks these up with no further changes.
        ops._log = lambda t: emit("log_line", text=t)
        return ops
    step_delay = float(os.environ.get("SIM_STEP_DELAY", "1.2"))
    return SimEngineOps(step_delay=step_delay, signals=signals, emit=emit)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m apps.worker <worker_id>", file=sys.stderr)
        return 2
    worker_id = sys.argv[1]
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    bus = Bus(connect(redis_url))
    consumer = f"worker-{os.getpid()}"
    signals = BusSignals(bus, worker_id, consumer)
    bus.emit_event(worker_id, "process_up", pid=os.getpid())

    cfg_payload = signals.wait_launch()
    if cfg_payload is None:
        bus.emit_event(worker_id, "terminal", outcome="stopped", detail="stopped before launch")
        return 0

    cfg = EngineConfig.from_settings(cfg_payload, no_book=bool(cfg_payload.get("no_book", False)))
    engine_kind = signals.launch_payload.get("_engine", "sim")
    ops = _build_ops(engine_kind, cfg, signals.launch_payload, bus, worker_id, signals)
    try:
        WorkerRuntime(bus, worker_id).run(cfg, ops)
    finally:
        if hasattr(ops, "close"):
            ops.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

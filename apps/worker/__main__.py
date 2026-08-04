"""Worker process entrypoint:  python -m apps.worker <worker_id>

Connects to the bus, waits for its `launch` command (carrying the frozen effective settings), runs
the phase machine via WorkerRuntime, and exits when it reaches a terminal state or is told to stop.
Uses SimEngineOps for now (control-plane slice); Phase 2b swaps in the live browser ops.

The worker needs only the bus (REDIS_URL) — no DB — matching the decoupled architecture (§2).
"""
from __future__ import annotations

import os
import sys

from libs.bus import Bus, connect
from libs.engine.config import EngineConfig

from .ops_sim import SimEngineOps
from .runtime import WorkerRuntime


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m apps.worker <worker_id>", file=sys.stderr)
        return 2
    worker_id = sys.argv[1]
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    bus = Bus(connect(redis_url))
    bus.ensure_command_group(worker_id)

    consumer = f"worker-{os.getpid()}"
    bus.emit_event(worker_id, "process_up", pid=os.getpid())

    # Wait for the launch command (persisted in the stream, so ordering vs the Hub doesn't matter).
    cfg_payload = None
    while cfg_payload is None:
        for msg in bus.read_commands(worker_id, consumer, block_ms=2000):
            bus.ack_command(worker_id, msg.id)
            if msg.type == "launch":
                cfg_payload = msg.payload.get("cfg", {})
                break
            if msg.type in ("stop", "shutdown"):
                bus.emit_event(worker_id, "terminal", outcome="stopped", detail="stopped before launch")
                return 0

    step_delay = float(os.environ.get("SIM_STEP_DELAY", "1.2"))
    cfg = EngineConfig.from_settings(cfg_payload, no_book=bool(cfg_payload.get("no_book", False)))
    ops = SimEngineOps(step_delay=step_delay)
    WorkerRuntime(bus, worker_id).run(cfg, ops)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

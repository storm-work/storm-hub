"""BusSignals — the worker's blocking wait-points on the bus (slot-open, assignment, launch).

The phase machine's await_slot_open / await_assignment are orchestration waits, not browser ops —
both sim and live workers block here for the SAME scheduler-driven commands:
  - launch   : carries the frozen effective settings
  - signal(name=slot_open) : the orchestrator fired slot-open
  - assign(slot=...)       : the orchestrator assigned a pooled slot to this (apply) worker
  - stop/shutdown          : abort

Commands persist in the per-worker Redis stream (consumer group), so ordering vs the worker's own
progress doesn't matter — each wait reads until its command arrives (or a timeout / stop).
"""
from __future__ import annotations

import time


class BusSignals:
    def __init__(self, bus, worker_id: str, consumer: str):
        self.bus = bus
        self.wid = worker_id
        self.consumer = consumer
        self.stopped = False
        bus.ensure_command_group(worker_id)

    def _drain(self, block_ms: int = 2000):
        for m in self.bus.read_commands(self.wid, self.consumer, block_ms=block_ms):
            self.bus.ack_command(self.wid, m.id)
            yield m

    def wait_launch(self, timeout: float = 3600) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline and not self.stopped:
            for m in self._drain():
                if m.type == "launch":
                    return m.payload.get("cfg", {})
                if m.type in ("stop", "shutdown"):
                    self.stopped = True
                    return None
        return None

    def wait_slot_open(self, timeout: float = 900) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline and not self.stopped:
            for m in self._drain():
                if m.type == "signal" and m.payload.get("name") == "slot_open":
                    return "slot_open"
                if m.type in ("stop", "shutdown"):
                    self.stopped = True
                    return "stop"
        return "timeout"

    def wait_assignment(self, timeout: float = 900) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline and not self.stopped:
            for m in self._drain():
                if m.type == "assign":
                    return m.payload.get("slot")
                if m.type in ("stop", "shutdown"):
                    self.stopped = True
                    return None
        return None

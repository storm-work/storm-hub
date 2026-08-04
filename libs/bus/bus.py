"""Bus — thin wrapper over a redis client for the storm-hub control/telemetry protocol.

Message envelope: each stream entry carries flat string fields {type, ts, data} where `data` is a
JSON blob of the payload. This keeps values redis-safe and decoding uniform.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import redis

from .names import CMD_STREAM, EVENT_STREAM, cmd_group, signal_channel


def connect(url: str, *, decode: bool = True) -> "redis.Redis":
    return redis.Redis.from_url(url, decode_responses=decode)


@dataclass
class Message:
    id: str
    type: str
    ts: float
    payload: dict[str, Any]


def _encode(type: str, payload: dict[str, Any]) -> dict[str, str]:
    return {"type": type, "ts": repr(time.time()), "data": json.dumps(payload or {})}


def _decode(msg_id: str, fields: dict) -> Message:
    return Message(
        id=msg_id,
        type=fields.get("type", ""),
        ts=float(fields.get("ts", "0") or 0),
        payload=json.loads(fields.get("data", "{}") or "{}"),
    )


class Bus:
    def __init__(self, client: "redis.Redis"):
        self.r = client

    # ── Commands: Hub -> worker ───────────────────────────────────────────────
    def send_command(self, worker_id: str, type: str, **payload) -> str:
        return self.r.xadd(CMD_STREAM(worker_id), _encode(type, payload))

    def ensure_command_group(self, worker_id: str) -> None:
        """Create the per-worker consumer group (idempotent); a worker calls this on startup."""
        try:
            self.r.xgroup_create(CMD_STREAM(worker_id), cmd_group(worker_id), id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    def read_commands(self, worker_id: str, consumer: str, *, block_ms: int = 1000,
                      count: int = 10) -> list[Message]:
        self.ensure_command_group(worker_id)
        resp = self.r.xreadgroup(
            cmd_group(worker_id), consumer,
            {CMD_STREAM(worker_id): ">"}, count=count, block=block_ms,
        )
        out: list[Message] = []
        for _stream, entries in resp or []:
            for msg_id, fields in entries:
                out.append(_decode(msg_id, fields))
        return out

    def ack_command(self, worker_id: str, msg_id: str) -> None:
        self.r.xack(CMD_STREAM(worker_id), cmd_group(worker_id), msg_id)

    # ── Events: worker -> Hub ─────────────────────────────────────────────────
    def emit_event(self, worker_id: str, type: str, **payload) -> str:
        payload = {**payload, "worker_id": worker_id}
        return self.r.xadd(EVENT_STREAM, _encode(type, payload))

    def read_events(self, last_id: str = "$", *, block_ms: int = 1000, count: int = 100) -> list[Message]:
        resp = self.r.xread({EVENT_STREAM: last_id}, count=count, block=block_ms)
        out: list[Message] = []
        for _stream, entries in resp or []:
            for msg_id, fields in entries:
                out.append(_decode(msg_id, fields))
        return out

    # ── Signals: broadcast ────────────────────────────────────────────────────
    def publish_signal(self, booking_id: str, name: str, **payload) -> int:
        return self.r.publish(signal_channel(booking_id), json.dumps({"name": name, **payload}))

    def subscribe_signal(self, booking_id: str) -> "redis.client.PubSub":
        ps = self.r.pubsub()
        ps.subscribe(signal_channel(booking_id))
        return ps

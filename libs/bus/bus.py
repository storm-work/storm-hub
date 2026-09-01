"""Bus — thin wrapper over a redis client for the storm-hub control/telemetry protocol.

Message envelope: each stream entry carries flat string fields {type, ts, data} where `data` is a
JSON blob of the payload. This keeps values redis-safe and decoding uniform.

Every stream write carries an approximate MAXLEN cap (Phase F1). This exists specifically because of
a confirmed incident: a hold-loop bug (settings resolving a poll-gap to 0.0, see
libs/domain/settings/resolver.py's BelowMinimum guard) drove a worker to ~150-280 writes/sec for 66
minutes, and this bus had -- and without this change, still would have had -- no bound on how large a
single stream could grow in that time. MAXLEN is a deliberate second, independent layer of defense: it
doesn't fix a runaway loop, but it stops a runaway loop from being able to exhaust Redis memory while
it's being fixed/detected by everything else (the settings floor, the event-rate breaker, the process
watchdogs). `~` (approximate trimming) is used everywhere for the same reason `XADD MAXLEN` documents:
exact trimming is O(N) per call under load; approximate is O(1) amortized and the few extra entries it
may leave don't matter here.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import redis

from .names import CMD_STREAM, EVENT_STREAM, cmd_group, signal_channel

# Env-driven, not catalog settings. The catalog homed these at L.BOOKING -- incoherent, because
# EVENT_STREAM is ONE stream shared by every booking, so two bookings would fight over one value,
# and Bus is constructed with a client and no booking context. They were never wireable as declared
# and nothing read them. Env is the honest mechanism for a process-wide infrastructure knob.
#
# EVENT default raised 5,000 -> 50,000. MEASURED 2026-08-31 at 35 running workers: 66 events/min,
# so 5,000 held ~76 minutes; projected to 80 workers that is ~151/min and a ~33-MINUTE window --
# and a failure storm is both when the rate is highest and when the evidence matters most. Losing
# it has already cost this project real answers: Booking 0821's events were gone before its run
# could be reconstructed. 50,000 buys ~5.5h at 80 workers for ~35MB against 2,144MB of headroom on
# a 2GB maxmemory. The cap still exists -- this is the F1 growth bound, just sized for the fleet
# that now exists rather than the one that did when it was written.
def _env_int(name: str, default: int) -> int:
    import os
    try:
        v = int(os.environ.get(name, "") or default)
        return v if v > 0 else default
    except ValueError:
        return default


CMD_STREAM_MAXLEN = _env_int("REDIS_CMD_STREAM_MAXLEN", 200)
EVENT_STREAM_MAXLEN = _env_int("REDIS_EVENT_STREAM_MAXLEN", 50_000)


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
        return self.r.xadd(
            CMD_STREAM(worker_id), _encode(type, payload), maxlen=CMD_STREAM_MAXLEN, approximate=True
        )

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

    def has_pending_commands(self, worker_id: str) -> bool:
        """True if the worker has any delivered-but-unacked command sitting in its stream (Phase
        F2): never send it another command of the same kind while one is still outstanding -- this
        is the general fix for the confirmed 2026-08-07 bug (a worker accrued 56 unconsumed `assign`
        commands because nothing checked for an existing one before sending another), and it was
        caught live, still missing from `discover` command sends, during this project's own Phase E
        sim validation -- the exact same bug class recurring in new code until this existed."""
        try:
            summary = self.r.xpending(CMD_STREAM(worker_id), cmd_group(worker_id))
        except Exception:
            return False   # no such group/stream yet -- nothing pending by definition
        count = summary.get("pending", 0) if isinstance(summary, dict) else (summary[0] if summary else 0)
        return bool(count)

    def delete_worker_stream(self, worker_id: str) -> None:
        """Remove a worker's command stream + consumer group entirely (Phase F1/D3) -- called when a
        worker is reaped (dead/stalled) or a booking ends, so a gone worker doesn't leave Redis state
        behind forever. Best-effort: a stream that's already gone is not an error."""
        try:
            self.r.delete(CMD_STREAM(worker_id))
        except redis.RedisError:
            pass

    # ── Events: worker -> Hub ─────────────────────────────────────────────────
    def emit_event(self, worker_id: str, type: str, **payload) -> str:
        payload = {**payload, "worker_id": worker_id}
        return self.r.xadd(
            EVENT_STREAM, _encode(type, payload), maxlen=EVENT_STREAM_MAXLEN, approximate=True
        )

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

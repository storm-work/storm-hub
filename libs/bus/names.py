"""Redis key/stream/channel naming — one place so Hub and worker agree."""
from __future__ import annotations

EVENT_STREAM = "evt"                       # shared worker -> Hub event stream


def CMD_STREAM(worker_id: str) -> str:     # noqa: N802 (named like a constant helper)
    return f"cmd:{worker_id}"


def cmd_group(worker_id: str) -> str:
    return f"g:{worker_id}"


def signal_channel(booking_id: str) -> str:
    return f"signal:{booking_id}"


def semaphore_key(name: str, booking_id: str) -> str:
    return f"sem:{name}:{booking_id}"


def breaker_key(booking_id: str) -> str:
    return f"breaker:{booking_id}"


def respawn_key(worker_id: str) -> str:
    return f"respawn:{worker_id}"


def notified_key(booking_id: str, signal_name: str) -> str:
    """The set of worker ids already confirmed-sent `signal_name` for this booking (D5's
    confirm-before-mark latch, apps/hub/scheduler.py's _latch_signals). Deliberately a shared
    helper, not an inline f-string in scheduler.py alone: apps/hub/main.py needs the EXACT same key
    to remove a worker's membership when it's stopped/requeued/relaunched (a stable worker_id that
    respawns must be treated as never-notified again in its new incarnation, or _latch_signals'
    "already notified" check -- correctly durable across a Hub restart -- incorrectly also survives
    across a worker's own restart, silently starving the new process of a signal its predecessor
    already received)."""
    return f"notified:{booking_id}:{signal_name}"

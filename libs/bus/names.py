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


def provider_semaphore_key(provider: str, kind: str | None) -> str:
    """Concurrency gate scoped to a PROXY POOL, deliberately not to a booking.

    Every other semaphore here is per-booking, because every other limit is ours. This one models
    somebody else's limit: soax does not establish unlimited concurrent sticky sessions per package,
    and the package is shared by every booking on the host. A per-booking key would let two bookings
    each stay under their own cap and jointly blow through the provider's -- which is exactly the
    gap acknowledged-but-unimplemented at apps/hub/main.py:1776.

    Replaces the `max_sessions` column as the real bound. That column never bounded anything: all
    42,600 rows carried max_sessions=2, a nominal 85,200-lease ceiling nothing could ever reach.
    """
    return f"sem:pool:{provider}:{kind or 'default'}"


def portal_liveness_key(booking_id: str) -> str:
    return f"portal_liveness:{booking_id}"


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


def manual_open_key(booking_id: str) -> str:
    """Marks a slot-open the OPERATOR fired by hand, so the scheduler's false-slot-open guard does
    not silently undo a deliberate decision. Set by POST /bookings/{id}/fire-slot-open; cleared
    wherever the slot-open latch itself is reset (a fresh /start)."""
    return f"manual_open:{booking_id}"

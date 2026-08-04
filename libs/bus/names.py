"""Redis key/stream/channel naming — one place so Hub and worker agree."""
from __future__ import annotations

EVENT_STREAM = "evt"                       # shared worker -> Hub event stream


def CMD_STREAM(worker_id: str) -> str:     # noqa: N802 (named like a constant helper)
    return f"cmd:{worker_id}"


def cmd_group(worker_id: str) -> str:
    return f"g:{worker_id}"


def signal_channel(booking_id: str) -> str:
    return f"signal:{booking_id}"

"""Control/telemetry bus (spec §2/§7) over Redis Streams + pub/sub.

- Commands  : Hub -> worker on `cmd:<worker_id>` (stream, per-worker consumer group).
- Events    : worker -> Hub on the shared `evt` stream (heartbeat/status/log/artifact/metric/terminal).
- Signals   : slot-open / slot-found broadcast on `signal:<booking_id>` (pub/sub).

Decoupling the Hub from workers via the bus is what lets the Hub restart without killing live workers
(spec §2). The Bus wraps a redis client; tests use fakeredis, prod uses a real server via `from_url`.
"""
from .bus import Bus, connect
from .names import CMD_STREAM, EVENT_STREAM, signal_channel

__all__ = ["Bus", "connect", "CMD_STREAM", "EVENT_STREAM", "signal_channel"]

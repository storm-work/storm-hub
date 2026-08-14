"""RedisSemaphore — cross-process concurrency gate (login/apply concurrency per booking).

A Redis sorted set holding {holder_id: last_seen_score}. Self-healing: any acquire/refresh first
prunes members whose score is older than `ttl`, so a crashed/killed holder's slot frees itself without
needing an explicit release. The prune-check-add sequence runs as one atomic Lua script -- a naive
ZADD+ZCARD pair has a TOCTOU race at the ~40-160 concurrent worker scale this gates (two holders could
both see `count < limit` and both add, overshooting the limit).

Every semaphore key also carries a hard outer EXPIRE (`backstop_ttl`) refreshed on every
acquire/refresh, as a second, independent layer beyond the self-healing score-based pruning -- so an
abandoned booking's semaphore key can't outlive the booking indefinitely even if nothing ever calls
release() (see the reconstruction plan's Root Cause Analysis: unbounded Redis growth is the confirmed
failure class this project needs to defend against everywhere, not just in the one place it was caught).
"""
from __future__ import annotations

import time

import redis

from .names import semaphore_key

_ACQUIRE_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[2] - ARGV[3])
local already = redis.call('ZSCORE', KEYS[1], ARGV[1])
local count = redis.call('ZCARD', KEYS[1])
if already == false and count >= tonumber(ARGV[4]) then
    return 0
end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[5])
return 1
"""

_REFRESH_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[2] - ARGV[4])
local exists = redis.call('ZSCORE', KEYS[1], ARGV[1])
if exists == false then
    return 0
end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 1
"""


class RedisSemaphore:
    def __init__(self, r: "redis.Redis", key: str, limit: int, *, ttl: float = 30.0,
                 backstop_ttl: float = 3600.0):
        self.r = r
        self.key = key
        self.limit = limit
        self.ttl = ttl
        self.backstop_ttl = backstop_ttl
        self._acquire = r.register_script(_ACQUIRE_SCRIPT)
        self._refresh_script = r.register_script(_REFRESH_SCRIPT)

    def acquire(self, holder_id: str, timeout: float | None = None) -> bool:
        """Try to acquire a slot. With `timeout=None`, a single non-blocking attempt. With a
        `timeout`, poll (short sleep) until acquired or the timeout elapses."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            now = time.time()
            ok = self._acquire(
                keys=[self.key],
                args=[holder_id, now, self.ttl, self.limit, int(self.backstop_ttl)],
            )
            if ok:
                return True
            if deadline is None or time.monotonic() >= deadline:
                return False
            time.sleep(min(0.5, max(0.05, self.ttl / 20)))

    def refresh(self, holder_id: str) -> bool:
        """Heartbeat while holding (e.g. during a long login) so the TTL-based self-healing prune
        doesn't reclaim a slot that's still legitimately in use. Returns False if the holder had
        already been pruned (caller must re-acquire, not assume it still holds a slot)."""
        now = time.time()
        return bool(
            self._refresh_script(
                keys=[self.key], args=[holder_id, now, int(self.backstop_ttl), self.ttl]
            )
        )

    def release(self, holder_id: str) -> None:
        self.r.zrem(self.key, holder_id)

    def current_count(self) -> int:
        now = time.time()
        self.r.zremrangebyscore(self.key, "-inf", now - self.ttl)
        return self.r.zcard(self.key)


def make_gate(r: "redis.Redis", booking_id: str, name: str, limit: int, **kwargs) -> RedisSemaphore:
    return RedisSemaphore(r, semaphore_key(name, booking_id), limit, **kwargs)

"""RedisBreaker — per-booking circuit breaker gating login/discover/apply when the portal itself is
down.

Trips ONLY on consecutive portal-DOWN signals (connection errors, timeouts, 5xx) -- NEVER on reCAPTCHA
rejects, "taken" responses, or any other application-level outcome. This module is deliberately
zero-HTTP-aware: the caller (LiveEngineOps) classifies an exception as portal-down vs. something else
before calling record_portal_down()/record_success(), which keeps the "diagnose as proxy/IP reputation,
never blame downtime" rule (see feedback_no_downtime_assumption) enforceable at the classification call
site, not buried inside this module.

Backed by a Redis hash with an EXPIRE TTL as a backstop (same principle as libs/bus/semaphore.py) so an
abandoned booking's breaker state can't outlive the booking indefinitely.
"""
from __future__ import annotations

import time

import redis

from .names import breaker_key

_FAIL_FIELD = "consecutive_fail"
_TRIPPED_UNTIL_FIELD = "tripped_until"


class RedisBreaker:
    def __init__(
        self, r: "redis.Redis", booking_id: str, *, trip_threshold: int = 5,
        cooldown: float = 120.0, backstop_ttl: float = 3600.0,
    ):
        self.r = r
        self.key = breaker_key(booking_id)
        self.trip_threshold = trip_threshold
        self.cooldown = cooldown
        self.backstop_ttl = backstop_ttl

    def record_success(self) -> None:
        self.r.hset(self.key, _FAIL_FIELD, 0)
        self.r.expire(self.key, int(self.backstop_ttl))

    def record_portal_down(self) -> None:
        fails = self.r.hincrby(self.key, _FAIL_FIELD, 1)
        if fails >= self.trip_threshold:
            self.r.hset(self.key, _TRIPPED_UNTIL_FIELD, time.time() + self.cooldown)
        self.r.expire(self.key, int(self.backstop_ttl))

    def is_tripped(self) -> bool:
        tripped_until = self.r.hget(self.key, _TRIPPED_UNTIL_FIELD)
        if tripped_until is None:
            return False
        if time.time() >= float(tripped_until):
            # cooldown elapsed -- clear the trip so the next call sees a closed breaker.
            self.r.hdel(self.key, _TRIPPED_UNTIL_FIELD)
            self.r.hset(self.key, _FAIL_FIELD, 0)
            return False
        return True

    def state(self) -> dict:
        raw = self.r.hgetall(self.key)
        return {
            "consecutive_fail": int(raw.get(_FAIL_FIELD, 0) or 0),
            "tripped_until": float(raw[_TRIPPED_UNTIL_FIELD]) if _TRIPPED_UNTIL_FIELD in raw else None,
            "is_tripped": self.is_tripped(),
        }

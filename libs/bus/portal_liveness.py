"""PortalLiveness — a per-booking, record-ONLY view of whether the portal itself is up.

Replaces the removed RedisBreaker (2026-09-23). The breaker GATED the fleet -- do_login/do_apply
refused for every worker while it was tripped -- and that gate did more harm than good: on a loaded
fleet a normal post-restart throttle burst tripped it, froze all workers ("waiting out the booking
breaker"), and oscillated (trip -> all wait -> all retry together -> trip again), starving discovery
and apply. Confirmed live on 'Prev 0923-3018'.

This class NEVER gates anything. Workers report two facts to it -- "the portal served me" (alive) and
"the portal showed its maintenance page" (down) -- and the Hub reads state() to show an alive/down
indicator on the booking dashboard. The operator decides what to do; the fleet is never frozen. The
per-worker maintenance backoff (a worker that hits the maintenance page sleeps on its own) provides
the one protection the breaker really gave -- not grinding accounts against a down portal -- without
any shared trip.

Redis-backed (a hash with an EXPIRE backstop) so worker processes write it and the Hub reads it, same
cross-process pattern the breaker used.
"""
from __future__ import annotations

import time

import redis

from .names import portal_liveness_key

_ALIVE_AT = "last_alive_at"
_DOWN_AT = "last_down_at"


class PortalLiveness:
    def __init__(
        self, r: "redis.Redis", booking_id: str, *,
        down_window: float = 180.0, alive_window: float = 180.0, backstop_ttl: float = 3600.0,
    ):
        self.r = r
        self.key = portal_liveness_key(booking_id)
        self.down_window = down_window
        self.alive_window = alive_window
        self.backstop_ttl = backstop_ttl
        self._down_accts = self.key + ":down_accts"

    def record_alive(self) -> None:
        """The portal served us a normal response (a successful login, warmup, discover or booking).
        Evidence the portal is up -- and it clears the down-account tally, since a success disproves
        'the portal is down for everyone'."""
        self.r.hset(self.key, _ALIVE_AT, time.time())
        self.r.delete(self._down_accts)
        self.r.expire(self.key, int(self.backstop_ttl))

    def record_down(self, sub_account_id: str = "") -> None:
        """A worker saw the portal's explicit maintenance / 'Serviço Indisponível' page. Records the
        time and the distinct sub-account (so the dashboard can show how broadly it is confirmed)."""
        self.r.hset(self.key, _DOWN_AT, time.time())
        if sub_account_id:
            self.r.sadd(self._down_accts, sub_account_id)
            self.r.expire(self._down_accts, int(self.down_window))
        self.r.expire(self.key, int(self.backstop_ttl))

    def _attempts_key(self, minute: int) -> str:
        return f"{self.key}:attempts:{minute}"

    def record_login_attempt(self) -> None:
        """Count one login shot in a per-minute bucket, so the dashboard can show the fleet's SHOT
        RATE. Under a global portal throttle (identical requests succeed ~1%, so bookings scale with
        shots taken), attempts/min is THE number to watch when tuning throughput. O(1), self-expiring."""
        m = int(time.time() // 60)
        k = self._attempts_key(m)
        self.r.incr(k)
        self.r.expire(k, 180)

    def _attempts_1m(self) -> int:
        """The most recent COMPLETE minute's attempt count (stable), falling back to the current
        partial minute right after start."""
        m = int(time.time() // 60)
        prev = self.r.get(self._attempts_key(m - 1))
        if prev is not None:
            return int(prev)
        cur = self.r.get(self._attempts_key(m))
        return int(cur) if cur is not None else 0

    def state(self) -> dict:
        """Current liveness for the dashboard. 'down' when a maintenance page was seen within
        down_window and no more-recent success; 'alive' when a success is within alive_window;
        'unknown' when neither is recent (e.g. right after start, before any worker reached the
        portal)."""
        raw = self.r.hgetall(self.key)
        raw = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
               for k, v in raw.items()}
        now = time.time()
        alive_at = float(raw.get(_ALIVE_AT, 0) or 0)
        down_at = float(raw.get(_DOWN_AT, 0) or 0)
        down_recent = down_at > 0 and (now - down_at) <= self.down_window
        alive_recent = alive_at > 0 and (now - alive_at) <= self.alive_window
        if down_recent and down_at >= alive_at:
            state, since = "down", down_at
        elif alive_recent:
            state, since = "alive", alive_at
        else:
            state, since = "unknown", max(alive_at, down_at) or None
        return {
            "state": state,
            "since": since,
            "last_alive_at": alive_at or None,
            "last_down_at": down_at or None,
            "down_accounts": int(self.r.scard(self._down_accts) or 0),
            "login_attempts_1m": self._attempts_1m(),
        }

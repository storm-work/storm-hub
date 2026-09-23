"""RedisBreaker — per-booking circuit breaker gating login/discover/apply when the portal itself is
down.

Trips on portal-DOWN signals -- NEVER on reCAPTCHA rejects, "taken" responses, or any other
application-level outcome. This module is deliberately zero-HTTP-aware: the caller (LiveEngineOps)
classifies a response before calling one of three feeds, which keeps the "diagnose as proxy/IP
reputation, never blame downtime" rule (see feedback_no_downtime_assumption) enforceable at the
classification call site, not buried inside this module. The three feeds differ in what counts as
fleet-wide evidence, because getting that wrong once made the breaker suppress the very logins that
would have cleared it (145 refusals, 0 successes over 91 minutes -- see the login-path comment):

- record_portal_down(): a signal that is ALREADY fleet-level (a degraded-window health verdict, a
  transport failure that reached no portal). Trips on CONSECUTIVE count, reset by any success.
- record_portal_busy(sub_account_id): the 'try again later' login throttle -- AMBIGUOUS between a
  per-account block and a fleet-wide refusal. Trips only once N DISTINCT sub-accounts hit it, which
  a single blocked account cannot fake no matter how many exits it burns.
- record_maintenance(sub_account_id): the explicit maintenance / 'Serviço Indisponível' page -- a
  strong fleet-wide signal, so it trips fast on a low distinct-account threshold.

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
        busy_threshold: int = 4, busy_window: float = 180.0,
        maint_threshold: int = 2, maint_window: float = 300.0,
    ):
        self.r = r
        self.key = breaker_key(booking_id)
        self.trip_threshold = trip_threshold
        self.cooldown = cooldown
        self.backstop_ttl = backstop_ttl
        # Distinct-SUB-ACCOUNT evidence thresholds. A login-throttle ('try again later') refusal
        # is ambiguous -- an account-level block returns it from EVERY exit, so counting refusals
        # (or exits) proves nothing about the portal. Counting DISTINCT sub-accounts does: only the
        # portal declining to serve everyone makes several different accounts fail at once. See the
        # login-path comment in ops_live for the incident this replaces.
        self.busy_threshold = busy_threshold
        self.busy_window = busy_window
        self.maint_threshold = maint_threshold
        self.maint_window = maint_window
        self._busy_key = self.key + ":busy_accts"
        self._maint_key = self.key + ":maint_accts"

    def _trip(self) -> None:
        self.r.hset(self.key, _TRIPPED_UNTIL_FIELD, time.time() + self.cooldown)
        self.r.expire(self.key, int(self.backstop_ttl))

    def record_success(self) -> None:
        self.r.hset(self.key, _FAIL_FIELD, 0)
        # A single successful login/submit disproves both "everyone is throttled" and "the portal
        # is down", so the distinct-account evidence accumulated so far is stale -- clear it, or a
        # degraded window that later recovers would still trip on leftover votes.
        self.r.delete(self._busy_key, self._maint_key)
        self.r.expire(self.key, int(self.backstop_ttl))

    def record_portal_down(self) -> None:
        """A portal-DOWN signal that is already fleet-level by construction (a degraded-window
        health verdict, a transport failure that reached no portal) -- trips on CONSECUTIVE count,
        unchanged. NOT for the login-throttle page; that goes through record_portal_busy()."""
        fails = self.r.hincrby(self.key, _FAIL_FIELD, 1)
        if fails >= self.trip_threshold:
            self._trip()
        self.r.expire(self.key, int(self.backstop_ttl))

    def record_portal_busy(self, sub_account_id: str) -> None:
        """The portal's 'try again later' login throttle. Ambiguous between a per-account block and
        a fleet-wide refusal, so charge it to a DISTINCT-sub-account set and trip only once
        `busy_threshold` DIFFERENT accounts have hit it inside `busy_window`. One blocked account
        failing across many exits can never reach that count on its own."""
        self.r.sadd(self._busy_key, sub_account_id or "?")
        self.r.expire(self._busy_key, int(self.busy_window))
        if self.r.scard(self._busy_key) >= self.busy_threshold:
            self._trip()

    def record_maintenance(self, sub_account_id: str) -> None:
        """The portal's explicit maintenance / 'Serviço Indisponível' page -- a strong fleet-wide
        signal (it is served to everyone and says nothing about our account, exit or session).
        Trips fast (low `maint_threshold`), because rolling exits or rotating accounts against a
        portal that is DOWN only burns the pool; still >=2 distinct accounts so one worker
        misreading a body cannot down the whole fleet."""
        self.r.sadd(self._maint_key, sub_account_id or "?")
        self.r.expire(self._maint_key, int(self.maint_window))
        if self.r.scard(self._maint_key) >= self.maint_threshold:
            self._trip()

    def is_tripped(self) -> bool:
        tripped_until = self.r.hget(self.key, _TRIPPED_UNTIL_FIELD)
        if tripped_until is None:
            return False
        if time.time() >= float(tripped_until):
            # cooldown elapsed -- clear the trip AND the distinct-account evidence so the next
            # window starts clean rather than re-tripping on stale votes.
            self.r.hdel(self.key, _TRIPPED_UNTIL_FIELD)
            self.r.hset(self.key, _FAIL_FIELD, 0)
            self.r.delete(self._busy_key, self._maint_key)
            return False
        return True

    def state(self) -> dict:
        raw = self.r.hgetall(self.key)
        return {
            "consecutive_fail": int(raw.get(_FAIL_FIELD, 0) or 0),
            "busy_accounts": int(self.r.scard(self._busy_key) or 0),
            "maint_accounts": int(self.r.scard(self._maint_key) or 0),
            "tripped_until": float(raw[_TRIPPED_UNTIL_FIELD]) if _TRIPPED_UNTIL_FIELD in raw else None,
            "is_tripped": self.is_tripped(),
        }

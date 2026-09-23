import time

import fakeredis
import pytest

from libs.bus.breaker import RedisBreaker


@pytest.fixture()
def r():
    return fakeredis.FakeStrictRedis(decode_responses=True)


def test_not_tripped_initially(r):
    b = RedisBreaker(r, "bk1", trip_threshold=3, cooldown=60.0)
    assert b.is_tripped() is False


def test_trips_after_threshold_consecutive_failures(r):
    b = RedisBreaker(r, "bk1", trip_threshold=3, cooldown=60.0)
    b.record_portal_down()
    b.record_portal_down()
    assert b.is_tripped() is False   # 2 < threshold
    b.record_portal_down()
    assert b.is_tripped() is True    # 3 >= threshold


def test_success_resets_consecutive_failures(r):
    b = RedisBreaker(r, "bk1", trip_threshold=3, cooldown=60.0)
    b.record_portal_down()
    b.record_portal_down()
    b.record_success()
    b.record_portal_down()
    assert b.is_tripped() is False   # only 1 consecutive failure since the reset


def test_never_trips_on_non_portal_failures():
    # the breaker has no concept of reCAPTCHA rejects / "taken" outcomes at all -- only the caller's
    # explicit portal-refusal calls can trip it. This is a design assertion, not a runtime check:
    # confirm the ONLY public writes are the three vetted portal-refusal feeds plus record_success,
    # i.e. there's no "record_reject"-style method that could accidentally be wired to a non-portal
    # failure. The three feeds differ only in how much fleet evidence they demand (see the module
    # docstring): record_portal_down (already-fleet, consecutive), record_portal_busy (throttle,
    # distinct-account), record_maintenance (down page, distinct-account, fast).
    public_methods = {m for m in dir(RedisBreaker) if not m.startswith("_") and m not in ("state",)}
    assert public_methods == {"record_success", "record_portal_down", "record_portal_busy",
                              "record_maintenance", "is_tripped"}


def test_cooldown_expires_and_clears_trip(r):
    b = RedisBreaker(r, "bk1", trip_threshold=2, cooldown=0.05)
    b.record_portal_down()
    b.record_portal_down()
    assert b.is_tripped() is True
    time.sleep(0.1)
    assert b.is_tripped() is False
    # after cooldown clears, the failure count is reset too -- one more failure shouldn't instantly
    # re-trip without hitting the threshold (2) again
    b.record_portal_down()
    assert b.is_tripped() is False


def test_state_reports_consistent_snapshot(r):
    b = RedisBreaker(r, "bk1", trip_threshold=2, cooldown=60.0)
    b.record_portal_down()
    b.record_portal_down()
    s = b.state()
    assert s["consecutive_fail"] == 2
    assert s["is_tripped"] is True
    assert s["tripped_until"] is not None


def test_backstop_ttl_set_on_the_key(r):
    b = RedisBreaker(r, "bk1", trip_threshold=3, cooldown=60.0, backstop_ttl=100.0)
    b.record_portal_down()
    ttl = r.ttl(b.key)
    assert 0 < ttl <= 100


# ── distinct-sub-account trip evidence (2026-09-23) ──────────────────────────
def test_throttle_from_ONE_account_across_many_exits_never_trips(r):
    """The bug this fixes: a single account-level block returns 'try again later' from every exit,
    so one blocked account rolling through many exits used to push the fleet breaker to a trip and
    suppress the logins that would have cleared it. record_portal_busy() counts DISTINCT accounts,
    so one account -- however many times it refuses -- can never reach the threshold alone."""
    b = RedisBreaker(r, "bk1", busy_threshold=4)
    for _ in range(20):
        b.record_portal_busy("acct-A")
    assert b.is_tripped() is False


def test_throttle_trips_only_once_enough_distinct_accounts_agree(r):
    b = RedisBreaker(r, "bk1", busy_threshold=4)
    b.record_portal_busy("acct-A")
    b.record_portal_busy("acct-B")
    b.record_portal_busy("acct-C")
    assert b.is_tripped() is False          # 3 distinct < 4
    b.record_portal_busy("acct-D")
    assert b.is_tripped() is True           # 4 distinct == threshold -> portal-wide


def test_a_success_clears_accumulated_throttle_evidence(r):
    """A login succeeding anywhere disproves 'everyone is throttled' -- the tally must reset, or a
    later degraded window would trip on stale votes."""
    b = RedisBreaker(r, "bk1", busy_threshold=3)
    b.record_portal_busy("acct-A")
    b.record_portal_busy("acct-B")
    b.record_success()
    b.record_portal_busy("acct-C")
    assert b.is_tripped() is False          # only 1 distinct account since the reset


def test_maintenance_trips_fast_on_a_low_distinct_threshold(r):
    """The maintenance page is unambiguous (down for everyone), so it trips on far fewer distinct
    accounts than the ambiguous throttle -- but still more than one, so a single misread cannot
    down the fleet."""
    b = RedisBreaker(r, "bk1", maint_threshold=2)
    b.record_maintenance("acct-A")
    assert b.is_tripped() is False          # 1 distinct < 2
    b.record_maintenance("acct-B")
    assert b.is_tripped() is True


def test_cooldown_clears_distinct_account_evidence_too(r):
    b = RedisBreaker(r, "bk1", cooldown=0.05, maint_threshold=2)
    b.record_maintenance("acct-A")
    b.record_maintenance("acct-B")
    assert b.is_tripped() is True
    time.sleep(0.06)
    assert b.is_tripped() is False          # cooldown elapsed
    # and the evidence is gone: two fresh distinct accounts are needed to trip again
    b.record_maintenance("acct-A")
    assert b.is_tripped() is False

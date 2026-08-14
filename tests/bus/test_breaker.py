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
    # explicit record_portal_down() call can trip it. This is a design assertion, not a runtime check:
    # confirm record_success() is the only other public write, i.e. there's no "record_reject"-style
    # method that could accidentally be wired to a non-portal-down failure.
    public_methods = {m for m in dir(RedisBreaker) if not m.startswith("_") and m not in ("state",)}
    assert public_methods == {"record_success", "record_portal_down", "is_tripped"}


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

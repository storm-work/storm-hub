"""PortalLiveness — the record-only view that replaced the removed booking breaker (2026-09-23).
It NEVER gates; it only records what workers report so the dashboard can show alive/down."""
import time
import fakeredis
import pytest

from libs.bus.portal_liveness import PortalLiveness


@pytest.fixture()
def r():
    return fakeredis.FakeStrictRedis(decode_responses=True)


def test_unknown_before_any_signal(r):
    assert PortalLiveness(r, "bk1").state()["state"] == "unknown"


def test_a_success_marks_alive(r):
    p = PortalLiveness(r, "bk1")
    p.record_alive()
    assert p.state()["state"] == "alive"


def test_the_maintenance_page_marks_down(r):
    p = PortalLiveness(r, "bk1")
    p.record_down("acct-A")
    s = p.state()
    assert s["state"] == "down"
    assert s["down_accounts"] == 1


def test_a_later_success_flips_down_back_to_alive_and_clears_accounts(r):
    p = PortalLiveness(r, "bk1")
    p.record_down("acct-A")
    p.record_down("acct-B")
    assert p.state()["state"] == "down"
    p.record_alive()
    s = p.state()
    assert s["state"] == "alive"
    assert s["down_accounts"] == 0, "a success disproves 'down for everyone' and clears the tally"


def test_down_decays_to_unknown_after_the_window(r):
    p = PortalLiveness(r, "bk1", down_window=0.05, alive_window=0.05)
    p.record_down("acct-A")
    assert p.state()["state"] == "down"
    time.sleep(0.06)
    assert p.state()["state"] == "unknown", "stale evidence must not read as down forever"


def test_never_gates_has_no_is_tripped_or_record_portal_down(r):
    # Design assertion: this class must not grow a gate. The breaker's gating API is gone.
    p = PortalLiveness(r, "bk1")
    for banned in ("is_tripped", "record_portal_down", "record_portal_busy", "record_maintenance"):
        assert not hasattr(p, banned), f"PortalLiveness must not expose a gate/breaker method ({banned})"


def test_login_attempt_counter(r):
    p = PortalLiveness(r, "bk1")
    for _ in range(7):
        p.record_login_attempt()
    # current partial minute is reported when there's no completed previous minute yet
    assert p.state()["login_attempts_1m"] == 7


def test_login_attempts_1m_prefers_the_last_complete_minute(monkeypatch, r):
    import libs.bus.portal_liveness as pl
    base = 1_000_000 * 60  # a minute boundary
    monkeypatch.setattr(pl.time, "time", lambda: base + 10)   # minute M
    p = PortalLiveness(r, "bk1")
    for _ in range(5):
        p.record_login_attempt()                              # 5 shots in minute M
    monkeypatch.setattr(pl.time, "time", lambda: base + 70)   # now in minute M+1
    for _ in range(2):
        p.record_login_attempt()                              # 2 shots in minute M+1
    # reports the last COMPLETE minute (M) = 5, not the current partial (2)
    assert p.state()["login_attempts_1m"] == 5

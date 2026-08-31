"""libs/proxies/health.py -- the real-portal proxy probe + scoring bookkeeping shared by
lease_proxy()'s on-demand pre-lease screening and the manual scripts/proxy_health_check.py tool.
Unit-tests only the pure scoring/classification (apply_result); check_proxy itself needs a real
browser and is exercised (via a fake) from lease_proxy's and events.py's own tests."""
import types

from libs.proxies.health import SCORE_MAX, SCORE_MIN, apply_result


def _proxy(score=0.0, is_healthy=True, health=None):
    return types.SimpleNamespace(score=score, is_healthy=is_healthy, health=health)


def test_apply_result_success_raises_score_and_marks_healthy():
    p = _proxy(score=50.0, is_healthy=False, health="unreachable")
    apply_result(p, True, "ok")
    assert p.is_healthy is True
    assert p.health == "ok"
    assert p.score == 60.0


def test_apply_result_success_caps_score_at_max():
    p = _proxy(score=95.0)
    apply_result(p, True, "ok")
    assert p.score == SCORE_MAX


def test_apply_result_failure_lowers_score_and_marks_unhealthy():
    p = _proxy(score=50.0, is_healthy=True)
    apply_result(p, False, "TimeoutError: nav timed out")
    assert p.is_healthy is False
    # 160 now, not 24 (migration cc000healthdetail): at 24 the net::ERR_* code -- the only part
    # saying WHICH failure this was -- is exactly what got cut off.
    assert p.health == "TimeoutError: nav timed out"
    assert p.score == 25.0


def test_apply_result_failure_floors_score_at_min():
    p = _proxy(score=10.0)
    apply_result(p, False, "ConnectionError: refused")
    assert p.score == SCORE_MIN


# ── rotating pools are not screened per row (2026-08-31) ─────────────────────────────────────
# MEASURED: all 32,600 rows are resource_type='rotating' and resolve to TWO credentials on TWO
# endpoints. A row is a credential template; the exit is a session id we generate, measured at 100%
# survival to 60 min and 0% at 75. The old probe navigated the REAL PORTAL, so a throttle condemned
# 4,489 webshare rows on 2026-08-28 and 73 of 120 workers never launched. Re-checked 2026-08-31
# against a neutral url: condemned rows connected 20/20, identical to the healthy control.

def test_provider_reachable_is_cached_per_provider(monkeypatch):
    from libs.proxies import health
    health.reset_provider_cache()
    calls = []
    monkeypatch.setattr("libs.proxies.exit_ip.observe_exit_ip",
                        lambda url, timeout=8.0: calls.append(url) or "1.2.3.4")
    a = health.provider_reachable("soax", "http://u:p@gw:1")
    b = health.provider_reachable("soax", "http://u:p@gw:1")
    assert a[0] is True and b[0] is True
    assert len(calls) == 1, f"the gateway was probed once per lease, not once per provider: {calls}"


def test_provider_reachable_reports_a_dead_gateway(monkeypatch):
    from libs.proxies import health
    health.reset_provider_cache()
    monkeypatch.setattr("libs.proxies.exit_ip.observe_exit_ip", lambda url, timeout=8.0: None)
    ok, why = health.provider_reachable("webshare", "http://u:p@gw:1")
    assert ok is False and "gateway" in why


def test_the_cache_expires(monkeypatch):
    from libs.proxies import health
    health.reset_provider_cache()
    n = []
    monkeypatch.setattr("libs.proxies.exit_ip.observe_exit_ip",
                        lambda url, timeout=8.0: n.append(1) or "1.2.3.4")
    health.provider_reachable("soax", "u", ttl=100.0, now=0.0)
    health.provider_reachable("soax", "u", ttl=100.0, now=50.0)
    assert len(n) == 1
    health.provider_reachable("soax", "u", ttl=100.0, now=200.0)
    assert len(n) == 2, "a stale verdict was served forever"


def test_provider_reachable_never_touches_the_portal(monkeypatch):
    """The whole defect being fixed: probing the portal made a throttle look like 4,489 dead rows."""
    from libs.proxies import health
    health.reset_provider_cache()
    seen = []
    monkeypatch.setattr("libs.proxies.exit_ip.observe_exit_ip",
                        lambda url, timeout=8.0: seen.append(url) or "1.2.3.4")
    health.provider_reachable("soax", "http://u:p@proxy.soax.com:5000")
    assert not any("pedidodevistos" in s for s in seen), \
        "the provider screen reached for the portal -- that is the bug, not the check"

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
    assert p.health == "TimeoutError: nav timed out"[:24]
    assert p.score == 25.0


def test_apply_result_failure_floors_score_at_min():
    p = _proxy(score=10.0)
    apply_result(p, False, "ConnectionError: refused")
    assert p.score == SCORE_MIN

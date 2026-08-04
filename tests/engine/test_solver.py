import pytest

from libs.engine.solver import (
    SolverError,
    _build_task,
    parse_proxy_for_capsolver,
    score_token,
    solve,
)


def test_score_token_length_buckets():
    assert score_token("x" * 2000) == 100   # auto-pass
    assert score_token("x" * 2350) == 85
    assert score_token("x" * 2600) == 10
    assert score_token("") == 0


def test_parse_proxy_for_capsolver():
    info = parse_proxy_for_capsolver("http://user:pass@1.2.3.4:5000")
    assert info == {"proxyType": "http", "proxyAddress": "1.2.3.4", "proxyPort": "5000",
                    "proxyLogin": "user", "proxyPassword": "pass"}
    assert parse_proxy_for_capsolver(None) is None
    assert parse_proxy_for_capsolver("garbage") is None


def test_build_task_proxyless_vs_proxy():
    t = _build_task("LOGIN_EVISA", None)
    assert t["type"] == "ReCaptchaV2EnterpriseTaskProxyLess" and "Authentication.jsp" in t["websiteURL"]
    tp = _build_task("SCHEDULE_EVISA", {"proxyType": "http", "proxyAddress": "h", "proxyPort": "1"})
    assert tp["type"] == "ReCaptchaV2EnterpriseTask" and "Schedule.jsp" in tp["websiteURL"]
    assert tp["proxy"].startswith("http:h:1:")


def test_solve_no_keys_raises():
    with pytest.raises(SolverError):
        solve([])

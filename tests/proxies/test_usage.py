"""libs.proxies.usage -- webshare account bandwidth client + billing-period maths."""
from datetime import datetime, timezone

from libs.proxies import usage


def _dt(y, m, d):
    return datetime(y, m, d, 12, 0, 0, tzinfo=timezone.utc)


def test_billing_period_start_most_recent_occurrence():
    # billing_day already passed this month -> this month
    assert usage.billing_period_start(15, now=_dt(2026, 10, 20)).date().isoformat() == "2026-10-15"
    # billing_day not yet reached -> previous month
    assert usage.billing_period_start(15, now=_dt(2026, 10, 9)).date().isoformat() == "2026-09-15"
    # day 1 default
    assert usage.billing_period_start(1, now=_dt(2026, 10, 9)).date().isoformat() == "2026-10-01"
    # clamp >28 so it exists in February
    assert usage.billing_period_start(31, now=_dt(2026, 3, 10)).date().isoformat() == "2026-02-28"


def test_webshare_usage_no_token_is_graceful():
    r = usage.webshare_usage("")
    assert r["ok"] is False and "not set" in r["error"]


class _FakeResp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self, routes):
        self._routes = routes

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        for frag, resp in self._routes:
            if frag in url:
                return resp
        return _FakeResp({}, 404)


def test_webshare_usage_computes_remaining(monkeypatch):
    import httpx
    routes = [
        ("subscription/plan", _FakeResp({"results": [{"bandwidth_limit": 250.0}]})),
        # 120 GB used, in bytes (decimal GB)
        ("stats/aggregate", _FakeResp({"bandwidth_total": 120 * 1_000_000_000})),
    ]
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _FakeClient(routes))
    r = usage.webshare_usage("tok", billing_day=1, now=_dt(2026, 10, 9))
    assert r["ok"] and r["unlimited"] is False
    assert r["limit_gb"] == 250.0 and r["used_gb"] == 120.0 and r["remaining_gb"] == 130.0
    assert r["period_start"].startswith("2026-10-01")


def test_webshare_usage_unlimited_plan(monkeypatch):
    import httpx
    routes = [
        ("subscription/plan", _FakeResp({"results": [{"bandwidth_limit": 0}]})),
        ("stats/aggregate", _FakeResp({"bandwidth_total": 5 * 1_000_000_000})),
    ]
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _FakeClient(routes))
    r = usage.webshare_usage("tok")
    assert r["ok"] and r["unlimited"] is True and r["limit_gb"] is None and r["remaining_gb"] is None
    assert r["used_gb"] == 5.0


def test_webshare_usage_bad_token_401(monkeypatch):
    import httpx
    routes = [("subscription/plan", _FakeResp({}, status=401))]
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _FakeClient(routes))
    r = usage.webshare_usage("bad")
    assert r["ok"] is False and "401" in r["error"]

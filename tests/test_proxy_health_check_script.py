"""scripts/proxy_health_check.py -- the manual, on-demand whole-pool diagnostic tool (NOT scheduled;
see the module docstring for why periodic sweeping was rejected in favor of lease_proxy()'s own
on-demand pre-lease screening). Unit-tests only `screen`'s concurrency/dispatch orchestration with a
fake probe -- no real browser or DB."""
from scripts.proxy_health_check import screen


def test_screen_runs_every_target_and_reports_results():
    targets = {"p1": "http://u:p@h1:1", "p2": "http://u:p@h2:2"}

    def fake_check(url, timeout):
        ok = url.endswith("h1:1")
        return (True, "ok") if ok else (False, "boom")

    seen = []
    results = screen(targets, check_fn=fake_check, concurrency=2, timeout=5.0,
                     on_result=lambda pid, ok, detail: seen.append((pid, ok, detail)))

    assert results == {"p1": (True, "ok"), "p2": (False, "boom")}
    assert set(seen) == {("p1", True, "ok"), ("p2", False, "boom")}


def test_screen_passes_timeout_through_to_check_fn():
    captured = []

    def fake_check(url, timeout):
        captured.append(timeout)
        return True, "ok"

    screen({"p1": "http://h:1"}, check_fn=fake_check, timeout=42.0)
    assert captured == [42.0]

"""The Donut CDP rate-limit relay: pacing, transparent resend, pass-through, and its wiring into
donut.connect/release. Driven against a fake Wayfern CDP socket that enforces a token bucket and answers
with the error text a real profile returned -- no Donut app, no browser, no portal."""
import asyncio
import json
import threading
import time

import pytest
from websockets.asyncio.server import serve
from websockets.sync.client import connect as ws_connect

import libs.engine.vendor.session as session  # noqa: F401  (puts the vendored modules on sys.path)
from libs.engine import vendor_env

donut = session._donut
import cdp_throttle  # noqa: E402  (vendored sibling module)

RATE_ERR = {"code": -32000, "message": "Automation rate limit exceeded (100 requests/minute). Retry shortly."}


class FakeWayfern:
    """CDP socket with Donut's throttle: Runtime.callFunctionOn draws from a bucket, everything else is free."""

    def __init__(self, burst=6, per_s=20.0, always_reject=False, other_error_for=None):
        self.burst, self.per_s, self.always_reject = burst, per_s, always_reject
        self.other_error_for = other_error_for
        self.tokens, self._at = float(burst), time.monotonic()
        self.rejected = self.served = 0
        self.url = None
        self._ready = threading.Event()
        self._stop_evt = None
        self._loop = None
        threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True).start()
        assert self._ready.wait(5)

    async def _main(self):
        self._loop, self._stop_evt = asyncio.get_running_loop(), asyncio.Event()

        async def handler(ws):
            async for raw in ws:
                msg = json.loads(raw)
                m = msg.get("method")
                if self.other_error_for and m == self.other_error_for:
                    await ws.send(json.dumps({"id": msg["id"], "error": {"code": -32601, "message": "nope"}}))
                    continue
                if m == "Runtime.callFunctionOn":
                    now = time.monotonic()
                    self.tokens = min(self.burst, self.tokens + (now - self._at) * self.per_s)
                    self._at = now
                    if self.always_reject or self.tokens < 1:
                        self.rejected += 1
                        await ws.send(json.dumps({"id": msg["id"], "error": RATE_ERR}))
                        continue
                    self.tokens -= 1
                self.served += 1
                await ws.send(json.dumps({"id": msg["id"], "result": {"ok": True}}))

        async with serve(handler, "127.0.0.1", 0, max_size=None) as srv:
            self.url = f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}/"
            self._ready.set()
            await self._stop_evt.wait()

    def close(self):
        self._loop.call_soon_threadsafe(self._stop_evt.set)


@pytest.fixture
def wayfern():
    fakes = []

    def make(**kw):
        f = FakeWayfern(**kw)
        fakes.append(f)
        return f
    yield make
    for f in fakes:
        f.close()


@pytest.fixture
def relay():
    made = []

    def make(upstream, **kw):
        t = cdp_throttle.CdpThrottle(upstream, log=lambda *_: None, **kw)
        made.append(t)
        return t, t.start()
    yield make
    for t in made:
        t.stop()


def _call_many(url, method, n, timeout=30):
    """n commands sent back to back (the way a busy Playwright does), replies collected by id."""
    got = {}
    with ws_connect(url, max_size=None) as ws:
        for i in range(1, n + 1):
            ws.send(json.dumps({"id": i, "method": method, "params": {}}))
        while len(got) < n:
            msg = json.loads(ws.recv(timeout=timeout))
            got[msg["id"]] = msg
    return got


def test_counted_commands_are_paced_to_the_configured_rate(wayfern, relay):
    f = wayfern(burst=6, per_s=20.0)
    t, url = relay(f.url, per_min=600, burst=4)            # 10/s after a burst of 4
    t0 = time.monotonic()
    got = _call_many(url, "Runtime.callFunctionOn", 20)
    took = time.monotonic() - t0
    assert all("result" in m for m in got.values()), "the relay must have kept every command under Donut's limit"
    assert f.rejected == 0 and t.stats["retried"] == 0
    assert took >= 1.2, f"20 commands at 10/s after a burst of 4 cannot finish in {took:.2f}s"
    assert t.stats["paced"] >= 10 and t.stats["sent"] == 20


def test_a_command_donut_rejects_anyway_is_resent_and_the_client_never_sees_the_error(wayfern, relay):
    f = wayfern(burst=6, per_s=20.0)
    t, url = relay(f.url, per_min=100000, burst=1000, retry_gap=0.1)     # relay deliberately too loose
    got = _call_many(url, "Runtime.callFunctionOn", 30)
    assert all("result" in m and "error" not in m for m in got.values())
    assert f.rejected > 0, "the fake must really have throttled, or this proves nothing"
    assert t.stats["retried"] > 0 and t.stats["gave_up"] == 0
    assert sorted(got) == list(range(1, 31)), "every command must get exactly its own reply"


def test_uncounted_commands_are_not_paced_or_delayed(wayfern, relay):
    f = wayfern(burst=6, per_s=20.0)
    t, url = relay(f.url, per_min=60, burst=1)             # a 1/s relay would take a minute if it counted these
    t0 = time.monotonic()
    got = _call_many(url, "Input.dispatchMouseEvent", 200)
    assert len(got) == 200 and time.monotonic() - t0 < 5
    assert t.stats["sent"] == 0 and t.stats["paced"] == 0


def test_other_errors_pass_through_untouched(wayfern, relay):
    f = wayfern(other_error_for="Runtime.evaluate")
    t, url = relay(f.url, per_min=100000, burst=1000)
    got = _call_many(url, "Runtime.evaluate", 3)
    assert all(m["error"]["message"] == "nope" for m in got.values())
    assert t.stats["retried"] == 0


def test_it_gives_up_after_max_wait_and_hands_the_error_back(wayfern, relay):
    f = wayfern(always_reject=True)
    t, url = relay(f.url, per_min=100000, burst=1000, retry_gap=0.1, max_wait=0.4)
    got = _call_many(url, "Runtime.callFunctionOn", 1, timeout=10)
    assert got[1]["error"]["message"].startswith("Automation rate limit exceeded")
    assert t.stats["gave_up"] == 1 and t.stats["retried"] >= 1


def test_large_replies_and_two_connections_work(wayfern, relay):
    f = wayfern()
    t, url = relay(f.url, per_min=100000, burst=1000)
    big = "x" * 3_000_000                                  # a screenshot-sized frame must not hit a size cap
    with ws_connect(url, max_size=None) as a, ws_connect(url, max_size=None) as b:
        a.send(json.dumps({"id": 1, "method": "Page.captureScreenshot", "params": {"p": big}}))
        b.send(json.dumps({"id": 1, "method": "Runtime.callFunctionOn", "params": {}}))
        assert json.loads(a.recv(timeout=10))["id"] == 1
        assert json.loads(b.recv(timeout=10))["id"] == 1


# ── wiring into donut.connect / release ───────────────────────────────────────────────────────────
class _Pw:
    def __init__(self):
        self.endpoints = []
        self.chromium = self

    def connect_over_cdp(self, endpoint, timeout=None):
        self.endpoints.append(endpoint)
        return "browser"


def _lease():
    return donut.Lease("pid", None, 9301, "127.0.0.1", "account", "storm-x")


def test_connect_attaches_through_the_relay_and_release_stops_it(wayfern, monkeypatch):
    f = wayfern()
    monkeypatch.setattr(donut, "_browser_ws", lambda lease: f.url)
    monkeypatch.setenv("DONUT_API_TOKEN", "tok")
    monkeypatch.delenv("DONUT_CDP_THROTTLE", raising=False)
    monkeypatch.setattr(donut, "_Api", lambda: (_ for _ in ()).throw(donut.DonutError("no api in this test")))
    pw, lease = _Pw(), _lease()
    assert donut.connect(pw, lease) == "browser"
    assert pw.endpoints[0].startswith("ws://127.0.0.1:") and pw.endpoints[0] != lease.cdp_url
    relay_obj = lease.throttle
    assert relay_obj is not None
    donut.release(lease)
    assert lease.throttle is None and not relay_obj._thread.is_alive()


def test_connect_goes_direct_when_the_relay_is_switched_off(monkeypatch):
    monkeypatch.setenv("DONUT_CDP_THROTTLE", "0")
    pw, lease = _Pw(), _lease()
    donut.connect(pw, lease)
    assert pw.endpoints == [lease.cdp_url] and lease.throttle is None


def test_a_relay_that_cannot_start_never_costs_the_session(monkeypatch):
    monkeypatch.delenv("DONUT_CDP_THROTTLE", raising=False)
    monkeypatch.setattr(donut, "_browser_ws", lambda lease: (_ for _ in ()).throw(OSError("no /json/version")))
    pw, lease = _Pw(), _lease()
    donut.connect(pw, lease)
    assert pw.endpoints == [lease.cdp_url] and lease.throttle is None


def test_settings_reach_the_vendored_engine(monkeypatch):
    from libs.domain.settings import resolve
    from libs.domain.enums import SettingsLayer as L
    eff = resolve({L.GLOBAL: {}})
    assert eff["DONUT_CDP_THROTTLE"] is True and eff["DONUT_CDP_PER_MIN"] == 90.0 and eff["DONUT_CDP_BURST"] == 180
    for k in ("DONUT_CDP_THROTTLE", "DONUT_CDP_PER_MIN", "DONUT_CDP_BURST"):
        monkeypatch.delenv(k, raising=False)
    exported = vendor_env.apply({"DONUT_CDP_THROTTLE": False, "DONUT_CDP_PER_MIN": 60.0, "DONUT_CDP_BURST": 100})
    assert exported["DONUT_CDP_THROTTLE"] == "0" and exported["DONUT_CDP_PER_MIN"] == "60" \
        and exported["DONUT_CDP_BURST"] == "100"
    assert donut._throttle_enabled() is False and donut._env_float("DONUT_CDP_PER_MIN", 90) == 60.0

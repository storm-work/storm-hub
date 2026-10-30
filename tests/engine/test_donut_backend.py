"""BROWSER_BACKEND=donut: the Donut Local API client (profile/proxy lifecycle) and its teardown wiring
in PlaywrightSession. Driven against an in-memory fake of the Donut API -- no app, no portal."""
import threading

import httpx
import pytest

import libs.engine.vendor.session as session
from libs.engine import vendor_env

donut = session._donut


class _FakeDonut:
    """Just enough of /v1/profiles + /v1/proxies to exercise the client."""

    def __init__(self):
        self.calls = []
        self.profiles, self.proxies, self.running = {}, {}, set()
        self.n = 0
        self.fail = {}          # (method, path-prefix) -> status

    def _id(self):
        self.n += 1
        return f"id{self.n}"

    def __call__(self, method, url, json=None, headers=None, timeout=None):
        path = url.split("10108", 1)[1]
        self.calls.append((method, path, json))
        assert headers == {"Authorization": "Bearer tok"}
        for (m, prefix), status in self.fail.items():
            if m == method and path.startswith(prefix):
                return httpx.Response(status, text="nope", headers={"Retry-After": "60"})
        parts = path.split("/")[2:]           # ['profiles', id, 'run'] ...
        coll = parts[0]
        store = self.profiles if coll == "profiles" else self.proxies
        key = "proxy" if coll == "proxies" else "profile"
        if method == "GET" and len(parts) == 1:
            return httpx.Response(200, json={coll: list(store.values())})
        if method == "POST" and len(parts) == 1:
            obj = dict(json, id=self._id())
            store[obj["id"]] = obj
            return httpx.Response(200, json={key: obj})
        oid = parts[1]
        if method == "PUT":
            store[oid].update(json)
            return httpx.Response(200, json={key: store[oid]})
        if method == "DELETE":
            store.pop(oid)
            return httpx.Response(204)
        if parts[2] == "run":
            attached = oid in self.running
            self.running.add(oid)
            return httpx.Response(200, json={"profile_id": oid, "remote_debugging_port": 9301,
                                             "headless": json["headless"], "attached": attached})
        if parts[2] == "kill":
            self.running.discard(oid)
            return httpx.Response(204)
        raise AssertionError(path)


@pytest.fixture
def api(monkeypatch):
    fake = _FakeDonut()
    monkeypatch.setattr(donut.httpx, "request", fake)
    monkeypatch.setenv("BROWSER_BACKEND", "donut")
    monkeypatch.setenv("DONUT_API_TOKEN", "tok")
    monkeypatch.delenv("DONUT_API_URL", raising=False)
    monkeypatch.delenv("DONUT_PROFILE_SCOPE", raising=False)
    monkeypatch.delenv("DONUT_PROFILE_OS", raising=False)
    monkeypatch.delenv("DONUT_HEADLESS", raising=False)
    monkeypatch.setattr(donut, "_profile_ids", {})
    monkeypatch.setattr(donut, "_proxy_ids", {})
    return fake


def test_proxy_url_becomes_donut_proxy_settings():
    assert donut.proxy_settings("http://user%40x:p%3Ass@gate.example:9000") == {
        "proxy_type": "http", "host": "gate.example", "port": 9000,
        "username": "user@x", "password": "p:ss"}
    assert donut.proxy_settings("socks5h://h:1")["proxy_type"] == "socks5"
    with pytest.raises(donut.DonutError, match="not supported"):
        donut.proxy_settings("ftp://h:1")


def test_account_scope_creates_once_then_reuses_and_rewrites_the_exit(api):
    lease = donut.launch("http://u:p@exit-a:1000", fp_seed="sub-1")
    assert lease.scope == "account" and lease.cdp_url == "http://127.0.0.1:9301"
    prof = api.profiles[lease.profile_id]
    assert prof["browser"] == "wayfern" and prof["wayfern_config"] == {"os": "windows"}
    assert prof["ephemeral"] is False and prof["clear_on_close"] is True
    assert prof["proxy_id"] == lease.proxy_id
    donut.release(lease)
    assert lease.profile_id in api.profiles          # account profiles persist; only stopped
    assert lease.profile_id not in api.running

    api.calls.clear()
    again = donut.launch("http://u:p@exit-b:2000", fp_seed="sub-1")
    assert (again.profile_id, again.proxy_id) == (lease.profile_id, lease.proxy_id)
    assert not [c for c in api.calls if c[0] == "POST" and c[1] in ("/v1/profiles", "/v1/proxies")]
    # the PUT carries the FULL proxy settings (a partial PUT clears username/password)
    assert api.proxies[lease.proxy_id]["proxy_settings"] == {
        "proxy_type": "http", "host": "exit-b", "port": 2000, "username": "u", "password": "p"}


def test_no_seed_or_session_scope_is_a_throwaway_profile(api, monkeypatch):
    lease = donut.launch("http://u:p@exit:1", fp_seed=None)
    assert lease.scope == "session" and api.profiles[lease.profile_id]["ephemeral"] is True
    donut.release(lease)
    assert not api.profiles and not api.proxies

    monkeypatch.setenv("DONUT_PROFILE_SCOPE", "session")
    assert donut.launch(None, fp_seed="sub-1").scope == "session"


def test_a_profile_left_running_is_restarted_not_shared(api):
    first = donut.launch("http://u:p@exit:1", fp_seed="sub-1")     # never released (crashed worker)
    api.calls.clear()
    donut.launch("http://u:p@exit:1", fp_seed="sub-1")
    paths = [(m, p) for m, p, _ in api.calls]
    run = f"/v1/profiles/{first.profile_id}/run"
    assert paths[-3:] == [("POST", run), ("POST", f"/v1/profiles/{first.profile_id}/kill"),
                          ("POST", run)]


def test_operator_readable_errors(api, monkeypatch):
    api.fail[("POST", "/v1/profiles/")] = 402
    with pytest.raises(donut.DonutError, match="plan does not include automation"):
        donut.launch(None, fp_seed=None)
    api.fail = {("POST", "/v1/profiles"): 429}
    monkeypatch.setattr(donut.time, "sleep", lambda s: None)   # a 429 is now waited out, then re-raised
    with pytest.raises(donut.DonutError, match="Retry-After=60"):
        donut.launch(None, fp_seed=None)
    monkeypatch.setenv("DONUT_API_TOKEN", "")
    with pytest.raises(donut.DonutError, match="DONUT_API_TOKEN is not set"):
        donut.launch(None, fp_seed=None)


def test_failed_run_stops_what_it_started(api):
    api.fail[("POST", "/v1/profiles/id2/run")] = 500     # id1 = proxy, id2 = profile
    with pytest.raises(donut.DonutError):
        donut.launch("http://u:p@exit:1", fp_seed=None)
    assert not api.profiles and not api.proxies


def test_release_is_idempotent_and_never_raises(api, monkeypatch):
    lease = donut.launch(None, fp_seed=None)
    donut.release(lease)
    n = len(api.calls)
    donut.release(lease)
    assert len(api.calls) == n
    donut.release(None)

    def _down(*a, **k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(donut.httpx, "request", _down)
    donut.release(donut.Lease("p", None, 1, "127.0.0.1", "session", "n"))


def test_enabled_reads_the_backend_setting(monkeypatch):
    monkeypatch.delenv("BROWSER_BACKEND", raising=False)
    assert not donut.enabled()
    monkeypatch.setenv("BROWSER_BACKEND", "Donut")
    assert donut.enabled()


def test_vendor_env_exports_the_token_but_never_echoes_it(monkeypatch):
    for k in ("BROWSER_BACKEND", "DONUT_API_TOKEN", "DONUT_HEADLESS"):
        monkeypatch.delenv(k, raising=False)
    out = vendor_env.apply({"BROWSER_BACKEND": "donut", "DONUT_API_TOKEN": "s3cret",
                            "DONUT_HEADLESS": False})
    import os
    assert os.environ["DONUT_API_TOKEN"] == "s3cret"
    assert "s3cret" not in str(out) and out["DONUT_API_TOKEN"] == "•••set"
    assert out["BROWSER_BACKEND"] == "donut" and os.environ["DONUT_HEADLESS"] == "0"


class _NeverReady(threading.Event):
    def wait(self, timeout=None):
        super().wait(0.2)
        return False


def test_init_timeout_stops_the_donut_profile(monkeypatch):
    released = []
    monkeypatch.setattr(session, "_kill_proc_tree", lambda pid: None)
    monkeypatch.setattr(session._donut, "release", lambda lease: released.append(lease))
    monkeypatch.setattr(session.threading, "Event", _NeverReady)

    def _hung_worker(self):
        self._donut_lease = "lease"      # profile started, then the bootstrap navs hung
    monkeypatch.setattr(session.PlaywrightSession, "_worker", _hung_worker)

    with pytest.raises(RuntimeError, match="init timed out"):
        session.PlaywrightSession(proxy=None)
    assert released == ["lease"]


# ── 429: wait out Retry-After instead of rolling into another run ─────────────────────────────────
def test_a_rate_limited_run_is_waited_out_then_succeeds(api, monkeypatch):
    """Every exit roll is a new profile run, so Donut's hourly run limit is the likeliest to bite.
    Sitting out a short Retry-After keeps the account and exit; rolling would just spend another run."""
    waits = []
    monkeypatch.setattr(donut.time, "sleep", waits.append)
    real = api.__call__
    hits = {"n": 0}

    def once_429(method, url, **kw):
        if method == "POST" and url.endswith("/run") and hits["n"] == 0:
            hits["n"] += 1
            return httpx.Response(429, text="hourly limit", headers={"Retry-After": "30"})
        return real(method, url, **kw)
    monkeypatch.setattr(donut.httpx, "request", once_429)
    lease = donut.launch(None, fp_seed="acct-1")
    assert lease.port == 9301 and 30.0 in waits


def test_a_long_retry_after_fails_fast(api, monkeypatch):
    waits = []
    monkeypatch.setattr(donut.time, "sleep", waits.append)
    api.fail = {("POST", "/v1/profiles"): 429}
    monkeypatch.setattr(donut.httpx, "request", lambda m, u, **kw: httpx.Response(
        429, text="hourly limit", headers={"Retry-After": "1800"}))
    with pytest.raises(donut.DonutRateLimited):
        donut.launch(None, fp_seed=None)
    assert waits == [], "a half-hour wait must not hold the worker thread"


def test_donut_infrastructure_failures_are_never_charged_to_the_account():
    from apps.worker.ops_live import LiveEngineOps
    for msg in ("Donut POST /v1/profiles/x/run -> HTTP 429 (rate limited, Retry-After=30s): limit",
                "Donut API unreachable at http://127.0.0.1:10108 (ConnectError: refused)",
                "could not attach to Donut profile storm-abc at http://127.0.0.1:9301: timeout",
                "Donut run returned no remote_debugging_port: {}"):
        assert LiveEngineOps._is_transient_login_failure(msg), msg


# ── close_browser waits for the teardown that stops the profile ──────────────────────────────────
def _closing_session(teardown):
    """A bare session whose browser thread acks CLOSE_BROWSER, then runs `teardown` -- the real thread's
    order: ack first, browser.close / pw.stop / Donut release after."""
    import queue
    import threading

    from libs.engine.vendor.session import PlaywrightSession, _StampedResultQueue

    class _S(PlaywrightSession):
        def __init__(self):
            self._cmd_q = queue.Queue()
            self._seq_lock = threading.Lock()
            self._next_seq = 0
            self._current_seq = 0
            self._res_q = _StampedResultQueue(self)
            self._driver_pid = None
            self._donut_lease = object()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

        def _run(self):
            cmd = self._cmd_q.get(timeout=5)
            self._current_seq = cmd["_seq"]
            self._res_q.put(("ok", None))
            teardown()

    return _S()


def test_close_browser_does_not_return_before_the_profile_is_stopped(monkeypatch):
    import time

    from libs.engine.vendor import session as sess_mod

    stopped = []
    monkeypatch.setattr(sess_mod._donut, "release", lambda lease: stopped.append("release"))

    def slow_teardown():
        time.sleep(0.4)
        stopped.append("thread-teardown")

    s = _closing_session(slow_teardown)
    s.close_browser()
    assert stopped[0] == "thread-teardown", (
        "close_browser returned on the ack, before the browser thread finished tearing down -- a worker "
        f"exiting right after would kill the thread mid-teardown; got {stopped}")
    assert "release" in stopped


def test_close_browser_stops_the_profile_even_if_the_teardown_never_finishes(monkeypatch):
    import threading

    from libs.engine.vendor import session as sess_mod

    released, killed = [], []
    monkeypatch.setattr(sess_mod._donut, "release", lambda lease: released.append(lease))
    monkeypatch.setattr(sess_mod, "_kill_proc_tree", lambda pid: killed.append(pid))
    gate = threading.Event()
    s = _closing_session(lambda: gate.wait(timeout=30))
    real_join = s._thread.join
    monkeypatch.setattr(s._thread, "join", lambda timeout=None: real_join(0.2))   # don't sit out 15s
    try:
        s.close_browser()
        assert released, "a wedged teardown must not leave the Donut profile running"
        assert killed, "and the driver tree is killed rather than left to leak"
    finally:
        gate.set()


def test_a_rejected_profile_create_does_not_leave_its_proxy_entry_behind(monkeypatch):
    """Donut answered 429 on POST /v1/profiles AFTER the proxy entry was created; every retry left one
    more stray entry (132 found on 2026-10-30)."""
    calls = []

    class _Api:
        host = "h"

        def call(self, method, path, body=None, timeout=30.0):
            calls.append((method, path))
            if method == "GET":
                return {"profiles": [], "proxies": []}
            if (method, path) == ("POST", "/v1/proxies"):
                return {"proxy": {"id": "px9"}}
            raise donut.DonutRateLimited("429", 60.0)

    monkeypatch.setattr(donut, "_Api", _Api)
    monkeypatch.setattr(donut, "_profile_ids", {})
    monkeypatch.setattr(donut, "_proxy_ids", {})
    with pytest.raises(donut.DonutRateLimited):
        donut._launch_once("http://u:p@h:1", "someuser")
    assert ("DELETE", "/v1/proxies/px9") in calls

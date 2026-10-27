"""A sub-account's device and exit country, from registration to booking: the proxy-URL country helpers,
the Donut profile name / keep-state behaviour, the session's device fingerprint, and the registrar's
recording of it. No Donut app, no browser, no portal."""
import ast
import sys
import types
from pathlib import Path

import pytest

import libs.engine.vendor.session as session
from libs.proxies.exit_ip import lookup_exit_country
from libs.proxies.rotating import pin_geo, reseat_geo, rotating_session_id, url_country
from tests.engine.test_donut_backend import _FakeDonut, api  # noqa: F401  (the `api` fixture)

donut = session._donut

SOAX = "http://package-352438-country-pt-sessionid-abc123def456ghij-sessionlength-3600:pw@proxy.soax.com:5000"
SOAX_ANY = "http://package-352438-sessionid-abc123def456ghij-sessionlength-3600:pw@proxy.soax.com:5000"
CAPSOLVER = "http://PKG-RESIDENTIAL-country_NL-rotate_5m-session_abc123:pw@proxy.capsolver.com:36583"
WEBSHARE = "http://rsdmfwzj-1234567890123456:pw@p.webshare.io:80"


# ── what country does an exit URL ask for, and can we change it ─────────────────────────────────────
def test_url_country_reads_every_rotating_grammar():
    assert url_country(SOAX) == "PT"
    assert url_country(CAPSOLVER) == "NL"
    assert url_country(pin_geo(WEBSHARE, "de", provider="webshare")) == "DE"
    assert url_country(SOAX_ANY) is None and url_country(WEBSHARE) is None
    assert url_country("http://u:p@203.0.113.7:8080") is None and url_country(None) is None


def test_pin_geo_reseats_webshare_without_touching_the_session_or_password():
    de = pin_geo(WEBSHARE, "de", provider="webshare")
    assert de == "http://rsdmfwzj-DE-1234567890123456:pw@p.webshare.io:80"
    assert pin_geo(de, "it") == "http://rsdmfwzj-IT-1234567890123456:pw@p.webshare.io:80"
    assert pin_geo(de, "") == WEBSHARE, "blank = any exit, the segment is dropped"
    assert rotating_session_id(de) == rotating_session_id(WEBSHARE)


def test_reseat_geo_itself_still_leaves_webshare_exits_as_minted():
    """The per-roll geo VARIATION must not start rewriting webshare usernames (an earlier, deliberate
    decision); only the explicit per-account pin does, through pin_geo."""
    assert reseat_geo(WEBSHARE, "DE") == WEBSHARE
    assert reseat_geo(WEBSHARE, "DE", provider="webshare") == WEBSHARE


def test_pin_geo_agrees_with_reseat_geo_on_soax_and_capsolver_and_leaves_unknowns_alone():
    assert pin_geo(SOAX, "DE") == reseat_geo(SOAX, "DE")
    assert pin_geo(CAPSOLVER, "DE") == reseat_geo(CAPSOLVER, "DE")
    for odd in ("http://u:p@203.0.113.7:8080", "http://a-b-c-d:pw@p.webshare.io:80", "", "garbage"):
        assert pin_geo(odd, "DE") == odd


# ── looking the country up when the credential does not say ─────────────────────────────────────────
def _fake_primp(monkeypatch, answers):
    calls = []

    class _Resp:
        def __init__(self, data):
            self._d = data

        def json(self):
            if isinstance(self._d, Exception):
                raise self._d
            return self._d

    class _Client:
        def __init__(self, **kw):
            calls.append(kw.get("proxy"))

        def get(self, url, timeout=None):
            return _Resp(answers[url])

    monkeypatch.setitem(sys.modules, "primp", types.SimpleNamespace(Client=_Client))
    return calls


def test_lookup_exit_country_uses_the_first_service_that_answers(monkeypatch):
    calls = _fake_primp(monkeypatch, {"https://ipwho.is/json/": {"country_code": "de"},
                                      "https://ipapi.co/json/": {"country_code": "XX"}})
    assert lookup_exit_country(WEBSHARE) == "DE" and calls == [WEBSHARE], "looked up THROUGH the exit"


def test_lookup_exit_country_falls_back_and_never_guesses(monkeypatch):
    _fake_primp(monkeypatch, {"https://ipwho.is/json/": ValueError("html"),
                              "https://ipapi.co/json/": {"country_code": "It"}})
    assert lookup_exit_country(WEBSHARE) == "IT"
    _fake_primp(monkeypatch, {"https://ipwho.is/json/": {"country_code": ""},
                              "https://ipapi.co/json/": {"error": True}})
    assert lookup_exit_country(WEBSHARE) is None, "unknown stays unknown"


# ── Donut profile identity ──────────────────────────────────────────────────────────────────────────
def test_profile_name_is_the_one_a_launch_uses_and_needs_a_seed(api):
    name = donut.profile_name("alice")
    assert name.startswith("storm-") and name == donut.profile_name("alice") != donut.profile_name("bob")
    assert donut.profile_name(None) is None and donut.profile_name("") is None
    lease = donut.launch("http://u:p@h:1", "alice")
    assert lease.name == name, "registration and booking must derive the SAME profile from the username"


def test_a_profile_clears_its_cookies_on_close_unless_keep_state_is_on(api, monkeypatch):
    monkeypatch.delenv("DONUT_PROFILE_KEEP_STATE", raising=False)
    donut.launch(None, "alice")
    assert next(p for p in api.profiles.values())["clear_on_close"] is True
    monkeypatch.setenv("DONUT_PROFILE_KEEP_STATE", "1")
    donut.launch(None, "alice")                                   # an existing profile is flipped too
    assert next(p for p in api.profiles.values())["clear_on_close"] is False
    donut.launch(None, "carol")
    assert [p["clear_on_close"] for p in api.profiles.values()] == [False, False]


def _attach(monkeypatch, keep_state):
    """Run PlaywrightSession._launch_donut against a fake browser; return the cookie clears it made."""
    clears = []

    class _Ctx:
        def clear_cookies(self, **kw):
            clears.append(kw)

    ctx = _Ctx()
    browser = types.SimpleNamespace(contexts=[ctx], new_context=lambda: ctx)
    monkeypatch.setattr(donut, "launch", lambda proxy, seed: object())
    monkeypatch.setattr(donut, "connect", lambda pw, lease, timeout_s=30.0: browser)
    monkeypatch.setenv("DONUT_PROFILE_KEEP_STATE", "1" if keep_state else "0")
    s = object.__new__(session.PlaywrightSession)
    s.proxy, s._fp_seed, s._storage_state = None, "alice", None
    s._launch_donut(object())
    return clears


def test_a_kept_profile_has_only_the_portals_cookies_cleared_on_attach(monkeypatch):
    clears = _attach(monkeypatch, keep_state=True)
    assert clears == [{"domain": "pedidodevistos.mne.gov.pt"}], \
        "a stale portal session must never reach a fresh login; everything else the profile kept stays"


def test_a_normal_profile_is_not_touched_on_attach(monkeypatch):
    assert _attach(monkeypatch, keep_state=False) == []


# ── the device fingerprint ──────────────────────────────────────────────────────────────────────────
def _fp_session(monkeypatch, result):
    s = object.__new__(session.PlaywrightSession)

    def _eval(js, timeout=30):
        if isinstance(result, Exception):
            raise result
        return result

    s.browser_eval = _eval
    return s


_FP = {"ua": "Mozilla/5.0 X", "platform": "Win32", "webgl": "ANGLE (AMD)", "screen": "1920x1080",
       "cores": 8, "mem": 8, "langs": "en-US,en"}


def test_identity_fingerprint_is_a_stable_hash_of_the_device(monkeypatch):
    a = _fp_session(monkeypatch, dict(_FP)).identity_fingerprint()
    b = _fp_session(monkeypatch, dict(reversed(list(_FP.items())))).identity_fingerprint()
    assert a["hash"] == b["hash"] and len(a["hash"]) == 16, "key order must not matter"
    c = _fp_session(monkeypatch, dict(_FP, webgl="ANGLE (NVIDIA)")).identity_fingerprint()
    assert c["hash"] != a["hash"], "a different GPU is a different device"


def test_identity_fingerprint_never_raises(monkeypatch):
    assert _fp_session(monkeypatch, RuntimeError("page closed")).identity_fingerprint() is None
    assert _fp_session(monkeypatch, {}).identity_fingerprint() is None
    assert _fp_session(monkeypatch, None).identity_fingerprint() is None


# ── the registrar ───────────────────────────────────────────────────────────────────────────────────
def test_every_registrar_browser_session_is_seeded_with_the_username():
    """Register AND verify must run on the account's own device -- an unseeded get_session is a random
    fingerprint (chromium) or a throwaway profile (donut) that booking can never get back to."""
    src = (Path(__file__).resolve().parents[2] / "apps" / "registrar" / "__main__.py").read_text(encoding="utf-8")
    calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "get_session"]
    assert len(calls) >= 2
    for c in calls:
        kw = {k.arg: k.value for k in c.keywords}
        assert "fp_seed" in kw and getattr(kw["fp_seed"], "id", None) == "username"


@pytest.fixture
def registrar():
    import apps.registrar.__main__ as r
    return r


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class _Exec:
    """A ThreadPoolExecutor stand-in that runs inline, so the helper's await is deterministic."""
    def submit(self, fn, *a):
        import concurrent.futures as cf
        f = cf.Future()
        f.set_result(fn(*a))
        return f


def _record(registrar, monkeypatch, proxy, backend="donut", scope="account", fp=None, lookup=None):
    monkeypatch.setenv("BROWSER_BACKEND", backend)
    monkeypatch.setenv("DONUT_PROFILE_SCOPE", scope)
    sess = types.SimpleNamespace(identity_fingerprint=lambda: fp)
    monkeypatch.setattr("libs.proxies.exit_ip.lookup_exit_country", lookup or (lambda p: None))

    async def go():
        import asyncio
        loop = asyncio.get_running_loop()
        return await registrar._record_device(loop, None, sess, proxy, "alice")

    import asyncio
    orig = asyncio.AbstractEventLoop.run_in_executor
    monkeypatch.setattr(asyncio.AbstractEventLoop, "run_in_executor",
                        lambda self, ex, fn, *a: _async_call(fn, *a))
    try:
        return _run(go())
    finally:
        monkeypatch.setattr(asyncio.AbstractEventLoop, "run_in_executor", orig)


def _async_call(fn, *a):
    import asyncio
    fut = asyncio.get_event_loop().create_future()
    fut.set_result(fn(*a))
    return fut


def test_a_donut_registration_records_profile_device_and_country(registrar, monkeypatch):
    got = _record(registrar, monkeypatch, SOAX, fp={"hash": "f00d"})
    assert got == {"browser_backend": "donut", "donut_profile": donut.profile_name("alice"),
                   "fingerprint_hash": "f00d", "registered_geo": "PT"}


def test_the_country_is_looked_up_only_when_the_credential_does_not_name_it(registrar, monkeypatch):
    looked = []
    got = _record(registrar, monkeypatch, WEBSHARE, fp={"hash": "f00d"},
                  lookup=lambda p: looked.append(p) or "DE")
    assert got["registered_geo"] == "DE" and looked == [WEBSHARE]
    looked.clear()
    _record(registrar, monkeypatch, SOAX, fp={"hash": "f00d"}, lookup=lambda p: looked.append(p) or "XX")
    assert looked == [], "a credential that names its country needs no network lookup"


def test_a_chromium_registration_records_no_profile(registrar, monkeypatch):
    got = _record(registrar, monkeypatch, SOAX, backend="chromium", fp={"hash": "f00d"})
    assert got["browser_backend"] == "chromium" and "donut_profile" not in got


def test_session_scope_has_no_stable_profile_to_record(registrar, monkeypatch):
    assert "donut_profile" not in _record(registrar, monkeypatch, SOAX, scope="session", fp={"hash": "f00d"})


def test_a_failing_device_read_never_fails_the_registration(registrar, monkeypatch):
    def _boom():
        raise RuntimeError("page closed")
    monkeypatch.setenv("BROWSER_BACKEND", "donut")
    sess = types.SimpleNamespace(identity_fingerprint=_boom)

    async def go():
        import asyncio
        return await registrar._record_device(asyncio.get_running_loop(), None, sess, SOAX, "alice")

    import asyncio
    monkeypatch.setattr(asyncio.AbstractEventLoop, "run_in_executor", lambda self, ex, fn, *a: _async_call(fn, *a))
    assert isinstance(_run(go()), dict)            # whatever it managed to gather, and no exception

import pathlib
"""LiveEngineOps -- construction, protocol conformance, and pure-logic unit tests. No real browser or
network calls (that's Phase E's live-gated tier); this covers what's testable offline: the response
classification, the discovery outcome shaping, and that the class actually satisfies EngineOps."""
import json
import pytest

from libs.domain.enums import SettingsLayer as L
from libs.domain.settings import resolve
from libs.engine.config import EngineConfig
from libs.engine.phases import EngineOps
from apps.worker.ops_live import LiveEngineOps, _extract_csrf, _submit_bounce_reason


def _origin_reply(js, otherwise=""):
    """Answer the opaque-origin probe with a LIVE portal origin, pass everything else through.

    Stubs that returned a bare "" for every eval made `_on_portal_origin` read them as a
    chrome-error page, so the guard correctly re-anchored and bailed out before the submit. These
    stubs are modelling a healthy page, so they have to say so.
    """
    import json as _j
    if "location.origin" in (js or ""):
        return _j.dumps({"o": "https://pedidodevistos.mne.gov.pt",
                         "h": "https://pedidodevistos.mne.gov.pt/VistosOnline/Schedule.jsp"})
    return otherwise


def _cfg(**template):
    return EngineConfig.from_settings(resolve({L.TEMPLATE: {"ROLE": "apply", **template}}))


_REAL_SAVE_SUBMIT_BODY = LiveEngineOps._save_submit_body


@pytest.fixture(autouse=True)
def _isolate_submit_bodies(tmp_path, monkeypatch):
    """Keep _save_submit_body out of the real var/submits/ tree.

    Same isolation class as the cleanup_checkpoints leak (F26): a test that drives do_apply through
    a rejection calls the REAL _save_submit_body, which resolves a path relative to the repo and
    wrote sub-1_*.html into production evidence. Point it at pytest's tmp_path instead.
    """
    real = LiveEngineOps._save_submit_body

    def _scoped(self, html, outcome):
        d = tmp_path / "submits"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{self.identity_id}_{outcome}.html"
        p.write_text(html or "", encoding="utf-8", errors="replace")
        return str(p)

    monkeypatch.setattr(LiveEngineOps, "_save_submit_body", _scoped)
    yield


def _make_ops(**overrides):
    kwargs = dict(
        cfg=_cfg(),
        account={"username": "u", "password": "p", "email": "e@example.com"},
        posto_id="5084",
        nationality="CPV",
        residence="IRL",
        quest_steps={"IRL": [{"id_pergunta": "1", "valor": "CPV"}]},
        form_defaults={"static": {}, "dynamic": {}, "from_account": {}, "from_nationality": {}},
        capsolver_keys=["key1"],
        warm_proxy="http://u:p@proxy:1234",
        sub_account_id="sub-1",
    )
    kwargs.update(overrides)
    return LiveEngineOps(**kwargs)


def test_satisfies_engine_ops_protocol():
    ops = _make_ops()
    assert isinstance(ops, EngineOps)


def test_identity_id_defaults_to_sub_account_id_if_not_given():
    ops = _make_ops()
    assert ops.identity_id == "sub-1"


def test_identity_id_explicit_overrides_default():
    ops = _make_ops(identity_id="identity-42")
    assert ops.identity_id == "identity-42"


class _FakeSession:
    def __init__(self, *, alive=True, cookies=None):
        self._alive = alive
        self.closed = False
        self._cookies = cookies or [{"name": "Vistos_sid", "value": "abc"}]

    def get_all_cookies(self):
        return self._cookies

    def browser_nav(self, url, timeout=30):
        return url

    def browser_eval(self, js):
        if "location.origin" in (js or ""): return _origin_reply(js)
        return "<html>cmbPeriodo</html>" if self._alive else "<html>Authentication.jsp</html>"

    def close_browser(self):
        self.closed = True


def test_do_swap_off_by_default_returns_none():
    ops = _make_ops()
    ops.session = _FakeSession()
    assert ops.cfg.hold_swap.swap_after_warmup is False   # catalog default
    assert ops.do_swap() is None
    assert ops.session.closed is False   # untouched


def test_do_swap_on_but_no_hold_proxy_returns_none():
    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_WARMUP=True))
    ops.session = _FakeSession()
    assert ops.do_swap() is None


def test_do_swap_success_replaces_session_and_closes_old(monkeypatch):
    import apps.worker.ops_live as ops_live_mod

    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_WARMUP=True), hold_proxy="http://u:p@holdhost:9999")
    old_session = _FakeSession()
    ops.session = old_session
    new_session = _FakeSession(alive=True)
    monkeypatch.setattr(ops_live_mod, "get_session", lambda **kw: new_session)

    result = ops.do_swap()

    assert result == "holdhost:9999"   # credentials stripped
    assert ops.session is new_session
    assert old_session.closed is True


def test_do_swap_reverts_on_failed_health_check(monkeypatch):
    import apps.worker.ops_live as ops_live_mod

    # HOLD_SWAP_RETRIES=0 -- testing the single-attempt revert behavior in isolation from the retry
    # protocol (covered separately below).
    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_WARMUP=True, HOLD_SWAP_RETRIES=0),
                    hold_proxy="http://u:p@holdhost:9999")
    old_session = _FakeSession()
    ops.session = old_session
    new_session = _FakeSession(alive=False)   # fails the post-swap health check
    monkeypatch.setattr(ops_live_mod, "get_session", lambda **kw: new_session)

    result = ops.do_swap()

    assert result is None
    assert ops.session is old_session   # reverted
    assert new_session.closed is True   # the failed attempt is cleaned up
    assert old_session.closed is False  # the original session is still usable


# ── swap retry protocol (operator spec 2026-08-14): first-attempt failure -> hold on the current
# proxy a while -> request a fresh hold proxy -> one more try; exhausting the budget gives up
# permanently but never stops the worker ─────────────────────────────────────────────────────────
def test_do_swap_retries_with_fresh_proxy_and_succeeds_on_final_attempt(monkeypatch):
    import apps.worker.ops_live as ops_live_mod

    sessions_by_proxy_call = [_FakeSession(alive=False), _FakeSession(alive=True)]
    calls = []

    def _spy(**kw):
        calls.append(kw["proxy"])
        return sessions_by_proxy_call[len(calls) - 1]

    monkeypatch.setattr(ops_live_mod, "get_session", _spy)
    sleeps = []
    monkeypatch.setattr(ops_live_mod.time, "sleep", lambda s: sleeps.append(s))

    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_WARMUP=True, HOLD_SWAP_RETRIES=1,
                             HOLD_SWAP_RETRY_HOLD_SECS=600.0),
                    hold_proxy="http://u:p@holdhost:9999", emit=lambda t, **p: None,
                    signals=_RotationSignals(proxy="http://u:p@freshhold:8888"))
    ops.session = _FakeSession()

    result = ops.do_swap()

    assert result == "freshhold:8888"   # succeeded on the retry, using the freshly rotated proxy
    assert calls == ["http://u:p@holdhost:9999", "http://u:p@freshhold:8888"]
    assert sleeps == [600.0]   # held on the current proxy before the retry
    assert ops.hold_proxy == "http://u:p@freshhold:8888"


# ── Phase 5 (D33 follow-up): a static hold identity must never be locally transformed by
# roll_sticky_session -- only a rotating one may be. ────────────────────────────────────────────────
def test_do_swap_retry_still_rolls_a_rotating_hold_proxy(monkeypatch):
    """Invariant D: rotating hold resources keep their previous local-rolling behavior exactly.
    Webshare-grammar credential (trailing numeric session id) so roll_sticky_session actually
    changes it -- the FIRST retry must use a locally-rolled credential, never touch the Hub at all."""
    import apps.worker.ops_live as ops_live_mod

    sessions = [_FakeSession(alive=False), _FakeSession(alive=True)]
    calls = []

    def _spy(**kw):
        calls.append(kw["proxy"])
        return sessions[len(calls) - 1]

    monkeypatch.setattr(ops_live_mod, "get_session", _spy)
    signals = _RotationSignals(proxy="http://sodade2026-99:pw@hold.example:80", resource_type="static")
    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_WARMUP=True, HOLD_SWAP_RETRIES=1,
                             HOLD_SWAP_RETRY_HOLD_SECS=0.0),
                    hold_proxy="http://sodade2026-51:pw@hold.example:80",
                    hold_proxy_static=False, emit=lambda t, **p: None, signals=signals)
    ops.session = _FakeSession()

    result = ops.do_swap()

    assert result is not None and result.startswith("hold.example:80#")   # rolled -- carries a sid
    assert calls[0] == "http://sodade2026-51:pw@hold.example:80"        # attempt 1: original
    assert calls[1].startswith("http://sodade2026-") and calls[1] != calls[0]  # attempt 2: ROLLED
    assert calls[1].endswith("@hold.example:80")   # same endpoint -- a local transform, not a lease
    assert signals.calls == 0   # the Hub was never asked -- rolling succeeded first, as before
    assert ops.hold_proxy == calls[1]


def test_do_swap_retry_never_rolls_a_static_hold_proxy(monkeypatch):
    """Invariant C: a static hold identity is never passed through roll_sticky_session. Same
    webshare-grammar credential as the rotating test above -- roll_sticky_session WOULD succeed on
    it syntactically, which is exactly why hold_proxy_static must gate the call, not rely on the
    roller declining. With the flag set, the retry must skip straight to the Hub."""
    import apps.worker.ops_live as ops_live_mod

    sessions = [_FakeSession(alive=False), _FakeSession(alive=True)]
    calls = []

    def _spy(**kw):
        calls.append(kw["proxy"])
        return sessions[len(calls) - 1]

    monkeypatch.setattr(ops_live_mod, "get_session", _spy)
    signals = _RotationSignals(proxy="http://freshstatic:pw@static.example:80", resource_type="static")
    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_WARMUP=True, HOLD_SWAP_RETRIES=1,
                             HOLD_SWAP_RETRY_HOLD_SECS=0.0),
                    hold_proxy="http://sodade2026-51:pw@hold.example:80",
                    hold_proxy_static=True, emit=lambda t, **p: None, signals=signals)
    ops.session = _FakeSession()

    result = ops.do_swap()

    assert result == "static.example:80"
    assert calls[0] == "http://sodade2026-51:pw@hold.example:80"                # attempt 1: original
    assert calls[1] == "http://freshstatic:pw@static.example:80"                # attempt 2: from the HUB
    assert signals.calls == 1   # the Hub WAS asked -- no local roll was ever attempted
    assert ops.hold_proxy == "http://freshstatic:pw@static.example:80"
    assert ops.hold_proxy_static is True   # still marked static after the fresh lease


def test_do_swap_gives_up_after_exhausting_retries_never_stops_worker(monkeypatch):
    import apps.worker.ops_live as ops_live_mod

    monkeypatch.setattr(ops_live_mod, "get_session", lambda **kw: _FakeSession(alive=False))
    monkeypatch.setattr(ops_live_mod.time, "sleep", lambda s: None)

    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_WARMUP=True, HOLD_SWAP_RETRIES=2), emit=lambda t, **p: None,
                    hold_proxy="http://u:p@holdhost:9999",
                    signals=_RotationSignals(proxy="http://u:p@freshhold:8888"))
    original_session = _FakeSession()
    ops.session = original_session

    result = ops.do_swap()

    assert result is None
    assert ops.session is original_session   # never left holding on a broken session
    assert ops.is_stopped() is False         # exhausting swap retries never stops the worker


# ── fp_seed: every session must present the same fingerprint for a given account (regression X3;
# a checkpoint restore injecting an authenticated cookie jar into a browser with a brand-new random
# fingerprint every time is a first-order detection signal) ────────────────────────────────────
def test_fresh_login_passes_stable_fp_seed(monkeypatch):
    import apps.worker.ops_live as ops_live_mod

    captured = {}

    def _spy(**kw):
        captured.update(kw)
        s = _FakeSession()
        s.browser_login = lambda *a, **k: {"status": 200, "body": '{"type":"success"}'}
        return s

    monkeypatch.setattr(ops_live_mod, "get_session", _spy)
    ops = _make_ops()
    assert ops._fresh_login() is True
    assert captured.get("fp_seed") == "u"   # _make_ops' default account username


def test_checkpoint_restore_passes_stable_fp_seed(monkeypatch):
    import apps.worker.ops_live as ops_live_mod

    captured = {}

    def _spy(**kw):
        captured.update(kw)
        return _FakeSession(alive=True)

    monkeypatch.setattr(ops_live_mod, "get_session", _spy)
    monkeypatch.setattr(ops_live_mod.ckpt, "load_checkpoint",
                        lambda identity_id: {"cookies": {}, "cookies_full": [], "sub_account_id": "sub-1"})
    ops = _make_ops(cfg=_cfg(RESTORE_STAMPEDE_JITTER=0))
    assert ops._try_restore_from_checkpoint() is True
    assert captured.get("fp_seed") == "u"


def test_do_swap_passes_stable_fp_seed(monkeypatch):
    import apps.worker.ops_live as ops_live_mod

    captured = {}

    def _spy(**kw):
        captured.update(kw)
        return _FakeSession(alive=True)

    monkeypatch.setattr(ops_live_mod, "get_session", _spy)
    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_WARMUP=True), hold_proxy="http://u:p@holdhost:9999")
    ops.session = _FakeSession()
    ops.do_swap()
    assert captured.get("fp_seed") == "u"


# ── P3: proxy rotation -- a worker asks the Hub for a fresh proxy drawn from the pool matching its
# CURRENT phase (warmup vs hold), since the worker process is deliberately DB-less and can't lease a
# proxy row itself ────────────────────────────────────────────────────────────────────────────────
class _RotationSignals:
    """Fake BusSignals exposing only wait_proxy -- the round-trip _request_proxy_rotation blocks on."""
    def __init__(self, ok=True, proxy=None, reason="pool exhausted", resource_type=None):
        self._ok = ok
        self._proxy = proxy
        self._reason = reason
        self._resource_type = resource_type
        self.calls = 0
        self.stopped = False   # matches real BusSignals' contract; is_stopped() reads this

    def wait_proxy(self, timeout=30):
        self.calls += 1
        if self._ok:
            return {"ok": True, "proxy": self._proxy, "resource_type": self._resource_type}
        return {"ok": False, "reason": self._reason}


def test_request_proxy_rotation_returns_none_without_bus_wired():
    ops = _make_ops()   # default _make_ops: no emit, no signals
    assert ops._request_proxy_rotation("warmup") is None


def test_request_proxy_rotation_emits_request_with_phase_and_returns_new_proxy():
    emitted = []
    ops = _make_ops(emit=lambda t, **p: emitted.append((t, p)),
                    signals=_RotationSignals(proxy="http://u:p@rotated:9999"))
    assert ops._request_proxy_rotation("hold") == "http://u:p@rotated:9999"
    assert emitted == [("proxy_request", {"phase": "hold"})]


def test_request_proxy_rotation_defaults_phase_to_current_phase():
    emitted = []
    ops = _make_ops(emit=lambda t, **p: emitted.append((t, p)),
                    signals=_RotationSignals(proxy="http://u:p@rotated:9999"))
    ops._current_phase = "hold"
    ops._request_proxy_rotation()
    assert emitted == [("proxy_request", {"phase": "hold"})]


def test_request_proxy_rotation_returns_none_when_pool_exhausted():
    ops = _make_ops(emit=lambda t, **p: None, signals=_RotationSignals(ok=False))
    assert ops._request_proxy_rotation("warmup") is None


# ── Phase 5: the failure reason used to be generated by the Hub and then silently discarded here --
# the single biggest "why did this fail" gap in the proxy subsystem (Phase 4 finding). ──────────────
def test_request_proxy_rotation_logs_the_discarded_reason_on_failure():
    logged = []
    ops = _make_ops(emit=lambda t, **p: None,
                    signals=_RotationSignals(ok=False, reason="no static resource available"))
    ops._log = lambda t: logged.append(t)
    assert ops._request_proxy_rotation("hold") is None
    assert any("no static resource available" in line for line in logged)


def test_request_proxy_rotation_logs_nothing_extra_when_bus_not_wired():
    """No signals wired -> returns None immediately, before any reason could exist. Must not log a
    spurious/blank failure line."""
    logged = []
    ops = _make_ops()
    ops._log = lambda t: logged.append(t)
    assert ops._request_proxy_rotation("hold") is None
    assert logged == []


# ── Phase 5: resource_type rides the Hub's proxy_assigned reply so the worker can tell a static hold
# assignment apart from a rotating one without any DB access (D1) -- used only to decide whether
# do_swap()'s retry ladder may locally roll the credential. ──────────────────────────────────────────
def test_request_proxy_rotation_sets_hold_proxy_static_true_from_hub_reply():
    ops = _make_ops(emit=lambda t, **p: None,
                    signals=_RotationSignals(proxy="http://u:p@fresh:80", resource_type="static"))
    assert ops.hold_proxy_static is False   # default
    assert ops._request_proxy_rotation("hold") == "http://u:p@fresh:80"
    assert ops.hold_proxy_static is True


def test_request_proxy_rotation_sets_hold_proxy_static_false_when_hub_says_rotating():
    ops = _make_ops(emit=lambda t, **p: None, hold_proxy_static=True,
                    signals=_RotationSignals(proxy="http://u:p@fresh:80", resource_type="rotating"))
    assert ops._request_proxy_rotation("hold") == "http://u:p@fresh:80"
    assert ops.hold_proxy_static is False   # follows the Hub's answer, not left stale from before


def test_request_proxy_rotation_warmup_phase_never_touches_hold_proxy_static():
    ops = _make_ops(emit=lambda t, **p: None, hold_proxy_static=True,
                    signals=_RotationSignals(proxy="http://u:p@fresh:80", resource_type=None))
    assert ops._request_proxy_rotation("warmup") == "http://u:p@fresh:80"
    assert ops.hold_proxy_static is True   # unrelated pool -- untouched


def test_do_swap_does_not_touch_the_pool_phase_when_swap_is_off():
    """REVERSED by the operator 2026-08-19. This previously asserted the opposite -- that the phase
    flips unconditionally because pool selection "will not be different when swap is off".

    That reading was wrong in practice: HOLD_PROXY_PROVIDER defaults to webshare, so flipping the
    phase with the swap OFF silently overrode the booking's own provider. A booking configured
    PROXY_PROVIDER=soax took 1427 webshare leases against 552 soax, and every login went through
    p.webshare.io. Off means off: skip the swap step and stay on the phase-1 pool."""
    ops = _make_ops()
    assert ops._current_phase == "warmup"
    ops.session = _FakeSession()
    assert ops.cfg.hold_swap.swap_after_warmup is False   # catalog default: swap off
    assert ops.do_swap() is None
    assert ops._current_phase == "warmup"


def test_fresh_login_requests_proxy_rotation_before_retry(monkeypatch):
    """Regression (L2/L3, closed by P3): a repeated login failure used to keep retrying on the SAME
    leased proxy forever. Attempt 1 uses the proxy already on hand; attempt 2+ must rotate first."""
    import apps.worker.ops_live as ops_live_mod

    calls = []

    def _spy(**kw):
        calls.append(kw)
        s = _FakeSession()
        if len(calls) == 1:
            s.browser_login = lambda *a, **k: {"status": 200, "body": '{"type":"error"}'}
        else:
            s.browser_login = lambda *a, **k: {"status": 200, "body": '{"type":"success"}'}
        return s

    monkeypatch.setattr(ops_live_mod, "get_session", _spy)
    ops = _make_ops(emit=lambda t, **p: None,
                    signals=_RotationSignals(proxy="http://u:p@rotated:9999"))
    ops._backoff = lambda attempt: None   # skip the real sleep

    assert ops._fresh_login() is True
    assert len(calls) == 2
    assert calls[0]["proxy"] == "http://u:p@proxy:1234"    # attempt 1: original proxy, no rotation yet
    assert calls[1]["proxy"] == "http://u:p@rotated:9999"  # attempt 2: rotated
    assert ops.warm_proxy == "http://u:p@rotated:9999"


def test_fresh_login_keeps_current_proxy_when_rotation_unavailable(monkeypatch):
    import apps.worker.ops_live as ops_live_mod

    calls = []

    def _spy(**kw):
        calls.append(kw)
        s = _FakeSession()
        s.browser_login = lambda *a, **k: {"status": 200, "body": '{"type":"error"}'}
        return s

    monkeypatch.setattr(ops_live_mod, "get_session", _spy)
    ops = _make_ops(emit=lambda t, **p: None, signals=_RotationSignals(ok=False))
    ops._backoff = lambda attempt: None

    assert ops._fresh_login() is False
    assert all(c["proxy"] == "http://u:p@proxy:1234" for c in calls)   # never rotated -- unavailable


# ── sub-account rotation: sticky retries on one candidate, then rotate to the next (operator spec,
# ported from storm-auto's confirmed app/worker.py:323-326 sticky-then-rotate pattern) ────────────
class _AccountAwareSession(_FakeSession):
    """browser_login's outcome depends on WHICH account logged in -- lets rotation tests tell apart
    which candidate a given attempt actually used."""
    def __init__(self, succeeds_for: set[str]):
        super().__init__()
        self._succeeds_for = succeeds_for

    def browser_login(self, username, password, **kw):
        if username in self._succeeds_for:
            return {"status": 200, "body": '{"type":"success"}'}
        return {"status": 200, "body": '{"type":"error"}'}


def test_fresh_login_rotates_to_next_account_after_sticky_exhausted(monkeypatch):
    import apps.worker.ops_live as ops_live_mod

    monkeypatch.setattr(ops_live_mod, "get_session",
                        lambda **kw: _AccountAwareSession(succeeds_for={"user-b"}))
    events = []
    accounts = [
        {"username": "user-a", "password": "pa", "sub_account_id": "sub-a"},
        {"username": "user-b", "password": "pb", "sub_account_id": "sub-b"},
    ]
    ops = _make_ops(cfg=_cfg(STICKY_RETRY_MAX=1), accounts=accounts, sub_account_id="sub-a",
                    emit=lambda t, **p: events.append((t, p)))
    ops._backoff = lambda attempt: None
    ops._request_proxy_rotation = lambda phase=None: None   # no proxy bus wired in this test

    assert ops._fresh_login() is True
    assert ops.sub_account_id == "sub-b"
    assert ops.account["username"] == "user-b"
    assert ("account_rotated", {"sub_account_id": "sub-b"}) in events


def test_fresh_login_gives_up_after_exhausting_all_accounts_sticky_budget(monkeypatch):
    import apps.worker.ops_live as ops_live_mod

    attempts = []

    def _spy(**kw):
        return _AccountAwareSession(succeeds_for=set())   # nobody ever succeeds

    monkeypatch.setattr(ops_live_mod, "get_session", _spy)
    accounts = [
        {"username": "user-a", "password": "pa", "sub_account_id": "sub-a"},
        {"username": "user-b", "password": "pb", "sub_account_id": "sub-b"},
        {"username": "user-c", "password": "pc", "sub_account_id": "sub-c"},
    ]
    ops = _make_ops(cfg=_cfg(STICKY_RETRY_MAX=2), accounts=accounts, sub_account_id="sub-a")
    ops._backoff = lambda attempt: attempts.append(attempt)
    ops._request_proxy_rotation = lambda phase=None: None

    assert ops._fresh_login() is False
    assert len(attempts) == 2 * 3   # sticky=2 x rotating=3 accounts -- the operator's own worked example


def test_fresh_login_rotation_starts_from_current_sub_account_not_index_zero():
    """A keepalive-restore re-login must resume rotation from wherever this worker currently is
    (e.g. sub-b, restored mid-fleet), not restart the whole ranking from index 0 every time."""
    import apps.worker.ops_live as ops_live_mod
    import unittest.mock

    login_order = []

    class _OrderTrackingSession(_FakeSession):
        def browser_login(self, username, password, **kw):
            login_order.append(username)
            if username == "user-a":
                return {"status": 200, "body": '{"type":"success"}'}
            return {"status": 200, "body": '{"type":"error"}'}

    accounts = [
        {"username": "user-a", "password": "pa", "sub_account_id": "sub-a"},
        {"username": "user-b", "password": "pb", "sub_account_id": "sub-b"},
    ]
    ops = _make_ops(cfg=_cfg(STICKY_RETRY_MAX=1), accounts=accounts, sub_account_id="sub-b")
    ops._backoff = lambda attempt: None
    ops._request_proxy_rotation = lambda phase=None: None

    with unittest.mock.patch.object(ops_live_mod, "get_session", return_value=_OrderTrackingSession()):
        assert ops._fresh_login() is True
    assert login_order == ["user-b", "user-a"]   # started at sub-b (current), wrapped to sub-a
    assert ops.sub_account_id == "sub-a"


def test_extract_csrf_primary_pattern():
    html = '<input type="hidden" name="__RequestVerificationToken" value="abc123" />'
    assert _extract_csrf(html) == "abc123"


def test_extract_csrf_alternate_attribute_order():
    html = '<input value="xyz789" type="hidden" name="__RequestVerificationToken" />'
    assert _extract_csrf(html) == "xyz789"


def test_extract_csrf_missing_returns_none():
    assert _extract_csrf("<html><body>nothing here</body></html>") is None
    assert _extract_csrf("") is None


def test_classify_submit_response_pdf_form_is_booked():
    ops = LiveEngineOps
    html = '<form name="frm_1" action="MostrarPdf"></form>'
    assert ops._classify_submit_response(html) == "booked"


def test_classify_submit_response_comprovativo_without_bounce_is_booked():
    html = "<html>comprovativo do pedido</html>"
    assert LiveEngineOps._classify_submit_response(html) == "booked"


# storm-auto's apply_book distinguishes the two bounce flavours by WHERE the portal put us, and
# both are gated behind the same bounce keywords. Read verbatim from the code behind 113 real
# bookings -- an earlier version of this file had these exactly backwards.
_HOME_DASHBOARD = ('<html><body><a href="Questionario.jsp">Solicitar Pedido de Visto</a>'
                   '<a href="c.jsp">Consultar Pedidos</a></body></html>')


def test_bounce_to_the_questionario_schedule_form_is_no_slot():
    """Flavour (a): back on the schedule/questionario FORM = the slot was taken or the submit was
    rejected. The SESSION IS FINE -- soft-release and try a different slot. Classifying this as
    form_stale (as this file previously did) is what made worker e5dfc1b1 cling to one slot and pay
    five full login+warmup cycles on it."""
    html = "<html>questionario comprovativo <select id='cmbPeriodo'></select></html>"
    assert LiveEngineOps._classify_submit_response(html) == "no_slot"


def test_bounce_all_the_way_to_the_home_dashboard_is_form_stale():
    """Flavour (b): out to the logged-in HOME dashboard = our booking form-state expired. The slot
    is NOT gone, our context is. Identified positively -- both nav strings present AND every
    schedule marker absent -- never by a single keyword."""
    assert LiveEngineOps._classify_submit_response(_HOME_DASHBOARD) == "form_stale"


def test_home_markers_with_a_schedule_marker_present_is_not_form_stale():
    """The absence of schedule markers is load-bearing: a page carrying both is the schedule page,
    not the dashboard, and re-warming for it would be the wrong recovery."""
    html = _HOME_DASHBOARD.replace("</body>", "getSlots</body>")
    assert LiveEngineOps._classify_submit_response(html) == "no_slot"


def test_a_response_with_no_confirmation_and_no_bounce_marker_is_an_error():
    """apply_book returns "error" here, not a bounce -- it is an unexpected response, and calling it
    a bounce would silently blame the slot for something unexplained."""
    assert LiveEngineOps._classify_submit_response("<html>something else</html>") == "error"


def test_classify_submit_response_pdf_form_wins_over_stray_bounce_keyword():
    # The MostrarPdf form is the deterministic signal -- a stray bounce/error keyword ELSEWHERE on
    # the confirmation page must not override it (the mirror of the false-APPLIED bug).
    html = '<form name="frm_1" action="MostrarPdf"></form><script>authentication.jsp analytics</script>'
    assert LiveEngineOps._classify_submit_response(html) == "booked"


def test_classify_submit_response_plain_200_no_markers_is_not_booked():
    # Never "booked" -- and per apply_book it is "error" (unexplained), not a bounce.
    html = "<html><body>some unrelated page</body></html>"
    assert LiveEngineOps._classify_submit_response(html) == "error"


# ── _is_login_success (regression: browser_login always returns HTTP 200 for every
# non-catastrophic outcome -- the real verdict is JSON-encoded in `body`. Checking status alone
# made every login attempt register as successful, including wrong credentials and a
# DataDome-blocked proxy.) ──────────────────────────────────────────────────────────────────────
def test_login_success_type_success():
    assert LiveEngineOps._is_login_success({"status": 200, "body": '{"type":"success"}'}) is True


def test_login_success_empty_type_treated_as_success():
    # storm-auto's proven classification treats an absent/empty "type" as success too.
    assert LiveEngineOps._is_login_success({"status": 200, "body": '{"description":"ok"}'}) is True


def test_login_rejected_wrong_credentials():
    body = '{"type":"error","description":"login_failed_auth_redirect"}'
    assert LiveEngineOps._is_login_success({"status": 200, "body": body}) is False


def test_login_rejected_datadome_challenge():
    body = '{"type":"error","description":"datadome_challenge_on_post"}'
    assert LiveEngineOps._is_login_success({"status": 200, "body": body}) is False


def test_login_rejected_non_200_status():
    assert LiveEngineOps._is_login_success({"status": 0, "body": ""}) is False
    assert LiveEngineOps._is_login_success({"status": 503, "body": '{"type":"success"}'}) is False


def test_login_rejected_empty_body_despite_200():
    assert LiveEngineOps._is_login_success({"status": 200, "body": ""}) is False
    assert LiveEngineOps._is_login_success({"status": 200, "body": None}) is False


def test_login_rejected_html_body_despite_200():
    # A DataDome/WAF interstitial can return an HTML page with status 200 -- never a success.
    assert LiveEngineOps._is_login_success(
        {"status": 200, "body": "<html>waiting room</html>"}
    ) is False


def test_login_rejected_non_json_non_html_body():
    assert LiveEngineOps._is_login_success({"status": 200, "body": "not json at all"}) is False


def test_login_rejected_malformed_result_shape():
    assert LiveEngineOps._is_login_success(None) is False
    assert LiveEngineOps._is_login_success("unexpected string") is False
    assert LiveEngineOps._is_login_success({}) is False


# ── keepalive during holds (real reason "holding" exists: keep the portal session alive; a
# side effect that matters just as much in production is that it refreshes last_seen, so the Hub's
# stall-reaper stops mistaking a healthy multi-hour hold for a dead worker) ─────────────────────
class _FakeSignals:
    """wait_slot_open/wait_assignment return TIMEOUT/"timeout" `timeouts` times, then `final`.
    `stop_on_final=True` also sets `self.stopped`, matching real BusSignals' contract that `stopped`
    is only ever set by an actual stop/shutdown command -- NOT by a real assign command whose slot
    happens to be None (the self-scout fallback signal), which must stay indistinguishable-from-stop
    only when the caller explicitly asks for that scenario."""
    def __init__(self, timeouts, final, stop_on_final=False):
        self._left = timeouts
        self._final = final
        self._stop_on_final = stop_on_final
        self.stopped = False
        self.slot_found_seen = False
        self.calls = 0

    def wait_slot_open(self, timeout=900):
        self.calls += 1
        if self._left > 0:
            self._left -= 1
            return "timeout"
        if self._stop_on_final:
            self.stopped = True
        return self._final

    def wait_assignment(self, timeout=900):
        from apps.worker.bus_signals import TIMEOUT
        self.calls += 1
        if self._left > 0:
            self._left -= 1
            return TIMEOUT
        if self._stop_on_final:
            self.stopped = True
        return self._final


def test_await_slot_open_survives_multiple_timeout_windows_and_pings():
    """Regression (D2, corrected): the wait must never give up on its own, AND must do something
    real while it waits -- a bare heartbeat isn't the point, keeping the server-side session alive
    is. Each timeout window must trigger a real keepalive probe."""
    ops = _make_ops(signals=_FakeSignals(timeouts=3, final="slot_open"))
    ops.session = _FakeSession()
    assert ops.await_slot_open() == "slot_open"
    assert ops._signals.calls == 4          # 3 timeouts + the final real result
    assert ops._keepalive_dead_streak == 0  # every probe succeeded (_FakeSession never raises)


def test_await_slot_open_stop_returns_immediately():
    ops = _make_ops(signals=_FakeSignals(timeouts=0, final="stop", stop_on_final=True))
    ops.session = _FakeSession()
    assert ops.await_slot_open() == "stop"


def test_await_assignment_survives_multiple_timeout_windows():
    ops = _make_ops(signals=_FakeSignals(timeouts=2, final={"date": "d", "period": 1}))
    ops.session = _FakeSession()
    assert ops.await_assignment() == {"date": "d", "period": 1}
    assert ops._signals.calls == 3


def test_await_assignment_null_slot_not_swallowed_as_timeout():
    """Regression (N1): the self-scout fallback signal is `assign slot=None, scouts_invalid=True` --
    a REAL command whose slot happens to be None, not a timeout. Before the TIMEOUT sentinel, this
    was indistinguishable from "still waiting" and silently discarded, making the scouts_invalid()
    fallback path in PhaseMachine unreachable."""
    ops = _make_ops(signals=_FakeSignals(timeouts=0, final=None))
    ops.session = _FakeSession()
    assert ops.await_assignment() is None
    assert ops._signals.calls == 1   # returned on the FIRST call -- not looped as if it were a timeout


def test_is_stopped_reflects_signals_stopped_flag():
    ops = _make_ops(signals=_FakeSignals(timeouts=0, final="stop", stop_on_final=True))
    assert ops.is_stopped() is False
    ops.await_slot_open()
    assert ops.is_stopped() is True


def test_keepalive_off_skips_probe_entirely():
    ops = _make_ops(cfg=_cfg(KEEPALIVE_OFF=True), signals=_FakeSignals(timeouts=1, final="slot_open"))

    class _ExplodingSession(_FakeSession):
        def browser_nav(self, url, timeout=30):
            raise AssertionError("keepalive probe must not fire when KEEPALIVE_OFF is set")

    ops.session = _ExplodingSession()
    assert ops.await_slot_open() == "slot_open"


def test_keepalive_dead_trip_after_slot_found_triggers_restore_attempt():
    """After KEEPALIVE_DEAD_TRIP consecutive failed probes AND a slot has actually been found, a
    restore must be attempted rather than silently continuing to wait on a dead session."""
    ops = _make_ops(cfg=_cfg(KEEPALIVE_DEAD_TRIP=2), signals=_FakeSignals(timeouts=2, final="slot_open"))
    ops._signals.slot_found_seen = True

    class _DyingSession(_FakeSession):
        def browser_nav(self, url, timeout=30):
            raise RuntimeError("net::ERR_TUNNEL_CONNECTION_FAILED")

    ops.session = _DyingSession()
    restore_calls = []
    ops._attempt_keepalive_restore = lambda context="": restore_calls.append(context)
    ops.await_slot_open()
    assert restore_calls == ["await_slot_open"]


def test_keepalive_no_restore_skips_restore_on_dead_trip():
    ops = _make_ops(cfg=_cfg(KEEPALIVE_DEAD_TRIP=1, KEEPALIVE_NO_RESTORE=True),
                    signals=_FakeSignals(timeouts=1, final="slot_open"))
    ops._signals.slot_found_seen = True

    class _DyingSession(_FakeSession):
        def browser_nav(self, url, timeout=30):
            raise RuntimeError("dead")

    ops.session = _DyingSession()
    restore_calls = []
    ops._attempt_keepalive_restore = lambda context="": restore_calls.append(context)
    ops.await_slot_open()
    assert restore_calls == []


def test_keepalive_loose_hold_preserves_session_before_slot_found():
    """Regression (N2): storm-auto's confirmed anti-attrition rule -- before a slot has actually
    been found there is nothing to book yet, so a proxy/session blip must NEVER burn a re-login.
    This was previously unconditional (any dead-trip triggered a restore regardless of slot_found)."""
    ops = _make_ops(cfg=_cfg(KEEPALIVE_DEAD_TRIP=1), signals=_FakeSignals(timeouts=1, final="slot_open"))
    assert ops._signals.slot_found_seen is False   # no slot found yet

    class _DyingSession(_FakeSession):
        def browser_nav(self, url, timeout=30):
            raise RuntimeError("net::ERR_TUNNEL_CONNECTION_FAILED")

    ops.session = _DyingSession()
    restore_calls = []
    ops._attempt_keepalive_restore = lambda context="": restore_calls.append(context)
    ops.await_slot_open()
    assert restore_calls == []   # preserved, not restored -- the session object is untouched
    assert ops.session is not None


def test_keepalive_restore_attempts_are_bounded():
    """Regression (N3): a worker whose restore keeps failing must not go silent forever (the
    original behavior: `session is None` short-circuited every future keepalive call, so the worker
    just sat there holding its proxy lease until the 25-minute stall reaper eventually killed it).
    It must also not retry unboundedly -- bounded by _MAX_KEEPALIVE_RESTORE_ATTEMPTS."""
    from apps.worker.ops_live import _MAX_KEEPALIVE_RESTORE_ATTEMPTS

    ops = _make_ops(signals=_FakeSignals(timeouts=_MAX_KEEPALIVE_RESTORE_ATTEMPTS + 2, final="slot_open"))
    ops.session = None   # already in the degraded "restore failed" state
    ops._try_restore_from_checkpoint = lambda: False
    attempts = []

    def _failing_login():
        attempts.append(1)
        return False

    ops._fresh_login = _failing_login
    ops.await_slot_open()
    assert len(attempts) == _MAX_KEEPALIVE_RESTORE_ATTEMPTS   # retried, but capped -- never disabled
                                                               # forever, never unbounded either


def test_fresh_login_resets_posto_pdf():
    """Regression (N4): a brand-new session has zero server-side form state. If posto_pdf survives
    from a previous session, the keepalive-restore chain's "only re-warm if posto_pdf is None" check
    incorrectly skips re-warming after a fresh login."""
    import apps.worker.ops_live as ops_live_mod

    ops = _make_ops()
    ops.posto_pdf = "5084"   # stale, from a previous (now-dead) session

    class _StubSession(_FakeSession):
        def browser_login(self, *a, **kw):
            return {"status": 200, "body": '{"type":"success"}'}

    import unittest.mock
    with unittest.mock.patch.object(ops_live_mod, "get_session", return_value=_StubSession()):
        assert ops._fresh_login() is True
    assert ops.posto_pdf is None


# ── do_discover: shared gettime cache ────────────────────────────────────────
class _RecordingSession(_FakeSession):
    """Records every nav'd URL and returns a canned body per-endpoint (gettime vs getPeriodosOcupados)
    so do_discover's cache-hit/cache-miss branches can be told apart by what actually got fetched."""
    GETTIME_BODY = (
        "var x=1; SPECIAL_DAYS[2026]=new Array(); SPECIAL_DAYS[2026][8] = new Array('15');"
    )
    PERIODOS_BODY = "<html>periodos_ocupados='1267891011121314'</html>"   # -> free=[3,4,5]

    def __init__(self):
        super().__init__()
        self.nav_urls: list[str] = []
        self._last_url = ""

    def browser_nav(self, url, timeout=30):
        self.nav_urls.append(url)
        self._last_url = url
        return url

    def browser_eval(self, js):
        if "location.origin" in (js or ""): return _origin_reply(js)
        if "gettime" in self._last_url:
            return self.GETTIME_BODY
        return self.PERIODOS_BODY


def test_do_discover_fetches_fresh_and_emits_gettime_result_when_nothing_cached():
    events = []
    ops = _make_ops(emit=lambda t, **p: events.append((t, p)))
    ops.session = _RecordingSession()

    out = ops.do_discover(["2026-08-15", "2026-08-16"])

    assert any("gettime" in u for u in ops.session.nav_urls)   # fetched fresh
    outcomes = {o["date"]: o["outcome"] for o in out}
    assert outcomes["2026-08-15"] == "empty"   # SPECIAL_DAYS[2026][8] includes 15
    assert outcomes["2026-08-16"] == "found"
    gettime_events = [p for t, p in events if t == "gettime_result"]
    assert len(gettime_events) == 1
    assert gettime_events[0]["special_days"] == {"2026": {"8": [15]}}


def test_do_discover_uses_cached_special_days_and_skips_gettime_fetch():
    events = []
    ops = _make_ops(emit=lambda t, **p: events.append((t, p)))
    ops._pending_special_days = {"2026": {"8": [15]}}   # as it arrives over the bus (json-decoded)
    ops.session = _RecordingSession()

    out = ops.do_discover(["2026-08-15", "2026-08-16"])

    assert not any("gettime" in u for u in ops.session.nav_urls)   # cache hit -- no gettime fetch
    assert ops._pending_special_days is None   # consumed
    outcomes = {o["date"]: o["outcome"] for o in out}
    assert outcomes["2026-08-15"] == "empty"
    assert outcomes["2026-08-16"] == "found"
    assert [t for t, _p in events if t == "gettime_result"] == []   # cache hit never re-reports


class _FlakySession(_RecordingSession):
    """Fails browser_nav on the periodos endpoint the first `fail_times` calls, then succeeds --
    tests do_discover's connection-level nav retry (DISCOVERY_NAV_RETRIES/_BACKOFF). Never fails the
    gettime nav, so the retry path under test is isolated to the per-date loop."""
    def __init__(self, fail_times):
        super().__init__()
        self.fail_times = fail_times
        self.periodos_attempts = 0

    def browser_nav(self, url, timeout=30):
        if "gettime" not in url:
            self.periodos_attempts += 1
            if self.periodos_attempts <= self.fail_times:
                raise RuntimeError("connection reset")
        return super().browser_nav(url, timeout=timeout)


def test_do_discover_retries_a_connection_level_nav_failure_and_recovers(monkeypatch):
    monkeypatch.setattr("apps.worker.ops_live.time.sleep", lambda _s: None)
    ops = _make_ops(cfg=_cfg(DISCOVERY_NAV_RETRIES=2, DISCOVERY_NAV_BACKOFF=0.01))
    ops.session = _FlakySession(fail_times=1)   # fails once, succeeds on the retry

    out = ops.do_discover(["2026-08-16"])

    assert ops.session.periodos_attempts == 2   # 1 failure + 1 successful retry
    assert out == [{"date": "2026-08-16", "outcome": "found", "periods": [3, 4, 5]}]


def test_do_discover_exhausts_retries_then_marks_the_date_failed(monkeypatch):
    monkeypatch.setattr("apps.worker.ops_live.time.sleep", lambda _s: None)
    ops = _make_ops(cfg=_cfg(DISCOVERY_NAV_RETRIES=2, DISCOVERY_NAV_BACKOFF=0.01))
    ops.session = _FlakySession(fail_times=99)   # never recovers

    out = ops.do_discover(["2026-08-16"])

    assert ops.session.periodos_attempts == 3   # initial attempt + 2 retries, then give up
    assert out == [{"date": "2026-08-16", "outcome": "failed"}]


def test_do_discover_default_is_a_single_attempt_no_retry():
    """DISCOVERY_NAV_RETRIES defaults to 0 -- byte-identical to the pre-fix behavior when unset."""
    ops = _make_ops()   # default cfg, no retry knobs set
    ops.session = _FlakySession(fail_times=1)

    out = ops.do_discover(["2026-08-16"])

    assert ops.session.periodos_attempts == 1   # no retry attempted
    assert out == [{"date": "2026-08-16", "outcome": "failed"}]


# ── login-rejection log noise (confirmed live 2026-08-14) ─────────────────────
def test_login_reject_reason_strips_the_captcha_token():
    """browser_login returns the solver's captcha_token alongside the response, and the whole dict
    used to be logged verbatim -- hundreds of base64 chars per attempt, once per retry, burying the
    actual reason and aging real history out of the capped Redis event stream."""
    from apps.worker.ops_live import _login_reject_reason

    result = {
        "status": 200,
        "body": '{"type":"error","description":"datadome_challenge_on_post"}',
        "captcha_token": "0cAFcWeA7vwgAPafAJ7GPimQvWGjJg-" + "x" * 900,
    }
    out = _login_reject_reason(result)
    assert "datadome_challenge_on_post" in out
    assert "status=200" in out
    assert "0cAFcWeA" not in out and len(out) < 120


def test_login_reject_reason_survives_a_non_json_body():
    from apps.worker.ops_live import _login_reject_reason

    out = _login_reject_reason({"status": 503, "body": "<html>waiting room</html>"})
    assert "status=503" in out and "waiting room" in out
    assert _login_reject_reason(None) == "None"


# ── transient vs sticky budget (confirmed live 2026-08-14) ────────────────────
def test_connection_errors_are_transient_credential_rejections_are_not():
    """A dead proxy says nothing about the account. These were NOT classified as transient, so
    proxy weather burned the sticky budget, exhausted sub-account rotation, failed the worker and
    consumed MAX_RESPAWN_ATTEMPTS -- six workers permanently FAILED without the portal ever
    rejecting them."""
    t = LiveEngineOps._is_transient_login_failure
    for infra in ("Page.goto: net::ERR_CONNECTION_CLOSED", "net::ERR_TUNNEL_CONNECTION_FAILED",
                  "status=200 datadome_challenge_on_post", "PlaywrightSession init timed out (>120s)",
                  "unexpected_redirect_chrome-error://chromewebdata/", "TypeError: Failed to fetch"):
        assert t(infra) is True, infra
    for verdict in ("status=200 login_failed_auth_redirect",
                    "unexpected_redirect_https://pedidodevistos.mne.gov.pt/VistosOnline/login"):
        assert t(verdict) is False, verdict


def test_transient_failures_do_not_burn_the_sticky_budget(monkeypatch):
    """The old loop was `for attempt in range(...)` where BOTH branches continue'd, so the transient
    classification advanced the counter identically and had no effect at all -- while logging
    "sticky count not burned"."""
    import apps.worker.ops_live as ops_live_mod

    calls = {"n": 0}

    class _FlakyThenGood(_FakeSession):
        def browser_login(self, *a, **k):
            calls["n"] += 1
            if calls["n"] <= 4:      # four pure-infrastructure failures
                raise RuntimeError("Page.goto: net::ERR_CONNECTION_CLOSED")
            return {"status": 200, "body": '{"type":"success"}'}

    monkeypatch.setattr(ops_live_mod, "get_session", lambda **kw: _FlakyThenGood())
    ops = _make_ops(cfg=_cfg(STICKY_RETRY_MAX=2, LOGIN_TRANSIENT_RETRY_MAX=6))
    ops._backoff = lambda n: None
    ops._request_proxy_rotation = lambda phase=None: None

    assert ops._sticky_login_attempts(ops.cfg.login.sticky_retry_max) is True
    assert calls["n"] == 5      # would have given up after 2 with the old loop


def test_credential_rejections_still_exhaust_the_sticky_budget(monkeypatch):
    """The flip side: a real portal verdict MUST burn the budget, or rotation never happens."""
    import apps.worker.ops_live as ops_live_mod

    calls = {"n": 0}

    class _AlwaysRejected(_FakeSession):
        def browser_login(self, *a, **k):
            calls["n"] += 1
            return {"status": 200, "body": '{"type":"error","description":"login_failed_auth_redirect"}'}

    monkeypatch.setattr(ops_live_mod, "get_session", lambda **kw: _AlwaysRejected())
    ops = _make_ops(cfg=_cfg(STICKY_RETRY_MAX=2, LOGIN_TRANSIENT_RETRY_MAX=6))
    ops._backoff = lambda n: None
    ops._request_proxy_rotation = lambda phase=None: None

    assert ops._sticky_login_attempts(ops.cfg.login.sticky_retry_max) is False
    assert calls["n"] == 2      # exactly the sticky budget, no more


def test_transient_budget_is_bounded(monkeypatch):
    """Not-charging must not mean retrying forever on a permanently dead pool."""
    import apps.worker.ops_live as ops_live_mod

    calls = {"n": 0}

    class _AlwaysDead(_FakeSession):
        def browser_login(self, *a, **k):
            calls["n"] += 1
            raise RuntimeError("Page.goto: net::ERR_CONNECTION_CLOSED")

    monkeypatch.setattr(ops_live_mod, "get_session", lambda **kw: _AlwaysDead())
    ops = _make_ops(cfg=_cfg(STICKY_RETRY_MAX=2, LOGIN_TRANSIENT_RETRY_MAX=3))
    ops._backoff = lambda n: None
    ops._request_proxy_rotation = lambda phase=None: None

    assert ops._sticky_login_attempts(ops.cfg.login.sticky_retry_max) is False
    assert calls["n"] == 5      # 3 transient (uncharged) + 2 sticky, then give up


# ── checkpoint restore: bounded, and self-cleaning (confirmed live 2026-08-14) ─
def _ckpt_ops(monkeypatch, session, **cfg_kw):
    import apps.worker.ops_live as ops_live_mod
    monkeypatch.setattr(ops_live_mod, "get_session", lambda **kw: session)
    monkeypatch.setattr(ops_live_mod.ckpt, "load_checkpoint",
                        lambda identity_id: {"cookies": {}, "cookies_full": [], "sub_account_id": "s"})
    cleared = []
    monkeypatch.setattr(ops_live_mod.ckpt, "clear_checkpoint", lambda iid: cleared.append(iid))
    ops = _make_ops(cfg=_cfg(RESTORE_STAMPEDE_JITTER=0, **cfg_kw), identity_id="id-1")
    ops._log = lambda t: None
    return ops, cleared


def test_a_dead_checkpoint_is_discarded_so_the_next_respawn_does_not_retry_it(monkeypatch):
    """The chain never deleted a checkpoint that had just proved itself dead, so every respawn
    reloaded the same cookies and paid the same minutes again -- CARLOS burned three consecutive
    restores, ERNESTO four, none producing a bookable session."""
    ops, cleared = _ckpt_ops(monkeypatch, _FakeSession(alive=False))
    ops._run_warmup_steps = lambda: False          # re-warm can't save it either

    assert ops._try_restore_from_checkpoint() is False
    assert cleared == ["id-1"]                     # cookies discarded
    assert ops.session is None


def test_a_rebuild_failure_keeps_the_checkpoint(monkeypatch):
    """A browser/proxy that never came up says nothing about the stored cookies -- keep them."""
    import apps.worker.ops_live as ops_live_mod

    monkeypatch.setattr(ops_live_mod.ckpt, "load_checkpoint",
                        lambda identity_id: {"cookies": {}, "cookies_full": []})
    cleared = []
    monkeypatch.setattr(ops_live_mod.ckpt, "clear_checkpoint", lambda iid: cleared.append(iid))
    def _boom(**kw):
        raise RuntimeError("Page.goto: net::ERR_CONNECTION_CLOSED")
    monkeypatch.setattr(ops_live_mod, "get_session", _boom)
    ops = _make_ops(cfg=_cfg(RESTORE_STAMPEDE_JITTER=0), identity_id="id-1")
    ops._log = lambda t: None

    assert ops._try_restore_from_checkpoint() is False
    assert cleared == []                           # kept for the next attempt


def test_restore_skips_the_rewarm_once_the_budget_is_spent(monkeypatch):
    """Every tier is a real navigation; the re-warm must not start if the chain has already spent
    its wall-clock budget -- that is what turned a doomed restore into 384 seconds."""
    import apps.worker.ops_live as ops_live_mod

    class _SlowProbe(_FakeSession):
        def browser_nav(self, url, timeout=30):
            ops_live_mod.time.sleep(0)             # patched clock does the work
            return "https://x/Authentication.jsp"  # -> not alive

    ops, cleared = _ckpt_ops(monkeypatch, _SlowProbe(alive=False), CHECKPOINT_RESTORE_BUDGET=5.0)
    rewarmed = []
    ops._run_warmup_steps = lambda: (rewarmed.append(1), True)[1]

    t = {"now": 1000.0}
    monkeypatch.setattr(ops_live_mod.time, "time", lambda: t["now"])
    real_sleep = ops_live_mod.time.sleep
    monkeypatch.setattr(ops_live_mod.time, "sleep", lambda s: None)
    # the probe "takes" 20s of wall clock, blowing the 5s budget
    orig_probe = ops._probe_alive
    def _probe():
        t["now"] += 20.0
        return orig_probe()
    ops._probe_alive = _probe

    assert ops._try_restore_from_checkpoint() is False
    assert rewarmed == []                          # re-warm never started
    assert cleared == ["id-1"]


def test_a_live_checkpoint_still_short_circuits_login(monkeypatch):
    """The whole point of the chain: a good checkpoint must still skip login+warmup entirely."""
    ops, cleared = _ckpt_ops(monkeypatch, _FakeSession(alive=True))
    assert ops._try_restore_from_checkpoint() is True
    assert ops._restore_stage == "alive"
    assert cleared == []


# ── the false-negative that cost a real appointment (confirmed live 2026-08-14) ───────────────
_PDF_SHELL = ('<!DOCTYPE html><html><head>\n    <link rel="stylesheet" '
              'href="chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai/pdf_embedder.css">\n'
              '  </head>\n  <body>\n    \n  \n\n')


def test_landing_on_mostrarpdf_is_unresolved_not_booked():
    """CHECKED AGAINST THE PORTAL 2026-08-14: worker e5dfc1b1 ended at final_url=.../MostrarPdf?
    with Chrome's pdf-viewer shell, and the account holds NO appointment. So reaching MostrarPdf is
    NOT a success signal -- it only means browser_form_post overshot the intermediate auto-submit
    page (it waits for navigation to settle; storm-auto reads that page before the JS fires), which
    makes the response unclassifiable from the body alone.

    Guessing "booked" from a URL would invent appointments: workers marked COMPLETED and
    BookedAppointment rows written for bookings that never happened. storm-auto carries the matching
    scar from the other direction (a 58KB HTML page once saved as a PDF -- josand5837), and its rule
    is strict: %PDF magic or nothing."""
    url = "https://pedidodevistos.mne.gov.pt/VistosOnline/MostrarPdf?"
    assert LiveEngineOps._classify_submit_response(_PDF_SHELL, url) == "verify_pdf"
    assert LiveEngineOps._classify_submit_response(_PDF_SHELL, "") == "verify_pdf"


def test_verify_pdf_is_never_itself_a_booking():
    """Drift guard: whatever else changes, `verify_pdf` must stay distinct from `booked` -- it means
    "go prove it", and only real %PDF bytes may promote it."""
    assert LiveEngineOps._classify_submit_response(_PDF_SHELL, "x/MostrarPdf") != "booked"


def test_storm_autos_original_signal_still_wins():
    """The intermediate auto-submit page is still the deterministic success signal."""
    assert LiveEngineOps._classify_submit_response(
        '<form name="frm_1" action="MostrarPdf"></form>', "https://x/SubmeterVistoCriaPDF") == "booked"


def test_a_bounce_is_still_a_bounce_when_the_url_is_not_the_pdf():
    """The URL check must not turn genuine bounces into phantom bookings."""
    home = "https://pedidodevistos.mne.gov.pt/VistosOnline/"
    # A bounce to the site root with NO schedule marker is the dashboard -> form_stale (see the
    # English-portal tests below). With a schedule marker it stays no_slot however it was reached.
    assert LiveEngineOps._classify_submit_response("<html>questionario</html>", home) == "form_stale"
    assert LiveEngineOps._classify_submit_response("<html>getSlots</html>", home) == "no_slot"
    assert LiveEngineOps._classify_submit_response(_HOME_DASHBOARD, home) == "form_stale"
    # ...and at a deeper URL, without the Portuguese markers, it is not the dashboard. It is now
    # form_stale rather than no_slot -- REFINED 2026-08-24: a questionnaire response with no
    # schedule markers is a submit that found no server-side anchor, not a taken slot. Either way
    # the point this test protects holds: it is never promoted to a booking.
    assert LiveEngineOps._classify_submit_response(
        "<html>questionario</html>", "https://x/VistosOnline/SubmeterVistoCriaPDF") == "form_stale"


# ── verify_pdf resolution: only real %PDF bytes may promote a bounce to a booking ─────────────
class _SubmitSession(_FakeSession):
    """Session whose SubmeterVistoCriaPDF POST lands on MostrarPdf showing the pdf-viewer shell --
    the exact 2026-08-14 response -- and whose in-page PDF fetch returns `pdf_bytes`."""
    def __init__(self, pdf_bytes: bytes):
        super().__init__()
        self._pdf = pdf_bytes

    def browser_form_post(self, url, data, timeout=100):
        return ("https://pedidodevistos.mne.gov.pt/VistosOnline/MostrarPdf?", _PDF_SHELL)

    def browser_eval(self, js, timeout=30):
        if "location.origin" in (js or ""): return _origin_reply(js)
        import base64
        return base64.b64encode(self._pdf).decode()


def _apply_ops(pdf_bytes: bytes, events: list):
    ops = _make_ops(emit=lambda t, **p: events.append((t, p)))
    ops.session = _SubmitSession(pdf_bytes)
    ops._run_warmup_steps = lambda: True
    ops._save_pdf = lambda b: "/tmp/x.pdf"
    return ops


def test_overshoot_promotes_to_booked_only_with_real_pdf_bytes():
    events = []
    ops = _apply_ops(b"%PDF-1.4\nreal confirmation document", events)
    assert ops.do_apply({"date": "2026-09-01", "period": 4}) == "booked"
    assert any(t == "booked_pdf" for t, _ in events)


def test_overshoot_with_no_pdf_is_not_a_booking():
    """The confirmed 2026-08-14 case: MostrarPdf reached, nothing rendered, no appointment. This
    must NOT report booked -- inventing a booking is strictly worse than missing one, because the
    worker completes, the run stops, and a BookedAppointment row is written for nothing."""
    events = []
    ops = _apply_ops(b"", events)
    assert ops.do_apply({"date": "2026-09-01", "period": 4}) != "booked"
    assert not any(t == "booked_pdf" for t, _ in events)


def test_overshoot_with_an_html_page_instead_of_a_pdf_is_not_a_booking():
    """storm-auto's josand5837 scar, in the new path: a large HTML page is not a PDF."""
    events = []
    ops = _apply_ops(b"<!DOCTYPE html><html>" + b"x" * 60000, events)
    assert ops.do_apply({"date": "2026-09-01", "period": 4}) != "booked"
    assert not any(t == "booked_pdf" for t, _ in events)


# ── the submit response must be a portal answer at all (storm-auto guards this before classifying,
# then falls back to a primp POST; we have no primp path, so we must at least not misread it) ────
class _SubmitBodySession(_FakeSession):
    def __init__(self, body, url="https://x/VistosOnline/"):
        super().__init__()
        self._body, self._url = body, url

    def browser_form_post(self, url, data, timeout=100):
        return (self._url, self._body)


def _ops_with_submit(body, url="https://x/VistosOnline/"):
    ops = _make_ops(emit=lambda t, **p: None)
    ops.session = _SubmitBodySession(body, url)
    ops._run_warmup_steps = lambda: True
    return ops


def test_blank_page_is_not_read_as_a_slot_rejection():
    """A blank page is not the portal answering. It returns "session", not "bounced": a bounce
    releases the slot WITH a penalty and feeds the Hub's false-slot verdict, and none of these
    responses are the slot's fault -- the request never reached the portal at all."""
    ops = _ops_with_submit("<html><head></head><body></body></html>")
    assert ops.do_apply({"date": "2026-09-01", "period": 4}) == "portal_unreachable"


def test_datadome_interstitial_is_not_read_as_a_slot_rejection():
    ops = _ops_with_submit("<html><head>datadome challenge</head><body>x</body></html>")
    assert ops.do_apply({"date": "2026-09-01", "period": 4}) == "portal_unreachable"


def test_release_slot_carries_the_penalty_tier():
    events = []
    ops = _make_ops(emit=lambda t, **p: events.append((t, p)))
    ops.release_slot({"date": "d", "period": 1})                    # default
    ops.release_slot({"date": "d", "period": 1}, "form_stale")
    assert [p["reason"] for _, p in events] == ["bounce", "form_stale"]


# ── the home-dashboard test must survive an English-rendered portal (confirmed live 2026-08-14) ──
_ROOT = "https://pedidodevistos.mne.gov.pt/VistosOnline/"
_SUBMIT_URL = "https://pedidodevistos.mne.gov.pt/VistosOnline/SubmeterVistoCriaPDF?posto_id=3018"


def test_home_dashboard_detected_by_url_when_the_portal_renders_in_english():
    """storm-auto matches only the PORTUGUESE nav text. We send lang=ENG in both the warmup and the
    booking payload, so those strings can be absent from an English dashboard and a genuine
    form-state expiry gets mis-read as "the slot was taken" -- no re-warm, and the slot pool wrongly
    blamed for our own lapsed session. Live: 20 of 24 bounces landed on the bare /VistosOnline/
    root, and only 6 matched the Portuguese markers.

    A page the portal served AT its own site root IS the dashboard, whatever language it rendered."""
    assert LiveEngineOps._classify_submit_response(
        "<html>questionario Request Visa Application</html>", _ROOT) == "form_stale"


def test_portuguese_markers_still_detect_home_without_any_url():
    assert LiveEngineOps._classify_submit_response(
        "<html>questionario Solicitar Pedido de Visto Consultar Pedidos</html>", "") == "form_stale"


def test_schedule_markers_veto_the_home_verdict_even_at_the_root_url():
    """The veto is absolute: a page carrying cmbPeriodo/getSlots is the schedule form, not the
    dashboard, and stays no_slot however it was reached. Without this the URL check would sweep up
    genuine slot-taken bounces and stop them ever being retried against another slot."""
    assert LiveEngineOps._classify_submit_response(
        "<html>questionario cmbPeriodo</html>", _ROOT) == "no_slot"
    assert LiveEngineOps._classify_submit_response(
        "<html>questionario getSlots</html>", _ROOT) == "no_slot"


def test_a_deeper_url_is_not_the_home_dashboard():
    """Uses a SCHEDULE-page bounce, not a questionnaire one, so the home-dashboard check is what is
    actually under test. (A questionnaire bounce now routes to form_stale via its own rule, which
    would mask what this asserts.)"""
    assert LiveEngineOps._classify_submit_response(
        "<html>getSlots cmbPeriodo</html>", _SUBMIT_URL) == "no_slot"


def test_bounce_reason_reports_both_halves_of_the_home_test():
    """With nothing logged there was no way to tell why 14 bounces at the home URL weren't detected
    as home. Every bounce now spells out which markers were and weren't found."""
    r = _submit_bounce_reason("<html><title>Portal</title>questionario cmbPeriodo</html>")
    assert "home_markers=none" in r and "cmbperiodo" in r and "title=" in r


# ── the warmup must PROVE it reached the scheduling page (root cause, 2026-08-14) ─────────────
class _WarmupSession(_FakeSession):
    """Runs the whole warmup chain successfully, and serves `sched_html` for Schedule.jsp."""
    def __init__(self, sched_html):
        super().__init__()
        self._sched_html = sched_html
        self.navs = []

    def browser_eval(self, js, timeout=30):
        if "location.origin" in (js or ""): return _origin_reply(js)
        if "outerHTML" in js:
            return self._sched_html
        # last quest step must yield fields, and the rest just need a 200
        return {"status": 200, "fields": {"a": "1"}}

    def browser_nav(self, url, timeout=30, **kw):
        self.navs.append(url)
        return url

    def browser_form_post(self, url, data, timeout=100):
        return (url, '<input name="__RequestVerificationToken" value="tok">')

    def browser_fetch(self, *a, **k):
        return {"status": 200}


def _warmup_ops(sched_html):
    ops = _make_ops()
    ops.session = _WarmupSession(sched_html)
    return ops


_SCHED_OK = ('<html><title>Schedule</title><form action="SubmeterVistoCriaPDF?posto_id=3018">'
             '<select id="cmbPeriodo"></select></form></html>')


def test_warmup_fails_when_schedule_jsp_has_no_scheduling_markers():
    """THE root cause of "applying doesn't work". storm-auto: "ScheduleController POST can return
    200 yet the server silently fails to set up form state, causing Schedule.jsp to redirect to
    authenticated_index ... must re-warm rather than returning a false 'warmed'."

    We fired ScheduleController and navigated Schedule.jsp and DISCARDED BOTH RESULTS, then returned
    True unconditionally -- so a rejected warmup reported success, do_apply POSTed with no
    server-side anchor, and the portal answered with the questionnaire page. Live: every such bounce
    carried title='questionnaire to apply for a visa' at 72113B."""
    ops = _warmup_ops("<html><title>questionnaire to apply for a visa</title>no markers</html>")
    assert ops._run_warmup_steps() is False


def test_warmup_succeeds_when_the_scheduling_page_is_really_there():
    ops = _warmup_ops(_SCHED_OK)
    assert ops._run_warmup_steps() is True


def test_warmup_reads_posto_pdf_from_the_schedule_page():
    """posto_pdf can legitimately differ from posto_id, and the submit URL must use the one the
    schedule page itself advertises -- this was hardcoded to posto_id and never read."""
    ops = _warmup_ops(_SCHED_OK.replace("posto_id=3018", "posto_id=9999"))
    assert ops._run_warmup_steps() is True
    assert ops.posto_pdf == "9999"


def test_warmup_falls_back_to_posto_id_when_the_page_advertises_none():
    ops = _warmup_ops('<html><select id="cmbPeriodo"></select>getSlots</html>')
    assert ops._run_warmup_steps() is True
    assert ops.posto_pdf == "5084"      # _make_ops' posto_id


# ── questionnaire step retry, ported from storm-auto (2026-08-14) ─────────────────────────────
class _FlakyQuestSession(_WarmupSession):
    """First `fail_n` quest-step calls come back non-200, then they succeed."""
    def __init__(self, sched_html, fail_n):
        super().__init__(sched_html)
        self.fail_n = fail_n
        self.quest_calls = 0

    def browser_eval(self, js, timeout=30):
        if "location.origin" in (js or ""): return _origin_reply(js)
        if "outerHTML" in js:
            return self._sched_html
        self.quest_calls += 1
        if self.quest_calls <= self.fail_n:
            return {"status": 500}
        return {"status": 200, "fields": {"a": "1"}}


def test_a_flaky_questionnaire_step_is_retried_not_silently_carried_past():
    """storm-auto retries a step that doesn't return 200 -- "like a manual page refresh" -- up to
    QUEST_STEP_RETRIES. We logged the non-200 and carried on regardless, so one flaky step silently
    poisoned the chain and surfaced later as the far vaguer quest_state_stale (6 of them in one live
    run, with zero non-200s logged, because by then the evidence was gone)."""
    ops = _make_ops()
    ops.session = _FlakyQuestSession(_SCHED_OK, fail_n=2)
    assert ops._run_warmup_steps() is True
    assert ops.session.quest_calls > 2      # it retried rather than accepting the 500


def test_quest_steps_give_up_after_the_retry_budget():
    ops = _make_ops()
    ops.session = _FlakyQuestSession(_SCHED_OK, fail_n=999)
    assert ops._run_warmup_steps() is False   # never yields fields -> quest_state_stale


class _RaisingQuestSession(_WarmupSession):
    """Quest-step calls RAISE (a dead in-page fetch) `fail_n` times, then succeed."""
    def __init__(self, sched_html, fail_n):
        super().__init__(sched_html)
        self.fail_n = fail_n
        self.quest_calls = 0

    def browser_eval(self, js, timeout=30):
        if "location.origin" in (js or ""): return _origin_reply(js)
        if "outerHTML" in js:
            return self._sched_html
        if "lang=ENG&lang=PT" in js:
            return 200                      # the home-lang POST succeeds
        self.quest_calls += 1
        if self.quest_calls <= self.fail_n:
            raise RuntimeError("Page.evaluate: TypeError: Failed to fetch")
        return {"status": 200, "fields": {"a": "1"}, "len": 10, "snippet": "x"}


def test_a_transient_in_page_fetch_failure_is_retried_not_fatal(monkeypatch):
    """storm-auto retries the EXCEPTION path of a quest step, not just a non-200 result. Ours only
    retried the result, so a transient "TypeError: Failed to fetch" escaped to the outer handler and
    killed the entire warmup on the first blip -- 306 aborted warmups in one live run."""
    monkeypatch.setattr("apps.worker.ops_live.time.sleep", lambda s: None)
    ops = _make_ops()
    ops.session = _RaisingQuestSession(_SCHED_OK, fail_n=2)
    assert ops._run_warmup_steps() is True
    assert ops.session.quest_calls > 2


def test_persistent_in_page_fetch_failure_still_fails_the_warmup(monkeypatch):
    """The retry must not paper over a genuinely dead session."""
    monkeypatch.setattr("apps.worker.ops_live.time.sleep", lambda s: None)
    ops = _make_ops()
    ops.session = _RaisingQuestSession(_SCHED_OK, fail_n=999)
    assert ops._run_warmup_steps() is False


class _DeadHomeSession(_WarmupSession):
    def browser_eval(self, js, timeout=30):
        if "location.origin" in (js or ""): return _origin_reply(js)
        if "lang=ENG&lang=PT" in js:
            raise RuntimeError("Page.evaluate: TypeError: Failed to fetch")
        return super().browser_eval(js, timeout)


def test_the_home_lang_post_failure_names_itself(monkeypatch):
    """"TypeError: Failed to fetch" names no step -- 306 of them in one run with no way to tell
    which fetch died."""
    logged = []
    ops = _make_ops()
    ops._log = logged.append          # _log defaults to a no-op; the worker wires it at launch
    ops.session = _DeadHomeSession(_SCHED_OK)
    assert ops._run_warmup_steps() is False
    assert any("home-lang POST failed" in m for m in logged), logged


class _FlakyHomeSession(_WarmupSession):
    """The home-lang POST fails `fail_n` times, then succeeds."""
    def __init__(self, sched_html, fail_n):
        super().__init__(sched_html)
        self.fail_n = fail_n
        self.home_calls = 0

    def browser_eval(self, js, timeout=30):
        if "location.origin" in (js or ""): return _origin_reply(js)
        if "lang=ENG&lang=PT" in js:
            self.home_calls += 1
            if self.home_calls <= self.fail_n:
                raise RuntimeError("Page.evaluate: TypeError: Failed to fetch")
            return 200
        return super().browser_eval(js, timeout)


def test_the_home_lang_post_is_retried_like_every_other_in_page_fetch(monkeypatch):
    """When the labelling went in, this one raised on the FIRST failure while the quest steps below
    it got three attempts -- and it is by far the most common failure point: 377 aborted warmups in
    one live run, 370 from just two workers, none of which ever reached ScheduleController."""
    monkeypatch.setattr("apps.worker.ops_live.time.sleep", lambda s: None)
    ops = _make_ops()
    ops.session = _FlakyHomeSession(_SCHED_OK, fail_n=2)
    assert ops._run_warmup_steps() is True
    assert ops.session.home_calls == 3


def test_the_home_lang_post_still_fails_the_warmup_when_it_never_recovers(monkeypatch):
    monkeypatch.setattr("apps.worker.ops_live.time.sleep", lambda s: None)
    ops = _make_ops()
    ops.session = _FlakyHomeSession(_SCHED_OK, fail_n=99)
    assert ops._run_warmup_steps() is False


# ── Chrome's network-error page is not a portal answer (confirmed live 2026-08-16) ────────────
_CHROME_ERROR = ('<!DOCTYPE html><html dir="ltr" lang="en"><head>\n  <meta charset="utf-8">\n'
                 '  <meta name="color-scheme" content="light dark">\n'
                 '  <meta name="theme-color" content="#fff">\n</head><body>'
                 + "x" * 180000 + '</body></html>')


class _ChromeErrorSession(_FakeSession):
    def __init__(self, url="chrome-error://chromewebdata/"):
        super().__init__()
        self._url = url

    def browser_form_post(self, url, data, timeout=100):
        return (self._url, _CHROME_ERROR)


def test_a_chrome_network_error_page_is_a_session_failure_not_a_slot_bounce():
    """Every one of the run's 7 submits came back as the SAME 187,732-byte chrome-error page, and
    the login path logged 96 rejections at chrome-error://chromewebdata/ in the same window. It is
    far too big to look blank and says nothing about DataDome, so it matched neither existing guard
    and fell through as an unexplained "[error]" -- releasing the slot as BOUNCED. The request never
    reached the portal; blaming the slot for our dead tunnel is the same misattribution that had a
    dead proxy manufacturing phantom-slot votes."""
    ops = _make_ops(emit=lambda t, **p: None)
    ops.session = _ChromeErrorSession()
    ops._run_warmup_steps = lambda: True
    assert ops.do_apply({"date": "2026-09-14", "period": 3}) == "portal_unreachable"


def test_the_chrome_error_page_is_caught_even_without_the_chrome_error_url():
    """browser_form_post does not always surface the chrome-error:// URL -- the page's own signature
    has to be enough on its own."""
    ops = _make_ops(emit=lambda t, **p: None)
    ops.session = _ChromeErrorSession(url="https://pedidodevistos.mne.gov.pt/VistosOnline/")
    ops._run_warmup_steps = lambda: True
    assert ops.do_apply({"date": "2026-09-14", "period": 3}) == "portal_unreachable"


def test_a_real_portal_bounce_is_still_a_real_bounce():
    """The new guard must not swallow genuine portal responses -- a schedule-form bounce still has
    to come back as no_slot so another slot gets tried."""
    ops = _make_ops(emit=lambda t, **p: None)
    ops.session = _SubmitBodySession("<html>questionario getSlots cmbPeriodo</html>",
                                     "https://pedidodevistos.mne.gov.pt/VistosOnline/")
    ops._run_warmup_steps = lambda: True
    assert ops.do_apply({"date": "2026-09-14", "period": 3}) == "no_slot"


# ── "swap after warmup" OFF must mean OFF (operator correction 2026-08-19) ────────────────────
def test_swap_on_moves_the_worker_to_the_hold_pool(monkeypatch):
    import apps.worker.ops_live as ops_live_mod

    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_WARMUP=True), hold_proxy="http://u:p@holdhost:9999")
    ops.session = _FakeSession()
    monkeypatch.setattr(ops_live_mod, "get_session", lambda **kw: _FakeSession(alive=True))
    assert ops.do_swap() == "holdhost:9999"
    assert ops._current_phase == "hold"


def test_the_old_setting_name_is_no_longer_accepted():
    """Phase 6: HOLD_SWAP_AFTER_FOUND named a trigger point the code never had, and was never a
    supported alias/compatibility mechanism -- it was removed outright, not reconciled with
    HOLD_SWAP_AFTER_WARMUP. A template that still names it must fail to resolve at all (the same
    UnknownSetting every other unrecognised key produces), not silently enable the swap."""
    from libs.domain.settings.resolver import SettingsError

    with pytest.raises(SettingsError):
        _cfg(HOLD_SWAP_AFTER_FOUND=True)


def test_hold_swap_after_warmup_is_the_sole_setting_that_enables_the_swap():
    cfg = _cfg(HOLD_SWAP_AFTER_WARMUP=True)
    assert cfg.hold_swap.swap_after_warmup is True


# ── keepalive must maintain READINESS, not just liveness (operator, 2026-08-19) ───────────────
class _KeepaliveSession(_FakeSession):
    """Records where the keepalive navigated, and serves `page` for the outerHTML read."""
    def __init__(self, page):
        super().__init__()
        self._page = page
        self.navs = []

    def browser_nav(self, url, timeout=30, **kw):
        self.navs.append(url)
        return url

    def browser_eval(self, js, timeout=30):
        if "location.origin" in (js or ""): return _origin_reply(js)
        return self._page if "outerHTML" in js else {"status": 200, "fields": {"a": "1"}}


def _keepalive_ops(page, warmed=True):
    ops = _make_ops(emit=lambda t, **p: None)
    ops.session = _KeepaliveSession(page)
    ops._log = lambda t: None
    ops.posto_pdf = "3018" if warmed else None
    return ops


def test_a_warmed_worker_probes_the_schedule_page_not_home():
    """Probing home keeps the AUTH session alive and nothing else, so a worker could hold for an
    hour, stay perfectly logged in, and still have a long-lapsed booking form-state -- its first
    gettime or submit then failed, and that failure was counted toward a re-login rather than the
    re-warm that fixes it."""
    ops = _keepalive_ops(_SCHED_OK)
    ops._do_keepalive("test")
    assert any("Schedule.jsp" in u for u in ops.session.navs), ops.session.navs


def test_a_lapsed_form_state_is_re_warmed_in_place():
    """No login: the auth session is fine, only the ScheduleController anchor is gone."""
    rewarmed = []
    ops = _keepalive_ops("<html><title>E-VISA</title>no scheduling markers here</html>")
    ops._run_warmup_steps = lambda: (rewarmed.append(1), True)[1]
    ops.do_login = lambda: pytest.fail("keepalive must never re-login")
    ops._do_keepalive("test")
    assert rewarmed, "form-state was lapsed and the keepalive did not re-warm"


def test_a_healthy_schedule_page_is_left_alone():
    """It must not re-warm on every ping -- that is a full quest replay each interval."""
    rewarmed = []
    ops = _keepalive_ops(_SCHED_OK)
    ops._run_warmup_steps = lambda: (rewarmed.append(1), True)[1]
    ops._do_keepalive("test")
    assert not rewarmed


def test_an_unwarmed_worker_still_probes_home():
    """Before warmup there is no form-state to hold, so home remains right."""
    ops = _keepalive_ops(_SCHED_OK, warmed=False)
    ops._do_keepalive("test")
    assert ops.session.navs and "Schedule.jsp" not in ops.session.navs[0]


# ── explicit session-state classifier (F21-F26) ────────────────────────────────────────────────
# The keepalive loop used to discard the very evidence it fetched: a logged-out home page returned
# HTTP 200 and was read as healthy (F23), a lapsed Schedule.jsp was always treated as a lapsed FORM
# even when the real cause was a dead SESSION (F24), a portal-busy page fell into that same bucket
# (F25), and no response had a name for "we don't actually know" (F26). classify_session_state is
# the fix: a pure function, tested here directly, with no session/browser fixture needed.
from apps.worker.ops_live import SessionState, classify_session_state  # noqa: E402

_HOME_AUTHENTICATED = ("<html><body>solicitar pedido de visto ... consultar pedidos ... "
                       "terminar sessão</body></html>")
_HOME_LOGGED_OUT = "<html><body>solicitar pedido de visto ... iniciar sessão</body></html>"
_SCHED_BUSY = ("<html><body>Não foi possível de momento concretizar o seu pedido, "
               "por favor tente mais tarde.</body></html>")


def test_classify_authenticated_home_page():
    assert classify_session_state(_HOME_AUTHENTICATED, "https://x/VistosOnline/",
                                  warmed=False) == SessionState.AUTHENTICATED


def test_classify_authenticated_schedule_page():
    assert classify_session_state(_SCHED_OK, "https://x/VistosOnline/Schedule.jsp",
                                  warmed=True) == SessionState.AUTHENTICATED


def test_classify_sessionlost_redirect_is_session_expired():
    """F22: sessionLost.jsp is recognised nowhere in production today -- this is the fix."""
    assert classify_session_state("<html>anything</html>",
                                  "https://x/VistosOnline/sessionLost.jsp",
                                  warmed=True) == SessionState.SESSION_EXPIRED
    assert classify_session_state("<html>anything</html>",
                                  "https://x/VistosOnline/sessionLost.jsp",
                                  warmed=False) == SessionState.SESSION_EXPIRED


def test_classify_authentication_jsp_redirect_is_session_expired():
    assert classify_session_state("<html>login form</html>",
                                  "https://x/VistosOnline/Authentication.jsp",
                                  warmed=True) == SessionState.SESSION_EXPIRED


def test_classify_portal_busy_page_is_portal_error_not_form_lapse():
    """F25: a busy portal must not be misread as a lapsed form."""
    assert classify_session_state(_SCHED_BUSY, "https://x/VistosOnline/Schedule.jsp",
                                  warmed=True) == SessionState.PORTAL_ERROR


def test_classify_unrecognised_warmed_page_is_unknown_not_expired():
    """F26: absence of the schedule markers is UNKNOWN, never silently promoted to SESSION_EXPIRED
    -- that verdict is reserved for actual expiry evidence (sessionLost/Authentication.jsp
    redirect, or the home page's own measured discriminator)."""
    assert classify_session_state("<html>no markers here</html>",
                                  "https://x/VistosOnline/Schedule.jsp",
                                  warmed=True) == SessionState.UNKNOWN


def test_classify_unrecognised_home_page_is_unknown():
    assert classify_session_state("<html>completely unrelated content</html>",
                                  "https://x/VistosOnline/",
                                  warmed=False) == SessionState.UNKNOWN


def test_classify_public_marker_alone_is_session_expired_not_authenticated():
    """The F23 regression test: this is the EXACT logged-out home signature (58,266B in
    production -- only the public menu item, never the auth-gated one). It must never classify
    AUTHENTICATED just because the navigation succeeded and returned a non-empty page."""
    assert classify_session_state(_HOME_LOGGED_OUT, "https://x/VistosOnline/",
                                  warmed=False) == SessionState.SESSION_EXPIRED


def test_classify_non_empty_body_alone_is_not_authenticated():
    """A non-empty, unrecognised body must not be read as 'logged in' -- only the specific
    positive markers do that."""
    assert classify_session_state("<html>some perfectly normal-looking but irrelevant page</html>",
                                  "https://x/VistosOnline/",
                                  warmed=False) != SessionState.AUTHENTICATED


# ── _do_keepalive wired to the classifier -- integration-level, exercises the real branches ────
class _RedirectingKeepaliveSession(_KeepaliveSession):
    """Like _KeepaliveSession, but browser_nav can report a DIFFERENT final URL than the one
    requested -- simulating a server-side redirect (sessionLost.jsp, Authentication.jsp)."""
    def __init__(self, page, final_url=None):
        super().__init__(page)
        self._final_url = final_url

    def browser_nav(self, url, timeout=30, **kw):
        self.navs.append(url)
        return self._final_url or url


def test_session_expired_triggers_reauth_not_rewarm():
    """F24's fix, exercised end to end: a Schedule.jsp probe that redirected to sessionLost.jsp
    must re-authenticate, and must NEVER call _run_warmup_steps (there is no session left to warm
    against)."""
    ops = _keepalive_ops("<html>session gone</html>")
    ops.session = _RedirectingKeepaliveSession(
        "<html>session gone</html>", final_url="https://x/VistosOnline/sessionLost.jsp")
    ops._run_warmup_steps = lambda: pytest.fail("must not re-warm a dead session")
    restore_calls = []
    ops._attempt_keepalive_restore = lambda context="": restore_calls.append(context)
    ops._do_keepalive("test")
    assert restore_calls == ["test"]


def test_portal_busy_backs_off_without_rewarm_or_relogin():
    """F25's fix: a busy portal must not re-warm (cannot succeed) and must not re-login (would
    burn a CAPTCHA solve on a condition unrelated to our credentials)."""
    ops = _keepalive_ops(_SCHED_BUSY)
    ops._run_warmup_steps = lambda: pytest.fail("must not re-warm against a busy portal")
    ops._attempt_keepalive_restore = lambda context="": pytest.fail("must not re-login on a busy portal")
    ops._do_keepalive("test")   # must return cleanly, doing neither


def test_prewarm_logged_out_home_is_no_longer_read_as_healthy():
    """THE F23 regression test. Before this fix, _do_keepalive never read the pre-warm home page's
    body at all -- a successful nav alone reset the dead-streak and logged 'probe OK', so a worker
    that lost its session before ever warming held indefinitely, reporting healthy, and was counted
    by the Hub as ready capacity."""
    ops = _keepalive_ops(_HOME_LOGGED_OUT, warmed=False)
    restore_calls = []
    ops._attempt_keepalive_restore = lambda context="": restore_calls.append(context)
    logged = []
    ops._log = logged.append
    ops._do_keepalive("test")
    assert restore_calls == ["test"], "a logged-out pre-warm home must trigger re-authentication"
    assert not any("probe OK" in m for m in logged), "must never report healthy on a dead session"


def test_prewarm_authenticated_home_still_reports_healthy():
    """The positive case, so the fix above isn't just inverting the polarity: a genuinely
    authenticated pre-warm home page must still be treated as healthy."""
    ops = _keepalive_ops(_HOME_AUTHENTICATED, warmed=False)
    ops._attempt_keepalive_restore = lambda context="": pytest.fail("must not re-auth a healthy session")
    events = []
    ops._emit = lambda t, **p: events.append((t, p))
    ops._do_keepalive("test")
    assert any(t == "log_line" and "probe OK" in p.get("text", "") for t, p in events)


def test_unrecognised_prewarm_response_is_inconclusive_not_healthy_not_dead():
    """F26: UNKNOWN must not be silently absorbed into either healthy or dead -- it does nothing
    and waits for the next cycle, which is the bounded 'don't guess' behaviour the spec calls for."""
    ops = _keepalive_ops("<html>a page that matches none of the known markers</html>", warmed=False)
    ops._attempt_keepalive_restore = lambda context="": pytest.fail("UNKNOWN must not trigger re-auth")
    events = []
    ops._emit = lambda t, **p: events.append((t, p))
    ops._do_keepalive("test")
    assert not any(t == "log_line" and "probe OK" in p.get("text", "") for t, p in events), \
        "UNKNOWN must not be reported as healthy"


def test_exit_ip_change_is_informational_and_does_not_override_a_healthy_classification():
    """Q16 (confirmed): an exit change does not by itself invalidate the portal session. The
    classifier must never consult _note_exit_ip's result -- an authenticated page stays
    AUTHENTICATED even when the exit moved this same cycle."""
    ops = _keepalive_ops(_SCHED_OK)
    ops._note_exit_ip = lambda when: True   # exit "moved" this cycle
    ops._run_warmup_steps = lambda: pytest.fail("must not re-warm a healthy schedule page")
    ops._attempt_keepalive_restore = lambda context="": pytest.fail(
        "an exit change must never by itself trigger re-authentication")
    ops._do_keepalive("test")   # must return cleanly: healthy page, exit-change is informational only


# ── escalating a re-warm that keeps failing (2026-08-22) ─────────────────────
# CONFIRMED LIVE on Test 2032: a worker held a HEALTHY auth session (keepalive nav returned a full
# 28,505-byte page every cycle) while every questionnaire POST came back 403 -- the exit IP was
# WAF-flagged, not the session. Neither ladder acted: the dead-session path never fired because
# browser_nav succeeded, and the re-warm path marked NOT BOOKABLE and returned. It looped to streak
# 7 on the same blocked proxy. Re-warming cannot clear an IP-level block; rebuilding on a fresh
# proxy can.
class _Restores:
    """Records whether the escalation reached the restore chain."""
    def __init__(self):
        self.calls = 0

    def __call__(self, context=""):
        self.calls += 1
        return True


def _stuck_ops(streak, *, slot_found, escalate_after=3):
    ops = _make_ops(cfg=_cfg(REWARM_ESCALATE_AFTER=escalate_after))
    ops._rewarm_fail_streak = streak
    ops.posto_pdf = "5084"
    ops._slot_found_seen = lambda: slot_found
    ops._attempt_keepalive_restore = _Restores()
    ops._mark_hold_bookable = lambda ok, why="": None
    ops._log = lambda *a, **k: None
    return ops


def test_below_the_threshold_it_keeps_re_warming_in_place():
    ops = _stuck_ops(2, slot_found=True)
    ops._maybe_escalate_stuck_rewarm("await_assignment")
    assert ops._attempt_keepalive_restore.calls == 0


def test_at_the_threshold_it_rebuilds_the_session():
    ops = _stuck_ops(3, slot_found=True)
    ops._maybe_escalate_stuck_rewarm("await_assignment")
    assert ops._attempt_keepalive_restore.calls == 1


def test_it_never_escalates_before_slot_found():
    """The LOOSE HOLD rule: a re-login costs a CAPTCHA solve and sub-account budget, and before
    slot_found there is nothing to book that would justify spending them."""
    ops = _stuck_ops(9, slot_found=False)
    ops._maybe_escalate_stuck_rewarm("await_slot_open")
    assert ops._attempt_keepalive_restore.calls == 0
    assert ops._rewarm_fail_streak == 9, "streak must survive so it escalates once slot_found fires"


def test_escalating_clears_the_streak_so_it_does_not_fire_every_tick():
    ops = _stuck_ops(7, slot_found=True)
    ops._maybe_escalate_stuck_rewarm("await_assignment")
    assert ops._rewarm_fail_streak == 0


def test_escalation_forces_the_anchor_to_be_rebuilt():
    """posto_pdf still holds the value from the warm-up that has since lapsed. It must be cleared,
    or _attempt_keepalive_restore stops at re-authenticating and leaves the dead anchor in place."""
    seen = {}
    ops = _stuck_ops(3, slot_found=True)

    def _restore(context=""):
        seen["posto_pdf_at_restore"] = ops.posto_pdf
        return True
    ops._attempt_keepalive_restore = _restore
    ops._maybe_escalate_stuck_rewarm("await_assignment")
    assert seen["posto_pdf_at_restore"] is None


def test_the_threshold_is_operator_configurable():
    ops = _stuck_ops(4, slot_found=True, escalate_after=9)
    ops._maybe_escalate_stuck_rewarm("await_assignment")
    assert ops._attempt_keepalive_restore.calls == 0, "REWARM_ESCALATE_AFTER must be read, not hardcoded"


# ── a challenged ScheduleController must not report a warm session (2026-08-22) ──
def test_challenge_body_is_recognised():
    """44699 bytes of DataDome shell -- the exact body that was accepted as a successful
    ScheduleController on worker SOFIA FILOMENA COSTA, moments before her submit died."""
    from apps.worker.ops_live import _is_challenge_body

    challenge = ('<!DOCTYPE html>\n<html>\n<head>\n<meta charset="UTF-8">\n'
                 '<meta name="robots" content="noindex,nofollow">\n<style>\n'
                 '  body{margin:0;background:#fff}\n</style>\n</head>\n<body>\n'
                 '<script src="/ch/bd.js"></script>\n')
    assert _is_challenge_body(challenge) is True


def test_a_real_schedule_page_is_not_a_challenge():
    """Guard rail: the real page must never be mistaken for an interstitial, or every warm-up
    fails."""
    from apps.worker.ops_live import _is_challenge_body

    real = ("<html><body><div id='captchaDiv'></div><script>getSlots();</script>"
            "<form action='SubmeterVistoCriaPDF'><input name='f_date_c'>"
            "<select name='cmbPeriodo'></select></form></body></html>")
    assert _is_challenge_body(real) is False
    assert _is_challenge_body("") is False
    assert _is_challenge_body(None) is False


# ── lazy re-warm: don't rebuild an anchor that is still good (2026-08-22) ────
class _SchedPageSession:
    """Serves a given Schedule.jsp body for the state probe."""
    def __init__(self, html, final_url="https://x/VistosOnline/Schedule.jsp"):
        self._html, self._url = html, final_url
        self.navs = 0

    def browser_nav(self, url, **kw):
        self.navs += 1
        return self._url

    def browser_eval(self, js):
        if "location.origin" in (js or ""): return _origin_reply(js)
        return self._html


_REAL_SCHED = ("<html><div id='captchaDiv'></div><script>getSlots()</script>"
               "<form action='SubmeterVistoCriaPDF'><input name='f_date_c'>"
               "<select name='cmbPeriodo'></select></form></html>")


def test_a_live_anchor_skips_the_rewarm_entirely():
    """storm-auto does ONE Schedule.jsp GET and only re-warms if the markers are gone. We rebuilt
    the whole questionnaire chain before EVERY submit -- 5 of this run's 8 apply rounds died inside
    that rebuild, not at the submit."""
    ops = _make_ops(emit=lambda t, **p: None)
    ops.session = _SchedPageSession(_REAL_SCHED)
    ops.posto_pdf = "2032"
    assert ops._schedule_state_ok() is True
    assert ops.session.navs == 1, "one GET, no rebuild"


def test_a_challenged_schedule_page_is_not_ok():
    ops = _make_ops(emit=lambda t, **p: None)
    ops.session = _SchedPageSession(
        '<html><head><meta name="robots" content="noindex,nofollow">'
        '<script src="/ch/bd.js"></script></head></html>')
    assert ops._schedule_state_ok() is False


def test_an_auth_redirect_is_not_ok():
    ops = _make_ops(emit=lambda t, **p: None)
    ops.session = _SchedPageSession("<html>login</html>",
                                    final_url="https://x/VistosOnline/Authentication.jsp")
    assert ops._schedule_state_ok() is False


def test_a_partial_render_is_refresh_retried_before_giving_up(monkeypatch):
    """A 200 with the form not yet AJAX-populated is a reload, not a reason to redo 7 quest steps."""
    monkeypatch.setattr("apps.worker.ops_live.time.sleep", lambda *_: None)
    ops = _make_ops(emit=lambda t, **p: None)
    ops.session = _SchedPageSession("<html>no markers yet</html>")
    assert ops._schedule_state_ok() is False
    assert ops.session.navs == 1 + ops.cfg.login.partial_render_retries


# ── the portal's "try again later" page is not the slot's fault (2026-08-23) ──
def test_portal_busy_page_is_recognised_in_both_languages():
    from apps.worker.ops_live import _is_portal_busy

    assert _is_portal_busy(
        "<html>Não foi possível de momento concretizar o seu pedido, por favor tente mais tarde</html>")
    assert _is_portal_busy(
        "<html>We were unable to complete your request at this time. Please try again later.</html>")
    assert _is_portal_busy("<html>getSlots cmbPeriodo</html>") is False
    assert _is_portal_busy("") is False


# ── the portal saying "you already have an appointment" (2026-08-23) ─────────
def test_already_booked_markers_match_the_portals_wording():
    from apps.worker.ops_live import _ALREADY_BOOKED_MARKERS

    real = ("&times; | This applicant already has an application registered with a future "
            "schedule. You do not need to request a new application").lower()
    assert any(m in real for m in _ALREADY_BOOKED_MARKERS)


def test_an_ordinary_validation_error_is_not_treated_as_already_booked():
    """Guard rail: retiring an identity is irreversible-ish (BOOKED only goes on to ARCHIVED), so a
    generic rejection must never trigger it."""
    from apps.worker.ops_live import _ALREADY_BOOKED_MARKERS

    other = "&times; | The selected date is no longer available. Please choose another.".lower()
    assert not any(m in other for m in _ALREADY_BOOKED_MARKERS)


# ── mail.tm rate limiting was the top registration failure (2026-08-23) ─────
def test_mailtm_takes_a_proxy_so_creation_is_not_all_from_one_ip():
    """87 of ~170 registration outcomes were "mail.tm create failed (429)" -- more than every
    portal-side cause combined -- because a module-level unproxied Session sent every mailbox
    creation from the server's single address."""
    from libs.engine.vendor.email_pool import MailTM

    m = MailTM(proxy="http://u:p@proxy.example:8080")
    assert m._sess.proxies["https"] == "http://u:p@proxy.example:8080"
    assert MailTM()._sess.proxies == {}, "no proxy configured must stay direct"


def test_mailtm_sessions_are_per_instance():
    """A shared module-level Session also meant concurrent registrations shared cookies/keep-alive
    against a per-IP rate limiter."""
    from libs.engine.vendor.email_pool import MailTM

    assert MailTM()._sess is not MailTM()._sess


def test_mailtm_backs_off_on_429_instead_of_failing_immediately():
    from libs.engine.vendor.email_pool import MailTM

    m = MailTM(retries=3, backoff=0.0)
    calls = {"n": 0}

    class _R:
        status_code = 429
        text = "rate limited"

    def _post(url, json=None, timeout=None):
        calls["n"] += 1
        return _R()

    m._sess.post = _post
    r = m._post("/accounts", {})
    assert calls["n"] == 3, "a 429 is a WAIT, not an immediate failure"
    assert r.status_code == 429


# ── a refusing portal must not drive a rebuild (2026-08-23) ──────────────────
_BUSY_PAGE = ("<!DOCTYPE html><html lang='en'><body>We were unable to complete your request at "
              "this time. Please try again later.</body></html>")


def test_the_state_probe_flags_a_refusing_portal_rather_than_a_stale_form():
    """CONFIRMED live on Test 2032 (AMILCAR LOPES): the submit was refused with the 'try again
    later' page, then Schedule.jsp returned the SAME page four times. That read as "200 but no
    scheduling markers", escalated into a full questionnaire re-warm whose every step was also
    refused, and ended in a full re-login -- a CAPTCHA solve spent rebuilding into a wall."""
    ops = _make_ops(emit=lambda t, **p: None)
    ops.session = _SchedPageSession(_BUSY_PAGE)
    ops.posto_pdf = "2032"
    assert ops._schedule_state_ok() is False
    assert ops._portal_busy_seen is True
    assert ops.session.navs == 1, "no point refresh-retrying a refusal"


# ── the silent gettime rejection (2026-08-23) ───────────────────────────────
def test_gettime_page_classifier_names_the_page():
    from apps.worker.ops_live import _classify_gettime_page

    assert _classify_gettime_page('<script src="/ch/bd.js"></script>') == "waf_challenge"
    assert _classify_gettime_page("We were unable to complete your request at this time") \
        == "portal_try_again_later"
    assert _classify_gettime_page("<form action='/VistosOnline/login'>") == "auth_redirect"
    assert _classify_gettime_page("<html>something new</html>") == "unrecognised"


def test_a_rejected_gettime_says_why_instead_of_returning_silently():
    """CONFIRMED live: a worker flipped discovering -> holding 15 times in 7 minutes emitting
    NOTHING, because this path was a bare `return`. The dashboard showed a busy discoverer that had
    scanned zero dates every time."""
    logged = []
    ops = _make_ops(emit=lambda t, **p: None)
    ops._log = logged.append

    # A REAL blocked gettime is a full HTML page: classify_gettime_body flags anything over 5000
    # bytes (or carrying the WAF markers) as BLOCKED. The live ones were 28-44KB.
    waf = ('<html><head><script src="/ch/bd.js"></script></head><body>'
           + "x" * 6000 + "</body></html>")

    class _Waf:
        def browser_nav(self, *a, **k): return "u"
        def browser_eval(self, js="", *a, **k): return _origin_reply(js, waf)
    ops.session = _Waf()

    out = ops.do_discover(["2026-09-01", "2026-09-02"])
    assert [o["outcome"] for o in out] == ["failed", "failed"]
    assert any("scanned NOTHING" in m and "waf_challenge" in m for m in logged), logged


# ── the escalation deadlock under MONO_BOOKING (2026-08-23) ─────────────────
def _stuck(mono, slot_open, slot_found, streak=5):
    # MONO_BOOKING is a BOOKING-layer setting; _cfg() puts everything in TEMPLATE.
    cfg = EngineConfig.from_settings(resolve({
        L.BOOKING: {"MONO_BOOKING": mono},
        L.TEMPLATE: {"ROLE": "apply", "REWARM_ESCALATE_AFTER": 3},
    }))
    ops = _make_ops(cfg=cfg)
    ops._rewarm_fail_streak = streak
    ops.posto_pdf = "2032"
    ops._slot_open_seen = lambda: slot_open
    ops._slot_found_seen = lambda: slot_found
    ops._mark_hold_bookable = lambda ok, why="": None
    ops._log = lambda *a, **k: None
    calls = {"n": 0}
    ops._attempt_keepalive_restore = lambda context="": calls.__setitem__("n", calls["n"] + 1) or True
    return ops, calls


def test_mono_escalates_once_slot_open_has_fired():
    """The deadlock: under MONO the apply worker IS the discoverer, so gating on slot_found is
    circular -- lapsed form-state means it cannot discover, so nothing is ever found, so escalation
    is refused forever. Four workers sat at 're-warm failed x7' for over an hour."""
    ops, calls = _stuck(mono=True, slot_open=True, slot_found=False)
    ops._maybe_escalate_stuck_rewarm("await_assignment")
    assert calls["n"] == 1


def test_split_mode_still_waits_for_slot_found():
    """The LOOSE HOLD rule is correct where an idle applier really is waiting on scouts."""
    ops, calls = _stuck(mono=False, slot_open=True, slot_found=False)
    ops._maybe_escalate_stuck_rewarm("await_assignment")
    assert calls["n"] == 0


def test_nothing_escalates_before_the_booking_opens():
    ops, calls = _stuck(mono=True, slot_open=False, slot_found=False)
    ops._maybe_escalate_stuck_rewarm("await_slot_open")
    assert calls["n"] == 0


# ── a portal refusal at login is fleet-wide news (2026-08-24) ───────────────
class _Breaker:
    def __init__(self): self.down = 0; self.ok = 0; self.tripped = False
    def is_tripped(self): return self.tripped
    def record_portal_down(self): self.down += 1
    def record_success(self): self.ok += 1


def test_the_portals_try_again_later_page_is_reported_to_the_breaker():
    """265 of 266 refusals landed at login, and every worker retried independently -- 341 attempts
    for 30 sessions. Retrying harder cannot help: the CAPTCHA solves and the token is accepted, the
    portal simply declines to serve us."""
    from apps.worker.ops_live import _is_portal_busy

    assert _is_portal_busy("Não foi possível de momento concretizar o seu pedido")
    assert _is_portal_busy("We were unable to complete your request at this time")


def test_a_credential_rejection_is_not_reported_as_portal_down():
    """Guard rail: the breaker gates the whole booking, so only the portal's own unwillingness may
    trip it -- never a verdict about one account."""
    from apps.worker.ops_live import _is_portal_busy

    assert _is_portal_busy("login_failed_auth_redirect") is False
    assert _is_portal_busy("Por favor, confirma o reCAPTCHA.") is False
    assert _is_portal_busy("datadome_challenge_on_post") is False


def test_a_tripped_breaker_stops_do_login_before_it_spends_anything():
    ops = _make_ops(emit=lambda t, **p: None)
    ops._log = lambda *a, **k: None
    b = _Breaker(); b.tripped = True
    ops._breaker = b
    assert ops.do_login() is False
    assert b.down == 0, "a tripped breaker must short-circuit, not re-report"


# ── the portal's 2-failure reCAPTCHA block (2026-08-24) ─────────────────────
def test_a_recaptcha_verdict_is_recognised():
    """The portal states the penalty itself: two failures allowed, more and platform access is
    BLOCKED. The registrar has respected that since 2026-08-21; the login path did not."""
    from apps.worker.ops_live import _is_recaptcha_verdict

    assert _is_recaptcha_verdict("Por favor, confirma o reCAPTCHA.")
    assert _is_recaptcha_verdict("type=ReCaptchaError desc=...")
    assert _is_recaptcha_verdict("Dispoe de mais 0 tentativas")


def test_other_rejections_do_not_consume_the_captcha_budget():
    """Only a CAPTCHA verdict may spend it -- a credential rejection or a WAF challenge says
    nothing about the reCAPTCHA counter."""
    from apps.worker.ops_live import _is_recaptcha_verdict

    assert _is_recaptcha_verdict("login_failed_auth_redirect") is False
    assert _is_recaptcha_verdict("datadome_challenge_on_post") is False
    assert _is_recaptcha_verdict("Não foi possível de momento concretizar o seu pedido") is False
    assert _is_recaptcha_verdict("") is False


def test_the_budget_defaults_to_one_leaving_a_margin():
    from libs.domain.settings.catalog import CATALOG

    assert CATALOG["CAPTCHA_FAILURE_BUDGET"].default == 1, \
        "the portal allows 2; spending 1 keeps a margin so a later retry cannot block the account"


# ── a questionnaire bounce is a lapsed anchor, not a taken slot (2026-08-24) ─
def test_a_bounce_to_the_questionnaire_is_form_stale_not_no_slot():
    """Measured on booking Test 2032: 60 bounced leases, 0 booked, and all 16 genuinely-available
    slots closed -- every bounce was the portal answering SubmeterVistoCriaPDF with the
    questionnaire (72131B, title 'questionnaire to apply for a visa', schedule_markers=none). That
    is the response to a submit with NO server-side anchor, not evidence the slot was taken.
    Calling it no_slot released each slot WITH a bounce penalty, parked the workers, and convinced
    the false-slot revert that a real pool was phantom."""
    from apps.worker.ops_live import LiveEngineOps

    html = ("<html><head><title>questionnaire to apply for a visa</title></head>"
            "<body><form action='/VistosOnline/questionario'>...</form></body></html>")
    assert LiveEngineOps._classify_submit_response(
        html, "https://x/VistosOnline/SubmeterVistoCriaPDF") == "form_stale"


def test_a_bounce_to_the_schedule_page_is_still_no_slot():
    """The guard rail: landing back on the SCHEDULE page means the anchor is intact and someone
    simply beat us to it -- storm-auto's original reading, which still holds there."""
    from apps.worker.ops_live import LiveEngineOps

    html = ("<html><body><div id='captchaDiv'></div><script>getSlots()</script>"
            "<select name='cmbPeriodo'></select> getPeriodosOcupados</body></html>")
    assert LiveEngineOps._classify_submit_response(
        html, "https://x/VistosOnline/SubmeterVistoCriaPDF") == "no_slot"


# ── the submit falls back to a direct POST (2026-08-24) ─────────────────────
class _BrowserErrorThenPrimpOK:
    """browser_form_post lands on chrome-error; the direct POST succeeds."""
    def __init__(self, fallback_html):
        self._fb = fallback_html
        self.posted = []

    def browser_form_post(self, url, data, timeout=100):
        return "chrome-error://chromewebdata/", "<html>" + "x" * 200000 + "</html>"

    def post(self, url, data=None, headers=None, params=None, timeout=30, **kw):
        self.posted.append(url)

        class _R:
            status_code = 200
            text = self._fb if False else None
        r = _R(); r.text = self._fb; r.url = url
        return r

    def browser_nav(self, *a, **k): return "u"
    def browser_eval(self, js="", *a, **k): return _origin_reply(js, "")


def test_the_browser_path_is_still_tried_first():
    """A real navigation is what DataDome trusts -- the direct POST is a fallback, never a
    replacement."""
    ops = _make_ops(emit=lambda t, **p: None)
    ops._log = lambda *a, **k: None
    ops.posto_pdf = "2032"
    ops._schedule_state_ok = lambda: True
    ops._run_warmup_steps = lambda: True

    class _BrowserOK:
        posted = []
        def browser_form_post(self, url, data, timeout=100):
            return ("https://x/VistosOnline/SubmeterVistoCriaPDF",
                    "<html><body><form action='MostrarPdf'></form></body></html>")
        def post(self, *a, **k):
            raise AssertionError("must not fall back when the browser path worked")
        def browser_nav(self, *a, **k): return "u"
        def browser_eval(self, js="", *a, **k): return _origin_reply(js, "")
        def submit_form_binary(self, **k): return {"status": 200, "body": b"%PDF-1.4", "content_type": "application/pdf"}
    ops.session = _BrowserOK()
    ops.do_apply({"date": "2026-08-28", "period": 3})


# ── the submit is an XHR, matching the real booking HAR (2026-08-24) ────────
# ── our own gates must not be charged to the slot (2026-08-24) ──────────────


def test_push_slots_emits_exactly_what_the_hub_persister_consumes():
    """The scout->Hub slot seam, which had no direct test.

    push_slots is one line, but it is a SEAM, and every bug this project actually hit lived in one:
    a payload key dropped between worker and Hub, an event type the consumer does not match on. The
    Hub's persister (apps/hub/events.py's _persist_slots) matches on the event type `slots_found`
    and reads `payload["slots"]`, then requires `date` and `period` on each entry -- anything else
    is silently skipped by its `continue`, which is exactly the kind of silence that costs a run.

    So this pins the contract from the producing side rather than trusting the two ends to agree.
    """
    emitted = []
    ops = _make_ops(emit=lambda t, **p: emitted.append((t, p)))
    found = [{"date": "2026-09-01", "period": 4}, {"date": "2026-09-02", "period": 8}]
    ops.push_slots(found)

    assert len(emitted) == 1
    etype, payload = emitted[0]
    assert etype == "slots_found", "the Hub consumer matches on this exact type"
    assert payload["slots"] == found, "the Hub reads payload['slots']"
    # every entry must carry the two keys _persist_slots requires, or it drops them silently
    for s in payload["slots"]:
        assert s.get("date") is not None and s.get("period") is not None


def test_push_slots_with_nothing_found_emits_nothing_worth_persisting():
    """An empty discovery round must not manufacture a slot push. `empty` is a successful 'no
    slots' answer (see do_discover's 3-way outcome) -- turning it into an event would let a clean
    empty scan look like a find."""
    emitted = []
    ops = _make_ops(emit=lambda t, **p: emitted.append((t, p)))
    ops.push_slots([])
    assert emitted == [] or emitted[0][1]["slots"] == []


def test_the_maintenance_page_is_recognised_as_a_portal_refusal():
    """The portal's maintenance page is the portal declining to serve -- not a bad session, not a
    bad proxy, not the account.

    It went unrecognised until 2026-08-27, and the cost was measured on booking "Test 2032": 154
    refusals across 13 sub-accounts and 99 rotations, with the booking's RedisBreaker hash still
    EMPTY -- record_portal_down() is gated on _is_portal_busy(), so nothing ever backed off and the
    fleet kept cycling accounts against a portal explicitly saying "come back later".
    """
    from apps.worker.ops_live import _is_portal_busy
    assert _is_portal_busy("Serviço em manutenção. Tente novamente mais tarde.")
    assert _is_portal_busy("SERVICO EM MANUTENCAO")            # accent-stripped form
    assert _is_portal_busy("<html>Service under maintenance</html>")
    # and it must not fire on an ordinary page
    assert not _is_portal_busy("<html>Solicitar pedido de Visto</html>")


def test_the_existing_busy_refusals_still_match():
    """Guard against the maintenance addition narrowing what was already recognised."""
    from apps.worker.ops_live import _is_portal_busy
    assert _is_portal_busy("Não foi possível de momento concretizar o seu pedido")
    assert _is_portal_busy("We were unable to complete your request at this time")


# ── Test2 regression: a failed origin re-anchor must abort the warm-up chain ──
class _DeadOriginSession(_FakeSession):
    """Off-origin page whose re-anchor navigation also fails -- the exact live state: the guard
    reports "the tunnel is genuinely down" and cannot restore a portal origin."""
    def __init__(self):
        super().__init__()
        self.fetches = 0
        self.navs = 0

    def browser_nav(self, url, timeout=30, **kw):
        self.navs += 1
        raise RuntimeError("net::ERR_TUNNEL_CONNECTION_FAILED")

    def browser_eval(self, js, timeout=30):
        if "location.origin" in (js or ""):
            return json.dumps({"o": "chrome-error://chromewebdata", "h": "chrome-error://chromewebdata/"})
        self.fetches += 1                       # any in-page fetch attempted after the guard
        raise RuntimeError("Page.evaluate: TypeError: Failed to fetch")


def test_warmup_aborts_when_the_portal_origin_cannot_be_restored():
    """LIVE REGRESSION (booking "Test2", 2026-08-27).

    _run_warmup_steps called _ensure_portal_origin and DISCARDED its return, so a re-anchor that
    had just reported "the tunnel is genuinely down" was followed by the very fetch loop the code
    calls guaranteed-to-fail. Measured: 24 off-origin detections -> 24 failed re-anchors -> 24
    warmup failures -> 96 "Failed to fetch" (4 attempts each), burning 30-43s per cycle and
    reporting the symptom instead of the known cause.

    The chain must now stop at the guard.
    """
    ops = _make_ops(emit=lambda t, **p: None)
    logs = []
    ops._log = logs.append
    ops.session = _DeadOriginSession()

    assert ops._run_warmup_steps() is False
    # THE POINT: not one in-page fetch was attempted after the guard said the origin was dead.
    assert ops.session.fetches == 0, (
        f"ran {ops.session.fetches} doomed in-page fetch(es) after a failed re-anchor")
    # and the reported cause is the real one, not the downstream symptom
    joined = " | ".join(logs)
    assert "re-anchor" in joined
    assert "Failed to fetch" not in joined, f"reported the symptom instead of the cause: {joined}"


def test_warmup_still_proceeds_when_the_origin_is_healthy():
    """Guard the successful path: an on-origin page must run the chain exactly as before, so the
    fix cannot turn a healthy warm-up into a refusal."""
    ops = _make_ops(emit=lambda t, **p: None)
    ops._log = lambda t: None
    reached = {"n": 0}

    class _OnOrigin(_FakeSession):
        def browser_eval(self, js, timeout=30):
            if "location.origin" in (js or ""):
                return _origin_reply(js)
            reached["n"] += 1
            raise RuntimeError("stop here -- we only need to prove the chain was entered")

    ops.session = _OnOrigin()
    ops._run_warmup_steps()
    assert reached["n"] > 0, "a healthy origin must still enter the in-page fetch chain"


# ── primer/origin diagnostic (2026-08-27) ────────────────────────────────────────────────────
# The /slots primer's result was discarded and a chrome-error page returns normally rather than
# raising, so a primer that parked the page on an opaque origin was invisible -- and since
# browser_form_post builds its form in the CURRENT document, that makes the SUBMIT unsendable.
# These pin the diagnostic as OBSERVATIONAL: it must record the primer + origin and change nothing.

class _PrimerSession(_SubmitSession):
    """Primer lands on chrome-error; the submit still behaves exactly as the stub dictates."""
    def __init__(self, pdf_bytes: bytes, primer_result):
        super().__init__(pdf_bytes)
        self._primer_result = primer_result
        self.calls = []

    def browser_form_post(self, url, data, timeout=100):
        self.calls.append(url)
        if "Slots" in url or "slots" in url:
            return self._primer_result
        return super().browser_form_post(url, data, timeout)



def test_the_diagnostic_does_not_change_the_apply_outcome():
    """Same stub, with and without a dead primer: the outcome must be identical. The diagnostic
    observes; it must not gate, guard or re-anchor anything (that decision comes after the data)."""
    ops_ok = _apply_ops(b"%PDF-1.4 real", [])
    ops_ok.session = _PrimerSession(b"%PDF-1.4 real", ("https://portal/slots", "<html>ok</html>"))
    ops_ok._log = lambda *a, **k: None

    ops_dead = _apply_ops(b"%PDF-1.4 real", [])
    ops_dead.session = _PrimerSession(b"%PDF-1.4 real",
                                      ("chrome-error://chromewebdata/", "<html>dead</html>"))
    ops_dead._log = lambda *a, **k: None

    slot = {"date": "2026-09-01", "period": 4}
    assert ops_ok.do_apply(slot) == ops_dead.do_apply(slot)



def test_a_rejected_submit_body_is_written_to_disk(tmp_path, monkeypatch):
    # _save_submit_body is stubbed below, so nothing reaches disk; this test never wrote a file.
    events = []
    ops = _apply_ops(b"", events)          # MostrarPdf shell, no PDF -> not booked
    written = {}
    monkeypatch.setattr(ops, "_save_submit_body",
                        lambda html, outcome: written.setdefault(outcome, html) or "/x.html")
    logs = []
    ops._log = lambda m, *a, **k: logs.append(m)

    out = ops.do_apply({"date": "2026-09-01", "period": 4})

    assert out != "booked"
    assert written, "the rejected submit response was discarded -- again"
    assert any("body=" in m for m in logs), "the saved path was not logged"


def test_saving_the_submit_body_never_breaks_the_round(tmp_path, monkeypatch):
    """Diagnostics must not be able to change an apply outcome. A write that blows up is logged
    and swallowed; the classification is whatever the portal actually said."""
    events = []
    ops = _apply_ops(b"", events)
    logs = []
    ops._log = lambda m, *a, **k: logs.append(m)
    # This one must run the REAL _save_submit_body -- the autouse isolation fixture replaces it,
    # so put the original back for this test before making the write fail.
    monkeypatch.setattr(LiveEngineOps, "_save_submit_body", _REAL_SAVE_SUBMIT_BODY)
    monkeypatch.setattr(pathlib.Path, "write_text",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))

    out = ops.do_apply({"date": "2026-09-01", "period": 4})

    assert out != "booked"          # unchanged by the failed write
    assert any("could not save the submit body" in m for m in logs)


def test_a_booked_submit_does_not_write_a_rejection_body(tmp_path):
    """Only rejections are captured; a real booking already persists its PDF artifact."""
    events = []
    ops = _apply_ops(b"%PDF-1.4 real booking", events)
    calls = []
    ops._save_submit_body = lambda html, outcome: calls.append(outcome) or "/x.html"
    ops._save_pdf = lambda b: "/tmp/x.pdf"

    assert ops.do_apply({"date": "2026-09-01", "period": 4}) == "booked"
    assert calls == [], "a successful booking wrote a rejection body"


# ── the apply round must not POST /slots before the submit (2026-08-27) ──────────────────────
# MEASURED, controlled, fresh session per arm, only the primer differing:
#   control (wait 6s, no primer)  Schedule.jsp 55,194B -> 55,196B, all 5 scheduling markers
#   primer                        Schedule.jsp 55,196B -> 39B, ZERO markers
# _SCHED_PAGE_MARKERS on Schedule.jsp is this codebase's own definition of a live booking cursor
# (it is what _run_warmup_steps checks). The CAPTCHA-less /slots POST destroyed it every round, so
# the submit that followed had no cursor and the portal answered with the questionnaire page.

class _UrlRecordingSession(_SubmitSession):
    """Records every URL the apply round navigates, so a re-introduced primer is visible."""
    def __init__(self, pdf_bytes: bytes):
        super().__init__(pdf_bytes)
        self.posted_urls = []

    def browser_form_post(self, url, data, timeout=100):
        self.posted_urls.append(url)
        return super().browser_form_post(url, data, timeout)


def test_the_apply_round_never_posts_slots_before_the_submit():
    ops = _apply_ops(b"%PDF-1.4 real", [])
    ops.session = _UrlRecordingSession(b"%PDF-1.4 real")
    ops._save_pdf = lambda b: "/tmp/x.pdf"

    ops.do_apply({"date": "2026-09-01", "period": 4})

    slots_posts = [u for u in ops.session.posted_urls if "/slots" in u]
    assert slots_posts == [], (
        "the /slots primer is back -- it destroys the server-side booking cursor "
        f"(Schedule.jsp 55KB+5 markers -> 39B+0 markers): {slots_posts}")


def test_the_apply_round_still_submits_exactly_once():
    """Removing the primer must not disturb the submit itself: one POST, to SubmeterVistoCriaPDF."""
    ops = _apply_ops(b"%PDF-1.4 real", [])
    ops.session = _UrlRecordingSession(b"%PDF-1.4 real")
    ops._save_pdf = lambda b: "/tmp/x.pdf"

    assert ops.do_apply({"date": "2026-09-01", "period": 4}) == "booked"
    submits = [u for u in ops.session.posted_urls if "SubmeterVistoCriaPDF" in u]
    assert len(submits) == 1, f"expected exactly one submit, got {ops.session.posted_urls}"


# ── pre-submit cursor diagnostic (2026-08-27b) ───────────────────────────────────────────────
# Removing the /slots primer did not stop form_stale, which falsified the assumption that
# Schedule.jsp's markers are the cursor the submit needs. This records the document the form is
# actually built in, at the instant of the real submit. Observational: it must never gate anything.

def test_the_pre_submit_document_is_recorded():
    logs = []
    ops = _apply_ops(b"%PDF-1.4 real", [])
    ops.session = _UrlRecordingSession(b"%PDF-1.4 real")
    ops._save_pdf = lambda b: "/tmp/x.pdf"
    ops._log = lambda m, *a, **k: logs.append(m)

    ops.do_apply({"date": "2026-09-01", "period": 4})

    assert any("PRE-SUBMIT doc" in m for m in logs), \
        "the document the submit is built in was not recorded"


def test_an_unreadable_pre_submit_document_does_not_change_the_outcome():
    """A diagnostic that can break an apply round is worse than no diagnostic."""
    class _EvalBlows(_UrlRecordingSession):
        def browser_eval(self, js, *a, **k):
            if "outerHTML" in js:
                raise RuntimeError("Execution context was destroyed")
            return super().browser_eval(js, *a, **k)

    logs = []
    ops = _apply_ops(b"%PDF-1.4 real", [])
    ops.session = _EvalBlows(b"%PDF-1.4 real")
    ops._save_pdf = lambda b: "/tmp/x.pdf"
    ops._log = lambda m, *a, **k: logs.append(m)

    assert ops.do_apply({"date": "2026-09-01", "period": 4}) == "booked"
    assert any("PRE-SUBMIT doc unreadable" in m for m in logs)


# ── discovery backs off when the portal says "try again later" (2026-08-27) ──────────────────
# MEASURED live on "Test 2032": one worker went `-> discovering` twenty times in ~10 minutes, every
# one answered portal_try_again_later (28,499B), five workers wide, for hours. We were asking again
# 30s after the portal explicitly said to try later. storm-auto carries this fix and the scar behind
# it: "~2.5h of empty polling drew 77 DataDome pages -> fixed by bounded backoff."

_BLOCKED_BODY = "<html><body>Não foi possível de momento concretizar o seu pedido, " \
                "por favor tente mais tarde" + "x" * 28000 + "</body></html>"
_OK_GETTIME = "<html>special_days = {'2026': {'8': [28]}};</html>"


class _GettimeSession(_FakeSession):
    def __init__(self, body):
        super().__init__()
        self.body = body

    def browser_nav(self, url, timeout=30, **kw):
        return url

    def browser_eval(self, js, timeout=30):
        if "location.origin" in (js or ""):
            return _origin_reply(js)
        return self.body


def _discover_ops(body, monkeypatch, **raw):
    slept = []
    import apps.worker.ops_live as _ol
    monkeypatch.setattr(_ol.time, "sleep", lambda s: slept.append(s))
    ops = _make_ops(emit=lambda t, **p: None)
    ops.session = _GettimeSession(body)
    ops._log = lambda *a, **k: None
    ops.posto_pdf = "2032"
    ops.cfg.raw.update(raw)
    return ops, slept


def test_a_blocked_gettime_backs_off_and_escalates(monkeypatch):
    ops, slept = _discover_ops(_BLOCKED_BODY, monkeypatch,
                               DISCOVERY_BLOCKED_BACKOFF=30.0, DISCOVERY_BLOCKED_BACKOFF_CAP=300.0)
    for _ in range(3):
        out = ops.do_discover(["2026-08-28"])
        assert all(r["outcome"] == "failed" for r in out), "a block must be failed, never empty"
    assert len(slept) == 3, f"no back-off on a blocked gettime: {slept}"
    assert slept[1] > slept[0] and slept[2] > slept[1], \
        f"back-off did not escalate across consecutive blocks: {slept}"


def test_the_backoff_is_capped(monkeypatch):
    ops, slept = _discover_ops(_BLOCKED_BODY, monkeypatch,
                               DISCOVERY_BLOCKED_BACKOFF=30.0, DISCOVERY_BLOCKED_BACKOFF_CAP=90.0)
    for _ in range(6):
        ops.do_discover(["2026-08-28"])
    assert max(slept) <= 90.0 * 1.16, f"back-off exceeded its cap: {slept}"


def test_the_backoff_can_be_disabled(monkeypatch):
    ops, slept = _discover_ops(_BLOCKED_BODY, monkeypatch, DISCOVERY_BLOCKED_BACKOFF=0.0)
    ops.do_discover(["2026-08-28"])
    assert slept == [], f"back-off ran despite being disabled: {slept}"


def test_a_healthy_gettime_resets_the_streak(monkeypatch):
    """A transient block must cost one short pause, not permanently raise the floor."""
    ops, slept = _discover_ops(_BLOCKED_BODY, monkeypatch, DISCOVERY_BLOCKED_BACKOFF=30.0)
    ops.do_discover(["2026-08-28"])
    ops.do_discover(["2026-08-28"])
    first_two = list(slept)
    ops.session.body = _OK_GETTIME          # portal answers properly
    ops.do_discover(["2026-08-28"])
    ops.session.body = _BLOCKED_BODY        # and blocks again later
    ops.do_discover(["2026-08-28"])
    # Compare against the LADDER, not against another jittered sample. The nap is
    # base * 2**(streak-1) * uniform(0.85, 1.15), so a step-1 nap is <= 34.5s and a step-2 nap is
    # >= 51s -- non-overlapping bounds, hence deterministic. The previous form asserted
    # `slept[-1] <= first_two[0] * 1.2`, which compared two jittered draws and failed roughly
    # whenever the first landed low and the last landed high (seen 2026-09-01: 33.42 vs 27.60).
    base = 30.0
    assert slept[1] >= base * 2 * 0.85, f"the second block did not escalate: {slept}"
    assert slept[-1] <= base * 1.15, \
        f"the streak did not reset after a healthy gettime -- still on the ladder: {slept}"


# ── DISCOVERY_SOURCE finally has a read site (2026-08-31) ────────────────────────────────────
# It sat in the settings catalog and in EngineConfig.discovery.source from the start with NOTHING
# consuming it -- the same unwired-setting trap as QUEST_STEP_GAP and PARTIAL_RENDER_RETRIES.
# The /slots path delivers the token the way the portal's own page does: onCaptchaSuccess(token)
# -> the page's getSlots() -> handleServerResponse() populates window.data. data/new_booking.har
# (a real booking) shows /slots returning 21,191B, so the endpoint works when driven correctly.

class _SlotsPageSession(_FakeSession):
    """Page-world stub: browser_eval returns whatever the portal's own script would have left."""
    def __init__(self, payload):
        super().__init__()
        self.payload = payload
        self.evals = []

    def browser_nav(self, url, timeout=30, **kw):
        return url

    def browser_eval(self, js, timeout=30):
        self.evals.append(js)
        # The readiness poll added 2026-09-01 also mentions onCaptchaSuccess, so it has to be
        # matched FIRST -- on the real page the widget script does eventually define the callback,
        # which is what `True` here stands for.
        if "onCaptchaSuccess === 'function'" in js and "window.data" not in js:
            return True
        if "onCaptchaSuccess" in js:
            return self.payload
        if "location.origin" in (js or ""):
            return _origin_reply(js)
        return ""


def _slots_ops(payload, source, monkeypatch):
    monkeypatch.setattr("libs.engine.vendor.race_all",
                        lambda *a, **k: "T" * 2617)
    ops = _make_ops(emit=lambda t, **p: None)
    ops.session = _SlotsPageSession(payload)
    ops._log = lambda *a, **k: None
    ops.posto_pdf = "3059"
    ops.cfg.raw["DISCOVERY_SOURCE"] = source
    object.__setattr__(ops.cfg.discovery, "source", source)
    return ops


def test_slots_source_parses_the_page_world_payload(monkeypatch):
    payload = {"ok": True, "data": [{"date": "2026-09-14", "periods": [3, 5]},
                                    {"date": "2026-09-15", "periods": []}]}
    ops = _slots_ops(payload, "slots", monkeypatch)
    out = ops.do_discover(["2026-09-14", "2026-09-15", "2026-09-16"])
    by = {r["date"]: r for r in out}
    assert by["2026-09-14"]["outcome"] == "found" and by["2026-09-14"]["periods"] == [3, 5]
    assert by["2026-09-15"]["outcome"] == "empty"
    assert by["2026-09-16"]["outcome"] == "empty", "absent from a payload the portal DID return"


def test_a_non_array_payload_is_failed_not_empty(monkeypatch):
    """The crash guard AND the classification guard. A non-array body is the portal declining, not
    an empty calendar -- reporting `empty` would prune real dates from the pool (bug fix #6)."""
    ops = _slots_ops({"coerced": True, "raw": '{"error":"ReCaptchaError"}'}, "slots", monkeypatch)
    out = ops.do_discover(["2026-09-14", "2026-09-15"])
    assert all(r["outcome"] == "failed" for r in out), out


def test_calendar_remains_the_default(monkeypatch):
    """The proven path must stay the default until a measurement justifies flipping it."""
    ops = _slots_ops({"ok": True, "data": []}, "calendar", monkeypatch)
    called = []
    ops._discover_via_calendar = lambda dates: called.append(dates) or [
        {"date": d, "outcome": "empty"} for d in dates]
    ops._discover_via_slots = lambda dates: (_ for _ in ()).throw(
        AssertionError("slots path ran while DISCOVERY_SOURCE=calendar"))
    ops.do_discover(["2026-09-14"])
    assert called == [["2026-09-14"]]


def test_both_falls_back_to_calendar_only_for_unresolved_dates(monkeypatch):
    ops = _slots_ops({"ok": True, "data": []}, "both", monkeypatch)
    ops._discover_via_slots = lambda dates: [
        {"date": "2026-09-14", "outcome": "found", "periods": [5]},
        {"date": "2026-09-15", "outcome": "failed"}]
    seen = []
    ops._discover_via_calendar = lambda dates: seen.append(list(dates)) or [
        {"date": d, "outcome": "empty"} for d in dates]
    out = {r["date"]: r["outcome"] for r in ops.do_discover(["2026-09-14", "2026-09-15"])}
    assert seen == [["2026-09-15"]], f"calendar was asked for the wrong dates: {seen}"
    assert out == {"2026-09-14": "found", "2026-09-15": "empty"}


def test_the_token_is_bound_to_SCHEDULE_EVISA(monkeypatch):
    """Action mismatch is the documented way this fails: a LOGIN_EVISA token is validated for the
    wrong action and the backend answers {"ReCaptchaError": true}."""
    seen = {}
    monkeypatch.setattr("libs.engine.vendor.race_all",
                        lambda *a, **k: seen.setdefault("action", a[4]) or "T" * 2617)
    ops = _make_ops(emit=lambda t, **p: None)
    ops.session = _SlotsPageSession({"ok": True, "data": []})
    ops._log = lambda *a, **k: None
    ops.posto_pdf = "3059"
    ops._discover_via_slots(["2026-09-14"])
    assert seen.get("action") == "SCHEDULE_EVISA", f"token bound to {seen.get('action')!r}"


# ── the real /slots payload shape (operator capture, 2026-08-31) ─────────────────────────────
#   [{"date":"2026-08-31","periods":[{"id":3,"description":"8-9"},
#                                    {"id":3,"description":"9-10"},
#                                    {"id":4,"description":"10-11"}]}]
# Top-level RAW ARRAY, and a period is an OBJECT not a scalar. Passing the object through puts a
# dict into cmbPeriodo (booking_payload does str(slot["period"])) and submits garbage.

_REAL_SLOTS_PAYLOAD = {"ok": True, "data": [
    {"date": "2026-08-31", "periods": [{"id": 3, "description": "8-9"},
                                       {"id": 3, "description": "9-10"},
                                       {"id": 4, "description": "10-11"}]}]}


def test_object_periods_are_reduced_to_integer_ids(monkeypatch):
    ops = _slots_ops(_REAL_SLOTS_PAYLOAD, "slots", monkeypatch)
    out = ops.do_discover(["2026-08-31"])
    assert len(out) == 1 and out[0]["outcome"] == "found"
    periods = out[0]["periods"]
    assert periods == [3, 4], f"expected integer ids, got {periods!r}"
    assert all(isinstance(p, int) for p in periods), \
        "a dict reached cmbPeriodo -- booking_payload would submit str(dict)"


def test_a_repeated_period_id_is_not_emitted_twice(monkeypatch):
    """id 3 appears twice (08-09 and 09-10). One selector value -- emit it once."""
    ops = _slots_ops(_REAL_SLOTS_PAYLOAD, "slots", monkeypatch)
    periods = ops.do_discover(["2026-08-31"])[0]["periods"]
    assert periods.count(3) == 1, f"duplicate selector value emitted: {periods!r}"


def test_scalar_periods_still_parse(monkeypatch):
    """Tolerate a payload of bare ids -- we have only one capture of the object form."""
    ops = _slots_ops({"ok": True, "data": [{"date": "2026-09-14", "periods": [5, 7]}]},
                     "slots", monkeypatch)
    assert ops.do_discover(["2026-09-14"])[0]["periods"] == [5, 7]


def test_unparseable_period_entries_are_skipped_not_crashed(monkeypatch):
    ops = _slots_ops({"ok": True, "data": [
        {"date": "2026-09-14", "periods": [{"id": "5"}, {"description": "no id"},
                                           None, {"id": "x"}, 7]}]}, "slots", monkeypatch)
    assert ops.do_discover(["2026-09-14"])[0]["periods"] == [5, 7]


def test_an_unrecognised_discovery_source_is_refused_loudly(monkeypatch):
    """The dashboard renders DISCOVERY_SOURCE as FREE TEXT -- the catalog has no enum type. A typo
    that silently ran the old path while the operator believed the new one was under test is the
    same 'looks configured, isn't' failure as every unwired setting found in this project."""
    ops = _slots_ops({"ok": True, "data": []}, "slot", monkeypatch)   # typo: missing 's'
    logs = []
    ops._log = lambda m, *a, **k: logs.append(m)
    ops._discover_via_slots = lambda dates: (_ for _ in ()).throw(
        AssertionError("ran the slots path on an invalid setting"))
    ops._discover_via_calendar = lambda dates: [{"date": d, "outcome": "empty"} for d in dates]

    ops.do_discover(["2026-09-14"])

    joined = " ".join(logs)
    assert "DISCOVERY_SOURCE" in joined and "REFUSING" in joined, \
        f"an invalid source fell back silently: {logs}"


def test_a_valid_source_is_accepted_case_and_space_insensitively(monkeypatch):
    ops = _slots_ops({"ok": True, "data": []}, "  SLOTS  ", monkeypatch)
    hit = []
    ops._discover_via_slots = lambda dates: hit.append(1) or [
        {"date": d, "outcome": "empty"} for d in dates]
    ops.do_discover(["2026-09-14"])
    assert hit, "a valid value was rejected over case/whitespace"


# ── the reCAPTCHA failure that made discovery 0% (2026-09-01) ────────────────────────────────
# Booking "Test 0901", webshare, RECAPTCHA_SOLVE_PROXY=False. Every solve returned
# `createTask failed: ERROR_PROXY...` because _discover_via_slots hardcoded self.warm_proxy and
# CapSolver cannot connect out through our webshare exits. The login path has always honoured the
# setting (session.py:1712); this path simply never read it. Catalog: proxied 2/5, proxyless 5/5.

def test_the_solve_is_proxyless_when_RECAPTCHA_SOLVE_PROXY_is_off(monkeypatch):
    # Capture the proxy however it is passed. The buggy version passed it POSITIONALLY, so a
    # kwargs-only assertion passed vacuously against the very code it was meant to catch.
    seen = {}
    def _spy(*a, **k):
        seen["proxy"] = k["proxy"] if "proxy" in k else (a[5] if len(a) > 5 else None)
        return "T" * 2617
    monkeypatch.setattr("libs.engine.vendor.race_all", _spy)
    ops = _make_ops(emit=lambda t, **p: None)
    ops.session = _SlotsPageSession({"ok": True, "data": []})
    ops._log = lambda *a, **k: None
    ops.posto_pdf = "3018"
    ops.warm_proxy = "http://u:p@gw:1"
    object.__setattr__(ops.cfg.solver, "solve_proxy", False)
    ops._discover_via_slots(["2026-09-14"])
    assert seen["proxy"] is None, \
        f"solved through our proxy despite RECAPTCHA_SOLVE_PROXY=False -> ERROR_PROXY: {seen}"


def test_the_solve_uses_our_proxy_when_the_setting_is_on(monkeypatch):
    seen = {}
    def _spy(*a, **k):
        seen["proxy"] = k["proxy"] if "proxy" in k else (a[5] if len(a) > 5 else None)
        return "T" * 2617
    monkeypatch.setattr("libs.engine.vendor.race_all", _spy)
    ops = _make_ops(emit=lambda t, **p: None)
    ops.session = _SlotsPageSession({"ok": True, "data": []})
    ops._log = lambda *a, **k: None
    ops.posto_pdf = "3018"
    ops.warm_proxy = "http://u:p@gw:1"
    object.__setattr__(ops.cfg.solver, "solve_proxy", True)
    ops._discover_via_slots(["2026-09-14"])
    assert seen["proxy"] == "http://u:p@gw:1"


def test_a_solver_failure_reports_nothing_rather_than_guessing(monkeypatch):
    """Rewritten 2026-09-02 (was ..._falls_back_to_calendar_instead_of_failing).

    The original insight stands: phases.py reads an all-`failed` round as a SESSION fault and
    re-warms + re-logs in, and a CapSolver outage is not a session fault. The answer is no longer to
    ask the calendar, though -- /slots is this pool's ground truth, and the calendar derives
    availability by subtraction, which is how phantom slots are made. On the 2026-09-01 run its one
    round re-confirmed 48 slots and shielded them from the retirement that had correctly closed 3.

    So: report NOTHING. An empty list writes nothing to the pool, pushes nothing, and (with the
    matching phases.py guard) neither feeds nor clears the rebuild counter."""
    def _boom(*a, **k): raise RuntimeError("capsolver: createTask failed: ERROR_PROXY_CONNECT_REFUSED")
    monkeypatch.setattr("libs.engine.vendor.race_all", _boom)
    ops = _make_ops(emit=lambda t, **p: None)
    ops.session = _SlotsPageSession({"ok": True, "data": []})
    ops._log = lambda *a, **k: None
    ops.posto_pdf = "3018"
    hit = []
    ops._discover_via_calendar = lambda dates: hit.append(list(dates)) or [
        {"date": d, "outcome": "found", "periods": [5]} for d in dates]

    out = ops._discover_via_slots(["2026-09-14"])

    assert hit == [], "the calendar was consulted -- it must never write into the /slots pool"
    assert out == [], \
        "a solver outage must report nothing: `failed` re-warms the session, `empty` prunes real dates"



# ── the onCaptchaSuccess race that wasted 47 of 55 solves (2026-09-01) ───────────────────────
# browser_nav returns when the DOCUMENT loads, but window.onCaptchaSuccess is registered by the
# portal's reCAPTCHA widget script, which is still loading. MEASURED on booking "Test 0901": 47 of
# 55 rounds died on `onCaptchaSuccess not defined` -- AFTER the token had been solved (2,553-2,702
# chars, 7-23s each), so the solver was paid 47 times for tokens the page could never receive. The
# one round that did land returned SEVENTY slots.
#
# The readiness poll loop runs INSIDE the browser, so Python sees a single browser_eval for it and
# `ready` is just what that loop concluded. What these tests pin is therefore the ORDER -- the probe
# must precede the solve -- not a poll count.

class _LateCallbackSession(_FakeSession):
    """Records the order of operations: probe -> solve -> deliver."""
    def __init__(self, ready=True, payload=None):
        super().__init__()
        self.ready = ready
        self.payload = payload or {"ok": True, "data": [{"date": "2026-09-14", "periods": [5]}]}
        self.order = []

    def browser_nav(self, url, timeout=30, **kw):
        return url

    def browser_eval(self, js, timeout=30):
        if "onCaptchaSuccess === 'function'" in js and "window.data" not in js:
            self.order.append("probe")           # the readiness poll
            return self.ready
        if "onCaptchaSuccess" in js:
            self.order.append("deliver")         # the real delivery call
            return self.payload
        if "location.origin" in (js or ""):
            return _origin_reply(js)
        return ""


def _callback_ops(sess):
    ops = _make_ops(emit=lambda t, **p: None)
    ops.session = sess
    ops._log = lambda *a, **k: None
    ops.posto_pdf = "3018"
    return ops


def test_the_callback_is_awaited_before_a_solve_is_spent(monkeypatch):
    sess = _LateCallbackSession(ready=True)
    monkeypatch.setattr("libs.engine.vendor.race_all",
                        lambda *a, **k: sess.order.append("solve") or "T" * 2617)

    out = _callback_ops(sess)._discover_via_slots(["2026-09-14"])

    assert sess.order == ["probe", "solve", "deliver"], (
        "the solve must come AFTER the readiness probe -- paying for a token the page cannot "
        f"receive is the 47-of-55 waste this fixes; got {sess.order}")
    assert out[0]["outcome"] == "found"


def test_no_solve_is_spent_when_the_callback_never_appears(monkeypatch):
    """The whole point: 47 wasted tokens. If the page never registers the callback, pay nothing."""
    sess = _LateCallbackSession(ready=False)
    solves = []
    monkeypatch.setattr("libs.engine.vendor.race_all",
                        lambda *a, **k: solves.append(1) or "T" * 2617)
    ops = _callback_ops(sess)
    hit = []
    ops._discover_via_calendar = lambda dates: hit.append(dates) or []

    out = ops._discover_via_slots(["2026-09-14"])

    assert solves == [], "a CAPTCHA was solved for a page that could not receive the token"
    assert hit == [], "the calendar must never be consulted (removed 2026-09-02: /slots is the " \
                      "ground truth and calendar-derived slots were laundering into the pool)"
    assert [r["outcome"] for r in out] == ["failed"], \
        "we DID reach the portal and it served a page with no widget -- that is session evidence"


def test_a_probe_that_raises_is_treated_as_not_ready(monkeypatch):
    """browser_eval can throw outright (destroyed execution context). Still spend no solve."""
    sess = _LateCallbackSession(ready=True)
    def _boom(js, timeout=30):
        if "onCaptchaSuccess === 'function'" in js:
            raise RuntimeError("Execution context was destroyed")
        return ""
    sess.browser_eval = _boom
    solves = []
    monkeypatch.setattr("libs.engine.vendor.race_all",
                        lambda *a, **k: solves.append(1) or "T" * 2617)
    ops = _callback_ops(sess)
    hit = []
    ops._discover_via_calendar = lambda dates: hit.append(dates) or []

    out = ops._discover_via_slots(["2026-09-14"])

    assert solves == [] and hit == []
    assert [r["outcome"] for r in out] == ["failed"]


# ── the /slots response itself is now recorded, not just its residue (2026-09-02) ─────────────
# window.data cannot distinguish a 200 carrying an empty object from a 429, a 403, a DataDome
# interstitial or a session redirect -- all four leave `{"data":{}}` behind. MEASURED on the
# 2026-09-01 23:18 run of "Test 0901": 206 of 214 delivered tokens produced exactly that residue and
# nothing recorded which it was, so the fleet re-warmed against a wall for 85 minutes. The JS half
# of this (clone() not breaking the page; the three ambiguous responses coming apart) is exercised
# by probes/slots_probe_harness.mjs, which node can run and pytest cannot.

def test_a_captured_response_is_rendered_with_what_distinguishes_it():
    from apps.worker.ops_live import _fmt_slots_probe
    out = _fmt_slots_probe({"via": "fetch", "status": 429, "ct": "text/plain",
                            "len": 17, "body": "Too Many Requests"})
    assert "429" in out and "Too Many Requests" in out, out


def test_an_html_interstitial_served_as_200_is_still_legible():
    """The status code alone is not enough -- this is the case that looks healthy and is not."""
    from apps.worker.ops_live import _fmt_slots_probe
    out = _fmt_slots_probe({"via": "xhr", "status": 200, "ct": "text/html",
                            "len": 42, "body": "<html><body>captcha-delivery</body></html>"})
    assert "200" in out and "text/html" in out and "captcha-delivery" in out, out


def test_no_capture_is_reported_as_such_not_as_an_error():
    """Absent and failed are different facts and must never render alike: the request may simply
    have been served from cache or issued before the hook existed."""
    from apps.worker.ops_live import _fmt_slots_probe
    assert _fmt_slots_probe(None) == "<no response captured>"
    assert "network error" in _fmt_slots_probe({"via": "fetch", "err": "TypeError: failed to fetch"})


def test_the_probe_is_logged_on_the_empty_object_case(monkeypatch):
    """The 206-of-214 path specifically: the round still reports `failed`, but the log now carries
    what the endpoint actually said instead of leaving it unknowable."""
    payload = {"coerced": True, "raw": '{"data":{}}',
               "probe": {"via": "fetch", "status": 403, "ct": "text/html",
                         "len": 9, "body": "Forbidden"}}
    sess = _LateCallbackSession(ready=True, payload=payload)
    monkeypatch.setattr("libs.engine.vendor.race_all", lambda *a, **k: "T" * 2617)
    ops = _callback_ops(sess)
    lines = []
    ops._log = lambda m, *a, **k: lines.append(m)

    out = ops._discover_via_slots(["2026-09-14"])

    assert all(r["outcome"] == "failed" for r in out), "classification must not change yet"
    assert any("403" in l and "Forbidden" in l for l in lines), \
        f"the endpoint's actual answer never reached the log: {lines}"


def test_a_healthy_round_also_records_what_the_endpoint_said(monkeypatch):
    """Not only failures: a success must report its response too, or there is no baseline to
    compare the failures against."""
    payload = {"ok": True, "data": [{"date": "2026-09-14", "periods": [5]}],
               "probe": {"via": "fetch", "status": 200, "ct": "application/json",
                         "len": 64, "body": '[{"date":"2026-09-14"}]'}}
    sess = _LateCallbackSession(ready=True, payload=payload)
    monkeypatch.setattr("libs.engine.vendor.race_all", lambda *a, **k: "T" * 2617)
    ops = _callback_ops(sess)
    lines = []
    ops._log = lambda m, *a, **k: lines.append(m)

    out = ops._discover_via_slots(["2026-09-14"])

    assert out[0]["outcome"] == "found"
    assert any("HTTP 200" in l for l in lines), f"a successful round logged no response: {lines}"


# ── a "try again later" page is per-exit evidence before it is fleet-wide (2026-09-02) ────────
# It went straight to the booking's circuit breaker without ever testing whether THIS EXIT was the
# problem. MEASURED on the 2026-09-01 23:18 run of "Test 0901": in the 23:30 bucket ELEVEN such
# pages coexisted with FOUR successful logins, so the page plainly does not mean "the portal is
# down for everyone". And because only a SUCCESSFUL login calls record_success(), a degraded exit
# pool locked the fleet out with no way back: 145 refusals over 91 minutes, zero logins after
# 23:37:52. The breaker re-opened every 120s and the next few refusals re-tripped it.

_BUSY_BODY = ('{"type":"error","description":"N\\u00e3o foi poss\\u00edvel de momento concretizar '
              'o seu pedido. Por favor tente mais tarde."}')


class _RecordingBreaker:
    def __init__(self): self.downs = 0; self.successes = 0
    def is_tripped(self): return False
    def record_portal_down(self): self.downs += 1
    def record_success(self): self.successes += 1


def _busy_login_ops(monkeypatch, calls, *, busy_rolls=3, sticky=2):
    import apps.worker.ops_live as ops_live_mod

    class _AlwaysBusy(_FakeSession):
        def browser_login(self, *a, **k):
            calls["n"] += 1
            return {"status": 200, "body": _BUSY_BODY}

    monkeypatch.setattr(ops_live_mod, "get_session", lambda **kw: _AlwaysBusy())
    ops = _make_ops(cfg=_cfg(STICKY_RETRY_MAX=sticky, LOGIN_TRANSIENT_RETRY_MAX=0,
                             LOGIN_PORTAL_BUSY_ROLLS=busy_rolls))
    ops._backoff = lambda n: None
    ops._request_proxy_rotation = lambda phase=None: None
    ops._breaker = _RecordingBreaker()
    rolls = []
    ops._roll_exit = lambda why="": rolls.append(why) or True
    return ops, rolls


def test_a_busy_page_rolls_the_exit_before_the_fleet_is_told_anything(monkeypatch):
    calls = {"n": 0}
    ops, rolls = _busy_login_ops(monkeypatch, calls, busy_rolls=3)

    ops._sticky_login_attempts(ops.cfg.login.sticky_retry_max)

    assert len(rolls) == 3, f"the refusal was blamed on the portal without trying another exit: {rolls}"
    assert ops._breaker.downs == 2, (
        "the breaker should hear about it only after 3 DIFFERENT exits refused -- then once per "
        f"remaining sticky attempt (got {ops._breaker.downs})")


def test_a_busy_page_is_not_charged_to_the_sub_account_while_rolling(monkeypatch):
    """It is not a verdict on the credentials, so it belongs beside `transient`/`ip_moves`, above
    the `attempt += 1`. Charged too early it would burn the sticky budget and rotate accounts over
    an exit problem."""
    calls = {"n": 0}
    ops, _ = _busy_login_ops(monkeypatch, calls, busy_rolls=3, sticky=2)

    ops._sticky_login_attempts(ops.cfg.login.sticky_retry_max)

    assert calls["n"] == 5, \
        f"expected 3 uncharged rolls + 2 charged sticky attempts, got {calls['n']}"


def test_rolling_is_bounded_so_a_genuine_outage_still_reaches_the_fleet(monkeypatch):
    """The 2026-08-23 finding stands: when the portal really is refusing everyone, the fleet must
    back off together rather than each worker grinding alone. Rolling only delays that until it is
    established, and setting the budget to 0 restores the original straight-to-breaker behaviour."""
    calls = {"n": 0}
    ops, rolls = _busy_login_ops(monkeypatch, calls, busy_rolls=0)

    ops._sticky_login_attempts(ops.cfg.login.sticky_retry_max)

    assert rolls == [], "with the budget at 0 no exit roll should be attempted"
    assert ops._breaker.downs > 0, "a genuine outage never reached the breaker"


# ── four /slots answers that need four different responses (2026-09-02) ───────────────────────
# All of them were `failed`, which routed every one into the re-warm ladder -- a treatment that
# fixes none of them. MEASURED on the 17:15 run of "Test 0901", two scouts polling the same endpoint
# seconds apart for the same dates: 88d2046d alternated strictly reject/success with successes 99s
# and 117s apart (a rate limit), while 28b8fe7f returned {"data":{}} six times out of six after
# re-warming successfully four times (a burned session, which only a fresh login cleared).

def test_the_four_responses_are_classified_apart():
    from apps.worker.ops_live import (_classify_slots_response, SLOTS_OK, SLOTS_RATE_LIMITED,
                                      SLOTS_SESSION_BURNED, SLOTS_BLOCKED)
    P = lambda st, b: {"probe": {"status": st, "body": b}}
    assert _classify_slots_response(P(200, '{"error": ReCaptchaError"}')) == SLOTS_RATE_LIMITED
    assert _classify_slots_response(P(200, '{"data":{}}\n')) == SLOTS_SESSION_BURNED
    assert _classify_slots_response(P(403, "<!DOCTYPE html>")) == SLOTS_BLOCKED
    out = dict(P(200, '[{"date":"2026-09-08"}]')); out["ok"] = True
    assert _classify_slots_response(out) == SLOTS_OK


def test_a_403_is_blocked_whatever_body_it_carries():
    """Status is checked before the body: a block page can say anything, including nothing."""
    from apps.worker.ops_live import _classify_slots_response, SLOTS_BLOCKED
    assert _classify_slots_response({"probe": {"status": 429, "body": '{"data":{}}'}}) == SLOTS_BLOCKED


def test_the_residue_is_used_when_nothing_was_captured():
    """The interceptor can miss (cache, or a request issued before the hook existed) -- the old
    page residue must still classify rather than silently becoming 'unknown'."""
    from apps.worker.ops_live import _classify_slots_response, SLOTS_SESSION_BURNED
    assert _classify_slots_response({"coerced": True, "raw": '{"data":{}}'}) == SLOTS_SESSION_BURNED


def _slots_kind_ops(monkeypatch, payload):
    sess = _LateCallbackSession(ready=True, payload=payload)
    monkeypatch.setattr("libs.engine.vendor.race_all", lambda *a, **k: "T" * 2617)
    ops = _callback_ops(sess)
    ops._roll_exit = lambda why="": True
    return ops


def test_a_rate_limit_reports_nothing_and_does_not_touch_the_session(monkeypatch):
    ops = _slots_kind_ops(monkeypatch, {"coerced": True, "raw": '{"error": ReCaptchaError"}',
                                        "probe": {"status": 200, "body": '{"error": ReCaptchaError"}'}})
    out = ops._discover_via_slots(["2026-09-14"])

    assert out == [], "a rate limit reported `failed`, which drives the re-warm ladder"
    assert ops.discovery_needs_relogin() is False, "a rate limit is not a session fault"
    assert ops._slots_backoff_until > 0, "no back-off was armed, so the next round refuses again"


def test_a_burned_session_asks_for_a_re_login_not_a_re_warm(monkeypatch):
    ops = _slots_kind_ops(monkeypatch, {"coerced": True, "raw": '{"data":{}}',
                                        "probe": {"status": 200, "body": '{"data":{}}\n'}})
    out = ops._discover_via_slots(["2026-09-14"])

    assert [r["outcome"] for r in out] == ["failed"], "the ladder must still engage"
    assert ops.discovery_needs_relogin() is True, \
        "without this the ladder re-warms, which is measured never to clear {\"data\":{}}"


def test_a_block_rolls_the_exit(monkeypatch):
    ops = _slots_kind_ops(monkeypatch, {"coerced": True, "raw": "<html>",
                                        "probe": {"status": 403, "body": "<!DOCTYPE html>"}})
    rolled = []
    ops._roll_exit = lambda why="": rolled.append(why) or True
    out = ops._discover_via_slots(["2026-09-14"])

    assert rolled, "a 403 left the worker on the exit that had just been blocked"
    assert out == [], "a blocked exit says nothing about the dates"


def test_a_grant_clears_the_relogin_flag_and_arms_the_pacing(monkeypatch):
    ops = _slots_kind_ops(monkeypatch, {"ok": True, "data": [{"date": "2026-09-14", "periods": [5]}],
                                        "probe": {"status": 200, "body": "[...]"}})
    ops._slots_needs_relogin = True
    out = ops._discover_via_slots(["2026-09-14"])

    assert out[0]["outcome"] == "found"
    assert ops.discovery_needs_relogin() is False, "a real array proves the session is fine"
    assert ops._slots_last_grant_at > 0


def test_no_solve_is_bought_inside_the_grant_interval(monkeypatch):
    """The measured limit is ~1 response per 100s. Asking sooner buys a token and trades it for a
    refusal, which is exactly the spend this removes."""
    solves = []
    sess = _LateCallbackSession(ready=True)
    monkeypatch.setattr("libs.engine.vendor.race_all",
                        lambda *a, **k: solves.append(1) or "T" * 2617)
    ops = _callback_ops(sess)
    import time as _t
    ops._slots_last_grant_at = _t.time()      # a grant just landed

    out = ops._discover_via_slots(["2026-09-14"])

    assert solves == [], "bought a CAPTCHA token inside the interval the endpoint refuses"
    assert out == [], "the skipped round must report nothing, not guess at the dates"

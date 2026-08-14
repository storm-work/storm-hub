"""LiveEngineOps -- construction, protocol conformance, and pure-logic unit tests. No real browser or
network calls (that's Phase E's live-gated tier); this covers what's testable offline: the response
classification, the discovery outcome shaping, and that the class actually satisfies EngineOps."""
from libs.domain.enums import SettingsLayer as L
from libs.domain.settings import resolve
from libs.engine.config import EngineConfig
from libs.engine.phases import EngineOps
from apps.worker.ops_live import LiveEngineOps, _extract_csrf


def _cfg(**template):
    return EngineConfig.from_settings(resolve({L.TEMPLATE: {"ROLE": "apply", **template}}))


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
        return "<html>cmbPeriodo</html>" if self._alive else "<html>Authentication.jsp</html>"

    def close_browser(self):
        self.closed = True


def test_do_swap_off_by_default_returns_none():
    ops = _make_ops()
    ops.session = _FakeSession()
    assert ops.cfg.hold_swap.swap_after_found is False   # catalog default
    assert ops.do_swap() is None
    assert ops.session.closed is False   # untouched


def test_do_swap_on_but_no_hold_proxy_returns_none():
    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_FOUND=True))
    ops.session = _FakeSession()
    assert ops.do_swap() is None


def test_do_swap_success_replaces_session_and_closes_old(monkeypatch):
    import apps.worker.ops_live as ops_live_mod

    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_FOUND=True), hold_proxy="http://u:p@holdhost:9999")
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
    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_FOUND=True, HOLD_SWAP_RETRIES=0),
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

    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_FOUND=True, HOLD_SWAP_RETRIES=1,
                             HOLD_SWAP_RETRY_HOLD_SECS=600.0),
                    hold_proxy="http://u:p@holdhost:9999", emit=lambda t, **p: None,
                    signals=_RotationSignals(proxy="http://u:p@freshhold:8888"))
    ops.session = _FakeSession()

    result = ops.do_swap()

    assert result == "freshhold:8888"   # succeeded on the retry, using the freshly rotated proxy
    assert calls == ["http://u:p@holdhost:9999", "http://u:p@freshhold:8888"]
    assert sleeps == [600.0]   # held on the current proxy before the retry
    assert ops.hold_proxy == "http://u:p@freshhold:8888"


def test_do_swap_gives_up_after_exhausting_retries_never_stops_worker(monkeypatch):
    import apps.worker.ops_live as ops_live_mod

    monkeypatch.setattr(ops_live_mod, "get_session", lambda **kw: _FakeSession(alive=False))
    monkeypatch.setattr(ops_live_mod.time, "sleep", lambda s: None)

    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_FOUND=True, HOLD_SWAP_RETRIES=2), emit=lambda t, **p: None,
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
    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_FOUND=True), hold_proxy="http://u:p@holdhost:9999")
    ops.session = _FakeSession()
    ops.do_swap()
    assert captured.get("fp_seed") == "u"


# ── P3: proxy rotation -- a worker asks the Hub for a fresh proxy drawn from the pool matching its
# CURRENT phase (warmup vs hold), since the worker process is deliberately DB-less and can't lease a
# proxy row itself ────────────────────────────────────────────────────────────────────────────────
class _RotationSignals:
    """Fake BusSignals exposing only wait_proxy -- the round-trip _request_proxy_rotation blocks on."""
    def __init__(self, ok=True, proxy=None, reason="pool exhausted"):
        self._ok = ok
        self._proxy = proxy
        self._reason = reason
        self.calls = 0
        self.stopped = False   # matches real BusSignals' contract; is_stopped() reads this

    def wait_proxy(self, timeout=30):
        self.calls += 1
        if self._ok:
            return {"ok": True, "proxy": self._proxy}
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


def test_do_swap_flips_current_phase_to_hold_even_when_swap_off():
    """The operator's explicit requirement: phase-based proxy-pool selection for rotation "will not
    be different when swap is off" -- do_swap() must flip the tracked phase unconditionally, before
    its own early-return for HOLD_SWAP_AFTER_FOUND being off."""
    ops = _make_ops()
    assert ops._current_phase == "warmup"
    ops.session = _FakeSession()
    assert ops.cfg.hold_swap.swap_after_found is False   # catalog default: swap off
    assert ops.do_swap() is None
    assert ops._current_phase == "hold"


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


def test_classify_submit_response_bounced_to_questionario_is_not_booked():
    # Even with "comprovativo" somewhere, a bounce-back marker means NOT confirmed.
    html = "<html>questionario comprovativo</html>"
    assert LiveEngineOps._classify_submit_response(html) == "bounced"


def test_classify_submit_response_error_keyword_is_not_booked():
    html = "<html>servico indisponivel comprovativo</html>"
    assert LiveEngineOps._classify_submit_response(html) == "bounced"


def test_classify_submit_response_pdf_form_wins_over_stray_bounce_keyword():
    # The MostrarPdf form is the deterministic signal -- a stray bounce/error keyword ELSEWHERE on
    # the confirmation page must not override it (the mirror of the false-APPLIED bug).
    html = '<form name="frm_1" action="MostrarPdf"></form><script>authentication.jsp analytics</script>'
    assert LiveEngineOps._classify_submit_response(html) == "booked"


def test_classify_submit_response_plain_200_no_markers_is_not_booked():
    html = "<html><body>some unrelated page</body></html>"
    assert LiveEngineOps._classify_submit_response(html) == "bounced"


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

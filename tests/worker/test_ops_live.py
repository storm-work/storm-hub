"""LiveEngineOps -- construction, protocol conformance, and pure-logic unit tests. No real browser or
network calls (that's Phase E's live-gated tier); this covers what's testable offline: the response
classification, the discovery outcome shaping, and that the class actually satisfies EngineOps."""
from libs.domain.enums import SettingsLayer as L
from libs.domain.settings import resolve
from libs.engine.config import EngineConfig
from libs.engine.phases import EngineOps
from apps.worker.ops_live import LiveEngineOps, _extract_csrf, _submit_bounce_reason


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
    # ...and at a deeper URL, without the Portuguese markers, it is not the dashboard.
    assert LiveEngineOps._classify_submit_response(
        "<html>questionario</html>", "https://x/VistosOnline/SubmeterVistoCriaPDF") == "no_slot"


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


def test_blank_chrome_error_page_is_not_read_as_a_slot_rejection():
    """A blank/chrome-error page is not the portal answering -- blaming the slot for it would
    soft-release a perfectly good slot and feed the Hub's false-slot counter with noise."""
    ops = _ops_with_submit("<html><head></head><body></body></html>")
    assert ops.do_apply({"date": "2026-09-01", "period": 4}) == "bounced"


def test_datadome_interstitial_is_not_read_as_a_slot_rejection():
    ops = _ops_with_submit("<html><head>datadome challenge</head><body>x</body></html>")
    assert ops.do_apply({"date": "2026-09-01", "period": 4}) == "bounced"


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
    assert LiveEngineOps._classify_submit_response(
        "<html>questionario</html>", _SUBMIT_URL) == "no_slot"


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

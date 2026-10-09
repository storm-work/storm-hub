import pathlib
"""LiveEngineOps -- construction, protocol conformance, and pure-logic unit tests. No real browser or
network calls (that's Phase E's live-gated tier); this covers what's testable offline: the response
classification, the discovery outcome shaping, and that the class actually satisfies EngineOps."""
import json
import time

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
    # HYBRID_MODE defaults ON in production (full-primp workflow), but these unit tests pin the
    # BROWSER transport they assert against; a test exercising the hybrid/primp path builds a cfg
    # with HYBRID_MODE on explicitly (e.g. via _cfg_schedule(booking=...)). HYBRID_MODE is a BOOKING
    # setting, so it goes in the BOOKING layer, not TEMPLATE.
    return EngineConfig.from_settings(
        resolve({L.TEMPLATE: {"ROLE": "apply", **template}, L.BOOKING: {"HYBRID_MODE": False}}))


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
    """A live session that answers per-PAGE, the way the portal does.

    It used to return "<html>cmbPeriodo</html>" for every url -- i.e. a session that is always
    WARMED. That made it impossible to express "authenticated but not yet warmed", which is the
    only state do_swap ever runs in, and is why every do_swap test passed for months while the real
    swap failed 20/20 in production (FAILURE_MODES.md F-swap-probe).

    `alive=True` now means authenticated AND warmed, so both probes see what they ask for:
      * home page      -> both home markers  -> classify_session_state(warmed=False) = AUTHENTICATED
      * Schedule.jsp   -> cmbPeriodo         -> _probe_alive()'s warmed check passes
    `alive=False` returns the explicit expiry signal on either page. A test that needs the
    authenticated-but-UNWARMED state uses _UnwarmedSession instead.
    """
    def __init__(self, *, alive=True, cookies=None):
        self._alive = alive
        self.closed = False
        self._cookies = cookies or [{"name": "Vistos_sid", "value": "abc"}]
        self._last_url = ""

    def get_all_cookies(self):
        return self._cookies

    def browser_nav(self, url, timeout=30):
        self._last_url = url or ""
        return url

    def browser_eval(self, js):
        if "location.origin" in (js or ""): return _origin_reply(js)
        if not self._alive:
            return "<html>Authentication.jsp</html>"
        if "Schedule.jsp" in self._last_url:
            return "<html>cmbPeriodo</html>"
        return "<html>solicitar pedido de visto ... consultar pedidos</html>"

    def close_browser(self):
        self.closed = True

    def browser_fetch(self, url, data, method="POST", headers=None, timeout=30,
                      params=None, encode="form", file_fields=None):
        # PT-version do_apply submits SubmeterVistoCriaPDF as an XHR. Default: delegate to whatever
        # browser_form_post the double defines (final_url, html) and adapt to the {status, body}
        # shape, so every existing apply double works unchanged. Doubles needing a distinct XHR
        # response override this directly (e.g. _SubmitSession).
        try:
            _url, html = self.browser_form_post(url, data)
        except NotImplementedError:
            raise
        except Exception:
            return {"status": 0, "body": ""}
        return {"status": 200, "body": html}


def test_do_swap_off_by_default_returns_none():
    ops = _make_ops()
    ops.session = _FakeSession()
    assert ops.cfg.hold_swap.swap_after_login is False   # catalog default
    assert ops.do_swap() is None
    assert ops.session.closed is False   # untouched


def test_do_swap_on_but_no_hold_proxy_returns_none():
    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_LOGIN=True))
    ops.session = _FakeSession()
    assert ops.do_swap() is None


def test_do_swap_success_replaces_session_and_closes_old(monkeypatch):
    import apps.worker.ops_live as ops_live_mod

    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_LOGIN=True), hold_proxy="http://u:p@holdhost:9999")
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
    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_LOGIN=True, HOLD_SWAP_RETRIES=0),
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

    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_LOGIN=True, HOLD_SWAP_RETRIES=1,
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
    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_LOGIN=True, HOLD_SWAP_RETRIES=1,
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
    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_LOGIN=True, HOLD_SWAP_RETRIES=1,
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

    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_LOGIN=True, HOLD_SWAP_RETRIES=2), emit=lambda t, **p: None,
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


def _restore_ops_with_checkpoint(monkeypatch, ckpt_data):
    import apps.worker.ops_live as ops_live_mod
    monkeypatch.setattr(ops_live_mod, "get_session", lambda **kw: _FakeSession(alive=True))
    monkeypatch.setattr(ops_live_mod.ckpt, "load_checkpoint", lambda identity_id: ckpt_data)
    return _make_ops(cfg=_cfg(RESTORE_STAMPEDE_JITTER=0))   # _make_ops default posto_id = "5084"


def test_checkpoint_restore_is_authed_only_never_adopts_the_form_anchor(monkeypatch):
    """A checkpoint restore re-establishes an AUTHENTICATED session ONLY (operator, 2026-09-23):
    it must NOT adopt the saved posto_pdf form-anchor or mark itself bookable, because KEEPING is an
    authed home session with no live Schedule-page cursor. It must warm before applying."""
    base = {"cookies": {}, "cookies_full": [], "sub_account_id": "sub-1"}
    ops = _restore_ops_with_checkpoint(monkeypatch, {**base, "posto_pdf": "5084", "posto_id": "5084"})
    assert ops._try_restore_from_checkpoint() is True     # authed (login short-circuited)
    assert ops.posto_pdf is None, "restore must NOT adopt the file form-anchor -- authed only"
    assert ops.is_warmed() is False, "an authed-only restore is NOT warmed"


def test_checkpoint_restore_discards_a_stale_posto_after_a_change(monkeypatch):
    """THE posto-change bug (confirmed 2026-09-21 "0921-3058"): a checkpoint is keyed by identity and
    survives a posto change; a bookable one pins the OLD posto via posto_pdf and adopting it SKIPS
    warmup, so the worker would discover+book against the old posto. It must be discarded so the
    worker re-warms against the run's current posto."""
    base = {"cookies": {}, "cookies_full": [], "sub_account_id": "sub-1"}
    # exact posto_id mismatch (new-format checkpoint)
    ops = _restore_ops_with_checkpoint(monkeypatch, {**base, "posto_pdf": "3059", "posto_id": "3059"})
    assert ops._try_restore_from_checkpoint() is False        # posto_id 3059 != this run's 5084
    # legacy checkpoint with only posto_pdf still guarded
    ops2 = _restore_ops_with_checkpoint(monkeypatch, {**base, "posto_pdf": "3018"})
    assert ops2._try_restore_from_checkpoint() is False        # posto_pdf 3018 != 5084


def test_checkpoint_restore_allows_a_login_stage_checkpoint_at_any_posto(monkeypatch):
    """A login-stage checkpoint pins no posto (posto_pdf/posto_id both absent) -- it is posto-agnostic
    and re-warms against the current posto, so the guard must not touch it."""
    ops = _restore_ops_with_checkpoint(monkeypatch,
                                       {"cookies": {}, "cookies_full": [], "sub_account_id": "sub-1"})
    assert ops._try_restore_from_checkpoint() is True


def test_do_swap_passes_stable_fp_seed(monkeypatch):
    import apps.worker.ops_live as ops_live_mod

    captured = {}

    def _spy(**kw):
        captured.update(kw)
        return _FakeSession(alive=True)

    monkeypatch.setattr(ops_live_mod, "get_session", _spy)
    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_LOGIN=True), hold_proxy="http://u:p@holdhost:9999")
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
    assert ops.cfg.hold_swap.swap_after_login is False   # catalog default: swap off
    assert ops.do_swap() is None
    assert ops._current_phase == "warmup"


def test_fresh_login_rolls_to_a_new_exit_before_retry(monkeypatch):
    """Regression (L2/L3, closed by P3): a repeated login failure used to keep retrying on the SAME
    proxy forever. Attempt 1 uses the exit already on hand; attempt 2+ must move first.

    The MECHANISM changed on 2026-09-15 -- it rolls the session id locally instead of asking the
    Hub for another pooled row (that round-trip measured 46-124s per rotation on booking 0911) --
    but the contract is the same, and the fixture now carries a realistic rotating credential so
    the roll has a session id to move. A credential with no session id deliberately does NOT roll
    (D2: never invent one); that is the static case, which goes through the Hub instead."""
    import apps.worker.ops_live as ops_live_mod
    from libs.proxies.lease import session_id

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
    start = ("http://package-352438-sessionid-AAAAAAAAAAAAAAAA-sessionlength-3600"
             "-bindttl-600-idlettl-900-opt-wb:pw@proxy.soax.com:5000")
    ops = _make_ops(emit=lambda t, **p: None, warm_proxy=start)
    ops._backoff = lambda attempt: None   # skip the real sleep
    # No bus rotation available at all -- proves the retry moves exits WITHOUT the Hub.
    ops._request_proxy_rotation = lambda phase=None: pytest.fail(
        "the warm path must not ask the Hub for a proxy -- rotating pools are self-minted")

    assert ops._fresh_login() is True
    assert len(calls) == 2
    assert calls[0]["proxy"] == start                       # attempt 1: the exit already on hand
    assert calls[1]["proxy"] != start                       # attempt 2: moved
    assert session_id(calls[1]["proxy"]) != "AAAAAAAAAAAAAAAA"
    # Everything except the session id must survive the roll -- same package, password, endpoint.
    assert calls[1]["proxy"].startswith("http://package-352438-sessionid-")
    assert calls[1]["proxy"].endswith("-opt-wb:pw@proxy.soax.com:5000")
    # The roll is persisted on the worker, not just used for one attempt.
    assert ops.warm_proxy == calls[1]["proxy"]


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


# ── a login storm must stay responsive to stop (confirmed live 2026-09-07) ─────────────────────
# The whole do_login -> _fresh_login -> _sticky_login_attempts chain ran with NO bus contact, so a
# worker that entered a retry storm went deaf to stop/assign/self-scout for as long as it lasted --
# traced live at 27+ minutes, and the worst case (13 rounds x 3 sub-accounts, each a browser launch
# + login attempt + backoff) is far longer. The Hub was never the bottleneck: it delivers in ~2s.
class _StopAfter:
    """Minimal signals stub: `stopped` flips True once `after` backoffs have elapsed."""
    def __init__(self, after=1):
        self.stopped = False
        self._after = after
        self.backoffs = 0

    def note_backoff(self):
        self.backoffs += 1
        if self.backoffs >= self._after:
            self.stopped = True


def test_a_login_storm_abandons_remaining_sub_accounts_once_stopped(monkeypatch):
    import apps.worker.ops_live as ops_live_mod

    monkeypatch.setattr(ops_live_mod, "get_session",
                        lambda **kw: _AccountAwareSession(succeeds_for=set()))   # nobody succeeds
    accounts = [
        {"username": "user-a", "password": "pa", "sub_account_id": "sub-a"},
        {"username": "user-b", "password": "pb", "sub_account_id": "sub-b"},
        {"username": "user-c", "password": "pc", "sub_account_id": "sub-c"},
    ]
    sig = _StopAfter(after=1)          # a stop lands during the very first backoff
    ops = _make_ops(cfg=_cfg(STICKY_RETRY_MAX=2), accounts=accounts, sub_account_id="sub-a",
                    signals=sig)
    ops._backoff = lambda attempt: sig.note_backoff()
    ops._request_proxy_rotation = lambda phase=None: None

    assert ops._fresh_login() is False
    # Without the fix this burns the full 2x3=6 attempts regardless of the stop. It must bail as
    # soon as the stop is visible -- one backoff in, long before sub-b/sub-c are even tried.
    assert sig.backoffs < 6, f"kept grinding after stop: {sig.backoffs} backoffs"


def test_a_stop_drained_between_retry_rounds_short_circuits_the_retry(monkeypatch):
    """A stop landing mid-storm must be acted on before the NEXT browser launch.

    The mechanism moved on 2026-09-15. It used to be incidental: _request_proxy_rotation blocked in
    wait_proxy(), which drained the stream as a side effect. Removing that round-trip (rotating
    pools are self-minted; there is nothing to ask the Hub for) would have removed the only bus
    drain in the whole login loop, making a storming worker deaf to `stop` again -- so the drain is
    now explicit, via BusSignals.poll(). This test pins the OUTCOME, which is unchanged: once the
    stop is observable, no further browser is launched."""
    import apps.worker.ops_live as ops_live_mod

    launches = []

    def _spy(**kw):
        launches.append(1)
        return _AccountAwareSession(succeeds_for=set())

    monkeypatch.setattr(ops_live_mod, "get_session", _spy)
    sig = _StopAfter(after=99)         # never stops via backoff -- the rotation call does it
    ops = _make_ops(cfg=_cfg(STICKY_RETRY_MAX=3), signals=sig,
                    accounts=[{"username": "u", "password": "p", "sub_account_id": "sub-a"}],
                    sub_account_id="sub-a")
    ops._backoff = lambda attempt: None

    # The explicit drain is what sets `stopped` now -- exactly what BusSignals.poll() does when a
    # stop is sitting on the command stream.
    sig.poll = lambda: setattr(sig, "stopped", True)
    ops._request_proxy_rotation = lambda phase=None: pytest.fail(
        "the warm path must not ask the Hub for a proxy")

    assert ops._sticky_login_attempts(3) is False
    # Round 1 launches a browser, fails, backs off; round 2 drains the bus and sees the stop.
    # It must NOT go on to launch another browser for round 2's login attempt.
    assert len(launches) == 1, f"launched a browser after the stop was observable: {launches}"


def test_backoff_is_interruptible_by_a_stop():
    """At the 120s cap (x1.2 jitter) a single backoff was ~144s of total deafness, and it runs on
    every failure branch in the login loop."""
    sig = _StopAfter(after=99)
    ops = _make_ops(cfg=_cfg(RESPAWN_BACKOFF_BASE=60.0, RESPAWN_BACKOFF_CAP=120.0), signals=sig)
    sig.stopped = True                 # already stopped when the backoff starts
    t0 = time.time()
    ops._backoff(3)                    # would be ~120s x jitter if it slept straight through
    assert time.time() - t0 < 2.0, "backoff slept through a stop instead of checking for it"


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


def test_keepalive_dead_trip_accumulates_across_a_nav_succeeds_eval_throws_cycle():
    """CONFIRMED LIVE 2026-09-03 ("Test 0901"): browser_nav succeeding is not the whole keepalive
    cycle -- browser_eval runs right after it and can throw on its own (the well-known Playwright
    "Execution context was destroyed, most likely because of a navigation" race). The dead-streak
    reset used to sit right after browser_nav, BEFORE the eval call -- so a cycle where nav
    succeeds and eval throws always netted dead_streak = 0 (just reset) + 1 (this cycle's own
    exception) = 1, forever, no matter how many consecutive cycles failed this exact way.
    dead_trip (a CONSECUTIVE-failure counter) could structurally never be reached: three apply
    workers sat at "streak=1/3" for 2+ hours straight and _attempt_keepalive_restore never fired.
    This pins the fix: the reset now only happens once nav AND eval have both succeeded, so this
    failure mode counts toward dead_trip like any other."""
    ops = _make_ops(cfg=_cfg(KEEPALIVE_DEAD_TRIP=2), signals=_FakeSignals(timeouts=2, final="slot_open"))
    ops._signals.slot_found_seen = True

    class _EvalAlwaysThrows(_FakeSession):
        def browser_eval(self, js):
            raise RuntimeError("Execution context was destroyed, most likely because of a navigation")

    ops.session = _EvalAlwaysThrows()
    restore_calls = []
    ops._attempt_keepalive_restore = lambda context="": restore_calls.append(context)
    ops.await_slot_open()
    assert restore_calls == ["await_slot_open"], (
        "two consecutive nav-succeeds-eval-throws cycles must reach KEEPALIVE_DEAD_TRIP=2 and "
        "trigger a restore -- got no restore, meaning the streak is still being reset mid-cycle")


def test_keepalive_a_recovered_nav_race_does_not_count_as_dead(monkeypatch):
    """THE 2026-09-21 keeping fix: 'Execution context was destroyed' on the read is a nav race, not
    a dead session (the healthy home page keeps navigating for a beat after load). The keepalive now
    re-reads once; a race recovers on the retry and must NOT trip dead-detection -- otherwise a
    healthy held session is condemned and its slot capacity deleted, exactly what killed the booking
    window. Contrast test_keepalive_dead_trip_accumulates... where the eval throws BOTH times (a
    genuinely dead page) and correctly does count."""
    import apps.worker.ops_live as ops_live_mod
    monkeypatch.setattr(ops_live_mod.time, "sleep", lambda *_a, **_k: None)   # no real 1s wait
    ops = _make_ops(cfg=_cfg(KEEPALIVE_DEAD_TRIP=1), signals=_FakeSignals(timeouts=1, final="slot_open"))
    ops._signals.slot_found_seen = True

    class _EvalThrowsOnceThenReads(_FakeSession):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._reads = 0

        def browser_eval(self, js):
            if "document.documentElement.outerHTML" in (js or ""):
                self._reads += 1
                if self._reads == 1:
                    raise RuntimeError("Execution context was destroyed, most likely because of a navigation")
                return "<html>solicitar pedido de visto ... consultar pedidos</html>"
            return super().browser_eval(js)   # settle poll and origin checks

    ops.session = _EvalThrowsOnceThenReads()
    restore_calls = []
    ops._attempt_keepalive_restore = lambda context="": restore_calls.append(context)
    ops.await_slot_open()
    assert restore_calls == [], \
        "a nav race that recovers on the re-read must not trip KEEPALIVE_DEAD_TRIP=1"


def test_classify_session_state_treats_a_waf_challenge_as_portal_error():
    """A WAF/bot-challenge interstitial is the portal declining our request, not a dead session --
    the keepalive was the one classifier blind to it, so it fell through to UNKNOWN limbo. Now it is
    PORTAL_ERROR (back off, don't re-authenticate)."""
    from apps.worker.ops_live import classify_session_state, SessionState
    challenge = ('<html><head><meta name="robots" content="noindex,nofollow">'
                 '<script src="/ch/bd.js"></script></head><body></body></html>')
    assert classify_session_state(challenge, "https://portal/", warmed=False) == SessionState.PORTAL_ERROR
    assert classify_session_state(challenge, "https://portal/", warmed=True) == SessionState.PORTAL_ERROR


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


def test_dead_trip_rebuilds_even_before_slot_found():
    """REPLACES test_keepalive_loose_hold_preserves_session_before_slot_found, which asserted the
    opposite and was WRONG (operator principle, 2026-09-11: a session that is not valid for its
    purpose must not be kept).

    The old LOOSE HOLD rule refused to restore before slot_found, on the premise that a
    pre-slot-found blip must never burn a scarce re-login. But dead-trip is KEEPALIVE_DEAD_TRIP
    CONSECUTIVE failures, not a blip -- ops_live's own comment calls it "the point at which this
    code already believes the session is gone". Keeping a session we believe is gone saves nothing:
    the re-login is owed either way, and the only question is whether it is paid during a quiet
    hold or discovered at slot-open with nothing to book from.

    CONFIRMED LIVE 2026-09-11 (booking 0911): workers sat at "not bookable: keepalive dead-trip"
    for hours under the old rule, were still dead when slot-open fired, and the booking could not
    book."""
    ops = _make_ops(cfg=_cfg(KEEPALIVE_DEAD_TRIP=1), signals=_FakeSignals(timeouts=1, final="slot_open"))
    assert ops._signals.slot_found_seen is False   # no slot found yet -- must NOT matter any more

    class _DyingSession(_FakeSession):
        def browser_nav(self, url, timeout=30):
            raise RuntimeError("net::ERR_TUNNEL_CONNECTION_FAILED")

    ops.session = _DyingSession()
    restore_calls = []
    ops._attempt_keepalive_restore = lambda context="": restore_calls.append(context)
    ops.await_slot_open()
    assert restore_calls == ["await_slot_open"], "a dead session must be rebuilt, not preserved"


def test_keepalive_no_restore_still_suppresses_the_rebuild_before_slot_found():
    """KEEPALIVE_NO_RESTORE remains the explicit diagnostic opt-out -- the only thing that still
    leaves a dead session dead."""
    ops = _make_ops(cfg=_cfg(KEEPALIVE_DEAD_TRIP=1, KEEPALIVE_NO_RESTORE=True),
                    signals=_FakeSignals(timeouts=1, final="slot_open"))
    assert ops._signals.slot_found_seen is False

    class _DyingSession(_FakeSession):
        def browser_nav(self, url, timeout=30):
            raise RuntimeError("net::ERR_TUNNEL_CONNECTION_FAILED")

    ops.session = _DyingSession()
    restore_calls = []
    ops._attempt_keepalive_restore = lambda context="": restore_calls.append(context)
    ops.await_slot_open()
    assert restore_calls == []


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


def test_a_live_checkpoint_short_circuits_LOGIN_but_not_warmup(monkeypatch):
    """A good checkpoint skips the LOGIN wall (the scarce resource) -- but NOT warmup. It is an
    authed-only session (operator 2026-09-23), so it is not marked bookable and must still run the
    real warmup chain before applying; is_warmed() stays False until that chain runs."""
    ops, cleared = _ckpt_ops(monkeypatch, _FakeSession(alive=True))
    assert ops._try_restore_from_checkpoint() is True     # authed -- login short-circuited
    assert ops._restore_stage == "none", "must NOT claim a warmed/bookable restore stage"
    assert ops.is_warmed() is False, "authed-only: not warmed until the chain runs"
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
        # PT-version do_apply submits via browser_fetch, which the base _FakeSession.browser_fetch
        # delegates to this method -- so the pdf-viewer shell response is exercised identically and
        # _UrlRecordingSession still records the submit URL.
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


def test_parse_appointment_ref_extracts_portal_date_and_window():
    """The booked confirmation carries no reference number -- only the appointment date + time window
    ('...no dia 2026/11/03  9-10'). _parse_appointment_ref returns that as 'YYYY/MM/DD H-H', or None."""
    from apps.worker.ops_live import _parse_appointment_ref
    html = ('<td>Imprima o formulário e dirija-se ao consulado escolhido no dia '
            '2026/11/03  9-10</td>')
    assert _parse_appointment_ref(html) == "2026/11/03 9-10"
    assert _parse_appointment_ref("dia 2026/11/09&nbsp;10 - 11") == "2026/11/09 10-11"
    assert _parse_appointment_ref("<html>no appointment here</html>") is None


def test_do_apply_booked_emits_appointment_ref():
    """A booked submit emits the parsed appointment_ref on the `booked` event so the Hub can persist
    it to BookedAppointment.appointment_ref (was always None before)."""
    events = []
    # a booked confirmation body carrying the portal date+window
    booked_html = ('<html><body>...no dia 2026/11/03  9-10</body>'
                   '<form action="MostrarPdf"></form></html>')
    ops = _make_ops(cfg=_cfg_schedule(booking={"HYBRID_MODE": True, "HYBRID_SUBMIT": True}),
                    emit=lambda t, **p: events.append((t, p)))
    ops.session = _SubmitSession(b"%PDF-1.4 ok")
    ops._run_warmup_steps = lambda: True
    ops._save_pdf = lambda b: "/tmp/x.pdf"
    ops._fetch_pdf_from_submit_body = lambda html: b"%PDF-1.4 ok"
    ops.posto_pdf = "3059"; ops._set_anchor("chain")
    ops._submit_via_primp = lambda url, body, to: (200, booked_html)
    assert ops.do_apply({"date": "2026-11-03", "period": 4}) == "booked"
    refs = [p.get("appointment_ref") for t, p in events if t == "booked"]
    assert refs and refs[0] == "2026/11/03 9-10", (refs, events)


def test_do_apply_hybrid_submit_goes_via_primp_not_the_browser():
    """HYBRID_SUBMIT: the SubmeterVistoCriaPDF submit is sent by the detached primp client, not the
    in-page browser XHR; the %PDF is still fetched via the browser (postponed)."""
    events = []
    ops = _make_ops(cfg=_cfg_schedule(booking={"HYBRID_MODE": True, "HYBRID_SUBMIT": True}),
                    emit=lambda t, **p: events.append((t, p)))
    primp_calls = []

    class _NoXHR(_SubmitSession):
        def browser_fetch(self, *a, **k):
            raise AssertionError("HYBRID must NOT submit via the browser XHR")

    ops.session = _NoXHR(b"%PDF-1.4 ok")
    ops._run_warmup_steps = lambda: True
    ops._save_pdf = lambda b: "/tmp/x.pdf"
    ops.posto_pdf = "3059"; ops._set_anchor("chain")
    ops._submit_via_primp = lambda url, body, to: (primp_calls.append((url, body)), (200, _PDF_SHELL))[1]
    assert ops.do_apply({"date": "2026-09-01", "period": 4}) == "booked"
    assert primp_calls and "SubmeterVistoCriaPDF" in primp_calls[0][0], "submit must go via primp"


def test_do_apply_default_still_submits_via_the_browser_xhr():
    """HYBRID_MODE off: the fallback in-page browser XHR submit path still works end to end."""
    events = []
    ops = _apply_ops(b"%PDF-1.4 ok", events)   # _cfg() pins HYBRID_MODE off -> browser XHR
    ops._submit_via_primp = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("default must NOT use primp"))
    ops.posto_pdf = "3059"
    assert ops.do_apply({"date": "2026-09-01", "period": 4}) == "booked"


# ── FULL HYBRID (HYBRID_MODE): warm-up / discovery / keepalive on detached primp ──────────────
class _FakeResp:
    def __init__(self, text="", status=200, url=""):
        self.text, self.status_code, self.url = text, status, url


class _FakePrimp:
    """Records requests and replays scripted responses keyed by a URL substring."""
    def __init__(self, routes):
        self.routes = routes          # list of (substr, _FakeResp)
        self.gets, self.posts = [], []

    def _match(self, url):
        for sub, resp in self.routes:
            if sub in url:
                return resp
        return _FakeResp("", 200, url)

    def get(self, url, headers=None):
        self.gets.append(url); return self._match(url)

    def post(self, url, data=None, content=None, headers=None):
        self.posts.append(url); return self._match(url)


def _hybrid_cfg(**booking):
    return EngineConfig.from_settings(resolve(
        {L.TEMPLATE: {"ROLE": "apply"}, L.BOOKING: {"HYBRID_MODE": True, **booking}}))


def test_hybrid_gating_is_master_AND_leg():
    """_hybrid(leg) = HYBRID_MODE AND HYBRID_<leg>; a browserless session forces it off."""
    ops = _make_ops(cfg=_hybrid_cfg()); ops.session = _FakeSession()   # master on, legs default on
    assert ops._hybrid("SUBMIT") and ops._hybrid("WARMUP") and ops._hybrid("DISCOVERY")
    off_leg = _make_ops(cfg=_hybrid_cfg(HYBRID_KEEPALIVE=False)); off_leg.session = _FakeSession()
    assert off_leg._hybrid("WARMUP") and not off_leg._hybrid("KEEPALIVE")
    master_off = _make_ops(cfg=_hybrid_cfg(HYBRID_MODE=False, HYBRID_SUBMIT=True)); master_off.session = _FakeSession()
    assert not master_off._hybrid("SUBMIT"), "master off disables every leg"
    ops.session = None
    assert not ops._hybrid("WARMUP"), "no browser session -> hybrid forced off"


def test_run_warmup_steps_hybrid_routes_to_primp_chain():
    """With HYBRID_WARMUP on, _run_warmup_steps delegates to the primp chain, not the browser."""
    ops = _make_ops(cfg=_hybrid_cfg()); ops.session = _FakeSession()
    called = []
    ops._primp_warmup_chain = lambda: (called.append(1), True)[1]
    ops.session.browser_eval = lambda *a, **k: pytest.fail("hybrid warm-up must not touch the browser")
    assert ops._run_warmup_steps() is True and called == [1]


def test_discover_via_calendar_hybrid_uses_primp_and_parses():
    """HYBRID_DISCOVERY: calendar discovery runs on primp (gettime + getPeriodosOcupados GETs) and
    feeds the real parsers -- a '136' occupied string decodes to free periods [2,4,5]."""
    ops = _make_ops(cfg=_hybrid_cfg(DISCOVERY_SOURCE="calendar")); ops.session = _FakeSession()
    ops.posto_id = "3018"
    fake = _FakePrimp([
        ("gettime", _FakeResp("SPECIAL_DAYS=[];", 200, "gettime")),
        ("getPeriodosOcupados", _FakeResp("x periodos_ocupados='136' y", 200, "gp")),
    ])
    ops._primp_client = lambda: (fake, {})
    ops._reject_phantom_round = lambda out: out      # isolate transport+parse from the phantom guard
    res = ops._discover_via_calendar(["2026-11-03"])
    assert res == [{"date": "2026-11-03", "outcome": "found", "periods": [2, 4, 5], "raw": "136"}], res
    assert any("gettime" in u for u in fake.gets) and any("getPeriodosOcupados" in u for u in fake.gets)


def test_do_keepalive_hybrid_routes_to_primp():
    """HYBRID_KEEPALIVE: _do_keepalive delegates to the primp keepalive and never navigates."""
    ops = _make_ops(cfg=_hybrid_cfg()); ops.session = _FakeSession()
    seen = []
    ops._do_keepalive_primp = lambda ctx: (seen.append(ctx), True)[1]
    ops.session.browser_nav = lambda *a, **k: pytest.fail("hybrid keepalive must not nav the browser")
    ops._note_exit_ip = lambda when: False
    ops._do_keepalive("hold")
    assert seen == ["hold"]


# ── Phase C (HYBRID_CLOSE_BROWSER): close the browser after login, run browserless ──────────────
class _CloseSession(_FakeSession):
    def __init__(self):
        super().__init__(cookies=[{"name": "Vistos_sid", "value": "abc"},
                                  {"name": "cookiesession1", "value": "z"}])
        self.close_calls = 0

    def browser_eval(self, js):
        if "userAgent" in (js or ""):
            return "UA/test"
        return super().browser_eval(js)

    def close_browser(self, timeout=10):
        self.close_calls += 1


def test_hybrid_close_browser_lazy_closes_on_first_primp_and_caches():
    """HYBRID_CLOSE_BROWSER: the first _primp_creds() caches cookies/UA + closes the browser; later
    calls serve the cache without touching the (now dead) browser."""
    ops = _make_ops(cfg=_hybrid_cfg()); s = _CloseSession(); ops.session = s
    cookies, ua = ops._primp_creds()
    assert s.close_calls == 1 and ops._browser_closed
    assert cookies == {"Vistos_sid": "abc", "cookiesession1": "z"} and ua == "UA/test"
    # second call must NOT re-close and must NOT read the closed browser
    s.browser_eval = lambda *a, **k: pytest.fail("must not read a closed browser")
    s.get_all_cookies = lambda: pytest.fail("must not read a closed browser")
    c2, ua2 = ops._primp_creds()
    assert s.close_calls == 1 and (c2, ua2) == (cookies, ua)


def test_hybrid_close_browser_off_keeps_the_browser_open():
    """With HYBRID_CLOSE_BROWSER off, _primp_creds reads live and never closes the browser."""
    ops = _make_ops(cfg=_hybrid_cfg(HYBRID_CLOSE_BROWSER=False)); s = _CloseSession(); ops.session = s
    ops._primp_creds()
    assert s.close_calls == 0 and not ops._browser_closed


def test_ensure_browser_for_pdf_reopens_a_cookie_injected_browser(monkeypatch):
    """Phase C PDF: when the hot-path browser was closed, _ensure_browser_for_pdf reopens one with the
    saved jar injected and clears the closed flag."""
    import apps.worker.ops_live as o
    ops = _make_ops(cfg=_hybrid_cfg())
    ops._browser_closed = True
    ops._hybrid_full_jar = [{"name": "Vistos_sid", "value": "abc"}]
    captured = {}
    monkeypatch.setattr(o, "get_session", lambda **k: captured.update(k) or _FakeSession())
    ops._ensure_browser_for_pdf()
    assert not ops._browser_closed
    assert captured.get("inject_cookies_full") == ops._hybrid_full_jar


def test_apply_submit_timeout_is_tunable_for_stress_mode():
    """APPLY_SUBMIT_TIMEOUT (default 100) controls the submit XHR timeout so stress mode can fast-fail
    a down-portal submit instead of hanging ~100s across the up-window."""
    events = []
    captured = {}
    ops = _make_ops(cfg=_cfg_schedule(booking={"APPLY_SUBMIT_TIMEOUT": 15.0}),
                    emit=lambda t, **p: events.append((t, p)))

    class _TimeoutRec(_SubmitSession):
        def browser_fetch(self, url, data, method="POST", headers=None, timeout=30, **k):
            captured["timeout"] = timeout
            return {"status": 200, "body": _PDF_SHELL}

    ops.session = _TimeoutRec(b"%PDF-1.4 ok")
    ops._run_warmup_steps = lambda: True
    ops._save_pdf = lambda b: "/tmp/x.pdf"
    ops.posto_pdf = "3059"; ops._set_anchor("chain")
    assert ops.do_apply({"date": "2026-09-01", "period": 4}) == "booked"
    assert captured.get("timeout") == 15.0, f"submit used timeout={captured.get('timeout')}"


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
    """storm-auto matches only the PORTUGUESE nav text. The PT-version branch sends lang=PT through
    the whole flow, so the dashboard renders Portuguese and these markers match -- but the site-root
    fallback is kept as belt-and-suspenders and this test locks it: a page served at the bare
    /VistosOnline/ root IS the dashboard whatever language it rendered. Live (old ENG flow): 20 of 24
    bounces landed on the bare /VistosOnline/ root, and only 6 matched the Portuguese markers.

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
        if "body:'lang=PT'" in js:
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
        if "body:'lang=PT'" in js:
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
        if "body:'lang=PT'" in js:
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

    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_LOGIN=True), hold_proxy="http://u:p@holdhost:9999")
    ops.session = _FakeSession()
    monkeypatch.setattr(ops_live_mod, "get_session", lambda **kw: _FakeSession(alive=True))
    assert ops.do_swap() == "holdhost:9999"
    assert ops._current_phase == "hold"


def test_the_old_setting_name_is_no_longer_accepted():
    """Phase 6: HOLD_SWAP_AFTER_FOUND named a trigger point the code never had, and was never a
    supported alias/compatibility mechanism -- it was removed outright, not reconciled with
    HOLD_SWAP_AFTER_LOGIN. A template that still names it must fail to resolve at all (the same
    UnknownSetting every other unrecognised key produces), not silently enable the swap."""
    from libs.domain.settings.resolver import SettingsError

    with pytest.raises(SettingsError):
        _cfg(HOLD_SWAP_AFTER_FOUND=True)


def test_hold_swap_after_login_is_the_sole_setting_that_enables_the_swap():
    cfg = _cfg(HOLD_SWAP_AFTER_LOGIN=True)
    assert cfg.hold_swap.swap_after_login is True


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



# ── keepalive is auth-only, unconditionally (consolidated 2026-09-09 after the Test 0907/
# 0907-auth A/B: authed-only matched schedule-keepalive on outcome while eliminating ~one full
# Schedule.jsp re-warm cycle every ~20min). do_apply/do_discover establish the booking form-state
# fresh on demand instead of keepalive trying to keep it warm -- so it never adds a cold-start
# cost, it only removes what was redundant churn. Must never weaken SESSION_EXPIRED/dead-trip
# detection -- keepalive never had a form-state to verify in the first place any more.

def test_keepalive_probes_home_never_schedule_jsp_even_when_previously_warmed():
    ops = _keepalive_ops("<html><title>E-VISA</title>no scheduling markers here</html>", warmed=True)
    ops._run_warmup_steps = lambda: pytest.fail("keepalive must never re-warm the booking form-state")
    ops._do_keepalive("test")
    assert ops.session.navs and "Schedule.jsp" not in ops.session.navs[0], ops.session.navs


def test_keepalive_restore_rewarms_the_form_only_in_schedule_mode():
    """A dead-exit keepalive restore re-AUTHENTICATES either way, but it must re-warm the booking
    FORM only in schedule mode. auth_only defers warmup to the signal, so re-warming in the restore
    put a worker in WARMING_UP before slot_found/slot_open (CONFIRMED live 2026-10-02 'Booking w
    Auth': full warmup chain ~10 min before slot_found)."""
    # auth_only (default): re-auth only, NEVER re-warm the form
    ops = _make_ops(emit=lambda t, **p: None)
    ops._log = lambda *a, **k: None
    ops._try_restore_from_checkpoint = lambda: True
    ops._anchor_src = None                      # re-auth did not establish a verified chain anchor
    warm = []
    ops._run_warmup_steps = lambda: (warm.append(1), True)[1]
    ops._mark_keep_bookable = lambda *a, **k: None
    assert ops._attempt_keepalive_restore("test") is True
    assert warm == [], "auth_only restore must re-authenticate only, never re-warm the form"

    # schedule: re-warm so KEEPING holds a bookable cursor
    ops2 = _make_ops(cfg=_cfg_schedule(), emit=lambda t, **p: None)
    ops2._log = lambda *a, **k: None
    ops2._try_restore_from_checkpoint = lambda: True
    ops2._anchor_src = None
    warm2 = []
    ops2._run_warmup_steps = lambda: (warm2.append(1), True)[1]
    ops2._mark_keep_bookable = lambda *a, **k: None
    assert ops2._attempt_keepalive_restore("test") is True
    assert warm2 == [1], "schedule restore must re-warm the cursor to stay bookable"


def test_keepalive_still_detects_session_expired_and_restores():
    """Session-death detection is independent of form-state verification -- it never depended on
    it, and must keep working now that there is no form-state check at all."""
    ops = _keepalive_ops("<html>session gone</html>", warmed=True)
    ops.session = _RedirectingKeepaliveSession(
        "<html>session gone</html>", final_url="https://x/VistosOnline/sessionLost.jsp")
    ops._run_warmup_steps = lambda: pytest.fail("must not re-warm a dead session")
    restore_calls = []
    ops._attempt_keepalive_restore = lambda context="": restore_calls.append(context)
    ops._do_keepalive("test")
    assert restore_calls == ["test"]


class _NavRaises(_FakeSession):
    def __init__(self, err):
        super().__init__(); self._err = err
    def browser_nav(self, url, timeout=30, **k):
        raise RuntimeError(self._err)


def test_keepalive_stress_hold_holds_through_a_portal_down_window():
    """KEEPALIVE_STRESS_HOLD: a portal-reachability error (timeout) with the exit reachable is a
    down-window, not a dead session -- HOLD the warm session, never roll or re-login."""
    ops = _make_ops(cfg=_cfg_schedule(booking={"KEEPALIVE_STRESS_HOLD": True},
                                      template={"KEEPALIVE_DEAD_TRIP": 2}), emit=lambda t, **p: None)
    ops._log = lambda *a, **k: None
    ops._note_exit_ip = lambda when="": False
    ops.session = _NavRaises("net::ERR_TIMED_OUT at https://pedidodevistos.mne.gov.pt/")
    restores, rolls = [], []
    ops._attempt_keepalive_restore = lambda context="": restores.append(context)
    ops._roll_exit = lambda why: rolls.append(why)
    for _ in range(6):            # well past dead_trip(2) but under patience(4*2=8)
        ops._do_keepalive("test")
    assert restores == [], "stress-hold must not re-login during a portal-down window"
    assert rolls == [], "stress-hold must not roll the exit on a portal-down window"


def test_stress_hold_escalates_once_a_portal_down_persists_past_patience():
    """Bounded: a 'portal down' that persists ~4x dead_trip may really be a dying exit -> escalate."""
    ops = _make_ops(cfg=_cfg_schedule(booking={"KEEPALIVE_STRESS_HOLD": True},
                                      template={"KEEPALIVE_DEAD_TRIP": 2}), emit=lambda t, **p: None)
    ops._log = lambda *a, **k: None
    ops._note_exit_ip = lambda when="": False
    ops.session = _NavRaises("net::ERR_TIMED_OUT")
    restores = []
    ops._attempt_keepalive_restore = lambda context="": restores.append(context)
    for _ in range(12):           # patience = 8 holds, then the dead-trip ladder (another 2) restores
        ops._do_keepalive("test")
    assert restores, "a portal-down that never recovers must eventually escalate to restore"


def test_stress_hold_still_rolls_a_genuinely_dead_exit():
    """A proxy/tunnel/chrome-error is the EXIT dying, not the portal -- always escalates even with
    stress-hold on (it reaches dead_trip and restores)."""
    ops = _make_ops(cfg=_cfg_schedule(booking={"KEEPALIVE_STRESS_HOLD": True},
                                      template={"KEEPALIVE_DEAD_TRIP": 2}), emit=lambda t, **p: None)
    ops._log = lambda *a, **k: None
    ops._note_exit_ip = lambda when="": False
    ops.session = _NavRaises("net::ERR_TUNNEL_CONNECTION_FAILED")
    restores = []
    ops._attempt_keepalive_restore = lambda context="": restores.append(context)
    for _ in range(3):
        ops._do_keepalive("test")
    assert restores, "a dead-exit (tunnel) must still escalate under stress-hold"


def test_keepalive_still_reaches_dead_trip_on_transport_failure():
    """The transport-failure/dead-trip branch never read `warmed` and is unaffected by the
    form-state check being removed entirely."""
    cfg = _cfg(KEEPALIVE_DEAD_TRIP=2)
    ops = _make_ops(cfg=cfg, signals=_FakeSignals(timeouts=2, final="slot_open"))
    ops._signals.slot_found_seen = True

    class _DyingSession(_FakeSession):
        def browser_nav(self, url, timeout=30):
            raise RuntimeError("net::ERR_TUNNEL_CONNECTION_FAILED")

    ops.session = _DyingSession()
    restore_calls = []
    ops._attempt_keepalive_restore = lambda context="": restore_calls.append(context)
    ops.await_slot_open()
    assert restore_calls == ["await_slot_open"]


def test_keepalive_recovers_bookable_after_a_prior_not_bookable_marking():
    """Recovery clearing (_mark_keep_bookable(True)) must fire on a clean AUTHENTICATED read even
    though there is no more `warmed` concept -- a worker previously marked not-bookable (e.g. by
    dead-trip) must not stay stuck unbookable forever once its session is confirmed healthy again."""
    ops = _keepalive_ops("<html><body>solicitar pedido de visto ... consultar pedidos ... "
                         "terminar sessão</body></html>")
    ops._keep_unbookable = True
    bookable_calls = []
    ops._mark_keep_bookable = lambda ok, why="": bookable_calls.append(ok)
    ops._do_keepalive("test")
    assert bookable_calls == [True]


# ── KEEPALIVE_MODE='schedule' = ACTIVE cursor-keeping (operator 2026-10-02) ─────────────────────
# The worker holds a WARM, bookable Schedule.jsp session through KEEPING instead of auth-only: it
# probes Schedule.jsp and RE-WARMS the cursor before FORM_STATE_TTL. auth_only stays the default and
# must be completely unaffected (the tests above already pin its behaviour; the two below re-pin it
# against this change specifically).

def _cfg_schedule(template=None, booking=None):
    # Browser transport by default (see _cfg); a hybrid test passes HYBRID_MODE / HYBRID_<leg> in
    # `booking` to opt the relevant leg onto primp.
    layers = {L.TEMPLATE: {"ROLE": "apply", **(template or {})},
              L.BOOKING: {"KEEPALIVE_MODE": "schedule", "HYBRID_MODE": False, **(booking or {})}}
    return EngineConfig.from_settings(resolve(layers))


def _sched_keep_ops(page, *, warmed_fresh=False):
    ops = _make_ops(cfg=_cfg_schedule(), emit=lambda t, **p: None)
    ops.session = _KeepaliveSession(page)
    ops._log = lambda *a, **k: None
    ops.posto_pdf = "3018"
    if warmed_fresh:
        ops._set_anchor("chain")          # fresh chain anchor -> is_warmed() True
    return ops


def test_schedule_keepalive_probes_schedule_jsp_and_does_not_rewarm_a_fresh_cursor():
    ops = _sched_keep_ops("<html>cmbPeriodo getSlots</html>", warmed_fresh=True)
    ops._run_warmup_steps = lambda: pytest.fail("a live, fresh cursor must not be re-warmed")
    ops._do_keepalive("test")
    assert ops.session.navs and "Schedule.jsp" in ops.session.navs[0], ops.session.navs


def test_schedule_keepalive_rewarms_when_cursor_absent():
    ops = _sched_keep_ops("<html><title>E-VISA</title>no scheduling markers</html>")
    warm = []
    ops._run_warmup_steps = lambda: (warm.append(1), True)[1]
    bookable = []
    ops._mark_keep_bookable = lambda ok, why="": bookable.append(ok)
    ops._do_keepalive("test")
    assert warm == [1], "cursor absent -> must re-warm to keep a bookable session"
    assert bookable == [True]


def test_schedule_keepalive_rewarms_when_anchor_is_stale_even_if_markers_present():
    # markers present but no FRESH chain anchor -> is_warmed() False -> re-warm anyway
    ops = _sched_keep_ops("<html>cmbPeriodo</html>", warmed_fresh=False)
    warm = []
    ops._run_warmup_steps = lambda: (warm.append(1), True)[1]
    ops._do_keepalive("test")
    assert warm == [1]


def test_schedule_keepalive_rewarm_failure_marks_not_bookable_without_dropping_auth():
    ops = _sched_keep_ops("<html>no markers</html>")
    ops._run_warmup_steps = lambda: False
    restore = []
    ops._attempt_keepalive_restore = lambda context="": restore.append(context)
    bookable = []
    ops._mark_keep_bookable = lambda ok, why="": bookable.append(ok)
    ops._do_keepalive("test")
    assert bookable == [False], "a failed re-warm marks not-bookable (shows WARMING_UP)"
    assert restore == [], "a re-warm failure must NOT throw away the authenticated session"


def test_auth_only_mode_is_unaffected_by_the_schedule_change():
    ops = _keepalive_ops("<html>consultar pedidos terminar sessão</html>", warmed=True)
    ops._set_anchor("chain")
    ops._run_warmup_steps = lambda: pytest.fail("auth_only keepalive must never re-warm")
    ops._do_keepalive("test")
    assert "Schedule.jsp" not in ops.session.navs[0], ops.session.navs


def test_schedule_keepalive_window_paces_under_form_state_ttl():
    ops = _make_ops(cfg=_cfg_schedule(template={"KEEPALIVE_INTERVAL": 900.0,
                             "KEEPALIVE_JITTER": 30.0, "FORM_STATE_TTL": 240.0}))
    for _ in range(20):
        assert ops._keepalive_window() <= 180.0 + 1e-9   # cap = 0.75 * 240


def test_schedule_keepalive_ttl_fraction_tightens_the_cap():
    """SCHEDULE_KEEPALIVE_TTL_FRACTION (L.BOOKING) tunes the cadence: 0.5 caps the window at half the
    TTL (fresher cursor, more re-warms) vs the 0.75 default."""
    ops = _make_ops(cfg=_cfg_schedule(
        template={"KEEPALIVE_INTERVAL": 900.0, "KEEPALIVE_JITTER": 30.0, "FORM_STATE_TTL": 240.0},
        booking={"SCHEDULE_KEEPALIVE_TTL_FRACTION": 0.5}))
    for _ in range(20):
        assert ops._keepalive_window() <= 120.0 + 1e-9   # cap = 0.5 * 240


def test_schedule_keepalive_ttl_fraction_upper_clamp_keeps_it_under_the_ttl():
    """The catalog enforces the 0.2 floor on resolve (BelowMinimum); there is no max, so the code
    clamps an over-1 fraction to 0.95 -- the cadence must stay strictly UNDER the TTL or the cursor
    would lapse every cycle."""
    hi = _make_ops(cfg=_cfg_schedule(
        template={"KEEPALIVE_INTERVAL": 900.0, "KEEPALIVE_JITTER": 0.0, "FORM_STATE_TTL": 240.0},
        booking={"SCHEDULE_KEEPALIVE_TTL_FRACTION": 5.0}))
    assert hi._keepalive_window() <= 0.95 * 240.0 + 1e-9   # clamped to 0.95, still under the TTL


def test_schedule_keepalive_ttl_fraction_floor_is_enforced_by_the_resolver():
    from libs.domain.settings.resolver import BelowMinimum
    with pytest.raises(BelowMinimum):
        _cfg_schedule(template={"FORM_STATE_TTL": 240.0},
                      booking={"SCHEDULE_KEEPALIVE_TTL_FRACTION": 0.01})


def test_auth_only_keepalive_window_is_the_full_interval():
    ops = _make_ops(cfg=_cfg(KEEPALIVE_INTERVAL=900.0, KEEPALIVE_JITTER=0.0))
    assert ops._keepalive_window() >= 900.0


def test_initial_schedule_warm_fires_once_when_not_warmed():
    ops = _make_ops(cfg=_cfg_schedule())
    ops.posto_pdf = None                      # not warmed
    calls = []
    ops._do_keepalive = lambda ctx: calls.append(ctx)
    ops._initial_schedule_warm("await_slot_open")
    assert calls == ["await_slot_open/initial"]


def test_initial_schedule_warm_is_a_noop_in_auth_only():
    ops = _make_ops(cfg=_cfg())
    ops._do_keepalive = lambda ctx: pytest.fail("auth_only must not warm on KEEPING entry")
    ops._initial_schedule_warm("await_slot_open")


def test_initial_schedule_warm_skips_when_already_warmed():
    ops = _make_ops(cfg=_cfg_schedule())
    ops.posto_pdf = "3018"
    ops._set_anchor("chain")                  # fresh -> is_warmed() True
    ops._do_keepalive = lambda ctx: pytest.fail("already warmed -> no initial warm")
    ops._initial_schedule_warm("await_slot_open")


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


# ── is_warmed() must check anchor PROVENANCE (confirmed live 2026-09-11, booking 0911) ────────
# posto_pdf has two writers meaning opposite things: _run_warmup_steps sets it from a freshly
# parsed Schedule.jsp (_set_anchor("chain") -- verified), a checkpoint restore sets it from a saved
# file (_set_anchor("checkpoint") -- never verified). is_warmed() shipped checking only
# `posto_pdf is not None`, so a checkpoint-restored worker reported warmed, skipped the deferred
# warmup, and hit /slots with a stale anchor. Live result: 26 workers entered DISCOVERING, only 3
# had ever been WARMED, and every discovery round died on "onCaptchaSuccess never appeared".
def test_is_warmed_is_true_only_for_a_verified_chain_anchor():
    ops = _make_ops(emit=lambda t, **p: None)
    assert ops.is_warmed() is False               # nothing established yet

    ops.posto_pdf = "5084"
    ops._set_anchor("chain")                      # the real questionnaire chain ran
    assert ops.is_warmed() is True


def test_is_warmed_rejects_an_unverified_checkpoint_anchor():
    ops = _make_ops(emit=lambda t, **p: None)
    ops.posto_pdf = "5084"                        # restored from a saved checkpoint file...
    ops._set_anchor("checkpoint")                 # ...never verified against the portal
    assert ops.is_warmed() is False, (
        "a checkpoint anchor is as old as the file -- reporting it as warmed makes the deferred "
        "warmup skip, and discovery then hits /slots with no captcha widget rendered")


def test_is_warmed_goes_false_once_the_anchor_is_cleared():
    ops = _make_ops(emit=lambda t, **p: None)
    ops.posto_pdf = "5084"
    ops._set_anchor("chain")
    ops._clear_anchor()
    assert ops.is_warmed() is False


def test_is_warmed_goes_false_once_the_chain_anchor_ages_past_the_ttl():
    """FAILURE_MODES F28's remedy spells out the full invariant: chain provenance AND age under the
    form-state TTL. Provenance alone is not enough."""
    ops = _make_ops(cfg=_cfg(FORM_STATE_TTL=60.0), emit=lambda t, **p: None)
    ops.posto_pdf = "5084"
    ops._set_anchor("chain")
    assert ops.is_warmed() is True                 # fresh
    ops._anchor_at = time.time() - 61.0            # aged past the TTL
    assert ops.is_warmed() is False


def test_is_warmed_rejects_a_stale_chain_anchor_left_by_a_FAILED_rewarm():
    """The exact case F28 warns about: 'a failed re-warm never reaches _set_anchor, so _anchor_src
    retains the previous *successful* warm's value.' Provenance still says "chain" -- only the age
    reveals that the anchor belongs to a warm that happened long ago."""
    ops = _make_ops(cfg=_cfg(FORM_STATE_TTL=120.0), emit=lambda t, **p: None)
    ops.posto_pdf = "5084"
    ops._set_anchor("chain")                       # an earlier warm DID succeed
    ops._anchor_at = time.time() - 3600.0          # ...an hour ago; every re-warm since failed
    assert ops._anchor_src == "chain"              # provenance is unchanged by the failures
    assert ops.is_warmed() is False, "age is what catches a stale anchor a failed re-warm left behind"


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


# ── portal-refusal detection (the fleet breaker that used to consume it was removed 2026-09-23) ──
class _RecordingLiveness:
    """Stand-in for PortalLiveness: records the two facts workers report, never gates."""
    def __init__(self): self.alive = 0; self.down = 0; self.down_accts = []; self.attempts = 0
    def record_alive(self): self.alive += 1
    def record_down(self, sub_account_id=""): self.down += 1; self.down_accts.append(sub_account_id)
    def record_login_attempt(self): self.attempts += 1
    def state(self): return {"state": "down" if self.down else "alive"}


def test_the_portals_try_again_later_page_is_detected():
    """The throttle page detection is unchanged; only what consumes it changed (it no longer feeds a
    fleet breaker -- that was removed -- it is handled per-worker with exit rolls)."""
    from apps.worker.ops_live import _is_portal_busy

    assert _is_portal_busy("Não foi possível de momento concretizar o seu pedido")
    assert _is_portal_busy("We were unable to complete your request at this time")


def test_a_credential_rejection_is_not_a_portal_refusal():
    """Guard rail: a verdict about one account (bad creds, reCAPTCHA, a WAF challenge) is NOT the
    portal's 'try again later' refusal."""
    from apps.worker.ops_live import _is_portal_busy

    assert _is_portal_busy("login_failed_auth_redirect") is False
    assert _is_portal_busy("Por favor, confirma o reCAPTCHA.") is False
    assert _is_portal_busy("datadome_challenge_on_post") is False


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
    refusals across 13 sub-accounts and 99 rotations. SPLIT OUT of _is_portal_busy 2026-09-23: it is
    now its OWN classifier (_is_maintenance) because it routes differently -- it trips the fleet
    breaker fast, while the ambiguous 'try again later' throttle needs distinct-account evidence. It
    must still be recognised as a refusal overall (_is_portal_refusal), but must NOT be mistaken for
    the throttle (_is_portal_busy).
    """
    from apps.worker.ops_live import _is_portal_busy, _is_maintenance, _is_portal_refusal
    for page in ("Serviço em manutenção. Tente novamente mais tarde.",
                 "SERVICO EM MANUTENCAO",                       # accent-stripped form
                 "<html>Service under maintenance</html>",
                 "Serviço Indisponível / Unavailable"):         # the observed screenshot title
        assert _is_maintenance(page)
        assert _is_portal_refusal(page)                         # still a refusal overall
        assert not _is_portal_busy(page)                        # but NOT the throttle
    # and it must not fire on an ordinary page
    assert not _is_maintenance("<html>Solicitar pedido de Visto</html>")
    assert not _is_portal_refusal("<html>Solicitar pedido de Visto</html>")


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


def test_a_booked_submit_captures_a_booked_body_never_a_rejection(tmp_path):
    """A booking now ALSO captures its confirmation HTML -- it carries the portal's appointment
    reference (BookedAppointment.appointment_ref, previously never populated) as parseable text.
    It must be labeled 'booked', NEVER as a rejection/bounce/stale body (the original invariant:
    a success must not be mis-recorded as a rejection)."""
    events = []
    ops = _apply_ops(b"%PDF-1.4 real booking", events)
    calls = []
    ops._save_submit_body = lambda html, outcome: calls.append(outcome) or "/x.html"
    ops._save_pdf = lambda b: "/tmp/x.pdf"

    assert ops.do_apply({"date": "2026-09-01", "period": 4}) == "booked"
    assert calls == ["booked"], "a booking captures its confirmation body, labeled 'booked'"
    assert all(c not in ("bounced", "no_slot", "session") and "stale" not in c for c in calls)


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


def test_the_apply_round_does_not_post_slots_before_the_submit():
    """Experiment C REMOVED 2026-09-26. Live measurement retired its premise: the apply-time /slots
    returned ReCaptchaError every round (activated nothing), added ~20-40s of latency, and a real
    booking landed without it -- pure cost on the round, and apply latency is what loses slot races.
    do_apply must NOT call _discover_via_slots; the submit stands on the round-start warm-up alone."""
    ops = _apply_ops(b"%PDF-1.4 real", [])
    ops.session = _UrlRecordingSession(b"%PDF-1.4 real")
    ops._save_pdf = lambda b: "/tmp/x.pdf"
    activated = {"n": 0}
    ops._discover_via_slots = lambda dates: (activated.__setitem__("n", activated["n"] + 1), [])[1]

    ops.do_apply({"date": "2026-09-01", "period": 4})

    assert activated["n"] == 0, "do_apply must NOT post /slots before the submit (Experiment C removed)"


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


class _SlotsSlipSession(_SlotsPageSession):
    """Models the registrar failure mode in the slots path: the callback IS present at ready-check
    time, then the browser page slips off-origin DURING the ~solve gap, so the FIRST token delivery
    finds `onCaptchaSuccess` gone. A single re-anchor (browser_nav back to the slots page) brings the
    widget back and the SAME token is re-delivered successfully."""
    def __init__(self, payload):
        super().__init__(payload)
        self.injects = 0
        self.navs = 0

    def browser_nav(self, url, timeout=30, **kw):
        self.navs += 1
        return url

    def browser_eval(self, js, timeout=30):
        self.evals.append(js)
        if "onCaptchaSuccess === 'function'" in js and "window.data" not in js:
            return True                       # the widget is (re)registered
        if "onCaptchaSuccess" in js and "window.data" in js:
            self.injects += 1
            if self.injects == 1:
                return {"err": "onCaptchaSuccess not defined"}   # slipped mid-solve
            return self.payload               # re-anchored -> token re-delivered
        if "location.origin" in (js or ""):
            return _origin_reply(js)
        return ""


def test_slots_reanchors_and_redelivers_when_callback_slips_mid_solve(monkeypatch):
    monkeypatch.setattr("libs.engine.vendor.race_all", lambda *a, **k: "T" * 2617)
    ops = _make_ops(emit=lambda t, **p: None)
    ops.session = _SlotsSlipSession(
        {"ok": True, "data": [{"date": "2026-09-14", "periods": [3, 5]}]})
    ops._log = lambda *a, **k: None
    ops.posto_pdf = "3059"
    ops.cfg.raw["DISCOVERY_SOURCE"] = "slots"
    object.__setattr__(ops.cfg.discovery, "source", "slots")
    out = {r["date"]: r for r in ops.do_discover(["2026-09-14"])}
    assert out["2026-09-14"]["outcome"] == "found", "the paid solve must survive one page slip"
    assert out["2026-09-14"]["periods"] == [3, 5]
    assert ops.session.injects == 2, "the SAME token is re-delivered after the re-anchor"
    assert ops.session.navs >= 2, "a re-anchor nav back to the slots page must have happened"


def test_slots_reports_failed_when_reanchor_cannot_restore_the_callback(monkeypatch):
    """If the callback never comes back after the re-anchor, the exit page is gone (not just the
    origin) -- do not loop; report failed and let the existing roll ladder escalate."""
    monkeypatch.setattr("libs.engine.vendor.race_all", lambda *a, **k: "T" * 2617)
    ops = _make_ops(emit=lambda t, **p: None)

    class _NeverRecovers(_SlotsPageSession):
        def __init__(self, payload):
            super().__init__(payload)
            self.ready_calls = 0

        def browser_nav(self, url, timeout=30, **kw):
            return url

        def browser_eval(self, js, timeout=30):
            self.evals.append(js)
            if "onCaptchaSuccess === 'function'" in js and "window.data" not in js:
                self.ready_calls += 1
                return self.ready_calls == 1   # ready first time, gone after the re-anchor
            if "onCaptchaSuccess" in js and "window.data" in js:
                return {"err": "onCaptchaSuccess not defined"}
            if "location.origin" in (js or ""):
                return _origin_reply(js)
            return ""

    ops.session = _NeverRecovers({"ok": True, "data": []})
    ops._log = lambda *a, **k: None
    ops.posto_pdf = "3059"
    ops.cfg.raw["DISCOVERY_SOURCE"] = "slots"
    object.__setattr__(ops.cfg.discovery, "source", "slots")
    out = ops.do_discover(["2026-09-14"])
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


def test_both_falls_back_to_calendar_when_slots_reports_nothing(monkeypatch):
    """Confirmed live 2026-09-21: a RATE-LIMITED /slots round returns `_no_report()` -> [], which
    carries no per-date `failed` outcomes. The old backfill keyed only on `outcome == 'failed'`, so
    an empty round left `unresolved` empty and calendar NEVER ran -- 'both' silently degraded to
    'report nothing' exactly when /slots was down and calendar was the reason for choosing 'both'.
    Now an empty (or partial) /slots round backfills every un-answered date from calendar."""
    ops = _slots_ops({"ok": True, "data": []}, "both", monkeypatch)
    ops._discover_via_slots = lambda dates: []          # rate-limited / blocked / no-token
    seen = []
    ops._discover_via_calendar = lambda dates: seen.append(list(dates)) or [
        {"date": d, "outcome": "found", "periods": [5]} for d in dates]
    out = {r["date"]: r["outcome"] for r in ops.do_discover(["2026-09-14", "2026-09-15"])}
    assert seen == [["2026-09-14", "2026-09-15"]], f"calendar must cover ALL dates: {seen}"
    assert out == {"2026-09-14": "found", "2026-09-15": "found"}


# ── getTables WAF handshake (ref-confirmed, 2026-09-23) ───────────────────────────────────────
class _GtSession(_FakeSession):
    """A session that records init scripts and answers the __gt_ok poll with a configurable result,
    modelling the getTables hook flagging (or never flagging) window.__gt_ok."""
    def __init__(self, gt_ok):
        super().__init__()
        self._gt_ok = gt_ok
        self.init_scripts = []
        self.navs = []

    def browser_add_init_script(self, js, timeout=15):
        self.init_scripts.append(js)
        return True

    def browser_nav(self, url, timeout=30, **kw):
        self.navs.append(url)
        return url

    def browser_eval(self, js, timeout=30):
        if "__gt_ok" in (js or ""):
            return self._gt_ok
        if "location.origin" in (js or ""):
            return _origin_reply(js)
        return ""


def test_gettables_hook_installed_once_and_reinstalled_on_new_session():
    ops = _make_ops(emit=lambda t, **p: None); ops._log = lambda *a, **k: None
    s1 = _GtSession(True); ops.session = s1
    ops._ensure_gettables_hook(); ops._ensure_gettables_hook()   # twice
    assert len(s1.init_scripts) == 1, "the getTables hook must be installed exactly once per session"
    assert "getTables" in s1.init_scripts[0] and "__gt_ok" in s1.init_scripts[0]
    # a rebuilt session (new object) must get the hook re-installed
    s2 = _GtSession(True); ops.session = s2
    ops._ensure_gettables_hook()
    assert len(s2.init_scripts) == 1


def test_gettables_confirmed_marks_posto_and_proceeds():
    ops = _make_ops(emit=lambda t, **p: None); ops._log = lambda *a, **k: None
    ops.session = _GtSession(True)
    assert ops._posto_gettables is None
    ops._await_gettables()
    assert ops._posto_gettables is True   # this posto fires getTables


def test_gettables_absent_is_remembered_and_not_waited_again(monkeypatch):
    """POSTO-OPTIONAL (ref: posto 5084 has no getTables but serves slots). A first miss is remembered
    as 'this posto doesn't use it', and later rounds must NOT poll again (no repeated 8s wait)."""
    ops = _make_ops(emit=lambda t, **p: None); ops._log = lambda *a, **k: None
    s = _GtSession(False); ops.session = s
    polls = {"n": 0}
    _orig = s.browser_eval
    def _count(js, timeout=30):
        if "__gt_ok" in (js or ""): polls["n"] += 1
        return _orig(js, timeout)
    s.browser_eval = _count
    ops._await_gettables()                     # first round: polls, times out -> remembers False
    assert ops._posto_gettables is False and polls["n"] == 1
    ops._await_gettables()                     # second round: must short-circuit, no poll
    assert polls["n"] == 1


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
    ops._backoff_seconds = lambda s: None
    ops._request_proxy_rotation = lambda phase=None: None
    ops._liveness = _RecordingLiveness()
    rolls = []
    ops._roll_exit = lambda why="": rolls.append(why) or True
    return ops, rolls


def _busy_rolls(rolls):
    """Only the rolls the PORTAL-BUSY budget is responsible for. Since 2026-09-15 every login retry
    round past the first also rolls the exit locally, so counting every _roll_exit call would
    conflate the two. What LOGIN_PORTAL_BUSY_ROLLS bounds is how many different exits a 'try again
    later' throttle tries before giving up -- now a purely per-worker action (the fleet breaker that
    used to consume it was removed 2026-09-23)."""
    return [w for w in rolls if str(w).startswith("portal busy")]


def test_a_busy_page_rolls_the_exit_and_touches_no_fleet_gate(monkeypatch):
    calls = {"n": 0}
    ops, rolls = _busy_login_ops(monkeypatch, calls, busy_rolls=3)

    ops._sticky_login_attempts(ops.cfg.login.sticky_retry_max)

    assert len(_busy_rolls(rolls)) == 3, \
        f"the refusal was blamed on the portal without trying another exit: {rolls}"
    assert ops._liveness.down == 0, \
        "a login throttle must NOT mark the portal down -- it says nothing about portal liveness"


def test_a_busy_page_is_not_charged_to_the_sub_account_while_rolling(monkeypatch):
    """It is not a verdict on the credentials, so it belongs beside `transient`/`ip_moves`, above
    the `attempt += 1`. Charged too early it would burn the sticky budget and rotate accounts over
    an exit problem."""
    calls = {"n": 0}
    ops, _ = _busy_login_ops(monkeypatch, calls, busy_rolls=3, sticky=2)

    ops._sticky_login_attempts(ops.cfg.login.sticky_retry_max)

    assert calls["n"] == 5, \
        f"expected 3 uncharged rolls + 2 charged sticky attempts, got {calls['n']}"


def test_a_busy_budget_of_zero_rolls_nothing_and_still_marks_no_portal_down(monkeypatch):
    """With the roll budget at 0 the throttle just counts as the attempt -- and it never marks the
    portal down (that would be false: a throttled account is refused from every exit while others
    log in fine). The fleet-wide backoff this used to trigger was removed 2026-09-23."""
    calls = {"n": 0}
    ops, rolls = _busy_login_ops(monkeypatch, calls, busy_rolls=0)

    ops._sticky_login_attempts(ops.cfg.login.sticky_retry_max)

    assert _busy_rolls(rolls) == [], f"with the budget at 0 no BUSY roll should be attempted (got {rolls})"
    assert ops._liveness.down == 0, "a throttle must never mark the portal down"


_MAINT_BODY = ('{"type":"error","description":"Servi\\u00e7o em manuten\\u00e7\\u00e3o. '
              'Tente novamente mais tarde."}')


def test_the_maintenance_page_marks_the_portal_down_and_does_NOT_roll(monkeypatch):
    """Unlike the throttle, the maintenance page is down-for-everyone: rolling exits or rotating
    sub-accounts against it only burns the pool (booking 'Test 2032': 154 refusals, 13 accounts).
    So the login path must NOT roll on it, must NOT spend a sticky attempt, must mark the portal
    DOWN on PortalLiveness (for the dashboard) and back this worker off on its own."""
    import apps.worker.ops_live as ops_live_mod

    calls = {"n": 0}
    class _AlwaysMaint(_FakeSession):
        def browser_login(self, *a, **k):
            calls["n"] += 1
            return {"status": 200, "body": _MAINT_BODY}
    monkeypatch.setattr(ops_live_mod, "get_session", lambda **kw: _AlwaysMaint())
    ops = _make_ops(cfg=_cfg(STICKY_RETRY_MAX=2, LOGIN_TRANSIENT_RETRY_MAX=0, LOGIN_PORTAL_BUSY_ROLLS=3))
    ops._backoff = lambda n: None
    backoffs = []
    ops._backoff_seconds = lambda s: backoffs.append(s)
    ops._request_proxy_rotation = lambda phase=None: None
    ops._liveness = _RecordingLiveness()
    rolls = []
    ops._roll_exit = lambda why="": rolls.append(why) or True

    ok = ops._sticky_login_attempts(ops.cfg.login.sticky_retry_max)
    assert ok is False
    assert calls["n"] == 1, f"maintenance must stop after ONE attempt, not grind (got {calls['n']})"
    assert rolls == [], f"must not roll the exit against a down portal (got {rolls})"
    assert ops._liveness.down == 1, "maintenance page must mark the portal DOWN on PortalLiveness"
    assert backoffs and backoffs[0] > 0, "the worker must back off on its own after maintenance"
    assert ops._saw_maintenance is True, "the flag that makes _fresh_login abandon rotation"


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


# ── HOLDING must stop meaning "ready" while the session is being rebuilt (2026-09-02) ─────────
# _assign_slots selects on `substate == HOLDING` and the NOT_BOOKABLE marker, nothing else. The
# keepalive restore closes the browser and runs a full login while the substate stays HOLDING --
# correctly, the worker IS still holding -- so the Hub kept counting it as ready capacity.
# CONFIRMED LIVE on the 20:45 run of "Test 0901": the pool's ONLY available slot (2026-09-30 p5)
# was leased at 22:22:04 to a worker inside that chain, which did not reach `applying` until
# 22:24:41 and then released it as `session`. The other lease went to a worker whose tunnel was
# dead. One slot, two workers, neither able to act, locked for three minutes.

def _marks(ops):
    """Every (bookable, why) the ops published, in order."""
    return [(p.get("detail") == "", p.get("detail")) for p in ops._emitted
            if p.get("_type") == "status_change"]


class _MarkRecordingOps:
    """Captures _mark_keep_bookable calls without needing the whole emit pipeline."""
    def __init__(self, ops):
        self.ops = ops
        self.calls = []
        ops._mark_keep_bookable = lambda bookable, why="": self.calls.append((bookable, why))


def test_a_session_rebuild_marks_the_worker_unbookable_for_its_duration(monkeypatch):
    ops = _make_ops(emit=lambda t, **p: None)
    ops._log = lambda *a, **k: None
    rec = _MarkRecordingOps(ops)
    ops.session = None
    monkeypatch.setattr(ops, "_try_restore_from_checkpoint", lambda: False)
    monkeypatch.setattr(ops, "_fresh_login", lambda: False)

    ops._attempt_keepalive_restore("test")

    assert rec.calls and rec.calls[0][0] is False, \
        f"the rebuild never marked the worker unbookable, so the Hub can still assign it: {rec.calls}"
    assert not any(b for b, _ in rec.calls), \
        "a FAILED rebuild must not leave the worker advertising itself as bookable"


def test_a_successful_rebuild_clears_the_marker(monkeypatch):
    """It must not become a one-way latch -- a permanently unbookable holding worker is invisible
    capacity loss, which is worse than the bug being fixed."""
    ops = _make_ops(emit=lambda t, **p: None)
    ops._log = lambda *a, **k: None
    rec = _MarkRecordingOps(ops)
    ops.session = None
    monkeypatch.setattr(ops, "_try_restore_from_checkpoint", lambda: True)
    monkeypatch.setattr(ops, "_run_warmup_steps", lambda: True)
    ops._anchor_src = "chain"

    ops._attempt_keepalive_restore("test")

    assert rec.calls[0][0] is False and rec.calls[-1][0] is True, \
        f"expected unbookable during, bookable after: {rec.calls}"


def test_the_marker_is_tracked_so_recovery_can_clear_whatever_set_it():
    """The clear used to be gated on `_rewarm_fail_streak > 0`, which was only true when the
    RE-WARM branch had marked. The dead-trip branch leaves that streak at 0, so a recovered worker
    would have stayed unbookable forever."""
    ops = _make_ops(emit=lambda t, **p: None)
    ops._mark_keep_bookable(False, "dead trip")
    assert ops._keep_unbookable is True
    ops._mark_keep_bookable(True)
    assert ops._keep_unbookable is False


# ── the two defects introduced by the pacing fix (2026-09-02) ─────────────────────────────────
def test_a_paced_out_round_waits_instead_of_spinning(monkeypatch):
    """Returning instantly cost nothing, so the discovery loop span at poll_gap speed doing no work
    -- 1,566 status changes in four hours, almost all DISCOVERING <-> POLLING flapping."""
    import time as _t
    sess = _LateCallbackSession(ready=True)
    monkeypatch.setattr("libs.engine.vendor.race_all", lambda *a, **k: "T" * 2617)
    ops = _callback_ops(sess)
    ops._SLOTS_MAX_PACING_SLEEP = 0.30
    ops._slots_last_grant_at = _t.time()          # a grant just landed: fully paced out

    t0 = _t.time()
    out = ops._discover_via_slots(["2026-09-14"])
    elapsed = _t.time() - t0

    assert out == []
    assert elapsed >= 0.25, f"returned in {elapsed:.2f}s -- it did not wait at all"


def test_the_pacing_wait_is_capped_and_stops_on_stop(monkeypatch):
    """This path also runs on an APPLY worker that is self-scouting, which must stay able to act on
    a real assignment -- so the wait is capped, and a stop ends it immediately."""
    import time as _t
    sess = _LateCallbackSession(ready=True)
    monkeypatch.setattr("libs.engine.vendor.race_all", lambda *a, **k: "T" * 2617)
    ops = _callback_ops(sess)
    ops._SLOTS_MAX_PACING_SLEEP = 30.0
    ops._slots_last_grant_at = _t.time()
    ops.is_stopped = lambda: True                  # operator stopped us mid-wait

    t0 = _t.time()
    ops._discover_via_slots(["2026-09-14"])
    assert _t.time() - t0 < 2.0, "a stopped worker sat through the whole pacing wait"


# ── host-wide pool concurrency gate (2026-09-15) ────────────────────────────
class _Sem:
    """Recording stand-in for RedisSemaphore -- enough surface for do_login's two gates."""
    def __init__(self, key="sem:test", grant=True):
        self.key, self.grant = key, grant
        self.acquired, self.released = [], []

    def acquire(self, holder_id, timeout=None):
        self.acquired.append(holder_id)
        return self.grant

    def release(self, holder_id):
        self.released.append(holder_id)


def _login_ops():
    ops = _make_ops(emit=lambda t, **p: None)
    ops._log = lambda *a, **k: None
    ops._try_restore_from_checkpoint = lambda: False
    ops._fresh_login = lambda: True
    return ops


def test_do_login_takes_and_releases_the_pool_gate():
    ops = _login_ops()
    ops._login_sem, ops._pool_sem = _Sem("sem:login:bk"), _Sem("sem:pool:soax:rotating")
    assert ops.do_login() is True
    assert ops._pool_sem.acquired == [ops._pool_holder_id()]
    assert ops._pool_sem.released == [ops._pool_holder_id()]


def test_a_full_pool_gate_still_releases_the_login_slot():
    """The failure mode this guards: an early `return False` between the two acquires would leak a
    per-booking login slot on every refusal. The gate is busiest exactly when the fleet is largest,
    so a leak here would ratchet LOGIN_CONCURRENCY down to zero and stall the booking outright."""
    ops = _login_ops()
    ops._login_sem = _Sem("sem:login:bk")
    ops._pool_sem = _Sem("sem:pool:soax:rotating", grant=False)
    assert ops.do_login() is False
    assert ops._login_sem.released == [ops.identity_id], "login slot leaked when the pool gate was full"
    assert ops._pool_sem.released == [], "never acquired, so must not be released"


def test_the_pool_holder_id_is_unique_across_bookings():
    """identity_id alone is not host-wide-unique: one identity can be assigned to several bookings,
    and to a host-wide sorted set those two workers would look like ONE holder -- the second would
    slip past a full gate and the first to finish would release the other's slot."""
    a, b = _login_ops(), _login_ops()
    a.booking_id, b.booking_id = "bk-1", "bk-2"
    a.identity_id = b.identity_id = "same-identity"
    assert a._pool_holder_id() != b._pool_holder_id()


def test_the_pool_gate_is_optional():
    """Older/simpler constructions (and every test double) carry no pool gate; do_login must not
    require one. PROVIDER_LOGIN_CONCURRENCY=0 is the operator's documented way to opt out."""
    ops = _login_ops()
    ops._login_sem, ops._pool_sem = _Sem(), None
    assert ops.do_login() is True


# ── do_swap probes AUTH, not form-state (booking Test 0916, 2026-09-16) ─────────────────────
_HOME_AUTHED = "<html>solicitar pedido de visto ... consultar pedidos</html>"
_SCHED_PAGE = "<html>cmbPeriodo getPeriodosOcupados</html>"


class _SwapSession:
    """Serves the page the portal really would at swap time: authenticated home, UNWARMED.

    Schedule.jsp carries none of the form markers because warmup has not run -- that is the state
    the machine guarantees between LOGGED_IN and KEEPING since warmup was deferred (2026-09-09).
    """
    def __init__(self):
        self.navs = []

    def browser_nav(self, url, timeout=None):
        self.navs.append(url)
        return url

    def browser_eval(self, _js):
        last = self.navs[-1]
        return _SCHED_PAGE if "Schedule.jsp" in last else _HOME_AUTHED

    def close_browser(self):
        pass


def test_the_swap_probe_accepts_an_authenticated_but_unwarmed_session():
    """THE REGRESSION. do_swap ran `_probe_alive`, which navigates Schedule.jsp and demands
    cmbPeriodo/getPeriodosOcupados -- markers warmup creates. do_swap runs BEFORE warmup, so the
    check could never pass: measured 20/20 failures on Test 0916 (5 workers x 4 attempts), each
    costing a 600s backoff, ~40 min per worker parked at LOGGED_IN. The three appointments that run
    booked went out over the phase-1 rotating proxy; the leased ISP exits never carried a session.

    The probe must judge AUTH on the home page, exactly as the auth-only keepalive does."""
    ops = _make_ops(emit=lambda t, **p: None)
    ops._log = lambda *a, **k: None
    ops.session = _SwapSession()

    assert ops._probe_swapped_session() is True
    assert all("Schedule.jsp" not in u for u in ops.session.navs), (
        "the swap probe must not depend on form-state that warmup has not built yet")


def test_the_swap_probe_rejects_an_expired_session():
    """The guard still has to work: a session that did NOT survive the move to the hold exit must
    revert to phase-1 rather than silently hold on an exit we have no evidence works."""
    ops = _make_ops(emit=lambda t, **p: None)
    ops._log = lambda *a, **k: None

    class _Expired(_SwapSession):
        def browser_nav(self, url, timeout=None):
            self.navs.append(url)
            return "https://pedidodevistos.mne.gov.pt/VistosOnline/Authentication.jsp"

    ops.session = _Expired()
    assert ops._probe_swapped_session() is False


def test_probe_alive_is_unchanged_for_the_checkpoint_restore_caller():
    """_probe_alive's OTHER caller restores posto_pdf from a checkpoint and therefore legitimately
    expects a warmed session. Fixing do_swap must not weaken that one."""
    ops = _make_ops(emit=lambda t, **p: None)
    ops._log = lambda *a, **k: None
    ops.session = _SwapSession()

    assert ops._probe_alive() is True
    assert any("Schedule.jsp" in u for u in ops.session.navs)


class _UnwarmedSession:
    """An AUTHENTICATED but UNWARMED session -- the only state do_swap ever actually runs in.

    The home page carries both home markers (so classify_session_state(warmed=False) reads
    AUTHENTICATED); Schedule.jsp carries NO form markers, because warmup has not run.

    This is the double the existing `_FakeSession` should have been: that one returns
    "<html>cmbPeriodo</html>" for EVERY url, i.e. a session that is always warmed, which is why
    every do_swap test passed for months while the real swap failed 20/20 in production.
    """
    def __init__(self):
        self.navs = []
        self.closed = False

    def get_all_cookies(self):
        return [{"name": "Vistos_sid", "value": "abc"}]

    def browser_nav(self, url, timeout=30):
        self.navs.append(url)
        return url

    def browser_eval(self, js):
        if "location.origin" in (js or ""):
            return _origin_reply(js)
        if "Schedule.jsp" in (self.navs[-1] if self.navs else ""):
            return "<html>nothing useful here yet</html>"     # unwarmed: no form markers
        return "<html>solicitar pedido de visto ... consultar pedidos</html>"

    def close_browser(self):
        self.closed = True


def test_do_swap_succeeds_on_an_unwarmed_session(monkeypatch):
    """THE SEAM TEST. Asserts do_swap itself completes against an authenticated-but-unwarmed
    session -- not that the probe works in isolation, which says nothing about whether do_swap
    calls it.

    Against the previous probe (`_probe_alive`, which navigates Schedule.jsp and requires
    cmbPeriodo/getPeriodosOcupados) this returns None on every attempt. Production measured exactly
    that on booking Test 0916: 20/20 failures, 5 workers x 4 attempts, ~40 min per worker parked at
    LOGGED_IN, and the three appointments booked went out over the phase-1 rotating proxy while the
    leased ISP exits never carried a session."""
    import apps.worker.ops_live as ops_live_mod

    monkeypatch.setattr(ops_live_mod, "get_session", lambda **kw: _UnwarmedSession())
    ops = _make_ops(cfg=_cfg(HOLD_SWAP_AFTER_LOGIN=True, HOLD_SWAP_RETRIES=0,
                             HOLD_SWAP_RETRY_HOLD_SECS=0.0),
                    hold_proxy="http://u:p@isp.example:61234",
                    hold_proxy_static=True, emit=lambda t, **p: None)
    ops._log = lambda *a, **k: None
    ops.session = _UnwarmedSession()

    result = ops.do_swap()

    assert result is not None, (
        "do_swap rejected a healthy authenticated session because it was not WARMED -- "
        "warmup is deferred to slot-open/slot-found, so it never can be at this point")


# ── phantom-slot guard (operator-confirmed rule, Booking 0918, 2026-09-18) ──────────────────
def _phantom_ops(**raw):
    ops = _make_ops(emit=lambda t, **p: None)
    ops._log = lambda *a, **k: None
    ops.cfg.raw.update(raw)
    return ops


def _found(d, periods, raw="12"):
    return {"date": d, "outcome": "found", "periods": list(periods), "raw": raw}


def test_a_uniform_thin_round_is_reported_as_unknown_not_found():
    """REPRODUCES BOOKING 0918. One scout found {3,4} on 10-14 and {3,4} on 10-15 while the
    portal's own calendar showed the 15th unavailable. Both fired slot_found; three apply workers
    leased them, re-warmed into a 503, and were escalated to re-login -- good sessions and reCAPTCHA
    strikes spent on slots that did not exist.

    The verdict is 'failed' (unknown), never 'empty': an unreadable round must not retire dates."""
    ops = _phantom_ops()
    out = ops._reject_phantom_round([_found("2026-10-14", [3, 4]), _found("2026-10-15", [3, 4]),
                                     {"date": "2026-10-16", "outcome": "empty"}])
    assert [o["outcome"] for o in out] == ["failed", "failed", "empty"]
    assert all("periods" not in o for o in out if o["outcome"] == "failed")


def test_a_real_open_answers_each_date_differently_and_is_kept():
    """Uniformity is the load-bearing half of the rule. Per-date variation is what a real
    getPeriodosOcupados answer looks like; only the portal's fallback template answers every date
    the same. Two thin-but-different dates must pass."""
    ops = _phantom_ops()
    out = ops._reject_phantom_round([_found("2026-10-14", [3, 4]), _found("2026-10-15", [3, 5])])
    assert [o["outcome"] for o in out] == ["found", "found"]


def test_a_uniform_but_generous_round_is_kept():
    """The size bound: three dates all showing six periods is a genuinely uniform open, not a
    template. Test 0916 booked real appointments on dates that all showed {3,4,5}."""
    ops = _phantom_ops()
    out = ops._reject_phantom_round([_found(f"2026-10-{d}", [3, 4, 5]) for d in (13, 14, 15)])
    assert all(o["outcome"] == "found" for o in out)


def test_a_single_thin_find_is_not_enough_to_call_phantom():
    """Uniformity across one date is not evidence of anything -- PHANTOM_MIN_DATES defaults to 2."""
    ops = _phantom_ops()
    out = ops._reject_phantom_round([_found("2026-10-14", [3, 4])])
    assert out[0]["outcome"] == "found"


def test_the_guard_can_be_disabled_and_tuned():
    ops = _phantom_ops(PHANTOM_MAX_PERIODS=0)
    out = ops._reject_phantom_round([_found("2026-10-14", [3, 4]), _found("2026-10-15", [3, 4])])
    assert all(o["outcome"] == "found" for o in out), "0 must disable the check"

    ops = _phantom_ops(PHANTOM_MAX_PERIODS=3)
    out = ops._reject_phantom_round([_found(f"2026-10-{d}", [3, 4, 5]) for d in (13, 14)])
    assert all(o["outcome"] == "failed" for o in out), "raising the bound widens what counts as thin"


def test_the_calendar_path_runs_the_guard_on_its_own_output():
    """THE SEAM -- drives the REAL do_discover, not the guard in isolation. The guard is worthless
    unless _discover_via_calendar actually routes its output through it, and a test that calls
    _reject_phantom_round directly proves nothing about that (the first version of this test did
    exactly that, and would have passed with the guard unwired).

    _RecordingSession serves the SAME getPeriodosOcupados body for every date -- the portal's
    fallback template under load. '1256789' decodes to closed prefix {1,2}, inactive tail 5..9,
    active holes {1,2,3,4}, free = {3,4}: the exact Booking 0918 signature."""
    events = []
    ops = _make_ops(emit=lambda t, **p: events.append((t, p)))
    ops._log = lambda *a, **k: None

    class _TemplateSession(_RecordingSession):
        PERIODOS_BODY = "<html>periodos_ocupados='1256789'</html>"    # -> free=[3,4] for EVERY date

    ops.session = _TemplateSession()
    out = ops.do_discover(["2026-10-14", "2026-10-15", "2026-10-16"])

    outcomes = {o["date"]: o["outcome"] for o in out}
    assert set(outcomes.values()) == {"failed"}, (
        f"a uniform thin round must come out of do_discover as unknown, got {outcomes}")
    assert not any(t == "slots_found" for t, _ in events), "phantom finds must never fire slots_found"
    assert any(t == "portal_health" and p.get("kind") == "phantom_slots" for t, p in events)


def test_the_calendar_path_still_reports_real_variation_as_found():
    """Control for the seam test: per-date variation through the same real path is kept."""
    ops = _make_ops(emit=lambda t, **p: None)
    ops._log = lambda *a, **k: None

    class _VaryingSession(_RecordingSession):
        def browser_eval(self, js):
            if "location.origin" in (js or ""): return _origin_reply(js)
            if "gettime" in self._last_url: return self.GETTIME_BODY
            # different closed prefix per date -> different free set per date
            d = self._last_url.rsplit("/", 1)[-1]
            return ("<html>periodos_ocupados='1256789'</html>" if d.endswith("14")
                    else "<html>periodos_ocupados='256789'</html>")   # free={1,3,4}

    ops.session = _VaryingSession()
    out = ops.do_discover(["2026-10-14", "2026-10-15"])
    assert {o["outcome"] for o in out} == {"found"}


# ── SLOTS_PRESOLVE: token pre-solve off the critical path (2026-09-20, A/B lever) ────────────
import time as _time_mod


def _presolve_ops(**raw):
    # Set via cfg.raw directly (as the existing slots tests set DISCOVERY_SOURCE) -- the presolve
    # methods read only cfg.raw, and this sidesteps the settings-layer plumbing entirely.
    ops = _make_ops(emit=lambda t, **p: None)
    ops._log = lambda *a, **k: None
    ops.cfg.raw.update(raw)
    return ops


def test_slots_token_uses_a_fresh_pre_solved_token_and_skips_the_inline_solve():
    """The whole point: a token solved during the wait is used without paying the 7-23s solve on
    the critical path. race_all must NOT be called when a fresh token is cached."""
    ops = _presolve_ops(SLOTS_PRESOLVE=True, SLOTS_TOKEN_TTL=90.0)
    ops._presolved_slots = ("PRESOLVED_TOKEN", _time_mod.time())   # fresh

    calls = []
    def _race(*a, **k): calls.append(a); return "INLINE_TOKEN"

    assert ops._slots_token(_race) == "PRESOLVED_TOKEN"
    assert calls == [], "a fresh cached token must skip the inline solve"
    assert ops._presolved_slots is None, "cached token is consumed once"


def test_a_stale_pre_solved_token_is_discarded_and_solved_inline():
    """reCAPTCHA tokens expire ~120s; a cached token older than SLOTS_TOKEN_TTL must never be sent.
    Fall back to a fresh inline solve rather than submitting an expired token."""
    ops = _presolve_ops(SLOTS_PRESOLVE=True, SLOTS_TOKEN_TTL=90.0)
    ops._presolved_slots = ("OLD_TOKEN", _time_mod.time() - 200)   # well past TTL

    assert ops._slots_token(lambda *a, **k: "FRESH_INLINE") == "FRESH_INLINE"
    assert ops._presolved_slots is None


def test_presolve_only_fires_in_the_lead_window_and_only_when_enabled():
    ON = _presolve_ops(SLOTS_PRESOLVE=True, SLOTS_TOKEN_TTL=90.0)
    OFF = _presolve_ops(SLOTS_PRESOLVE=False)

    def _race(*a, **k): return "T"

    # disabled: never solves, never caches
    OFF._maybe_presolve_slots(20.0, _race)
    assert OFF._presolved_slots is None

    # too early (wait far above the lead): a token now would age out before use
    ON._maybe_presolve_slots(80.0, _race)
    assert ON._presolved_slots is None

    # inside the lead window: pre-solves and caches
    ON._maybe_presolve_slots(ON._SLOTS_PRESOLVE_LEAD - 5, _race)
    assert ON._presolved_slots is not None and ON._presolved_slots[0] == "T"

    # already has a fresh one: does not solve again
    before = ON._presolved_slots
    ON._maybe_presolve_slots(10.0, lambda *a, **k: "SECOND")
    assert ON._presolved_slots is before


def test_presolve_off_is_a_pure_inline_solve_no_behaviour_change():
    """The default path must be byte-for-byte the old behaviour: no cache consulted, always solve."""
    ops = _presolve_ops(SLOTS_PRESOLVE=False)
    calls = []
    def _race(*a, **k): calls.append(1); return "TOK"
    assert ops._slots_token(_race) == "TOK"
    assert len(calls) == 1 and ops._presolved_slots is None


# ── dead-exit (chrome-error / tunnel) rolls the exit instead of re-probing (2026-09-23) ──
def test_is_dead_exit_response_recognises_dead_tunnel_pages():
    ops = _make_ops()
    assert ops._is_dead_exit_response("", "chrome-error://chromewebdata/")
    assert ops._is_dead_exit_response("This site can't be reached ERR_CONNECTION_CLOSED", "https://portal/")
    assert ops._is_dead_exit_response("... ERR_TUNNEL_CONNECTION_FAILED ...", "")
    assert ops._is_dead_exit_response("... ERR_TIMED_OUT ...", "")
    # a healthy portal page is NOT a dead exit
    assert not ops._is_dead_exit_response("<html>Solicitar pedido de Visto</html>",
                                          "https://pedidodevistos.mne.gov.pt/VistosOnline/")


def test_keepalive_rolls_the_exit_on_a_dead_exit_page_returned_as_a_string(monkeypatch):
    """The dominant 'Prev 0923-3018' stall: a dead tunnel returns the chrome-error page as a STRING
    (nav raised nothing), classify reads it as UNKNOWN, and the keepalive re-probed the SAME dead
    exit forever -- warmed workers stuck in await_assignment, never assignable, never applying. A
    dead exit is not inconclusive: roll to a fresh exit and re-authenticate."""
    import apps.worker.ops_live as ops_live_mod
    monkeypatch.setattr(ops_live_mod.time, "sleep", lambda *_a, **_k: None)
    ops = _make_ops(cfg=_cfg(KEEPALIVE_DEAD_TRIP=5), signals=_FakeSignals(timeouts=1, final="slot_open"))
    ops._signals.slot_found_seen = True

    class _DeadExitSession(_FakeSession):
        def browser_nav(self, url, timeout=30, **k):
            return "chrome-error://chromewebdata/"       # nav 'succeeds' onto the dead-tunnel shell
        def browser_eval(self, js):
            return "<html><body>This site can't be reached ERR_TUNNEL_CONNECTION_FAILED</body></html>"

    ops.session = _DeadExitSession()
    rolls, restores = [], []
    ops._roll_exit = lambda why="": rolls.append(why) or True
    ops._attempt_keepalive_restore = lambda context="": restores.append(context)
    ops.await_slot_open()
    assert rolls, "a dead-exit keepalive page must roll the exit, not re-probe it"
    assert restores == ["await_slot_open"], "and re-authenticate on the fresh exit"


def test_apply_rolls_the_exit_when_round_start_rewarm_dies_on_a_dead_exit():
    """178 of 'Prev 0923-3018's 186 slot releases were reason 'session' -- the round-start re-warm
    failing on a dead exit. Releasing without rolling means the worker re-logs in on the SAME dead
    exit and the next slot dies identically. Roll first so the re-login runs on a fresh exit."""
    ops = _make_ops()
    ops.is_warmed = lambda: False
    def _dead_rewarm():
        ops._warmup_last_error = "TypeError: Failed to fetch"   # a dead-exit signature
        return False
    ops._run_warmup_steps = _dead_rewarm
    rolls = []
    ops._roll_exit = lambda why="": rolls.append(why) or True
    assert ops.do_apply({"date": "2026-09-01", "period": 4}) == "session"
    assert rolls, "a dead-exit round-start re-warm must roll the exit before releasing the slot"


def test_apply_does_NOT_roll_when_rewarm_fails_on_a_portal_rejection():
    """A portal rejection (quest_state_stale / csrf / sc-rejected) leaves _warmup_last_error empty,
    and the exit may be perfectly live -- rolling it would discard a good exit over a portal hiccup."""
    ops = _make_ops()
    ops.is_warmed = lambda: False
    def _clean_fail():
        ops._warmup_last_error = ""     # a non-exception return False -- NOT a dead exit
        return False
    ops._run_warmup_steps = _clean_fail
    rolls = []
    ops._roll_exit = lambda why="": rolls.append(why) or True
    assert ops.do_apply({"date": "2026-09-01", "period": 4}) == "session"
    assert rolls == [], "a portal-rejection re-warm failure must NOT roll the exit"


# ── restore = authed only; warmup is mandatory before apply (operator model, 2026-09-23) ──
def test_do_warmup_always_runs_the_chain_even_after_a_restore():
    """The checkpoint-restore skip is gone: do_warmup must run the real questionnaire chain, never
    return True off a restore stage. A restore is authed-only, never warmed."""
    ops = _make_ops()
    ops._restore_stage = "alive"                 # the OLD skip condition
    calls = {"n": 0}
    ops._run_warmup_steps = lambda: (calls.__setitem__("n", calls["n"] + 1), True)[1]
    assert ops.do_warmup() is True
    assert calls["n"] == 1, "do_warmup must run the chain, not skip on a restore stage"


def test_do_apply_rewarms_unconditionally_even_when_is_warmed():
    """The Schedule form cursor is NAVIGATION-bound, not time-bound -- a keeping worker that reads
    'warmed' (chain anchor within FORM_STATE_TTL) may have a cursor destroyed by the keepalive
    home-nav. do_apply must run the warmup right before EVERY submit, not trust is_warmed(); a
    skipped warm is a guaranteed form_stale."""
    events = []
    ops = _apply_ops(b"%PDF-1.4\nreal", events)
    ops.is_warmed = lambda: True                  # even though it claims warmed...
    calls = {"n": 0}
    def _count():
        calls["n"] += 1
        return True
    ops._run_warmup_steps = _count
    ops.do_apply({"date": "2026-09-01", "period": 4})
    assert calls["n"] >= 1, "do_apply must re-warm before the submit regardless of is_warmed()"


# ── a bad-but-stable exit's reCAPTCHA rolls the exit, never burns the account (2026-09-24) ──
_RECAPTCHA_BODY = '{"type":"error","description":"Por favor, confirma o reCAPTCHA."}'


def test_recaptcha_verdict_rolls_the_exit_and_does_not_burn_the_account(monkeypatch):
    """CONFIRMED live (PEDRO RODRIGUES): a bad-but-STABLE exit returns 'confirma o reCAPTCHA' for
    every sub-account, while a good exit logs the same accounts in. The old code only rolled when the
    exit demonstrably MOVED, so a stable-flagged exit fell through and charged/rotated the accounts,
    burning them. Now the verdict rolls to a fresh exit (uncharged) whether or not a move is seen."""
    import apps.worker.ops_live as ops_live_mod

    calls = {"n": 0}
    class _AlwaysRecaptcha(_FakeSession):
        def browser_login(self, *a, **k):
            calls["n"] += 1
            return {"status": 200, "body": _RECAPTCHA_BODY}
    monkeypatch.setattr(ops_live_mod, "get_session", lambda **kw: _AlwaysRecaptcha())
    ops = _make_ops(cfg=_cfg(STICKY_RETRY_MAX=2, LOGIN_TRANSIENT_RETRY_MAX=0,
                             LOGIN_PORTAL_BUSY_ROLLS=0, LOGIN_RECAPTCHA_ROLLS=3))
    ops._backoff = lambda n: None
    ops._backoff_seconds = lambda s: None
    ops._note_exit_ip = lambda when="": False          # exit did NOT detectably move (stable-bad)
    ops._liveness = _RecordingLiveness()
    rolls = []
    ops._roll_exit = lambda why="": rolls.append(why) or True

    ok = ops._sticky_login_attempts(ops.cfg.login.sticky_retry_max)
    assert ok is False
    recaptcha_rolls = [w for w in rolls if "recaptcha" in str(w).lower()]
    assert len(recaptcha_rolls) == 3, \
        f"a reCAPTCHA verdict must roll to a fresh exit (3 budgeted), got rolls={rolls}"
    # 3 uncharged rolls + then it charges the account (budget exhausted) -> at most sticky_max attempts,
    # NOT an immediate rotate on the first verdict. The point: the account was not burned on attempt 1.
    assert calls["n"] >= 4, f"expected 3 uncharged roll-retries before charging, got {calls['n']}"


# ── already-has-a-request: retire the identity and STOP the worker (2026-09-24) ──
def test_already_booked_signal_detects_the_portal_alert():
    """The portal delivers 'Este requerente já tem um pedido registado' as a JS alert, captured by
    the session dialog handler and read via browser_last_dialog(). _already_booked_signal must catch
    it there (and in a response body), so the worker can retire the identity instead of grinding it."""
    ops = _make_ops()
    class _S:
        def __init__(self, dlg): self._dlg = dlg
        def browser_last_dialog(self): return self._dlg
    ops.session = _S("Este requerente já tem um pedido registado. Poderá inserir novo 5 dias após o pedido anterior.")
    assert ops._already_booked_signal("") is not None
    # also detectable in a response body when there is no dialog
    ops.session = _S(None)
    assert ops._already_booked_signal("<html>... já tem um pedido registado ...</html>") is not None
    # and a normal page is not a false positive
    assert ops._already_booked_signal("<html>Schedule</html>") is None


def test_do_apply_returns_already_booked_when_identity_unavailable():
    """When warmup flags the identity unavailable (already has a request), do_apply must return the
    terminal 'already_booked' -- NOT 'session' (which would roll the exit and retry the doomed
    sub-account forever)."""
    events = []
    ops = _apply_ops(b"", events)
    ops.is_warmed = lambda: False
    def _warm_fail():
        ops._identity_unavailable = True     # warmup detected already-booked
        return False
    ops._run_warmup_steps = _warm_fail
    rolls = []
    ops._roll_exit = lambda why="": rolls.append(why) or True
    assert ops.do_apply({"date": "2026-09-01", "period": 4}) == "already_booked"
    assert rolls == [], "already-booked must NOT roll the exit (identity, not exit, is the problem)"


def test_already_booked_is_a_terminal_completed_status_not_respawned():
    """The worker terminates as COMPLETED on already_booked (never HELD/FAILED, which respawn)."""
    from apps.hub.events import _TERMINAL_STATUS
    from libs.domain.enums import WorkerStatus
    assert _TERMINAL_STATUS["already_booked"] == WorkerStatus.COMPLETED


# ── PT version: submit is an in-page XHR (X-Requested-With) with lang=PT (2026-09-24) ──
def test_pt_submit_is_an_xhr_with_lang_pt_and_xrw_header():
    """The confirmed manual booking (data/new_booking.har) submits SubmeterVistoCriaPDF as an in-page
    XHR (X-Requested-With: XMLHttpRequest, urlencoded, lang=PT) -- NOT a form navigation. Pin that
    the PT-version do_apply calls browser_fetch with those, since a navigation-POST got the
    questionnaire back (form_stale)."""
    captured = {}
    class _XHRSession(_SubmitSession):
        def browser_fetch(self, url, data, method="POST", headers=None, timeout=30,
                          params=None, encode="form", file_fields=None):
            captured["url"] = url; captured["data"] = data
            captured["headers"] = headers or {}; captured["encode"] = encode
            return {"status": 200, "body": _PDF_SHELL}
    ops = _apply_ops(b"%PDF-1.4 real", [])
    ops.session = _XHRSession(b"%PDF-1.4 real")
    ops._save_pdf = lambda b: "/tmp/x.pdf"
    assert ops.do_apply({"date": "2026-09-01", "period": 4}) == "booked"
    assert "SubmeterVistoCriaPDF" in captured["url"]
    assert captured["encode"] == "form", "urlencoded body, matching the HAR"
    assert captured["headers"].get("X-Requested-With") == "XMLHttpRequest"
    assert captured["data"]["lang"] == "PT", "the HAR submit sends lang=PT"
    assert captured["data"]["f_date_c"] == "2026/09/01" and captured["data"]["cmbPeriodo"] == "4"


# ── /slots smart reload (operator 2026-09-25: rely on /slots harder, it is the ground truth) ────
class _SlotsCallbackSession:
    """onCaptchaSuccess never registers; the page HTML decides stale-render vs interstitial."""
    def __init__(self, page_html):
        self._html = page_html
        self.navs = 0

    def browser_nav(self, url, timeout=30):
        self.navs += 1

    def browser_eval(self, js, timeout=30):
        if "onCaptchaSuccess === 'function'" in js:
            return False               # the callback never appears
        if "outerHTML" in js:
            return self._html
        return None


def _slots_callback_ops(page_html):
    ops = _make_ops()
    ops.session = _SlotsCallbackSession(page_html)
    ops.posto_pdf = "3059"
    ops.logs = []
    ops._log = ops.logs.append
    ops._ensure_gettables_hook = lambda: None
    ops._await_gettables = lambda: None
    ops._maybe_presolve_slots = lambda *a, **k: None
    ops.is_stopped = lambda: False
    ops._slots_backoff_until = 0.0
    ops._slots_last_grant_at = 0.0
    return ops


def test_slots_reloads_once_on_a_stale_widget_render():
    """Callback missing but Schedule.jsp markers present = stale render -> reload exactly once, then
    give up cheaply (no solve) if still not ready."""
    ops = _slots_callback_ops("<html> getSlots cmbPeriodo f_date_c </html>")   # has _SCHED_PAGE_MARKERS
    out = ops._discover_via_slots(["2026-11-03"])
    assert ops.session.navs == 2, "a stale render must be reloaded exactly once"
    assert out == [{"date": "2026-11-03", "outcome": "failed"}]
    assert any("stale widget render" in m for m in ops.logs), ops.logs


def test_slots_does_not_reload_an_interstitial():
    """No schedule markers = a WAF interstitial; re-nav serves the same block, so never reload."""
    ops = _slots_callback_ops("<html>Access Denied / please try again later</html>")   # no markers
    out = ops._discover_via_slots(["2026-11-03"])
    assert ops.session.navs == 1, "an interstitial must NOT be reloaded"
    assert out == [{"date": "2026-11-03", "outcome": "failed"}]
    assert any("interstitial" in m for m in ops.logs), ops.logs


def test_slots_rolls_the_exit_after_two_consecutive_interstitials():
    """Operator 2026-09-25: a flagged exit never un-flags, so grinding it wastes rounds. First
    interstitial keeps the cheap skip; the SECOND rolls the exit (like login does)."""
    ops = _slots_callback_ops("<html>Access Denied / please try again later</html>")  # no markers
    rolls = []
    ops._roll_exit = lambda why: rolls.append(why)
    ops._discover_via_slots(["2026-11-03"])            # 1st interstitial -> no roll
    assert rolls == []
    ops._discover_via_slots(["2026-11-03"])            # 2nd consecutive -> roll
    assert len(rolls) == 1 and "interstitial" in rolls[0].lower(), rolls


def test_slots_rolls_the_exit_after_repeated_recaptcha(monkeypatch):
    """A ReCaptchaError that repeats on the same exit past the rate-limit window is a FLAGGED exit,
    not rate-limiting -- roll it, exactly as login rolls on the same reCAPTCHA verdict."""
    ops = _slots_ops({"probe": {"status": 200, "body": '{"error":"ReCaptchaError"}'}},
                     "slots", monkeypatch)
    rolls = []
    ops._roll_exit = lambda why: rolls.append(why)
    ops._discover_via_slots(["2026-11-03"])            # 1st -> backoff, no roll
    assert rolls == []
    ops._slots_backoff_until = 0.0                     # clear the backoff so the 2nd round runs
    ops._discover_via_slots(["2026-11-03"])            # 2nd consecutive -> roll
    assert len(rolls) == 1 and "recaptcha" in rolls[0].lower(), rolls


def test_an_unhealthy_keeping_worker_leaves_keeping_for_warming_up():
    """keeping = healthy (operator 2026-09-26): a holding worker whose session is dead / rebuilding
    must NOT show KEEPING -- it shows WARMING_UP, so _assign_slots (which selects substate==KEEPING)
    excludes it by the SUBSTATE alone. A recovery returns it to KEEPING with a cleared detail."""
    from libs.domain.enums import NOT_BOOKABLE_PREFIX
    ops = _make_ops()
    events = []
    ops._emit = lambda typ, **p: events.append((typ, p))

    ops._mark_keep_bookable(False, "keepalive dead-trip")
    ops._mark_keep_bookable(True)

    sc = [p for t, p in events if t == "status_change"]
    assert sc[0]["substate"] == "warming_up", "an unhealthy holder must leave KEEPING"
    assert sc[0]["detail"].startswith(NOT_BOOKABLE_PREFIX)
    assert sc[1]["substate"] == "keeping" and sc[1]["detail"] == "", "recovery returns to KEEPING"
    assert ops._keep_unbookable is False


def test_do_discover_prefers_the_hub_pushed_source_over_frozen_cfg(monkeypatch):
    """The Hub-pushed DISCOVERY_SOURCE (self._pending_discover_source, set from the discover command)
    overrides the launch-frozen cfg -- so a scout that launched in 'calendar' switches to 'slots' on
    its next round with no restart (2026-09-26, Booking 0925)."""
    ops = _slots_ops({"ok": True, "data": [{"date": "2026-09-14", "periods": [5]}]},
                     "calendar", monkeypatch)          # frozen cfg = calendar
    ops._pending_discover_source = "slots"             # Hub pushed slots on the command
    called = {"slots": 0, "cal": 0}
    ops._discover_via_slots = lambda dates: called.__setitem__("slots", called["slots"] + 1) or [
        {"date": d, "outcome": "empty"} for d in dates]
    ops._discover_via_calendar = lambda dates: called.__setitem__("cal", called["cal"] + 1) or [
        {"date": d, "outcome": "empty"} for d in dates]
    ops.do_discover(["2026-09-14"])
    assert called["slots"] == 1 and called["cal"] == 0, "must honour the pushed 'slots', not frozen 'calendar'"


# ─────────────────────────────────────────────────────────────────────────────
# 2026-10-01 session-health batch: warm-up fails fast on a broken quest step,
# discovery survives an origin/context loss, do_apply's mandatory re-warm is visible.
# ─────────────────────────────────────────────────────────────────────────────

class _QuestStorm(_WarmupSession):
    """Warm-up chain where one quest step keeps returning non-200 (a 503 storm). Records the step
    marker of every quest-step eval so a test can prove the chain does NOT advance past the failure."""
    def __init__(self, sched_html, *, fail_mark, status=503):
        super().__init__(sched_html)
        self._fail_mark = fail_mark
        self._status = status
        self.steps_seen: list[str] = []

    def browser_eval(self, js, timeout=30):
        if "location.origin" in (js or ""):
            return _origin_reply(js)
        if "outerHTML" in js:
            return self._sched_html
        if "method:'GET'" in js:                      # a questionnaire step fetch
            for mark in ("STEP1", "STEP2", "STEP3"):
                if mark in js:
                    self.steps_seen.append(mark)
                    if mark == self._fail_mark:
                        return {"status": self._status}
                    return {"status": 200, "fields": {"a": "1"}}
        return "200"                                   # home-lang POST


def _quest_storm_ops(fail_mark, **template):
    ops = _make_ops(
        cfg=_cfg(QUEST_STEP_GAP=0, QUEST_STEP_BACKOFF=0, **template),
        quest_steps={"IRL": [{"q": "STEP1"}, {"q": "STEP2"}, {"q": "STEP3"}]},
    )
    ops.session = _QuestStorm(_SCHED_OK, fail_mark=fail_mark)
    return ops


def test_warmup_aborts_the_chain_when_a_quest_step_never_succeeds():
    """0925 root cause: a 503 step exhausted its retries, the loop logged it and FELL THROUGH to the
    next step, and the correlated chain died later with the vaguer csrf_missing. The questionnaire is
    a stateful server-side sequence, so a failed step must ABORT the whole chain, not advance."""
    ops = _quest_storm_ops("STEP2")
    assert ops._run_warmup_steps() is False
    assert "STEP3" not in ops.session.steps_seen, "the chain must NOT advance past the failed step"
    assert ops.session.steps_seen.count("STEP2") == 3   # QUEST_STEP_RETRIES default = retried in place


def test_warmup_abort_on_503_is_not_a_dead_exit_so_do_apply_would_not_roll():
    """A 503 is portal overload, not a dead tunnel -- the recorded failure must NOT classify as
    transient/infra, or do_apply's dead-exit guard would roll a perfectly good exit."""
    ops = _quest_storm_ops("STEP1")
    assert ops._run_warmup_steps() is False
    assert "quest step 1" in ops._warmup_last_error
    assert ops._is_transient_login_failure(ops._warmup_last_error) is False


def test_warmup_quest_step_backoff_grows_per_attempt(monkeypatch):
    """The old flat 1.5s burned all three attempts inside the same second against a WAF/503 storm.
    Backoff now grows (base, 2x) so each retry gets real recovery time."""
    sleeps: list[float] = []
    monkeypatch.setattr("apps.worker.ops_live.time.sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr("apps.worker.ops_live.random.uniform", lambda a, b: 1.0)   # kill jitter
    ops = _make_ops(
        cfg=_cfg(QUEST_STEP_GAP=0, QUEST_STEP_BACKOFF=1.5),
        quest_steps={"IRL": [{"q": "STEP1"}, {"q": "STEP2"}, {"q": "STEP3"}]},
    )
    ops.session = _QuestStorm(_SCHED_OK, fail_mark="STEP1")
    ops._run_warmup_steps()
    # STEP1 fails 3x: backoff after attempts 1 and 2, none after the final (it aborts).
    assert sleeps[:2] == [1.5, 3.0]


class _ContextLostSession(_RecordingSession):
    """gettime read raises 'Execution context was destroyed' the first `fail_times` times, then
    succeeds -- models the page-context race / dead-exit that failed the whole 0925 discovery round."""
    def __init__(self, fail_times=1):
        super().__init__()
        self._fail = fail_times
        self.gettime_reads = 0

    def browser_eval(self, js):
        if "location.origin" in (js or ""):
            return _origin_reply(js)
        if "gettime" in self._last_url:
            self.gettime_reads += 1
            if self.gettime_reads <= self._fail:
                raise RuntimeError("Page.evaluate: Execution context was destroyed")
            return self.GETTIME_BODY
        return self.PERIODOS_BODY


def test_do_discover_recovers_when_gettime_context_is_destroyed(monkeypatch):
    """ss#1 root cause: a context-destroyed gettime failed the round AND left the page off-origin, so
    the next warm-up logged 're-anchoring to Schedule.jsp'. do_discover now re-anchors and retries
    once instead of surrendering the whole round."""
    monkeypatch.setattr("apps.worker.ops_live.time.sleep", lambda _s: None)
    ops = _make_ops()
    ops.session = _ContextLostSession(fail_times=1)
    out = ops.do_discover(["2026-08-16"])
    assert ops.session.gettime_reads == 2    # failed once, re-anchored + retried, then OK
    assert {o["date"]: o["outcome"] for o in out}["2026-08-16"] == "found"


def test_do_discover_gives_up_after_the_single_context_retry(monkeypatch):
    """One re-anchor+retry, not an unbounded loop: a page that never recovers reports the round
    failed (and the scout loop's rebuild + the pre-discover probe gate take it from there)."""
    monkeypatch.setattr("apps.worker.ops_live.time.sleep", lambda _s: None)
    ops = _make_ops()
    ops.session = _ContextLostSession(fail_times=99)
    out = ops.do_discover(["2026-08-16"])
    assert ops.session.gettime_reads == 2    # initial + exactly one retry
    assert out == [{"date": "2026-08-16", "outcome": "failed"}]


def test_session_probe_ok_true_for_an_authenticated_home_page():
    ops = _make_ops()
    ops.session = _FakeSession(alive=True)
    assert ops.session_probe_ok() is True


def test_session_probe_ok_false_when_the_session_is_expired():
    ops = _make_ops()
    ops.session = _FakeSession(alive=False)
    assert ops.session_probe_ok() is False


def test_session_probe_ok_false_when_there_is_no_session():
    ops = _make_ops()
    ops.session = None
    assert ops.session_probe_ok() is False


def _apply_ops_counting(pdf_bytes, events, *, mode="auth_only", warmed=False):
    cfg = _cfg() if mode == "auth_only" else _cfg_schedule()
    ops = _make_ops(cfg=cfg, emit=lambda t, **p: events.append((t, p)))
    ops.session = _SubmitSession(pdf_bytes)
    ops._warm_calls = []
    def _w():
        ops._warm_calls.append(1)
        ops.posto_pdf = "3059"; ops._set_anchor("chain")   # a successful warm establishes the cursor
        return True
    ops._run_warmup_steps = _w
    ops._save_pdf = lambda b: "/tmp/x.pdf"
    if warmed:
        ops.posto_pdf = "3059"; ops._set_anchor("chain")   # pretend KEEPING kept it warm
    return ops


def test_do_apply_schedule_fresh_cursor_applies_without_a_round_start_rewarm():
    events = []
    ops = _apply_ops_counting(b"%PDF-1.4 ok", events, mode="schedule", warmed=True)
    assert ops.do_apply({"date": "2026-09-01", "period": 4}) == "booked"
    assert ops._warm_calls == [], "schedule + fresh kept cursor must apply directly, no re-warm"
    substates = [p.get("substate") for t, p in events if t == "status_change"]
    assert "applying" in substates and "warming_up" not in substates, substates


def test_do_apply_schedule_stale_cursor_rewarms_first():
    events = []
    ops = _apply_ops_counting(b"%PDF-1.4 ok", events, mode="schedule", warmed=False)
    assert ops.do_apply({"date": "2026-09-01", "period": 4}) == "booked"
    assert ops._warm_calls == [1], "schedule + stale/absent cursor must re-warm before applying"


def test_do_apply_auth_only_always_rewarms_even_when_warmed():
    events = []
    ops = _apply_ops_counting(b"%PDF-1.4 ok", events, mode="auth_only", warmed=True)
    assert ops.do_apply({"date": "2026-09-01", "period": 4}) == "booked"
    assert ops._warm_calls == [1], "auth_only re-warms every round regardless of is_warmed()"


def test_do_apply_non_booked_clears_anchor_so_the_retry_rewarms():
    events = []
    ops = _apply_ops_counting(b"", events, mode="schedule", warmed=True)   # MostrarPdf, no PDF -> bounce
    out = ops.do_apply({"date": "2026-09-01", "period": 4})
    assert out != "booked", out
    assert not ops.is_warmed(), "a non-booked submit consumed the cursor -> anchor cleared -> retry re-warms"


def test_do_apply_makes_its_mandatory_rewarm_visible():
    """'applying without warmup' was an observability lie: do_apply DOES re-warm unconditionally, but
    phases.py sets APPLYING before calling it and the ~40s re-warm emitted no substate. It now emits
    warming_up then applying so the dashboard shows the warm-up it was always running."""
    events = []
    ops = _apply_ops(b"%PDF-1.4\nreal confirmation", events)
    assert ops.do_apply({"date": "2026-09-01", "period": 4}) == "booked"
    substates = [p.get("substate") for t, p in events if t == "status_change"]
    assert "warming_up" in substates and "applying" in substates
    assert substates.index("warming_up") < substates.index("applying")


# ── KEEPALIVE_MODE='schedule': ACTIVE cursor-keeping (operator 2026-10-02; was measurement-only 2026-10-01) ──
def _schedule_keepalive_ops(page, events, session=None):
    # KEEPALIVE_MODE is a BOOKING-layer setting (so a template can't silently override the operator's
    # per-booking choice), so it must be set at L.BOOKING, not via _cfg's TEMPLATE-only helper.
    # HYBRID_MODE off: these assert the BROWSER keepalive path (nav + _KeepaliveSession); the primp
    # keepalive is covered by its own tests below.
    cfg = EngineConfig.from_settings(
        resolve({L.TEMPLATE: {"ROLE": "apply"},
                 L.BOOKING: {"KEEPALIVE_MODE": "schedule", "HYBRID_MODE": False}}))
    ops = _make_ops(cfg=cfg, emit=lambda t, **p: events.append((t, p)))
    ops.session = session or _KeepaliveSession(page)
    ops.posto_pdf = "3018"
    return ops


def test_keepalive_schedule_mode_probes_schedule_jsp_and_rewarms_a_stale_cursor():
    events = []
    ops = _schedule_keepalive_ops(_SCHED_OK, events)   # posto set but NO fresh chain anchor -> stale
    warm = []
    ops._run_warmup_steps = lambda: (warm.append(1), True)[1]
    ops._do_keepalive("test")
    assert ops.session.navs and "Schedule.jsp" in ops.session.navs[0], ops.session.navs
    assert warm == [1], "active keeping re-warms a cursor that is not verified-fresh"


def test_keepalive_schedule_mode_leaves_a_fresh_cursor_alone():
    events = []
    ops = _schedule_keepalive_ops(_SCHED_OK, events)   # cmbPeriodo + SubmeterVistoCriaPDF present
    ops._set_anchor("chain")                           # verified-fresh -> must NOT re-warm
    ops._run_warmup_steps = lambda: pytest.fail("a live, fresh cursor must not be re-warmed")
    ops._do_keepalive("test")
    lines = [p["text"] for t, p in events if t == "log_line"]
    assert any("keepalive[schedule]" in x and "live & fresh" in x for x in lines), lines


def test_keepalive_schedule_mode_rewarms_an_absent_cursor():
    events = []
    ops = _schedule_keepalive_ops(
        "<html><title>E-VISA</title>authenticated_index -- no scheduling markers</html>", events)
    warm = []
    ops._run_warmup_steps = lambda: (warm.append(1), True)[1]
    ops._do_keepalive("test")
    assert warm == [1], "cursor absent -> active keeping re-warms to keep a bookable session"


def test_keepalive_schedule_mode_still_detects_auth_loss_and_restores():
    events = []
    sess = _RedirectingKeepaliveSession(
        "<html>gone</html>", final_url="https://pedidodevistos.mne.gov.pt/VistosOnline/Authentication.jsp")
    ops = _schedule_keepalive_ops(None, events, session=sess)
    ops._run_warmup_steps = lambda: pytest.fail("must not re-warm a dead session")
    restores = []
    ops._attempt_keepalive_restore = lambda context="": restores.append(context)
    ops._do_keepalive("test")
    assert restores == ["test"]


def test_keepalive_defaults_to_auth_only_and_probes_home():
    events = []
    ops = _make_ops(emit=lambda t, **p: events.append((t, p)))   # default cfg -> auth_only
    ops.session = _KeepaliveSession("<html>solicitar pedido de visto ... consultar pedidos</html>")
    ops.posto_pdf = "3018"
    ops._do_keepalive("test")
    assert ops.session.navs and "Schedule.jsp" not in ops.session.navs[0], ops.session.navs


# ── Phase-K structured logging: key=value lifecycle lines + ISO timestamps (2026-10-01) ──
def test_kv_renders_key_value_and_quotes_spaces():
    from apps.worker.ops_live import _kv
    assert _kv({"a": 1, "b": "two words", "c": None, "d": "x=y"}) == 'a=1 b="two words" c= d="x=y"'


def test_slog_emits_structured_line_with_standard_context():
    logs = []
    ops = _make_ops()
    ops._log = lambda t: logs.append(t)
    ops._slog("warmup_end", result="ok", duration_ms=1234)
    assert len(logs) == 1
    line = logs[0]
    for frag in ("event=warmup_end", "result=ok", "duration_ms=1234",
                 "role=apply", "booking=", "ka_mode=auth_only"):
        assert frag in line, (frag, line)


def test_do_apply_emits_structured_apply_submit_with_latency():
    logs = []
    ops = _apply_ops(b"%PDF-1.4\nreal", [])
    ops._log = lambda t: logs.append(t)
    ops.do_apply({"date": "2026-09-01", "period": 4})
    assert any("event=apply_submit" in l and "round_ms=" in l and "len=" in l for l in logs), logs


def test_do_apply_emits_warmup_start_and_end_for_the_round():
    logs = []
    ops = _apply_ops(b"%PDF-1.4\nreal", [])
    ops._log = lambda t: logs.append(t)
    ops.do_apply({"date": "2026-09-01", "period": 4})
    assert any("event=warmup_start" in l and "phase=apply_round" in l for l in logs), logs
    assert any("event=warmup_end" in l and "result=ok" in l for l in logs), logs


def test_do_discover_emits_structured_discover_end():
    logs = []
    ops = _make_ops()
    ops._log = lambda t: logs.append(t)
    ops.session = _RecordingSession()
    ops.do_discover(["2026-08-16"])
    assert any("event=discover_start" in l and "source=calendar" in l for l in logs), logs
    assert any("event=discover_end" in l and "found=1" in l for l in logs), logs


def test_do_warmup_emits_structured_warmup_lifecycle():
    logs = []
    ops = _warmup_ops(_SCHED_OK)
    ops._log = lambda t: logs.append(t)
    assert ops.do_warmup() is True
    assert any("event=warmup_start" in l for l in logs), logs
    assert any("event=warmup_end" in l and "result=ok" in l for l in logs), logs


# ── signal lifecycle taxonomy + per-signal correlation id (Phase-K item 1 completion, 2026-10-01) ──
import re as _re_sig


def test_signal_received_then_accepted_share_a_correlation_id():
    logs = []
    ops = _make_ops(signals=_FakeSignals(timeouts=0, final="slot_open"))
    ops.session = _FakeSession()
    ops._log = lambda t: logs.append(t)
    assert ops.await_slot_open() == "slot_open"
    recv = [l for l in logs if "event=signal_received" in l and "signal=slot_open" in l]
    acc = [l for l in logs if "event=signal_accepted" in l and "signal=slot_open" in l]
    assert recv and acc, logs
    m = _re_sig.search(r"sig=([0-9a-f]{6})", recv[0])
    assert m, recv[0]
    assert f"sig={m.group(1)}" in acc[0]   # same round id on both lines


def test_slot_open_redelivery_is_duplicated_not_accepted():
    logs = []
    sig = _FakeSignals(timeouts=0, final="slot_open")
    sig.slot_open_seen = True   # already known before the wait -> a redelivery
    ops = _make_ops(signals=sig)
    ops.session = _FakeSession()
    ops._log = lambda t: logs.append(t)
    ops.await_slot_open()
    assert any("event=signal_duplicated" in l and "signal=slot_open" in l for l in logs), logs
    assert not any("event=signal_accepted" in l for l in logs), logs


def test_slot_open_stop_is_logged_as_ignored():
    logs = []
    ops = _make_ops(signals=_FakeSignals(timeouts=0, final="stop"))
    ops.session = _FakeSession()
    ops._log = lambda t: logs.append(t)
    assert ops.await_slot_open() == "stop"
    assert any("event=signal_ignored" in l and "reason=stop" in l for l in logs), logs


def test_correlation_id_threads_from_assignment_into_the_apply_round():
    logs = []
    ops = _apply_ops(b"%PDF-1.4\nreal", [])
    ops._log = lambda t: logs.append(t)
    ops._signals = _FakeSignals(timeouts=0, final={"date": "2026-09-01", "period": 4})
    slot = ops.await_assignment()
    ops.do_apply(slot)
    recv = [l for l in logs if "event=signal_received" in l and "signal=assignment" in l]
    assert recv, logs
    sig = _re_sig.search(r"sig=([0-9a-f]{6})", recv[0]).group(1)
    assert any("event=warmup_start" in l and f"sig={sig}" in l for l in logs), logs
    assert any("event=apply_submit" in l and f"sig={sig}" in l for l in logs), logs


class _DiscoverSignals:
    def __init__(self, result):
        self._result = result
        self.stopped = False
        self.last_discover_special_days = None
        self.last_discover_source = None

    def wait_discover_task(self, timeout=900):
        return self._result


def test_discover_task_received_is_accepted():
    logs = []
    ops = _make_ops(signals=_DiscoverSignals(["2026-10-20", "2026-10-21"]))
    ops._log = lambda t: logs.append(t)
    assert ops.await_discover_task() == ["2026-10-20", "2026-10-21"]
    assert any("event=signal_received" in l and "signal=discover" in l and "dates=2" in l
               for l in logs), logs
    assert any("event=signal_accepted" in l and "signal=discover" in l for l in logs), logs


def test_discover_inactivate_is_ignored_as_false_slot_open():
    from apps.worker.bus_signals import INACTIVATE
    logs = []
    ops = _make_ops(signals=_DiscoverSignals(INACTIVATE))
    ops._log = lambda t: logs.append(t)
    assert ops.await_discover_task() is INACTIVATE
    assert any("event=signal_ignored" in l and "reason=false_slot_open" in l for l in logs), logs


# ── per-roll geo: a worker varies its exit country across the booking set, never bound to one ──
_SOAX_WARM = ("http://package-352438-country-de-sessionid-ABC123-sessionlength-3600-bindttl-600-"
              "idlettl-900-opt-wb:pw@proxy.soax.com:5000")


def test_roll_exit_varies_the_exit_geo_across_the_booking_set(monkeypatch):
    import apps.worker.ops_live as m
    monkeypatch.setattr(m.random, "choice", lambda seq: "IT")   # deterministic pick
    ops = _make_ops(warm_proxy=_SOAX_WARM, proxy_geos=["IT", "ES"])
    ops._log = lambda t: None
    assert ops._roll_exit("test") is True
    assert "-country-it-" in ops.warm_proxy and "-country-de-" not in ops.warm_proxy
    assert "sessionid-ABC123-" not in ops.warm_proxy   # the session id was also rolled


def test_roll_exit_keeps_geo_when_no_set_given():
    ops = _make_ops(warm_proxy=_SOAX_WARM)   # proxy_geos unset -> no geo constraint, don't re-geo
    ops._log = lambda t: None
    assert ops._roll_exit("test") is True
    assert "-country-de-" in ops.warm_proxy   # country unchanged; only the session id rolled


# ── login consumes a pooled pre-solved token (throughput lever, 2026-10-01) ──
def test_login_consumes_a_pooled_token_as_pre_token(monkeypatch):
    import fakeredis
    import apps.worker.ops_live as m
    from libs.bus.token_pool import TokenPool
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    TokenPool(r).push("POOLTOKEN")
    captured = {}

    class _LoginSess:
        def browser_login(self, user, pw, capsolver_keys=None, pre_token=None):
            captured["pre_token"] = list(pre_token or [])
            return {"status": 200, "body": '{"type":"success"}'}
        def close_browser(self): pass
        def get_all_cookies(self): return [{"name": "Vistos_sid", "value": "x"}]

    monkeypatch.setattr(m, "get_session", lambda **kw: _LoginSess())
    ops = _make_ops(redis_client=r)
    ops._log = lambda t: None
    ops._note_exit_ip = lambda *a, **k: None
    ops._close_session_quietly = lambda: None
    ops._save_checkpoint = lambda *a, **k: None
    assert ops._fresh_login() is True
    assert captured["pre_token"] == ["POOLTOKEN"]


def test_login_falls_back_to_inline_solve_when_pool_empty(monkeypatch):
    import fakeredis
    import apps.worker.ops_live as m
    r = fakeredis.FakeStrictRedis(decode_responses=True)   # empty pool
    captured = {}

    class _LoginSess:
        def browser_login(self, user, pw, capsolver_keys=None, pre_token=None):
            captured["pre_token"] = list(pre_token or [])
            return {"status": 200, "body": '{"type":"success"}'}
        def close_browser(self): pass
        def get_all_cookies(self): return [{"name": "Vistos_sid", "value": "x"}]

    monkeypatch.setattr(m, "get_session", lambda **kw: _LoginSess())
    ops = _make_ops(redis_client=r)
    ops._log = lambda t: None
    ops._note_exit_ip = lambda *a, **k: None
    ops._close_session_quietly = lambda: None
    ops._save_checkpoint = lambda *a, **k: None
    assert ops._fresh_login() is True
    assert captured["pre_token"] == [""]   # empty holder -> browser_login solves inline

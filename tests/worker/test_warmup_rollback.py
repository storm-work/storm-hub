"""2026-11-06 manual capture (reference/1106-capture, not committed: it carries a real applicant's
data) + the operator's notes:

  * the Questionario -> Formulario transition can answer "Request could not be processed" (proxy or
    portal) -> roll the exit and try again FROM THE QUESTIONARIO, on the same login;
  * when everything goes wrong, roll back to the Questionario and retry from there;
  * never decide to re-login -- the login is kept by our own activity on it;
  * the real client sends no home-lang POST.
"""
from __future__ import annotations

import apps.worker.ops_live as ol
from apps.worker.ops_live import (FORMULARIO, HOME_URL, QUEST_URL, _form_error_text,
                                  _is_portal_refusal, _refusal_ip, schedule_page_availability)
from tests.worker.test_ops_live import _SCHED_OK, _WarmupSession, _cfg, _make_ops

# The refusal page from the operator's snapshot (Formulario?copy=true), text only.
_REFUSAL = ("<html><head><title>Não foi possível processar o pedido</title></head><body>"
            "<h3>Request could not be processed</h3><p>We were unable to complete your request at "
            "this time. Please try again later.</p><p>URI: pedidodevistos.mne.gov.pt/VistosOnline/"
            "Formulario<br>IP: 186.105.116.66</p></body></html>")
_FORM_OK = '<input name="__RequestVerificationToken" value="tok">'
# The real bounce: ScheduleController re-rendered the form with the required-field banner.
_FORM_INVALID = ('<html><td class="texto_erro" colspan="0" align=left> &gt;&gt; Os items marcados '
                 'com * são obrigatórios<br /> [ 19. Residência habitual ]</td>'
                 '<div class="close">&times;</div></html>')
# Schedule.jsp document.ready, both server-rendered shapes (2026-11-06 capture, abridged).
_SCHED_NONE = ('<html><div id="captchaDiv"></div><script>$(document).ready(function(){'
               'document.getElementById("captchaDiv").style.display = "none";'
               'document.getElementById("calendarDiv").style.display = "block";'
               "getSlots('5094', null);});function onCaptchaSuccess(token){getSlots('5094', token);}"
               '</script><form action="SubmeterVistoCriaPDF?posto_id=5094"><select id="cmbPeriodo">'
               '</select><input name="f_date_c"></form></html>')
_SCHED_CAPTCHA = ('<html><div id="captchaDiv"><div class="g-recaptcha d-flex" data-sitekey="k" '
                  'data-callback="onCaptchaSuccess" data-action="SCHEDULE_EVISA"></div></div>'
                  '<script src="https://www.google.com/recaptcha/enterprise.js?hl=PT"></script>'
                  "<script>function onCaptchaSuccess(token){getSlots('2032', token);}</script>"
                  '<form action="SubmeterVistoCriaPDF?posto_id=2032"><select id="cmbPeriodo">'
                  '</select></form></html>')


# ── page classifiers ───────────────────────────────────────────────────────────────────────────
def test_the_request_could_not_be_processed_page_is_a_refusal():
    assert _is_portal_refusal(_REFUSAL)
    assert _refusal_ip(_REFUSAL) == "186.105.116.66"
    assert not _is_portal_refusal(_SCHED_OK) and not _is_portal_refusal(_FORM_OK)


def test_schedule_page_shape_is_classified():
    assert schedule_page_availability(_SCHED_NONE) == "no_captcha"
    assert schedule_page_availability(_SCHED_CAPTCHA) == "captcha"
    assert schedule_page_availability("<html>login</html>") == "unknown"


def test_form_errors_drop_the_close_button():
    assert _form_error_text(_FORM_INVALID) == [
        ">> Os items marcados com * são obrigatórios [ 19. Residência habitual ]"]


# ── the warm-up chain ──────────────────────────────────────────────────────────────────────────
class _Session(_WarmupSession):
    """A full chain whose Formulario answers are scripted per call; records navs and evals."""

    def __init__(self, forms, sched=_SCHED_OK, sc_body=""):
        super().__init__(sched)
        self._forms = list(forms)
        self._sc_body = sc_body
        self.evals: list[str] = []
        self.form_posts = 0

    def browser_eval(self, js, timeout=30):
        self.evals.append(js)
        if "questForm.lang" in js:
            return "PT"
        return super().browser_eval(js, timeout)

    def browser_form_post(self, url, data, timeout=100):
        self.form_posts += 1
        page = self._forms.pop(0) if self._forms else _FORM_OK
        return page if isinstance(page, tuple) else (url, page)

    def browser_fetch(self, *a, **k):
        return {"status": 200, "body": self._sc_body}


def _ops(session, monkeypatch=None, **template):
    ops = _make_ops(cfg=_cfg(QUEST_STEP_GAP=0, QUEST_STEP_BACKOFF=0, **template),
                    warm_proxy="http://user-rotate-session-1234:p@p.webshare.io:80")   # rollable
    ops.session = session
    ops._log = lambda *a, **k: None
    ops._note_exit_ip = lambda *a, **k: None
    if monkeypatch is not None:
        rebinds = []

        def _get_session(**kw):
            rebinds.append(kw)
            return session                       # same fake, "new" browser on the new exit
        monkeypatch.setattr(ol, "get_session", _get_session)
        ops._rebinds = rebinds
    return ops


def test_a_refused_formulario_rolls_the_exit_and_restarts_from_the_questionario(monkeypatch):
    s = _Session([_REFUSAL])                           # the refusal ends pass 1 at Formulario
    ops = _ops(s, monkeypatch)
    before = ops.warm_proxy
    logins = []
    ops.do_login = lambda: logins.append(1) or True
    assert ops._run_warmup_steps() is True
    assert s.navs.count(QUEST_URL) == 2, "the second pass starts at the Questionario"
    assert ops.warm_proxy != before, "the refused exit was rolled"
    assert ops._rebinds and ops._rebinds[0]["inject_cookies_full"], "the login was carried over"
    assert ops._rebinds[0]["proxy"] == ops.warm_proxy
    assert not logins and ops.login_lost(probe=False) is False


def test_validation_errors_are_not_rolled_back():
    s = _Session([_FORM_OK], sched="<html>the form again</html>", sc_body=_FORM_INVALID)
    ops = _ops(s)
    assert ops._run_warmup_steps() is False
    assert ops.warmup_failure_reason() == "validation"
    assert s.navs.count(QUEST_URL) == 1, "resending the same data cannot fix it"
    assert "Residência habitual" in ops._warmup_last_error


def test_an_auth_redirect_is_positive_evidence_and_not_rolled_back():
    s = _Session([(ol.AUTH_URL, "<html>login</html>")])
    ops = _ops(s)
    assert ops._run_warmup_steps() is False
    assert ops.warmup_failure_reason() == "auth"
    assert ops.login_lost(probe=False) is True
    assert s.navs.count(QUEST_URL) == 1


def test_restarts_are_bounded(monkeypatch):
    s = _Session([_REFUSAL] * 50)
    ops = _ops(s, monkeypatch, WARMUP_CHAIN_RESTARTS=2)
    assert ops._run_warmup_steps() is False
    assert s.navs.count(QUEST_URL) == 3                # 1 pass + 2 roll-backs
    assert ops.warmup_failure_reason() == "refused"


def test_no_home_lang_post_when_the_questionario_is_already_pt():
    s = _Session([])
    ops = _ops(s)
    assert ops._run_warmup_steps() is True
    assert not [j for j in s.evals if "lang=PT" in j and "method:'POST'" in j]
    assert s.navs[0] == QUEST_URL


def test_a_non_pt_questionario_is_corrected_with_the_lang_post():
    class _Eng(_Session):
        def browser_eval(self, js, timeout=30):
            if "questForm.lang" in js:
                self.evals.append(js)
                return "ENG"
            return super().browser_eval(js, timeout)
    s = _Eng([])
    ops = _ops(s)
    assert ops._run_warmup_steps() is True
    assert [j for j in s.evals if "lang=PT" in j and HOME_URL in j]
    assert s.navs[:2] == [QUEST_URL, QUEST_URL]


def test_warm_up_records_the_schedule_page_shape_without_acting_on_it():
    ops = _ops(_Session([], sched=_SCHED_NONE))
    assert ops._run_warmup_steps() is True          # a captcha-less page is still a warmed page
    assert ops._sched_availability == "no_captcha"


# ── login_lost: only positive evidence ───────────────────────────────────────────────────────────
class _Home(_Session):
    def __init__(self, home):
        super().__init__([])
        self._home = home

    def browser_eval(self, js, timeout=30):
        if "location.href" in js:
            return HOME_URL
        if "outerHTML" in js or "innerHTML" in js or "body" in js:
            return self._home
        return super().browser_eval(js, timeout)


def test_login_lost_needs_a_logged_out_home_page():
    ops = _ops(_Home("<html>solicitar pedido de visto consultar pedidos</html>"))
    assert ops.login_lost() is False
    ops = _ops(_Home("<html>solicitar pedido de visto</html>"))
    assert ops.login_lost() is True
    ops = _ops(_Home("<html>chrome-error</html>"))
    assert ops.login_lost() is False, "an unreadable page is not evidence"


# ── keepalive restore keeps the login unless the portal says it is gone ───────────────────────
def _restore_ops(monkeypatch, home, *, closed=False):
    s = _Home(home)
    ops = _ops(s, monkeypatch)
    ops._emit = None
    ops._mark_keep_bookable = lambda *a, **k: None
    ops._try_restore_from_checkpoint = lambda: False
    ops._browser_closed = closed
    ops._hybrid_full_jar = [{"name": "Vistos_sid", "value": "abc"}]
    logins = []
    ops._fresh_login = lambda: logins.append(1) or True
    return ops, logins


def test_keepalive_restore_carries_the_live_jar_instead_of_logging_in(monkeypatch):
    ops, logins = _restore_ops(monkeypatch, "<html>solicitar pedido de visto consultar pedidos</html>")
    assert ops._attempt_keepalive_restore("dead exit") is True
    assert not logins and ops._rebinds and ops.session is not None


def test_keepalive_restore_from_a_closed_hybrid_browser_still_rebuilds_one(monkeypatch):
    ops, logins = _restore_ops(monkeypatch, "<html>solicitar pedido de visto consultar pedidos</html>",
                               closed=True)
    assert ops._attempt_keepalive_restore("dead exit") is True
    assert ops._rebinds and ops.session is not None and not logins


def test_keepalive_restore_logs_in_when_the_home_page_is_logged_out(monkeypatch):
    ops, logins = _restore_ops(monkeypatch, "<html>solicitar pedido de visto</html>")
    ops._attempt_keepalive_restore("dead exit")
    assert logins == [1]


# ── Test 1106 (2026-11-06): a roll left the Donut browser on the refused exit ────────────────
def test_a_rolled_exit_moves_the_browser_before_the_next_warm_up(monkeypatch):
    s = _Session([])
    s.proxy = "http://user-rotate-session-1234:p@p.webshare.io:80"     # what the browser runs on
    ops = _ops(s, monkeypatch)
    ops._roll_exit("slots blocked")                                    # /slots 403 -> roll
    assert ops.warm_proxy != s.proxy and ops._browser_on_stale_exit()
    assert ops._run_warmup_steps() is True
    assert ops._rebinds and ops._rebinds[0]["proxy"] == ops.warm_proxy, "browser follows the roll"


def test_a_dead_page_rolls_and_rebinds_instead_of_looping(monkeypatch):
    s = _Session([])
    ops = _ops(s, monkeypatch)
    dead = {"n": 0}

    def _origin(why):
        dead["n"] += 1
        return dead["n"] > 1                     # dead on the first exit, fine after the rebind
    ops._ensure_portal_origin = _origin
    before = ops.warm_proxy
    assert ops._run_warmup_steps() is True
    assert ops.warm_proxy != before and ops._rebinds
    assert dead["n"] == 2


def test_checkpoint_restore_is_off_by_default():
    from libs.domain.enums import SettingsLayer as L
    from libs.domain.settings import resolve
    from libs.engine.config import EngineConfig
    ops = _make_ops(cfg=EngineConfig.from_settings(resolve({L.TEMPLATE: {"ROLE": "apply"}})))
    ops.session = None
    assert ops._try_restore_from_checkpoint() is False
    assert ops._restore_stage == "none"


# ── Booking 1106 (2026-11-06): a portal outage must not park the fleet for good ───────────────
def _failing_rebind_ops(monkeypatch, error):
    ops, logins = _restore_ops(monkeypatch, "<html>solicitar pedido de visto consultar pedidos</html>")

    def _boom(**kw):
        raise RuntimeError(error)
    monkeypatch.setattr(ol, "get_session", _boom)
    return ops, logins


def test_a_rebuild_the_portal_never_answered_does_not_spend_the_restore_budget(monkeypatch):
    ops, logins = _failing_rebind_ops(
        monkeypatch, "Page.goto: net::ERR_EMPTY_RESPONSE at https://pedidodevistos.mne.gov.pt/")
    for _ in range(ol._MAX_KEEPALIVE_RESTORE_ATTEMPTS + 2):
        assert ops._attempt_keepalive_restore("portal down") is False
    assert ops._keepalive_restore_attempts == 0, "portal silence says nothing about the login"
    assert not logins


def test_a_dead_proxy_rebuild_still_counts(monkeypatch):
    ops, _ = _failing_rebind_ops(monkeypatch, "Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED")
    ops._attempt_keepalive_restore("dead exit")
    assert ops._keepalive_restore_attempts == 1


def test_a_spent_restore_budget_is_a_cooldown_not_the_end(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ol.time, "time", lambda: clock[0])
    ops, _ = _failing_rebind_ops(monkeypatch, "Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED")
    for _ in range(ol._MAX_KEEPALIVE_RESTORE_ATTEMPTS):
        ops._attempt_keepalive_restore("dead exit")
    assert ops._attempt_keepalive_restore("dead exit") is False      # spent -> cooldown starts
    assert ops._keepalive_restore_attempts == ol._MAX_KEEPALIVE_RESTORE_ATTEMPTS
    clock[0] += 299
    ops._attempt_keepalive_restore("dead exit")
    assert ops._keepalive_restore_attempts == ol._MAX_KEEPALIVE_RESTORE_ATTEMPTS, "still cooling"
    clock[0] += 2
    ops._attempt_keepalive_restore("dead exit")
    assert ops._keepalive_restore_attempts == 1, "a fresh budget after the cooldown"


def test_restore_cooldown_zero_keeps_the_old_give_up(monkeypatch):
    ops, _ = _failing_rebind_ops(monkeypatch, "Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED")
    ops.cfg.raw["KEEPALIVE_RESTORE_COOLDOWN_S"] = 0
    for _ in range(ol._MAX_KEEPALIVE_RESTORE_ATTEMPTS + 3):
        ops._attempt_keepalive_restore("dead exit")
    assert ops._keepalive_restore_attempts == ol._MAX_KEEPALIVE_RESTORE_ATTEMPTS
    assert ops._restore_budget_left() is False


def test_a_sick_portal_does_not_convict_the_exit():
    """Maintenance/error pages (degraded) and the pulse's optimistic 'up' (one newest 'up' among
    hundreds of degraded/no-answer looks) are the portal being sick. Only a fleet majority of
    normal app answers -- or the edge answering others (reachable) -- convicts this exit."""
    class _P:
        def __init__(self, st, **counts):
            self.st, self.c = st, counts

        def state(self):
            return {"state": self.st, "counts": self.c}
    ops = _ops(_Session([]))
    ops._exit_probe_ok, ops._exit_ip = True, "1.2.3.4"
    for p in (_P("degraded", degraded=40, noanswer=10),
              _P("up", up=13, degraded=165, noanswer=176)):         # Booking 1106, 10:40-10:57
        ops._pulse, ops._portal_down_since = p, None
        assert ops._portal_down_not_exit("test") is True, p.st
    for p in (_P("up", up=30, degraded=5, noanswer=5), _P("reachable", refused=20)):
        ops._pulse, ops._portal_down_since = p, None
        assert ops._portal_down_not_exit("test") is False, p.st

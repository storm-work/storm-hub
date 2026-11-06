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

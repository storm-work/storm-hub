"""KEEPALIVE_MODE='quest_hold': park on Questionario, one quest step per jittered window, resume the
questionnaire at the signal. Measured by probes/quest_hold_probe.py (40 min, 13/13 clean)."""
from apps.worker.ops_live import HOME_URL, QUEST_NEXT, QUEST_URL
from libs.domain.enums import SettingsLayer as L
from libs.domain.settings import resolve
from libs.engine.config import EngineConfig
from tests.worker.test_ops_live import _UNIT_MODE, _FakeSession, _make_ops

_STEPS = [{"id_pergunta": str(i), "valor": "x"} for i in range(1, 4)]
_QUEST_PAGE = "<html>cb_question_1 QuestNextQuestion</html>"


class _QSession(_FakeSession):
    """Answers the quest page, the step fetches and the home-lang POST, and records every call."""
    def __init__(self, *, page=_QUEST_PAGE, step=None, final_url=None):
        super().__init__()
        self.calls, self.page, self.final_url = [], page, final_url
        self.step = step if step is not None else {"status": 200, "body": "<input name=a value=1>",
                                                   "fields": {"a": "1"}, "len": 20}

    def browser_nav(self, url, timeout=30, skip_cookie_sync=False):
        self.calls.append(("nav", url))
        self._last_url = url
        return self.final_url or url

    def browser_eval(self, js):
        if "location.origin" in (js or ""):
            return super().browser_eval(js)
        if "questForm.lang" in js:
            return "PT"                      # the real Questionario's questForm carries lang=PT
        if "lang=PT" in js and "method:'POST'" in js:
            self.calls.append(("home_lang", HOME_URL))
            return 200
        if "QuestNextQuestion" in js:
            self.calls.append(("step", js))
            return self.step
        if "outerHTML" in js or "readyState" in js:
            return self.page
        return self.page


def _hold_ops(session=None, steps=_STEPS, **template):
    cfg = EngineConfig.from_settings(resolve({
        L.TEMPLATE: {"ROLE": "apply", **template},
        L.BOOKING: {**_UNIT_MODE, "KEEPALIVE_MODE": "quest_hold"}}))
    ops = _make_ops(cfg=cfg, quest_steps={"IRL": steps})
    ops.session = session or _QSession()
    ops._log = lambda *a, **k: None
    ops._note_exit_ip = lambda *a, **k: None
    return ops


def test_hold_enters_with_a_reload_then_walks_the_steps_then_reloads():
    ops = _hold_ops()
    positions = []
    for _ in range(5):
        ops._do_keepalive("t")
        positions.append(ops._qh_pos)
    # reload -> 0, step1 -> 1, step2 -> 2, step3 -> 3, then (pos == len) a reload -> 0
    assert positions == [0, 1, 2, 3, 0]
    kinds = [c[0] for c in ops.session.calls]
    assert kinds.count("home_lang") == 2 and kinds.count("step") == 3
    assert ("nav", QUEST_URL) in ops.session.calls
    # no Schedule.jsp / home probe nav: the hold never touches the booking cursor
    assert not any(c[0] == "nav" and c[1] != QUEST_URL for c in ops.session.calls)


def test_hold_session_expired_restores_and_forgets_the_position():
    ops = _hold_ops(_QSession(step={"status": 200, "body": "Perdeu a sessão"}))
    restored = []
    ops._attempt_keepalive_restore = lambda ctx: restored.append(ctx)
    ops._qh_pos = 1
    ops._do_keepalive("t")
    assert restored and ops._qh_pos is None


def test_hold_step_answering_user_not_found_in_session_is_an_expired_session():
    """HTTP 200 with 'User not found in session' is a lost session, not a healthy step (2026-10-30:
    the hold counted these ok for ~100 min and the fleet found out at the pre-warm)."""
    ops = _hold_ops(_QSession(step={"status": 200, "body": "(QuestNextQuestion) ERROR: User not found in session"}))
    restored = []
    ops._attempt_keepalive_restore = lambda ctx: restored.append(ctx)
    ops._qh_pos = 1
    ops._do_keepalive("t")
    assert restored and ops._qh_pos is None


def test_hold_challenge_backs_off_without_relogin():
    ops = _hold_ops(_QSession(page="<html>/ch/bd.js captcha-delivery</html>"))
    restored = []
    ops._attempt_keepalive_restore = lambda ctx: restored.append(ctx)
    ops._do_keepalive("t")          # entering reload hits the challenge
    assert not restored and ops._qh_pos is None


def test_hold_non_200_step_restarts_the_pass_without_relogin():
    ops = _hold_ops(_QSession(step={"status": 503, "body": "busy"}))
    restored = []
    ops._attempt_keepalive_restore = lambda ctx: restored.append(ctx)
    ops._qh_pos = 1
    ops._do_keepalive("t")
    assert not restored and ops._qh_pos is None


def test_hold_is_inert_once_a_fresh_cursor_is_held():
    ops = _hold_ops()
    ops._set_anchor("chain")
    ops.posto_pdf = "5084"
    ops._do_keepalive("t")          # falls through to the plain authed-home probe
    assert all(c != ("home_lang", HOME_URL) for c in ops.session.calls)
    assert ops._qh_pos is None


def test_qh_take_returns_only_a_resumable_position_and_always_consumes_it():
    ops = _hold_ops()
    for pos, want in ((None, None), (0, None), (1, 1), (2, 2), (3, None)):
        ops._qh_pos = pos
        assert ops._qh_take() == want
        assert ops._qh_pos is None


def test_keepalive_window_is_the_hold_range_until_warmed():
    ops = _hold_ops()
    for _ in range(20):
        assert 60.0 <= ops._keepalive_window() <= 300.0


def test_warmup_resumes_the_held_questionnaire():
    # One pass: this is about what the RESUMED pass sends (a roll-back pass starts at the Questionario).
    ops = _hold_ops(WARMUP_CHAIN_RESTARTS=0)
    ops._qh_pos = 2
    ops._run_warmup_steps()
    kinds = [c[0] for c in ops.session.calls]
    assert "home_lang" not in kinds                       # no lang POST
    assert ("nav", QUEST_URL) not in ops.session.calls    # no Questionario nav
    steps_sent = [c[1] for c in ops.session.calls if c[0] == "step"]
    assert len(steps_sent) == 1 and QUEST_NEXT in steps_sent[0]   # only step 3 of 3
    assert ops._qh_pos is None                            # consumed


def test_warmup_without_a_held_position_runs_the_whole_chain():
    ops = _hold_ops(WARMUP_CHAIN_RESTARTS=0)
    ops._run_warmup_steps()
    kinds = [c[0] for c in ops.session.calls]
    # No home-lang POST: the questForm is already PT (the real client sends none, 2026-11-06 capture).
    assert kinds.count("home_lang") == 0 and kinds.count("step") == 3
    assert ("nav", QUEST_URL) in ops.session.calls


class _PageSession(_QSession):
    """Also answers the page's own goNext() helper (QUEST_HOLD_VIA_PAGE)."""
    def __init__(self, via_page, **kw):
        super().__init__(**kw)
        self.via_page = via_page

    def browser_eval(self, js):
        if "typeof goNext" in (js or ""):
            self.calls.append(("gonext", js))
            return self.via_page
        return super().browser_eval(js)


def _via_page_ops(session, steps=_STEPS):
    cfg = EngineConfig.from_settings(resolve({
        L.TEMPLATE: {"ROLE": "apply"},
        L.BOOKING: {**_UNIT_MODE, "KEEPALIVE_MODE": "quest_hold", "QUEST_HOLD_VIA_PAGE": True}}))
    ops = _make_ops(cfg=cfg, quest_steps={"IRL": steps})
    ops.session, ops.logged = session, []
    ops._log = lambda m, *a, **k: ops.logged.append(m)
    ops._note_exit_ip = lambda *a, **k: None
    return ops


def test_via_page_sends_the_step_through_the_pages_own_gonext_not_our_fetch():
    s = _PageSession({"status": 200, "body": "<input type=hidden id=cb_next_1 value=21>"})
    ops = _via_page_ops(s)
    ops._quest_hold_event("t"); ops._quest_hold_event("t")        # reload, then step 1
    kinds = [c[0] for c in s.calls]
    assert "gonext" in kinds and "step" not in kinds
    assert '"1"' in dict(s.calls[-1:])["gonext"] and ops._qh_pos == 1


def test_via_page_falls_back_to_the_fetch_when_the_page_has_no_gonext():
    s = _PageSession({"status": -1, "body": "no goNext"})
    ops = _via_page_ops(s)
    ops._quest_hold_event("t"); ops._quest_hold_event("t")
    assert "step" in [c[0] for c in s.calls] and ops._qh_pos == 1


def test_default_keeps_using_the_plain_fetch():
    s = _PageSession({"status": 200, "body": "x"})
    ops = _hold_ops(s)
    ops._quest_hold_event("t"); ops._quest_hold_event("t")
    assert "gonext" not in [c[0] for c in s.calls]


def test_a_refusal_log_names_which_check_fired():
    shell = '<html><head><meta name="robots" content="noindex,nofollow"></head><script src="/ch/bd.js"></script>'
    s = _PageSession({"status": 200, "body": shell})
    ops = _via_page_ops(s)
    ops._quest_hold_event("t"); ops._quest_hold_event("t")
    msg = [m for m in ops.logged if "refused/challenged" in m][0]
    assert "bot-check shell" in msg and "bd.js" in msg and ops._qh_pos is None


# ── #1 reduce footprint: the hold backs off while the portal is refusing ──────────────────────
def _backoff_ops(backoff):
    cfg = EngineConfig.from_settings(resolve({
        L.TEMPLATE: {"ROLE": "apply"},
        L.BOOKING: {**_UNIT_MODE, "KEEPALIVE_MODE": "quest_hold", "QUEST_HOLD_MIN_S": 100,
                    "QUEST_HOLD_MAX_S": 100, "QUEST_HOLD_REFUSED_BACKOFF": backoff}}))
    ops = _make_ops(cfg=cfg, quest_steps={"IRL": _STEPS})
    ops._log = lambda *a, **k: None
    return ops


def test_refused_streak_stretches_the_next_hold_window_capped():
    ops = _backoff_ops(2.0)
    assert abs(ops._keepalive_window() - 100.0) < 1e-6      # no refusals yet: base wait
    ops._qh_refused_streak = 1
    assert abs(ops._keepalive_window() - 200.0) < 1e-6      # x2
    ops._qh_refused_streak = 2
    assert abs(ops._keepalive_window() - 400.0) < 1e-6      # x4
    ops._qh_refused_streak = 5
    assert ops._keepalive_window() == 480.0                 # hard ceiling under the idle expiry


def test_backoff_off_by_default_keeps_the_base_window():
    ops = _backoff_ops(1.0)
    ops._qh_refused_streak = 4
    assert abs(ops._keepalive_window() - 100.0) < 1e-6      # 1.0 = off


def test_a_served_step_resets_the_refused_streak():
    s = _PageSession({"status": 200, "body": "x"})
    ops = _hold_ops(s)
    ops._qh_refused_streak = 3
    ops._quest_hold_event("t")                              # reload ok
    assert ops._qh_refused_streak == 0

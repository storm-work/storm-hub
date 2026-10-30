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
        if "lang=PT" in js and "method:'POST'" in js:
            self.calls.append(("home_lang", HOME_URL))
            return 200
        if "QuestNextQuestion" in js:
            self.calls.append(("step", js))
            return self.step
        if "outerHTML" in js or "readyState" in js:
            return self.page
        return self.page


def _hold_ops(session=None, steps=_STEPS):
    cfg = EngineConfig.from_settings(resolve({
        L.TEMPLATE: {"ROLE": "apply"}, L.BOOKING: {**_UNIT_MODE, "KEEPALIVE_MODE": "quest_hold"}}))
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
    ops = _hold_ops()
    ops._qh_pos = 2
    ops._run_warmup_steps()
    kinds = [c[0] for c in ops.session.calls]
    assert "home_lang" not in kinds                       # no lang POST
    assert ("nav", QUEST_URL) not in ops.session.calls    # no Questionario nav
    steps_sent = [c[1] for c in ops.session.calls if c[0] == "step"]
    assert len(steps_sent) == 1 and QUEST_NEXT in steps_sent[0]   # only step 3 of 3
    assert ops._qh_pos is None                            # consumed


def test_warmup_without_a_held_position_runs_the_whole_chain():
    ops = _hold_ops()
    ops._run_warmup_steps()
    kinds = [c[0] for c in ops.session.calls]
    assert kinds.count("home_lang") == 1 and kinds.count("step") == 3
    assert ("nav", QUEST_URL) in ops.session.calls

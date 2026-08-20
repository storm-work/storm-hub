"""An authenticated session must never be thrown away over a warm-up failure.

Confirmed live 2026-08-20, worker 8fc9bdfe (identity ERNESTO PIRES, booking "Test 3018"):

    [session] /login 200 ok -- navigating to AUTH_URL to complete redirect
    [session] form_post -> .../Formulario?copy=true  len=862223
    [session] browser_fetch POST ScheduleController status=200
    [session] browser closed - primp session active
    ===== launch 2026-08-20T06:11:19 =====        <- new process, full re-login

Login had SUCCEEDED. The warm-up then failed and PhaseMachine._warmup returned _fail("warmup"),
which ends run() and exits the process -- discarding a live authed session and paying for a new
proxy lease, a new CAPTCHA solve and a new login against a portal that rate-limits sub-accounts.

The recovery ladder already existed for this exact failure later in the run (_rebuild_session, and
the form_stale path). The INITIAL warm-up just never got it.
"""
from __future__ import annotations

from libs.domain.enums import WorkerRole, WorkerSubstate
from libs.engine.config import EngineConfig
from libs.engine.phases import PhaseMachine


class _Ops:
    """Scriptable EngineOps double: warm-up fails the first `warmup_failures` times."""

    def __init__(self, *, warmup_failures=0, login_ok=True, relogin_ok=True):
        self._warmup_failures = warmup_failures
        self._login_ok = login_ok
        self._relogin_ok = relogin_ok
        self.warmups = 0
        self.logins = 0

    def do_login(self):
        self.logins += 1
        return self._login_ok if self.logins == 1 else self._relogin_ok

    def do_warmup(self):
        self.warmups += 1
        if self.warmups <= self._warmup_failures:
            return False
        return True

    def do_swap(self):
        return False

    def poll_gap(self):
        pass

    def await_slot_open(self):
        return False          # stop right after warm-up; we only care about the warm-up ladder

    def await_slot_found(self):
        return False


def _machine(ops, **raw):
    cfg = EngineConfig.from_settings({"ROLE": "scout", "WARMUP_ONLY": True, **raw})
    return PhaseMachine(cfg, ops)


def test_a_single_warmup_failure_no_longer_kills_the_process():
    """The exact ERNESTO PIRES case: login ok, first warm-up fails, second succeeds."""
    ops = _Ops(warmup_failures=1)
    res = _machine(ops).run()

    assert res.outcome == "warmed", f"got {res.outcome!r} ({res.detail!r})"
    assert ops.logins == 1, "re-logged in despite the session being fine -- that is the waste"
    assert ops.warmups == 2, "should have re-warmed in place exactly once"
    assert WorkerSubstate.FAILED not in res.visited


def test_re_warm_is_preferred_over_re_login():
    """Cheapest step first. A re-login costs a CAPTCHA solve and burns sub-account rate limit."""
    ops = _Ops(warmup_failures=1)
    _machine(ops, REWARM_MAX_ATTEMPTS=3).run()
    assert ops.logins == 1


def test_escalates_to_re_login_only_once_re_warm_stops_working():
    """Past REWARM_MAX_ATTEMPTS only a fresh login rebuilds the workflow anchor (storm-auto abc5)."""
    ops = _Ops(warmup_failures=3)
    res = _machine(ops, REWARM_MAX_ATTEMPTS=2).run()

    assert res.outcome == "warmed", f"got {res.outcome!r} ({res.detail!r})"
    assert ops.logins == 2, "should have re-logged in exactly once, in-process"
    assert ops.warmups == 4          # 1 initial + 2 re-warms + 1 after the re-login


def test_still_fails_when_even_a_fresh_login_cannot_warm():
    """Resilience is not "never fail" -- the genuinely unrecoverable case must still terminate."""
    ops = _Ops(warmup_failures=99)
    res = _machine(ops, REWARM_MAX_ATTEMPTS=2).run()

    assert res.outcome == "failed"
    assert res.detail == "warmup"
    assert res.visited[-1] == WorkerSubstate.FAILED


def test_a_failed_re_login_is_terminal():
    ops = _Ops(warmup_failures=99, relogin_ok=False)
    res = _machine(ops, REWARM_MAX_ATTEMPTS=1).run()
    assert res.outcome == "failed" and res.detail == "warmup"


def test_login_failure_is_still_terminal_there_is_no_session_to_protect():
    ops = _Ops(login_ok=False)
    res = _machine(ops).run()
    assert res.outcome == "failed" and res.detail == "login"
    assert ops.warmups == 0


def test_the_recovery_path_never_routes_through_FAILED():
    """FAILED is what the Hub reaps on. A recoverable warm-up must not transiently claim it."""
    ops = _Ops(warmup_failures=2)
    res = _machine(ops, REWARM_MAX_ATTEMPTS=3).run()
    assert WorkerSubstate.FAILED not in res.visited, res.visited

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


# ── consecutive session-failure cap (live incident 2026-08-20) ───────────────
class _AlwaysSessionOps(_Ops):
    """Apply always fails with `session`, and the rebuild always CLAIMS success.

    That combination is what produced the live loop: worker CARLOS FURTADO on booking "Test 3018"
    took 121 slot leases in 55 minutes, 120 of them `session`, ~26s per cycle, and would never have
    stopped. The specific cause is fixed in ops_live (a consumed checkpoint-restore flag), but "a
    rebuild that always reports success" is a class, and this cap is the backstop for it.
    """
    def __init__(self, **kw):
        super().__init__(**kw)
        self.applies = 0
        self.released = 0

    def await_slot_open(self): return "ok"
    def slot_found_seen(self): return True
    def scouts_invalid(self): return False
    def is_stopped(self): return False
    def await_assignment(self): return {"date": "2026-09-01", "period": 5}

    def do_apply(self, slot):
        self.applies += 1
        return "session"

    def release_slot(self, slot, reason="bounce"):
        self.released += 1


def test_endless_session_failures_terminate_instead_of_looping_forever():
    ops = _AlwaysSessionOps()
    cfg = EngineConfig.from_settings({"ROLE": "apply", "APPLY_MAX_ATTEMPTS": 2})
    res = PhaseMachine(cfg, ops).run()

    assert res.outcome == "failed"
    assert "session unrecoverable" in res.detail
    # APPLY_MAX_ATTEMPTS=2 -> cap of 6 consecutive session failures.
    assert ops.applies == 6, f"looped {ops.applies} times instead of stopping at the cap"
    assert ops.released == 6, "every attempt must still hand its slot back, unpenalised"


def test_the_cap_is_generous_enough_for_transient_session_trouble():
    """A dead proxy that recovers must not be terminal -- that regression parked every applier once."""
    ops = _AlwaysSessionOps()
    cfg = EngineConfig.from_settings({"ROLE": "apply", "APPLY_MAX_ATTEMPTS": 5})
    PhaseMachine(cfg, ops).run()
    assert ops.applies == 15, "5 * 3 = 15 chances before giving up"


# ── Phase 3: a slow portal must not be escalated into a re-login ─────────────
class _SlowPortalOps(_Ops):
    """Warm-up keeps failing, and the engine reports the cause as SLOW rather than broken."""
    def __init__(self, kind="slow", **kw):
        super().__init__(warmup_failures=99, **kw)
        self._kind = kind

    def warmup_failure_kind(self):
        return self._kind


def test_a_slow_portal_never_costs_a_re_login():
    """A re-login cannot make the portal faster. It spends a CAPTCHA solve and a slice of the
    sub-account rate limit -- the binding constraint -- to arrive back at the same slow portal."""
    ops = _SlowPortalOps(kind="slow")
    res = _machine(ops, REWARM_MAX_ATTEMPTS=2).run()

    assert ops.logins == 1, f"re-logged in {ops.logins}x over portal slowness"
    # F40 (run 1023): the login is HELD and re-warmed in process until the portal answers -- the
    # double's outage lifts after 99 failed warm-ups. This used to log "holding" and return False,
    # and the caller then failed the worker into a respawn = a FRESH login, the very thing avoided.
    assert res.outcome == "warmed" and ops.warmups == 100


def test_a_capped_stress_hold_still_terminates_without_a_re_login():
    """Resilience is not "never fail": past STRESS_HOLD_MAX_S a still-slow portal ends the worker."""
    ops = _SlowPortalOps(kind="slow")
    res = _machine(ops, REWARM_MAX_ATTEMPTS=2, STRESS_HOLD_MAX_S=30, STRESS_HOLD_GAP=10).run()
    assert res.outcome == "failed" and ops.logins == 1
    assert ops.warmups == 1 + 2 + 3      # initial + REWARM_MAX_ATTEMPTS + 30s/10s held re-warms


def test_stress_hold_off_keeps_the_old_slow_portal_behaviour():
    ops = _SlowPortalOps(kind="slow")
    res = _machine(ops, REWARM_MAX_ATTEMPTS=2, STRESS_HOLD=False).run()
    assert res.outcome == "failed" and ops.logins == 1 and ops.warmups == 3


def test_an_unreachable_portal_still_escalates():
    """The opposite case: the request never landed, so the session/proxy really is the problem."""
    ops = _SlowPortalOps(kind="unreachable")
    _machine(ops, REWARM_MAX_ATTEMPTS=2).run()
    assert ops.logins == 2, "an unreachable portal must still reach the re-login rung"


def test_an_engine_without_the_classifier_keeps_the_old_behaviour():
    """SimEngineOps and older doubles have no warmup_failure_kind -- they must not silently change."""
    ops = _Ops(warmup_failures=99)
    assert not hasattr(ops, "warmup_failure_kind")
    _machine(ops, REWARM_MAX_ATTEMPTS=2).run()
    assert ops.logins == 2


# ── re-warm after N consecutive apply bounces (operator, 2026-08-21) ─────────
class _BouncingOps(_Ops):
    """Apply always bounces. Counts warmups so we can see the re-warm actually happen."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self.applies = 0
        self.released = 0

    MAX_ASSIGNMENTS = 12          # the real orchestrator stops assigning; an unbounded double hangs

    def await_slot_open(self): return "ok"
    def slot_found_seen(self): return True
    def scouts_invalid(self): return False
    def is_stopped(self):
        # A holding worker with nothing assigned waits FOREVER by design -- that is what HOLDING is
        # for, and production bounds it with a stop or the booking timeout. A double that never
        # stops just hangs the test.
        return getattr(self, "_assigned", 0) > self.MAX_ASSIGNMENTS

    def await_assignment(self):
        self._assigned = getattr(self, "_assigned", 0) + 1
        return None if self._assigned > self.MAX_ASSIGNMENTS else {"date": "2026-09-01", "period": 5}

    def do_apply(self, slot):
        self.applies += 1
        return "bounced"

    def release_slot(self, slot, reason="bounce"):
        self.released += 1


def test_two_consecutive_bounces_trigger_a_re_warm():
    """Two failures in a row is more likely a stale form anchor than two taken slots, and a re-warm
    is ~38s against a re-login's CAPTCHA solve and sub-account spend.

    SCHEDULE mode: the proactive post-bounce re-warm is gated to schedule mode + a non-blind slot
    (2026-10-16). In auth_only do_apply re-warms unconditionally next round, so a proactive re-warm
    here is redundant and misleadingly leaves the worker WARMED while KEEPING -- see
    test_config_and_phases.test_auth_only_does_not_proactively_rewarm_after_bounces."""
    ops = _BouncingOps()
    cfg = EngineConfig.from_settings({"ROLE": "apply", "KEEPALIVE_MODE": "schedule",
                                      "APPLY_MAX_ATTEMPTS": 5, "APPLY_REWARM_AFTER_BOUNCES": 2})
    PhaseMachine(cfg, ops).run()
    # bounces at 1,2,3,4,5 -> re-warm on 2 and 4, park at 5. 1 initial warmup + 2 re-warms.
    # bounces 1..12, re-warm on every 2nd that is not the park boundary (5, 10).
    assert ops.warmups > 1, f"no re-warm happened (warmups={ops.warmups})"
    assert ops.logins == 1, "a bounce must never cost a re-login"


def test_the_park_bound_still_applies():
    """The outer budget protects the Hub's false-slot detection from one degraded session."""
    ops = _BouncingOps()
    cfg = EngineConfig.from_settings({"ROLE": "apply", "APPLY_MAX_ATTEMPTS": 5,
                                      "APPLY_REWARM_AFTER_BOUNCES": 2})
    PhaseMachine(cfg, ops).run()
    # _park backs off and keeps holding -- it does not end the run -- so the count is bounded by
    # the double's assignment supply, not by max_bounces. What matters is that every bounce handed
    # its slot back and none of them cost a re-login.
    assert ops.released == ops.applies, "every bounce must hand its slot back"
    assert ops.logins == 1


def test_rewarm_cadence_is_configurable():
    ops = _BouncingOps()
    cfg = EngineConfig.from_settings({"ROLE": "apply", "KEEPALIVE_MODE": "schedule",
                                      "APPLY_MAX_ATTEMPTS": 6, "APPLY_REWARM_AFTER_BOUNCES": 3})
    PhaseMachine(cfg, ops).run()
    assert ops.warmups > 1, "a cadence of 3 must still produce re-warms"



# ── F40: hold the login through portal stress (run 1023, 2026-10-23) ─────────────────────────
class _StressedApplyOps(_AlwaysSessionOps):
    """do_apply's round-start re-warm keeps failing because the PORTAL is down (kind slow)."""
    def __init__(self, rounds=20, kind="slow", **kw):
        super().__init__(**kw)
        self._rounds, self._kind, self.waits = rounds, kind, 0

    def warmup_failure_kind(self): return self._kind
    def stress_wait(self):
        self.waits += 1
        return False
    def is_stopped(self): return self.applies >= self._rounds


def test_a_portal_outage_at_apply_holds_the_login():
    """Run 1023: 75/123 workers threw a live login away on ONE failed re-warm (portal 503). Now the
    login is held, the next assignment's re-warm is the retry, and it never counts toward the cap."""
    ops = _StressedApplyOps(rounds=20)
    cfg = EngineConfig.from_settings({"ROLE": "apply", "APPLY_MAX_ATTEMPTS": 2})   # cap would be 6
    res = PhaseMachine(cfg, ops).run()

    assert res.outcome == "stopped", f"{res.outcome} {res.detail}"
    assert ops.applies == 20 and ops.released == 20, "every slot still handed back unpenalised"
    assert ops.logins == 1, "no re-login over portal stress"
    assert ops.warmups == 0, "no rebuild re-warm -- the next do_apply's re-warm IS the retry"
    assert ops.waits == 20


def test_the_apply_stress_hold_is_capped_then_the_old_ladder_resumes():
    ops = _StressedApplyOps(rounds=100)
    cfg = EngineConfig.from_settings({"ROLE": "apply", "APPLY_MAX_ATTEMPTS": 2,
                                      "STRESS_HOLD_MAX_S": 30, "STRESS_HOLD_GAP": 10})
    res = PhaseMachine(cfg, ops).run()
    assert res.outcome == "failed" and "session unrecoverable" in res.detail
    assert ops.waits == 3, "30s / 10s = 3 holds"
    assert ops.applies == 3 + 6, "then the old cap (APPLY_MAX_ATTEMPTS*3) applies unchanged"


def test_stress_hold_off_is_the_old_apply_behaviour():
    ops = _StressedApplyOps(rounds=100)
    cfg = EngineConfig.from_settings({"ROLE": "apply", "APPLY_MAX_ATTEMPTS": 2, "STRESS_HOLD": False})
    res = PhaseMachine(cfg, ops).run()
    assert res.outcome == "failed" and ops.applies == 6 and ops.waits == 0


def test_an_unreachable_exit_at_apply_is_not_held():
    ops = _StressedApplyOps(rounds=100, kind="unreachable")
    cfg = EngineConfig.from_settings({"ROLE": "apply", "APPLY_MAX_ATTEMPTS": 2})
    res = PhaseMachine(cfg, ops).run()
    assert res.outcome == "failed" and ops.applies == 6 and ops.waits == 0


class _RebuildStressOps(_AlwaysSessionOps):
    """do_apply fails `session` for a non-stress reason; the rebuild's re-warm then meets a portal
    outage that lifts after `outage` more attempts."""
    def __init__(self, outage=5):
        super().__init__(warmup_failures=1 + outage)
        self._k, self.waits = "unknown", 0

    def do_apply(self, slot):
        self.applies += 1
        self._k = "unknown"
        return "session"

    def do_warmup(self):
        ok = super().do_warmup()
        self._k = "ok" if ok else "slow"
        return ok

    def warmup_failure_kind(self): return "slow" if self._k == "slow" else "unknown"
    def stress_wait(self):
        self.waits += 1
        return False
    def is_stopped(self): return self.applies >= 2


def test_rebuild_session_rides_out_the_outage_instead_of_re_logging_in():
    """PA508829 (run 1023): both re-warms in _rebuild_session failed [slow] and it re-logged in
    anyway, into the outage. Now it re-warms on the same login until the portal answers."""
    ops = _RebuildStressOps(outage=5)
    cfg = EngineConfig.from_settings({"ROLE": "apply", "APPLY_MAX_ATTEMPTS": 5})
    PhaseMachine(cfg, ops).run()
    assert ops.logins == 1, f"re-logged in {ops.logins - 1}x into a portal outage"
    assert ops.waits == 6 and ops.warmups == 8   # 1 + 6 held re-warms, then 1 rebuild on round 2

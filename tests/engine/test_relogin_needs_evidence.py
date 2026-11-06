"""Re-login only on POSITIVE evidence the portal login is gone (operator, 2026-11-06).

"In any case, it's not good to decide to re-login. The logged-in session is kept by our own activity
on that logged-in session." A failed warm-up step, the portal's "Request could not be processed"
page or a dead exit says the workflow cursor/tunnel is wrong -- not that the login is gone. Every
ladder used to re-login after one or two failed re-warms on no evidence at all.
"""
from __future__ import annotations

from libs.engine.config import EngineConfig
from libs.engine.phases import PhaseMachine
from tests.engine.test_warmup_resilience import _Ops


class _EvidenceOps(_Ops):
    """_Ops that can say whether the login is gone, and why its last warm-up failed."""

    def __init__(self, *, lost=False, lost_after_warmups=None, reason="", **kw):
        super().__init__(**kw)
        self._lost = lost
        self._lost_after = lost_after_warmups
        self._reason = reason
        self.lost_probes = 0

    def login_lost(self, probe=True):
        if probe:
            self.lost_probes += 1
        if self._lost_after is not None and self.warmups >= self._lost_after:
            return True
        return self._lost

    def warmup_failure_reason(self):
        return self._reason

    def stress_wait(self):
        return False          # no real pause in tests

    def is_stopped(self):
        return False


def _machine(ops, **raw):
    cfg = EngineConfig.from_settings({"ROLE": "scout", "WARMUP_ONLY": True, "REWARM_MAX_ATTEMPTS": 1,
                                      "STRESS_HOLD_GAP": 1.0, **raw})
    return PhaseMachine(cfg, ops)


def test_logged_in_worker_keeps_re_warming_instead_of_re_logging_in():
    ops = _EvidenceOps(warmup_failures=5)          # 1 initial + 1 re-warm + 4 held re-warms -> warm
    res = _machine(ops).run()
    assert res.outcome == "warmed", (res.outcome, res.detail)
    assert ops.logins == 1, "re-logged in with no evidence the login was gone"
    assert ops.warmups == 6
    assert ops.lost_probes == 1, "the login is checked once, decisively, before holding"


def test_positive_evidence_still_re_logs_in():
    ops = _EvidenceOps(warmup_failures=2, lost=True)
    res = _machine(ops).run()
    assert res.outcome == "warmed", (res.outcome, res.detail)
    assert ops.logins == 2, "the portal said the login is gone -- re-login is the cure"


def test_evidence_appearing_mid_hold_switches_to_re_login():
    ops = _EvidenceOps(warmup_failures=3, lost_after_warmups=3)   # 3rd (held) re-warm fails + evidence
    res = _machine(ops).run()
    assert res.outcome == "warmed", (res.outcome, res.detail)
    assert ops.logins == 2


def test_validation_errors_end_the_hold_without_a_re_login():
    """Our own form data is rejected: no re-warm and no re-login can fix it."""
    ops = _EvidenceOps(warmup_failures=99, reason="validation")
    res = _machine(ops).run()
    assert res.outcome == "failed" and res.detail == "warmup"
    assert ops.logins == 1
    assert ops.warmups == 2                       # initial + the one re-warm, then gave up


def test_hold_is_bounded():
    ops = _EvidenceOps(warmup_failures=999)
    res = _machine(ops, WARM_HOLD_MAX_S=5.0, STRESS_HOLD_GAP=1.0).run()
    assert res.outcome == "failed"
    assert ops.logins == 1
    assert ops.warmups <= 2 + 5


def test_the_old_ladder_is_one_setting_away():
    ops = _EvidenceOps(warmup_failures=2)
    res = _machine(ops, RELOGIN_ONLY_ON_AUTH_LOSS=False).run()
    assert res.outcome == "warmed"
    assert ops.logins == 2 and ops.lost_probes == 0

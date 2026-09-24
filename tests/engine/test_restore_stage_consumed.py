"""Warm-up must ALWAYS do real work -- there is no free short-circuit after a restore.

History (why this file exists): traced live 2026-08-20, booking "Test 3018", worker CARLOS FURTADO:
an infinite ~26s loop, 120 slot leases deep. do_warmup() short-circuited on _restore_stage forever,
so the recovery ladder's re-warm was a silent no-op and the worker could never recover -- while
churning a slot lease every cycle. The fix at the time made the short-circuit consume-once.

Superseded 2026-09-23 (KEEPING is authed-only; warmup mandatory): the short-circuit was removed
ENTIRELY, not merely made consume-once. A checkpoint restore yields an AUTHENTICATED session only,
never a warmed/bookable one -- the booking form cursor is navigation-bound, not time-bound, so
there is no restored form-state to trust and warmup must actually run the
questionnaire->ScheduleController->Schedule.jsp chain before every apply. These tests now lock that
stronger invariant: do_warmup() runs the real chain on EVERY call, whatever _restore_stage says.
"""
from __future__ import annotations

import pytest

from apps.worker.ops_live import LiveEngineOps


class _Ops(LiveEngineOps):
    """Bypass __init__ -- do_warmup only touches _run_warmup_steps now."""
    def __init__(self, stage):
        self._restore_stage = stage
        self.warmup_calls = 0
        self.logs: list[str] = []
        self._anchor_src = "checkpoint"
        self._anchor_at = None

    def _log(self, msg):
        self.logs.append(msg)

    def _run_warmup_steps(self):
        self.warmup_calls += 1
        return False          # the degraded session in the live incident (csrf_missing)


@pytest.mark.parametrize("stage", ["alive", "rewarmed", "none"])
def test_warmup_never_shortcircuits_on_restore_stage(stage):
    """Whatever the restore stage, the very first do_warmup() must reach the portal -- the old
    "the warm-up right after a restore is legitimately free" shortcut is gone. A restore is
    authed-only; it never leaves a bookable form cursor to skip re-establishing."""
    ops = _Ops(stage)
    assert ops.do_warmup() is False, "a degraded re-warm must report failure, not a free 'warmed'"
    assert ops.warmup_calls == 1, "the real questionnaire->Schedule.jsp chain must run on call 1"


def test_a_degraded_session_can_never_report_warmed_forever():
    """The loop's signature was warming_up -> warmed in the same instant. Now every call does real
    work and a degraded session honestly reports False every time -- no phantom 'warmed'."""
    ops = _Ops("alive")
    results = [ops.do_warmup() for _ in range(5)]
    assert results == [False, False, False, False, False], results
    assert ops.warmup_calls == 5, "all 5 calls must reach the portal"


def test_no_restore_means_every_call_does_real_work():
    ops = _Ops("none")
    ops.do_warmup(); ops.do_warmup()
    assert ops.warmup_calls == 2


# ── browser-tree leak on session replacement (live incident 2026-08-21) ──────
def test_replacing_a_session_closes_the_previous_one():
    """self.session was reassigned on every login retry, orphaning a whole Chromium tree each time.

    Measured live on "Test 3018": 9 workers cycling on transient chrome-error failures had produced
    588 chromium and 656 node processes -- ~65 per worker against ~29 the day before, which is
    9 workers x ~6 retries x ~11 processes. Self-amplifying, too: orphans raise resource pressure,
    pressure produces more chrome-error navigation failures, each retry leaks another tree.
    """
    closed = []

    class _Sess:
        _driver_pid = 1234
        def close_browser(self): closed.append("closed")

    ops = _Ops("none")
    ops.session = _Sess()
    ops._log = lambda t: None
    LiveEngineOps._close_session_quietly(ops)

    assert closed == ["closed"], "the previous browser must be closed, not orphaned"
    assert ops.session is None


def test_closing_a_missing_session_is_a_no_op():
    ops = _Ops("none")
    ops.session = None
    ops._log = lambda t: None
    LiveEngineOps._close_session_quietly(ops)      # must not raise
    assert ops.session is None


def test_a_close_failure_still_clears_the_slot_and_is_logged():
    """A cleanup failure must not mask the real error, but it must not be silent either -- a close
    that quietly fails is exactly how a tree leaks."""
    logged = []

    class _BadSess:
        _driver_pid = None
        def close_browser(self): raise RuntimeError("hung")

    ops = _Ops("none")
    ops.session = _BadSess()
    ops._log = lambda t: logged.append(t)
    LiveEngineOps._close_session_quietly(ops)

    assert ops.session is None
    assert any("close_browser failed" in t for t in logged), logged

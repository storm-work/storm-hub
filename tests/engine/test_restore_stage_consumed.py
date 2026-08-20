"""A checkpoint-restore short-circuit must be consumed, not permanent.

Traced live 2026-08-20, booking "Test 3018", worker CARLOS FURTADO: an infinite ~26s loop, 120 slot
leases deep. do_warmup() short-circuited on _restore_stage forever, so the recovery ladder's re-warm
was a silent no-op and the worker could never recover -- while churning a slot lease every cycle.
"""
from __future__ import annotations

import pytest

from apps.worker.ops_live import LiveEngineOps


class _Ops(LiveEngineOps):
    """Bypass __init__ -- do_warmup only touches _restore_stage and _run_warmup_steps."""
    def __init__(self, stage):
        self._restore_stage = stage
        self.warmup_calls = 0

    def _run_warmup_steps(self):
        self.warmup_calls += 1
        return False          # the degraded session in the live incident (csrf_missing)


@pytest.mark.parametrize("stage", ["alive", "rewarmed"])
def test_the_restore_shortcircuit_applies_only_once(stage):
    ops = _Ops(stage)
    assert ops.do_warmup() is True, "the warm-up right after a restore is legitimately free"
    assert ops.warmup_calls == 0

    # Every LATER call is the recovery ladder asking for a real re-warm.
    assert ops.do_warmup() is False, "a second re-warm must do real work, not short-circuit"
    assert ops.warmup_calls == 1


def test_a_degraded_session_can_never_report_warmed_forever():
    """The loop's signature: warming_up -> warmed in the same instant, repeatedly."""
    ops = _Ops("alive")
    results = [ops.do_warmup() for _ in range(5)]
    assert results == [True, False, False, False, False], results
    assert ops.warmup_calls == 4, "4 of 5 calls must reach the portal"


def test_no_restore_means_every_call_does_real_work():
    ops = _Ops("none")
    ops.do_warmup(); ops.do_warmup()
    assert ops.warmup_calls == 2

"""Our own gates must not be charged to the slot.

A gate that stops us applying says nothing about the slot; reporting it as "bounced" releases the
lease WITH a penalty, that penalty feeds the Hub's false-slot revert, the slot is closed as phantom,
and the worker parks on "5 consecutive bounces". Measured 2026-08-15: 5082 bounced leases against a
pool that was never phantom.

The booking-breaker gate (a second case here) was REMOVED 2026-09-23 -- do_apply no longer refuses
fleet-wide, so only the concurrency gate remains.
"""
from apps.worker.ops_live import LiveEngineOps


class _Sem:
    def __init__(self, ok): self.ok = ok
    def acquire(self, *a, **k): return self.ok
    def release(self, *a, **k): pass


def _ops(sem=None):
    o = object.__new__(LiveEngineOps)
    o._apply_sem = sem
    o._liveness = None
    o._log = lambda *a, **k: None
    o.identity_id = "i1"
    return o


def test_a_full_concurrency_gate_is_not_the_slots_fault():
    assert _ops(sem=_Sem(False)).do_apply({"date": "2026-09-23", "period": 5}) == "portal_unreachable"

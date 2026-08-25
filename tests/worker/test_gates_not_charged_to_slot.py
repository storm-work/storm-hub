"""Our own gates must not be charged to the slot.

The ONLY deviation from ad1c570 inside do_apply, kept because it protects the slot pool rather than
changing how we talk to the portal. A gate that stops us applying says nothing about the slot;
reporting it as "bounced" releases the lease WITH a penalty, that penalty feeds the Hub's false-slot
revert, the slot is closed as phantom, and the worker parks on "5 consecutive bounces". Measured
2026-08-15: 5082 bounced leases against a pool that was never phantom.
"""
from apps.worker.ops_live import LiveEngineOps


class _Sem:
    def __init__(self, ok): self.ok = ok
    def acquire(self, *a, **k): return self.ok
    def release(self, *a, **k): pass


class _Breaker:
    def __init__(self, tripped): self.tripped = tripped
    def is_tripped(self): return self.tripped
    def record_portal_down(self): pass
    def record_success(self): pass


def _ops(sem=None, breaker=None):
    o = object.__new__(LiveEngineOps)
    o._apply_sem, o._breaker = sem, breaker
    o._log = lambda *a, **k: None
    o.identity_id = "i1"
    return o


def test_a_full_concurrency_gate_is_not_the_slots_fault():
    assert _ops(sem=_Sem(False)).do_apply({"date": "2026-09-23", "period": 5}) == "portal_unreachable"


def test_a_tripped_breaker_is_not_the_slots_fault():
    ops = _ops(sem=_Sem(True), breaker=_Breaker(True))
    assert ops.do_apply({"date": "2026-09-23", "period": 5}) == "portal_unreachable"

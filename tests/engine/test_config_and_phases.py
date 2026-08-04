from libs.domain.enums import SettingsLayer as L
from libs.domain.enums import WorkerRole, WorkerSubstate
from libs.domain.settings import resolve
from libs.engine.config import EngineConfig
from libs.engine.phases import PhaseMachine


# ── config adapter ────────────────────────────────────────────────────────────
def test_engine_config_from_resolved_settings():
    eff = resolve({
        L.TEMPLATE: {"ROLE": "apply", "NAV_TIMEOUT_SCALE": 4.0, "LOGIN_FIELD_RETRIES": 4},
        L.BOOKING: {"DISCOVERY_NAV_RETRIES": 2} if False else {},  # nav_retries is a template knob
    })
    cfg = EngineConfig.from_settings(eff)
    assert cfg.role == WorkerRole.APPLY
    assert cfg.login.nav_timeout_scale == 4.0
    assert cfg.login.field_retries == 4
    assert cfg.discovery.mode == "parallel"       # booking default
    assert cfg.pool.multi_capacity is True


def test_scout_role_implies_capture_only_default():
    eff = resolve({L.TEMPLATE: {"ROLE": "scout"}})
    cfg = EngineConfig.from_settings(eff)
    assert cfg.role == WorkerRole.SCOUT
    assert cfg.capture_only is True


def test_no_book_flag_forced_from_booking():
    eff = resolve({L.BOOKING: {"no_book": True}})
    cfg = EngineConfig.from_settings(eff)
    assert cfg.no_book is True


# ── phase machine (stub ops) ──────────────────────────────────────────────────
class StubOps:
    def __init__(self, **overrides):
        self.calls = []
        self._o = overrides

    def _r(self, name, default):
        self.calls.append(name)
        v = self._o.get(name, default)
        return v() if callable(v) else v

    def do_login(self):   return self._r("do_login", True)
    def do_warmup(self):  return self._r("do_warmup", True)
    def do_swap(self):    return self._r("do_swap", None)
    def await_signal(self): return self._r("await_signal", "slot_open")
    def do_discover(self): return self._r("do_discover", [{"date": "2026-08-18", "period": 4}])
    def push_slots(self, slots): self.calls.append(("push", len(slots)))
    def lease_slot(self): return self._r("lease_slot", None)
    def do_apply(self, slot): return self._r("do_apply", "booked")


def _cfg(**s):
    from libs.domain.enums import SettingsLayer as L
    return EngineConfig.from_settings(resolve({L.TEMPLATE: s}))


def test_apply_worker_books_via_own_discovery():
    ops = StubOps()
    res = PhaseMachine(_cfg(ROLE="apply"), ops).run()
    assert res.outcome == "booked"
    assert res.final_substate == WorkerSubstate.BOOKED
    assert WorkerSubstate.APPLYING in res.visited


def test_apply_worker_books_leased_pool_slot_over_own():
    ops = StubOps(lease_slot={"date": "2026-08-18", "period": 3})
    res = PhaseMachine(_cfg(ROLE="apply"), ops).run()
    assert res.outcome == "booked"


def test_scout_pushes_and_never_books():
    ops = StubOps()
    res = PhaseMachine(_cfg(ROLE="scout"), ops).run()
    assert res.outcome == "discovered"
    assert ("push", 1) in ops.calls
    assert "do_apply" not in ops.calls
    assert WorkerSubstate.BOOKED not in res.visited


def test_no_book_reaches_applying_but_does_not_submit():
    ops = StubOps()
    cfg = EngineConfig.from_settings(
        resolve({__import__("libs.domain.enums", fromlist=["SettingsLayer"]).SettingsLayer.TEMPLATE: {"ROLE": "apply"},
                 __import__("libs.domain.enums", fromlist=["SettingsLayer"]).SettingsLayer.BOOKING: {"no_book": True}})
    )
    res = PhaseMachine(cfg, ops).run()
    assert res.outcome == "dry_run"
    assert WorkerSubstate.APPLYING in res.visited
    assert "do_apply" not in ops.calls


def test_warmup_only_stops_after_warmed():
    ops = StubOps()
    res = PhaseMachine(_cfg(ROLE="apply", WARMUP_ONLY=True), ops).run()
    assert res.outcome == "warmed"
    assert res.final_substate == WorkerSubstate.WARMED
    assert "await_signal" not in ops.calls


def test_swap_records_swapped_substate():
    ops = StubOps(do_swap="104.253.0.1")
    res = PhaseMachine(_cfg(ROLE="apply"), ops).run()
    assert WorkerSubstate.SWAPPED in res.visited


def test_login_failure_is_terminal_failed():
    ops = StubOps(do_login=False)
    res = PhaseMachine(_cfg(ROLE="apply"), ops).run()
    assert res.outcome == "failed"
    assert res.detail == "login"
    assert res.final_substate == WorkerSubstate.FAILED

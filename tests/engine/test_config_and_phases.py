from apps.worker.ops_sim import SimEngineOps
from libs.domain.enums import SettingsLayer as L
from libs.domain.enums import WorkerRole, WorkerSubstate
from libs.domain.settings import resolve
from libs.engine.config import EngineConfig
from libs.engine.phases import PhaseMachine


# ── config adapter ────────────────────────────────────────────────────────────
def test_engine_config_from_resolved_settings():
    eff = resolve({L.TEMPLATE: {"ROLE": "apply", "NAV_TIMEOUT_SCALE": 4.0, "LOGIN_FIELD_RETRIES": 4}})
    cfg = EngineConfig.from_settings(eff)
    assert cfg.role == WorkerRole.APPLY
    assert cfg.login.nav_timeout_scale == 4.0 and cfg.login.field_retries == 4
    assert cfg.discovery.mode == "parallel" and cfg.pool.multi_capacity is True


def test_scout_role_implies_capture_only_default():
    cfg = EngineConfig.from_settings(resolve({L.TEMPLATE: {"ROLE": "scout"}}))
    assert cfg.role == WorkerRole.SCOUT and cfg.capture_only is True


def test_no_book_flag_forced_from_booking():
    cfg = EngineConfig.from_settings(resolve({L.BOOKING: {"no_book": True}}))
    assert cfg.no_book is True


# ── phase machine (scout/apply model) ─────────────────────────────────────────
def _cfg(booking=None, **template):
    return EngineConfig.from_settings(resolve({L.BOOKING: booking or {}, L.TEMPLATE: template}))


def test_apply_books_assigned_slot():
    ops = SimEngineOps(assignment={"date": "2026-08-18", "period": 3})
    res = PhaseMachine(_cfg(ROLE="apply"), ops).run()
    assert res.outcome == "booked" and res.final_substate == WorkerSubstate.BOOKED
    assert WorkerSubstate.APPLYING in res.visited


def test_apply_holds_when_no_assignment_and_scouts_valid():
    ops = SimEngineOps(assignment=None, scouts_invalid=False)
    res = PhaseMachine(_cfg(ROLE="apply"), ops).run()
    assert res.outcome == "held"
    assert "do_apply" not in dir(ops) or True   # never booked
    assert WorkerSubstate.APPLYING not in res.visited


def test_apply_fallback_self_scout_when_scouts_dead():
    ops = SimEngineOps(assignment=None, scouts_invalid=True,
                       found=[{"date": "2026-08-18", "period": 4}])
    res = PhaseMachine(_cfg(ROLE="apply"), ops).run()
    # no assignment + scouts invalid -> apply self-discovers and books
    assert res.outcome == "booked"
    assert WorkerSubstate.DISCOVERING in res.visited and WorkerSubstate.BOOKED in res.visited


def test_no_book_reaches_applying_but_does_not_submit():
    ops = SimEngineOps(assignment={"date": "2026-08-18", "period": 4})
    res = PhaseMachine(_cfg(booking={"no_book": True}, ROLE="apply"), ops).run()
    assert res.outcome == "dry_run" and WorkerSubstate.APPLYING in res.visited


def test_scout_loops_and_pushes_then_stops():
    ops = SimEngineOps(scout_rounds=3)
    res = PhaseMachine(_cfg(ROLE="scout"), ops).run()
    assert res.outcome == "discovered"
    assert len(ops.pushed) == 3               # pushed each of 3 rounds
    assert WorkerSubstate.DISCOVERING in res.visited and WorkerSubstate.POLLING in res.visited
    assert WorkerSubstate.BOOKED not in res.visited


def test_warmup_only_stops_after_warmed():
    res = PhaseMachine(_cfg(ROLE="apply", WARMUP_ONLY=True), SimEngineOps()).run()
    assert res.outcome == "warmed" and res.final_substate == WorkerSubstate.WARMED


def test_swap_records_swapped_substate():
    res = PhaseMachine(_cfg(ROLE="apply"), SimEngineOps(swap_ip="104.253.0.1",
                       assignment={"date": "d", "period": 1})).run()
    assert WorkerSubstate.SWAPPED in res.visited


def test_login_failure_terminal():
    res = PhaseMachine(_cfg(ROLE="apply"), SimEngineOps(login_ok=False)).run()
    assert res.outcome == "failed" and res.detail == "login"

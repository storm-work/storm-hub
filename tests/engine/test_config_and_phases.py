from datetime import date, timedelta

from apps.worker.ops_sim import SimEngineOps
from libs.domain.enums import SettingsLayer as L
from libs.domain.enums import WorkerRole, WorkerSubstate
from libs.domain.settings import resolve
from libs.engine.config import EngineConfig
from libs.engine.phases import PhaseMachine


def _future_weekday(days_ahead: int = 5) -> str:
    """A date guaranteed to be in the future and a weekday, so date-window-dependent tests never
    become a ticking time bomb against a hardcoded past date."""
    d = date.today() + timedelta(days=days_ahead)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


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


def test_apply_with_no_assignment_holds_indefinitely_instead_of_terminating():
    """An empty assignment queue is what HOLDING is FOR -- it is not a failure and it is never a
    reason to discard a warm authed session. The worker must sit in HOLDING and keep re-asking; the
    only thing that ends the run is the operator stop the sim models via stop_after_polls."""
    ops = SimEngineOps(assignment=None, scouts_invalid=False, stop_after_polls=4)
    res = PhaseMachine(_cfg(ROLE="apply"), ops).run()
    assert res.outcome == "stopped"              # NOT "held" -- holding is not an ending
    assert ops.applied == 0                      # never booked, never even tried
    assert res.final_substate == WorkerSubstate.HOLDING
    assert WorkerSubstate.APPLYING not in res.visited


def test_apply_fallback_self_scout_when_scouts_dead():
    fut = _future_weekday()
    ops = SimEngineOps(
        assignment=None, scouts_invalid=True,
        discover_outcomes={fut: {"outcome": "found", "periods": [4]}},
    )
    cfg = EngineConfig.from_settings(resolve({
        L.BOOKING: {"CAL_START_DATE": fut, "CAL_END_DATE": fut, "CAL_NUM_DAYS": 1},
        L.TEMPLATE: {"ROLE": "apply"},
    }))
    res = PhaseMachine(cfg, ops).run()
    # no assignment + scouts invalid -> apply self-discovers (over its own configured date window) and books
    assert res.outcome == "booked"
    assert WorkerSubstate.DISCOVERING in res.visited and WorkerSubstate.BOOKED in res.visited


def test_no_book_reaches_applying_but_does_not_submit():
    ops = SimEngineOps(assignment={"date": "2026-08-18", "period": 4})
    res = PhaseMachine(_cfg(booking={"no_book": True}, ROLE="apply"), ops).run()
    assert res.outcome == "dry_run" and WorkerSubstate.APPLYING in res.visited


# ── "stopped" vs "held" (regression: an explicit operator stop must never come back as "held",
# which events.py maps to FAILED and the scheduler's hold-respawn then relaunches -- silently
# undoing the stop) ────────────────────────────────────────────────────────────────────────────
class _StoppedSignals:
    """Reports an immediate stop from whichever wait_* is called first."""
    def __init__(self):
        self.stopped = True

    def wait_slot_open(self, timeout=900):
        return "stop"

    def wait_assignment(self, timeout=900):
        return None

    def wait_discover_task(self, timeout=900):
        return None


def test_apply_stopped_before_slot_open_reports_stopped_not_held():
    ops = SimEngineOps(signals=_StoppedSignals())
    res = PhaseMachine(_cfg(ROLE="apply"), ops).run()
    assert res.outcome == "stopped"


def test_scout_stopped_before_slot_open_reports_stopped_not_held():
    ops = SimEngineOps(signals=_StoppedSignals())
    res = PhaseMachine(_cfg(ROLE="scout"), ops).run()
    assert res.outcome == "stopped"


class _StoppedDuringAssignmentSignals:
    """slot_open fires normally; the stop arrives while awaiting an assignment."""
    def __init__(self):
        self.stopped = False

    def wait_slot_open(self, timeout=900):
        return "slot_open"

    def wait_assignment(self, timeout=900):
        self.stopped = True
        return None


def test_apply_stopped_while_awaiting_assignment_reports_stopped_not_held():
    ops = SimEngineOps(signals=_StoppedDuringAssignmentSignals(), scouts_invalid=False)
    res = PhaseMachine(_cfg(ROLE="apply"), ops).run()
    assert res.outcome == "stopped"


def test_scout_loops_and_pushes_then_stops():
    ops = SimEngineOps(
        discover_tasks=[["d1"], ["d2"], ["d3"]],
        discover_outcomes={
            "d1": {"outcome": "found", "periods": [1]},
            "d2": {"outcome": "found", "periods": [2]},
            "d3": {"outcome": "found", "periods": [3]},
        },
    )
    res = PhaseMachine(_cfg(ROLE="scout"), ops).run()
    assert res.outcome == "discovered"
    assert len(ops.pushed) == 3               # pushed each of 3 rounds (one date each)
    assert len(ops.reported_dates) == 3        # report_dates called once per round too
    assert WorkerSubstate.DISCOVERING in res.visited and WorkerSubstate.POLLING in res.visited
    assert WorkerSubstate.BOOKED not in res.visited


def test_scout_discover_distinguishes_found_empty_failed():
    # Bug fix #6: a WAF-blocked/failed scan must never be silently pruned like a confirmed-empty one.
    ops = SimEngineOps(
        discover_tasks=[["found_date", "empty_date", "failed_date"]],
        discover_outcomes={
            "found_date": {"outcome": "found", "periods": [7]},
            "empty_date": {"outcome": "empty"},
            "failed_date": {"outcome": "failed"},
        },
    )
    PhaseMachine(_cfg(ROLE="scout"), ops).run()
    assert ops.pushed == [{"date": "found_date", "period": 7}]
    outcomes = {o["date"]: o["outcome"] for o in ops.reported_dates}
    assert outcomes == {"found_date": "found", "empty_date": "empty", "failed_date": "failed"}


def test_scout_inactivate_returns_to_holding_then_resumes_on_a_new_slot_open():
    """False-slot-open revert (operator spec 2026-08-14): an INACTIVATE must drop the scout back to
    HOLDING and re-await slot-open -- NOT terminate it (that's what `None` means) -- so the same
    scout, session intact, is fully reusable in the next genuine window."""
    from libs.engine.phases import INACTIVATE

    ops = SimEngineOps(
        discover_tasks=[["d1"], INACTIVATE, ["d2"]],
        discover_outcomes={"d1": {"outcome": "found", "periods": [1]},
                           "d2": {"outcome": "found", "periods": [2]}},
    )
    res = PhaseMachine(_cfg(ROLE="scout"), ops).run()

    assert res.outcome == "discovered"       # ran to a normal end, never "stopped"
    assert len(ops.pushed) == 2               # discovered BOTH before and after the revert
    # POLLING (end of round 1) -> HOLDING (the revert) -> DISCOVERING (round 2, new window)
    holding_idx = res.visited.index(WorkerSubstate.HOLDING, res.visited.index(WorkerSubstate.POLLING))
    assert res.visited[holding_idx + 1] == WorkerSubstate.DISCOVERING


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


# ── an apply worker must not burn its warm session on ONE attempt (2026-08-14) ─
def test_apply_retries_on_the_same_warm_session_after_a_bounce():
    """Confirmed live (DANYA ISABEL VIEIRA SILVA): 90s to log in, 59s to warm, held 4.5 minutes,
    applied once, bounced back to `questionario` -- and the process EXITED, so hold-respawn paid the
    whole login+warmup again for the next single attempt. The login wall and the CAPTCHA solve are
    the scarce resources; portal traffic is not. A bounced worker still holds a valid warm session
    and must keep using it until the operator stops it."""
    ops = SimEngineOps(assignment={"date": "d1", "period": 3}, apply_outcome="no_slot",
                       stop_after_polls=6)
    res = PhaseMachine(_cfg(ROLE="apply"), ops).run()

    assert res.outcome == "stopped"              # bouncing never ends the run by itself
    assert ops.applied > 1                       # many attempts bought by ONE login
    assert ops.released == ops.applied           # every bounce hands its slot back


def test_apply_form_stale_releases_with_the_no_penalty_reason_and_rewarms():
    """storm-auto's apply_book releases the lease on form_stale too -- with a DIFFERENT penalty, not
    by hoarding it: slot_manager.release(lease, "form_stale") doesn't count toward retiring the slot,
    release(lease, "bounce") does. An earlier version of this file kept the slot to "avoid poisoning
    the pool", which only starved the worker of new assignments while it re-logged in, and hid the
    failure from the Hub completely."""
    ops = SimEngineOps(assignment={"date": "d1", "period": 3},
                       apply_outcome=["form_stale", "booked"], stop_after_polls=8)
    res = PhaseMachine(_cfg(ROLE="apply", REWARM_MAX_ATTEMPTS=2), ops).run()

    assert res.outcome == "booked"
    assert ops.applied == 2
    assert ops.release_reasons == ["form_stale"]   # released, and NOT as a bounce
    assert ops.logins == 1                          # cost no new login below the re-warm cap
    # No extra warm in the loop: do_apply re-warms at the top of every attempt (abc5 -- warming
    # again here would replay steps 2-6 twice).
    assert ops.warmups == 1


def test_apply_form_stale_escalates_to_a_fresh_login_in_process():
    """Past REWARM_MAX_ATTEMPTS only a fresh login rebuilds the anchor (abc5). In-process: same
    worker, same proxy -- never a process exit."""
    ops = SimEngineOps(assignment={"date": "d1", "period": 3},
                       apply_outcome=["form_stale", "booked"], stop_after_polls=8)
    res = PhaseMachine(_cfg(ROLE="apply", REWARM_MAX_ATTEMPTS=1), ops).run()

    assert res.outcome == "booked"
    assert ops.logins == 2                          # the escalation fired...
    assert ops.release_reasons == ["form_stale"]    # ...and the slot went back unpenalised


def test_no_slot_bounce_releases_with_the_penalised_reason():
    """The other tier: a bounce back to the schedule form may mean the slot really is gone, so it
    counts against the slot -- and is the only tier the Hub's false-slot revert may act on."""
    ops = SimEngineOps(assignment={"date": "d1", "period": 3}, apply_outcome="no_slot",
                       stop_after_polls=4)
    PhaseMachine(_cfg(ROLE="apply"), ops).run()
    assert ops.release_reasons and set(ops.release_reasons) == {"bounce"}


def test_apply_parks_after_a_bounce_storm_instead_of_churning_the_pool():
    """storm-auto soak10: one degraded session bounced ~28 good slots off the shared pool. Past
    APPLY_MAX_ATTEMPTS consecutive bounces the worker parks -- keeps its session, backs off -- so it
    cannot manufacture the bounce volume that would convince the Hub its real slots are false."""
    ops = SimEngineOps(assignment={"date": "d1", "period": 3}, apply_outcome="no_slot",
                       stop_after_polls=12)
    details: list[str] = []
    res = PhaseMachine(_cfg(booking={"APPLY_MAX_ATTEMPTS": 2}, ROLE="apply"), ops,
                       on_transition=lambda sub, detail: details.append(detail)).run()

    assert res.outcome == "stopped"
    assert [d for d in details if d.startswith("parked:")], f"never parked; details={details}"
    # parking must cost poll gaps, not slots: far fewer bounces than an unparked run would produce
    assert ops.released < 12


def test_apply_stops_at_the_first_booking():
    ops = SimEngineOps(assignment={"date": "d1", "period": 3}, apply_outcome="booked")
    res = PhaseMachine(_cfg(booking={"APPLY_MAX_ATTEMPTS": 5}, ROLE="apply"), ops).run()
    assert res.outcome == "booked"
    assert ops.applied == 1
    assert ops.released == 0                     # a booked slot is never handed back


def test_apply_bounce_releases_the_slot_so_the_worker_stays_assignable():
    """The lease must be handed back on each no_slot bounce -- otherwise _assign_slots'
    one-active-lease rule leaves the worker warm and idle until SLOT_LEASE_TTL reclaims it minutes
    later. (form_stale is the deliberate exception: that slot is still real, see above.)"""
    ops = SimEngineOps(assignment={"date": "d1", "period": 3}, apply_outcome="no_slot",
                       stop_after_polls=2)
    PhaseMachine(_cfg(ROLE="apply"), ops).run()
    assert ops.released >= 1


# ── a scout whose session dies must rebuild it, not spin (2026-08-14) ─────────────────────────
def test_scout_rebuilds_its_session_after_consecutive_all_failed_rounds():
    """Confirmed live (062e6dd6): logged in fine, then every gettime died with "Execution context
    was destroyed" -- 631 failures over 53 minutes at 0.6s a round -- while the dashboard showed a
    healthy running/polling. All-failed rounds are a property of the session, not the calendar."""
    ops = SimEngineOps(
        discover_tasks=[["d1"], ["d2"], ["d3"], ["d4"]],
        discover_outcomes={d: {"outcome": "failed"} for d in ("d1", "d2", "d3", "d4")},
    )
    res = PhaseMachine(_cfg(booking={"SCOUT_REBUILD_AFTER_FAILED_ROUNDS": 2}, ROLE="scout"), ops).run()

    # 4 all-failed rounds at a cap of 2 = exactly 2 rebuilds. Each is a RE-WARM, not a re-login:
    # the auth session is fine, only the booking form-state has lapsed.
    assert ops.warmups == 3          # initial warmup + 2 re-warms
    assert ops.logins == 1           # and not one extra login
    assert res.outcome in ("discovered", "stopped")   # never "failed": the rebuild worked


def test_scout_does_not_rebuild_while_rounds_are_merely_empty():
    """"empty" is a real answer about the calendar -- rebuilding on it would re-login endlessly
    through every quiet period."""
    ops = SimEngineOps(
        discover_tasks=[["d1"], ["d2"], ["d3"], ["d4"]],
        discover_outcomes={d: {"outcome": "empty"} for d in ("d1", "d2", "d3", "d4")},
    )
    PhaseMachine(_cfg(booking={"SCOUT_REBUILD_AFTER_FAILED_ROUNDS": 2}, ROLE="scout"), ops).run()
    assert ops.logins == 1                   # no rebuild


def test_a_failed_rewarm_is_a_session_problem_not_a_slot_bounce():
    """do_apply returns "session" when its OWN re-warm fails -- it never spoke to the portal about
    this slot. Blaming the slot is what let a dead proxy manufacture 5082 "bounced" leases against a
    pool that was never phantom, park every applier on "5 consecutive bounces", and drive the Hub's
    false-slot revert on real slots (live 2026-08-15)."""
    ops = SimEngineOps(assignment={"date": "d1", "period": 3},
                       apply_outcome=["session", "booked"], stop_after_polls=8)
    res = PhaseMachine(_cfg(ROLE="apply"), ops).run()

    assert res.outcome == "booked"
    assert ops.release_reasons == ["session"]     # released, and NOT as a bounce
    assert ops.logins == 1                        # rebuilt by RE-WARM -- no login burned
    assert ops.warmups == 2


def test_repeated_session_failures_never_park_the_worker_on_bounces():
    """The park exists for a degraded session churning the shared POOL. A worker whose own re-warm
    keeps failing has consumed nothing, so it must rebuild rather than be counted out on bounces."""
    ops = SimEngineOps(assignment={"date": "d1", "period": 3}, apply_outcome="session",
                       stop_after_polls=10)
    details = []
    PhaseMachine(_cfg(booking={"APPLY_MAX_ATTEMPTS": 2}, ROLE="apply"), ops,
                 on_transition=lambda sub, d: details.append(d)).run()

    assert not [d for d in details if d.startswith("parked:")], details
    assert set(ops.release_reasons) == {"session"}


def test_an_unreachable_portal_does_not_cost_the_login():
    """A Chrome network-error page on the SUBMIT means the request never left the machine -- the
    session is fine (ScheduleController had returned 200 seconds earlier). Rebuilding it threw away
    a healthy login and ~10 MINUTES of warmup for one dropped POST, three times in a single run,
    while the run's real constraint is how rarely a login succeeds at all. do_apply re-warms at the
    top of every attempt, so taking the next assignment IS the recovery."""
    ops = SimEngineOps(assignment={"date": "d1", "period": 3},
                       apply_outcome=["portal_unreachable", "booked"], stop_after_polls=8)
    res = PhaseMachine(_cfg(ROLE="apply"), ops).run()

    assert res.outcome == "booked"
    assert ops.logins == 1                        # the login was NOT thrown away
    assert ops.release_reasons == ["session"]     # still released without a slot penalty


def test_a_failed_rewarm_still_rebuilds_the_session():
    """The distinction must hold in the other direction: when OUR warmup is what failed, the
    session really does need rebuilding."""
    ops = SimEngineOps(assignment={"date": "d1", "period": 3},
                       apply_outcome=["session", "booked"], stop_after_polls=8,
                       warmup_ok=[True, False, True])      # the re-warm fails -> escalate
    PhaseMachine(_cfg(ROLE="apply"), ops).run()
    assert ops.logins == 2


# ── recovery ladder: re-warm BEFORE re-login (operator correction 2026-08-19) ─────────────────
def test_a_degraded_scout_rewarms_before_it_re_logins():
    """A re-warm is ~40s and costs nothing scarce; a re-login is minutes, burns a CAPTCHA solve and
    puts the sub-account back through a wall that rejects ~90% of attempts. Nearly always the auth
    session is fine and only the booking form-state has lapsed -- exactly what a re-warm restores.

    Live 2026-08-19: scouts whose gettime failed three rounds went STRAIGHT back to logging_in, and
    so did every holding apply worker."""
    ops = SimEngineOps(
        discover_tasks=[["d1"], ["d2"], ["d3"], ["d4"]],
        discover_outcomes={d: {"outcome": "failed"} for d in ("d1", "d2", "d3", "d4")},
    )
    PhaseMachine(_cfg(booking={"SCOUT_REBUILD_AFTER_FAILED_ROUNDS": 2}, ROLE="scout"), ops).run()

    assert ops.warmups > 1, "never re-warmed"
    assert ops.logins == 1, f"re-logged in when a re-warm would do (logins={ops.logins})"


def test_it_still_re_logins_when_the_rewarm_cannot_recover():
    """The escalation must survive: a re-warm that keeps failing means the workflow anchor is gone
    and only a fresh login rebuilds it (abc5)."""
    ops = SimEngineOps(
        discover_tasks=[["d1"], ["d2"], ["d3"]],
        discover_outcomes={d: {"outcome": "failed"} for d in ("d1", "d2", "d3")},
        warmup_ok=[True, False, True],    # initial warmup OK; the RE-warm fails -> re-login
    )
    PhaseMachine(_cfg(booking={"SCOUT_REBUILD_AFTER_FAILED_ROUNDS": 2}, ROLE="scout"), ops).run()
    assert ops.logins >= 2, "never escalated to a fresh login"

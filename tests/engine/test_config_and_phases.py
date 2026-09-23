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
    assert cfg.discovery.source == "calendar" and cfg.pool.multi_capacity is True


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
    assert res.final_substate == WorkerSubstate.KEEPING
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
    holding_idx = res.visited.index(WorkerSubstate.KEEPING, res.visited.index(WorkerSubstate.POLLING))
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
    # No warm at all in this run: warmup is deferred to when a signal is actually acted on
    # (2026-09-09) -- an immediately-assigned apply worker never calls do_warmup() on its own; and
    # do_apply's own re-warm at the top of every attempt (abc5) is implicit in the sim's outcome,
    # not a tracked do_warmup() call (warming again here would replay steps 2-6 twice).
    assert ops.warmups == 0


def test_apply_form_stale_escalates_to_a_fresh_login_in_process():
    """Past REWARM_MAX_ATTEMPTS only a fresh login rebuilds the anchor (abc5). In-process: same
    worker, same proxy -- never a process exit."""
    ops = SimEngineOps(assignment={"date": "d1", "period": 3},
                       apply_outcome=["form_stale", "booked"], stop_after_polls=8)
    res = PhaseMachine(_cfg(ROLE="apply", REWARM_MAX_ATTEMPTS=1), ops).run()

    assert res.outcome == "booked"
    assert ops.logins == 2                          # the escalation fired...
    assert ops.release_reasons == ["form_stale"]    # ...and the slot went back unpenalised


def test_a_blind_slot_never_takes_the_form_stale_ladder():
    """THE SEAM for blind apply (operator direction 2026-09-18). The test directly above proves that
    form_stale past REWARM_MAX_ATTEMPTS escalates to a fresh LOGIN. For an operator-seeded BLIND
    slot that is the failure the operator named: most of a blind grid is phantom by construction,
    apply_path_proven_healthy records that the portal rejects a well-formed submit for a gone slot
    (which lands as form_stale with the cursor intact), and routing that through the ladder spends
    a good session and a reCAPTCHA strike on a combination that never existed.

    Same harness, same REWARM_MAX_ATTEMPTS=1, same form_stale outcome -- the ONLY difference is
    `blind: True` on the assignment. It must go down the no_slot path: release with the bounce
    penalty (so the Hub can retire the combination), no second login, on to the next slot."""
    ops = SimEngineOps(assignment={"date": "d1", "period": 3, "blind": True},
                       apply_outcome=["form_stale", "booked"], stop_after_polls=8)
    res = PhaseMachine(_cfg(ROLE="apply", REWARM_MAX_ATTEMPTS=1), ops).run()

    assert res.outcome == "booked"
    assert ops.logins == 1, "a blind form_stale must never escalate to re-login"
    assert ops.release_reasons == ["bounce"], (
        "released with the BOUNCE penalty so the Hub can retire a phantom combination, "
        f"got {ops.release_reasons}")


def test_a_discovered_slot_still_takes_the_form_stale_ladder():
    """Control: the blind rule must not leak. Without `blind`, the identical scenario keeps the
    proven behaviour (release as form_stale, escalate to re-login)."""
    ops = SimEngineOps(assignment={"date": "d1", "period": 3},
                       apply_outcome=["form_stale", "booked"], stop_after_polls=8)
    PhaseMachine(_cfg(ROLE="apply", REWARM_MAX_ATTEMPTS=1), ops).run()
    assert ops.logins == 2 and ops.release_reasons == ["form_stale"]


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
    # Just the _rebuild_session rewarm -- no pre-hold warmup any more (2026-09-09, deferred to
    # when a signal is actually acted on; this worker is assigned immediately so it never calls
    # do_warmup() on its own before do_apply runs).
    assert ops.warmups == 1


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
                       # No pre-hold warmup call any more (2026-09-09) -- index 0 is now
                       # _rebuild_session's own rewarm attempt, which must fail to escalate.
                       warmup_ok=[False, True])      # the re-warm fails -> escalate
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


# ── operator-specified scout ladder (2026-08-20) ─────────────────────────────
def test_scout_rewarms_after_a_SINGLE_failed_first_round():
    """Step 1 of the operator's ladder: slot-open -> discover x1, and one failure is enough.

    Traced on booking "Test 3059", scout 9e505283 (PEDRO TEIXEIRA RAMOS). slot-open fired 11:05:16,
    the first gettime returned special_days={} (an unwarmed session), and the flat threshold of 3
    made it re-run that broken probe for three rounds at ~68s each before permitting a re-warm. The
    re-warm took 40s and discovery then worked immediately -- slot-found at 11:12:35, 7m18s after
    slot-open, ~4.5 minutes of it spent on rounds that could not have succeeded.
    """
    ops = SimEngineOps(
        discover_tasks=[["d1"]],
        discover_outcomes={"d1": {"outcome": "failed"}},
    )
    PhaseMachine(_cfg(booking={"SCOUT_REBUILD_AFTER_FAILED_ROUNDS": 3}, ROLE="scout"), ops).run()
    assert ops.warmups == 2, "one failed round after slot-open must trigger exactly one re-warm"
    assert ops.logins == 1, "a re-warm, never a re-login -- the auth session is fine"


def test_after_the_first_rewarm_the_steady_budget_applies():
    """Step 2: once re-warmed, a working session is not re-warmed over a single unlucky round."""
    ops = SimEngineOps(
        discover_tasks=[["d1"], ["d2"], ["d3"], ["d4"]],
        discover_outcomes={d: {"outcome": "failed"} for d in ("d1", "d2", "d3", "d4")},
    )
    PhaseMachine(_cfg(booking={"SCOUT_REBUILD_AFTER_FAILED_ROUNDS": 3}, ROLE="scout"), ops).run()
    # round1 -> rebuild (budget 1), then budget 3: rounds 2,3,4 -> one more rebuild on round 4.
    assert ops.warmups == 3, f"expected initial + 2 re-warms, got {ops.warmups}"
    assert ops.logins == 1


def test_a_successful_first_round_never_rewarms():
    """Step 3: discovery that works goes straight to the normal discover/poll loop."""
    ops = SimEngineOps(
        discover_tasks=[["d1"], ["d2"]],
        discover_outcomes={d: {"outcome": "empty"} for d in ("d1", "d2")},
    )
    PhaseMachine(_cfg(booking={"SCOUT_REBUILD_AFTER_FAILED_ROUNDS": 3}, ROLE="scout"), ops).run()
    assert ops.warmups == 1, "an 'empty' answer is news about the calendar, not a broken session"
    assert ops.logins == 1


# ── MONO_BOOKING: the worker discovers its own slots and books them ──────────
def test_mono_booking_self_discovers_without_waiting_for_a_scout():
    """storm-auto's architecture: no separate scout fleet, every warmed worker scans and books. The
    split model's idle hold is what lets the booking anchor lapse (147 lapses vs 42 restores,
    2026-08-21) -- a worker running its own discovery rounds keeps it alive the way scouts do."""
    fut = _future_weekday()
    ops = SimEngineOps(
        assignment=None, scouts_invalid=False,          # scouts are ALIVE -- the fallback must not fire
        discover_outcomes={fut: {"outcome": "found", "periods": [4]}},
    )
    cfg = EngineConfig.from_settings(resolve({
        L.BOOKING: {"CAL_START_DATE": fut, "CAL_END_DATE": fut, "CAL_NUM_DAYS": 1,
                    "MONO_BOOKING": True},
        L.TEMPLATE: {"ROLE": "apply"},
    }))
    res = PhaseMachine(cfg, ops).run()
    assert res.outcome == "booked"
    assert WorkerSubstate.DISCOVERING in res.visited


def test_without_mono_a_worker_with_live_scouts_never_self_discovers():
    """The guard rail on the above: with scouts alive and MONO off, the worker must keep holding and
    leave discovery to the scouts -- otherwise every applier duplicates the whole scan."""
    fut = _future_weekday()
    ops = SimEngineOps(
        assignment=None, scouts_invalid=False, stop_after_polls=3,
        discover_outcomes={fut: {"outcome": "found", "periods": [4]}},
    )
    cfg = EngineConfig.from_settings(resolve({
        L.BOOKING: {"CAL_START_DATE": fut, "CAL_END_DATE": fut, "CAL_NUM_DAYS": 1},
        L.TEMPLATE: {"ROLE": "apply"},
    }))
    res = PhaseMachine(cfg, ops).run()
    assert res.outcome == "stopped"
    assert WorkerSubstate.DISCOVERING not in res.visited
    assert ops.applied == 0


def test_mono_shares_what_it_finds_with_the_pool():
    """The old fallback kept its find PRIVATE: the Hub never learned the slot existed, slot_found
    never fired, and every other applier re-scanned the same window for the same thing."""
    fut = _future_weekday()
    ops = SimEngineOps(
        assignment=None, scouts_invalid=False,
        discover_outcomes={fut: {"outcome": "found", "periods": [4]}},
    )
    cfg = EngineConfig.from_settings(resolve({
        L.BOOKING: {"CAL_START_DATE": fut, "CAL_END_DATE": fut, "CAL_NUM_DAYS": 1,
                    "MONO_BOOKING": True},
        L.TEMPLATE: {"ROLE": "apply"},
    }))
    PhaseMachine(cfg, ops).run()
    assert ops.pushed, "a mono worker must push its finds so slot_found fires and others benefit"


# ── Test2 regression: the apply loop must not churn slot leases ───────────────
def test_portal_unreachable_is_bounded_and_parks_instead_of_churning_leases():
    """LIVE REGRESSION (booking "Test2", 2026-08-27).

    `portal_unreachable` had no counter and no cap, AND it reset the session cap, so nothing
    anywhere could stop it. Worker 9fd63b7f took 64 of them in a SINGLE process -- lease, fail,
    release, immediately re-lease -- producing 70 slot leases alone and 102 across the booking in
    45 minutes, with zero bookings.

    The worker must now back off (park) instead of consuming pooled slots forever.
    """
    ops = SimEngineOps(assignment={"date": "2026-09-01", "period": 4},
                       apply_outcome="portal_unreachable", stop_after_polls=12)
    details: list[str] = []
    PhaseMachine(_cfg(ROLE="apply", booking={"APPLY_MAX_ATTEMPTS": 2}), ops,
                 on_transition=lambda sub, detail: details.append(detail)).run()

    # It must PARK -- the session is fine, so this backs off instead of rebuilding or failing.
    assert [d for d in details if d.startswith("parked:")], f"never parked; details={details}"
    # Parking must cost poll gaps, not slots. Before the fix this branch had no counter at all and
    # ran 1:1 with the poll budget (live: 64 in one process). Parking inserts _PARK_POLL_GAPS of
    # back-off, so the same budget must now buy materially fewer lease attempts.
    assert ops.applied < 12, f"unbounded portal_unreachable churn: {ops.applied} applies in 12 polls"
    assert ops.release_reasons, "the slot must be handed back, not hoarded"
    assert set(ops.release_reasons) == {"session"}, \
        "portal_unreachable must release WITHOUT a slot penalty"


def test_portal_unreachable_does_not_clear_the_session_cap():
    """The old code reset session_streak on any outcome != "session", justified as "any other
    answer means the session reached the portal". That is exactly inverted for
    portal_unreachable -- the request never reached the portal -- so it was clearing the very cap
    meant to stop the churn. Alternating the two outcomes must still terminate.
    """
    ops = SimEngineOps(assignment={"date": "2026-09-01", "period": 4},
                       apply_outcome=["session", "portal_unreachable"] * 40, stop_after_polls=400)
    res = PhaseMachine(_cfg(ROLE="apply", booking={"APPLY_MAX_ATTEMPTS": 2}), ops).run()
    assert ops.applied <= 20, (
        f"alternating session/portal_unreachable never hit a cap: {ops.applied} attempts")


def test_a_reachable_portal_still_clears_the_session_cap():
    """Guard the other direction: a real portal answer (a bounce) still proves the session is
    healthy and must keep resetting the session cap, or a worker would fail for the wrong reason."""
    ops = SimEngineOps(assignment={"date": "2026-09-01", "period": 4},
                       apply_outcome=["session", "no_slot"] * 30, stop_after_polls=200)
    PhaseMachine(_cfg(ROLE="apply", booking={"APPLY_MAX_ATTEMPTS": 5}), ops).run()
    # bounced resets session_streak, so the run ends on the BOUNCE budget (park), not on
    # "session unrecoverable" -- i.e. the reset still works where it is correct.
    assert ops.applied > 2


def test_the_exact_test2_recovery_loop_terminates_instead_of_churning():
    """INTEGRATION REGRESSION -- the full sequence observed live on booking "Test2", 2026-08-27.

    Observed: WARMED -> HOLDING -> APPLYING -> portal origin dies -> re-anchor fails -> apply
    re-warm fails -> roll exit -> re-login/checkpoint restore -> APPLYING again -> identical
    failure, forever. Worker 9fd63b7f ran 64 `portal_unreachable` cycles in a SINGLE process and
    took 70 slot leases on its own; the booking took 102 in 45 minutes and booked nothing.

    Nothing could stop it: `portal_unreachable` had no cap of its own AND reset the session cap.
    This drives the same alternating sequence and requires that the worker reaches a bounded end
    state -- parking and/or the session cap -- rather than leasing indefinitely.
    """
    # Alternate the two outcomes exactly as the live run did (64 unreachable : 7 session).
    script = (["portal_unreachable"] * 9 + ["session"]) * 20
    ops = SimEngineOps(assignment={"date": "2026-09-01", "period": 4},
                       apply_outcome=script, stop_after_polls=60)
    details: list[str] = []
    res = PhaseMachine(_cfg(ROLE="apply", booking={"APPLY_MAX_ATTEMPTS": 3}), ops,
                       on_transition=lambda sub, detail: details.append(detail)).run()

    # It must have applied a brake -- parked, or ended on the session cap. Not neither.
    parked = [d for d in details if d.startswith("parked:")]
    ended_on_cap = "unrecoverable" in (res.detail or "")
    assert parked or ended_on_cap, (
        f"no brake was applied: outcome={res.outcome} detail={res.detail!r} applies={ops.applied}")

    # And the churn must be paced: before the fix this ran 1:1 with the poll budget.
    assert ops.applied < 60, f"still churning: {ops.applied} applies against a 60-poll budget"

    # Every release must be blameless -- neither failure is the slot's fault, so nothing here may
    # feed the Hub's false-slot revert.
    assert set(ops.release_reasons) <= {"session"}, \
        f"a non-slot failure was charged to the slot: {set(ops.release_reasons)}"


# ── a round that reported NOTHING must not clear the rebuild counter (2026-09-02) ─────────────
# ops_live._no_report returns [] when our own tooling failed before the portal was asked (a
# CapSolver outage). That is deliberately neither `failed` (which would re-warm and re-login over a
# fault no session change can fix) nor `empty` (which would prune real dates). It must therefore
# also not RESET the counter: the reset lived in a bare `else`, so an intermittent solver would let
# a genuinely dead session alternate failed/nothing forever and never reach the threshold.
#
# NOTE on the fixture: passing an empty TASK ([]) does not model this -- the scout loop skips a
# round with no dates to scan and never calls do_discover at all. The real shape is dates REQUESTED
# and nothing reported back, so do_discover itself is what has to return [].

class _NoReportOps(SimEngineOps):
    """Reports nothing on alternate rounds, as _no_report does during a solver outage."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.rounds = 0

    def do_discover(self, dates):
        self.rounds += 1
        if self.rounds % 2 == 0:
            return []                      # asked for dates, learned nothing about any of them
        return super().do_discover(dates)


def test_a_round_that_reported_nothing_neither_feeds_nor_clears_the_rebuild_counter():
    # SEVEN rounds, not four. `rebuild_after` starts at 1 (step 1 deliberately spends a single round
    # before re-warming), so the FIRST all-failed round always rebuilds and zeroes the counter --
    # the reset under test cannot matter until after that. Only from round 3 does the steady budget
    # of 2 apply, and the divergence shows on round 7: interleaved failures reach the threshold with
    # the guard (3 warmups) and never reach it without one (2).
    dates = [f"d{i}" for i in range(1, 8)]
    ops = _NoReportOps(
        discover_tasks=[[d] for d in dates],
        discover_outcomes={d: {"outcome": "failed"} for d in dates},
    )
    PhaseMachine(_cfg(booking={"SCOUT_REBUILD_AFTER_FAILED_ROUNDS": 2}, ROLE="scout"), ops).run()

    assert ops.warmups == 3, (
        "a round that reported nothing cleared the rebuild counter between all-failed rounds, so a "
        f"dead session could hide behind an intermittent solver forever (warmups={ops.warmups})")


def test_a_round_that_reported_nothing_does_not_itself_trigger_a_rebuild():
    """The other half: reporting nothing is not evidence of a broken session either."""
    class _AlwaysNothing(SimEngineOps):
        def do_discover(self, dates):
            return []
    ops = _AlwaysNothing(discover_tasks=[["d1"], ["d2"], ["d3"], ["d4"]], discover_outcomes={})
    PhaseMachine(_cfg(booking={"SCOUT_REBUILD_AFTER_FAILED_ROUNDS": 2}, ROLE="scout"), ops).run()

    assert ops.warmups == 1, "rebuilt the session over rounds that said nothing about it"
    assert ops.logins == 1
    assert ops.reported_dates == [], "a round that reported nothing still wrote to the pool"


# ── a login failure fails immediately (the booking breaker was REMOVED 2026-09-23) ─────────────
# The breaker used to make _warmup WAIT on a failed do_login ("the fleet is gated, come back
# later"). That gate did more harm than good (froze the fleet on a normal throttle burst and
# oscillated), so it was removed: a failed do_login is now always a real login failure and the
# worker fails immediately. breaker_tripped() survives as a False stub, so this is the only path.

class _FailLoginOps(SimEngineOps):
    """Login always fails."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.login_calls = 0

    def do_login(self) -> bool:
        self.login_calls += 1
        return False


def test_a_login_failure_fails_immediately_without_waiting():
    """A genuine login failure must NOT be waited out (there is no gate to wait on any more), or a
    dead sub-account would hold a proxy lease pointlessly."""
    ops = _FailLoginOps(discover_tasks=[["d1"]], discover_outcomes={})
    res = PhaseMachine(_cfg(ROLE="scout"), ops).run()

    assert res.outcome == "failed" and res.detail == "login"
    assert ops.login_calls == 1, f"a login failure was retried {ops.login_calls} times, expected 1"


# ── the ladder must skip a rung it is known not to fix (2026-09-02) ───────────────────────────
class _BurnedSlotsOps(SimEngineOps):
    """Discovery always fails and reports that only a re-login can clear it -- the {"data":{}} case.
    do_warmup still SUCCEEDS, which is the trap: the old ladder stopped there and called it fixed."""
    def discovery_needs_relogin(self) -> bool:
        return True


def test_a_burned_discovery_session_skips_straight_to_the_re_login():
    ds = [f"d{i}" for i in range(1, 5)]
    ops = _BurnedSlotsOps(discover_tasks=[[d] for d in ds],
                          discover_outcomes={d: {"outcome": "failed"} for d in ds})
    PhaseMachine(_cfg(booking={"SCOUT_REBUILD_AFTER_FAILED_ROUNDS": 1}, ROLE="scout"), ops).run()

    assert ops.logins > 1, (
        "the ladder re-warmed and stopped: a successful re-warm returns True, so a session that is "
        "burned for /slots never reaches the re-login rung -- 28b8fe7f sat in that loop for 2h")


def test_an_ordinary_failing_round_still_tries_the_cheap_rung_first():
    """The skip must be evidence-driven. Without that evidence a re-warm is right and a re-login is
    expensive: minutes, a CAPTCHA solve, and the sub-account back through the login wall."""
    ds = [f"d{i}" for i in range(1, 5)]
    ops = SimEngineOps(discover_tasks=[[d] for d in ds],
                       discover_outcomes={d: {"outcome": "failed"} for d in ds})
    PhaseMachine(_cfg(booking={"SCOUT_REBUILD_AFTER_FAILED_ROUNDS": 1}, ROLE="scout"), ops).run()

    assert ops.logins == 1, "re-logged in over a failure a re-warm was able to handle"
    assert ops.warmups > 1, "the cheap rung was not tried at all"


# ── a round that reported nothing must not emit an empty event (2026-09-02) ───────────────────
def test_every_round_reports_completion_even_when_empty():
    """ROTATION model (2026-09-21): the Hub's dates_reported drain is the ONLY signal that clears a
    scout's in-flight slot and advances the round-robin, so EVERY round must report -- even an empty
    one (classified as a failed round -> advance immediately). Before, an empty round reported
    nothing and left the Hub stuck on that scout until the 180s hang-timeout. This inverts the old
    'suppress empty reports as noise' behaviour: one scout runs at a time now, so it is at most one
    report per round, and the report is functional rather than noise."""
    class _NothingOps(SimEngineOps):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.report_calls = 0

        def do_discover(self, dates):
            return []

        def report_dates(self, outcomes):
            self.report_calls += 1
            super().report_dates(outcomes)

    ops = _NothingOps(discover_tasks=[["d1"], ["d2"], ["d3"]], discover_outcomes={})
    PhaseMachine(_cfg(ROLE="scout"), ops).run()

    assert ops.report_calls == 3, \
        f"every round must report completion for the rotation to advance; got {ops.report_calls}"


def test_a_round_with_real_outcomes_still_reports():
    """The suppression must be about emptiness, not about failure -- `failed` is a real outcome the
    Hub needs, and dropping it would silently disable slot retirement."""
    ops = SimEngineOps(discover_tasks=[["d1"], ["d2"]],
                       discover_outcomes={"d1": {"outcome": "failed"}, "d2": {"outcome": "empty"}})
    PhaseMachine(_cfg(ROLE="scout"), ops).run()

    got = {r["date"]: r["outcome"] for r in ops.reported_dates}
    assert got == {"d1": "failed", "d2": "empty"}, got

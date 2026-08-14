from libs.engine import orchestration as orch


def test_single_mode_picks_one_scout_per_tick():
    state = orch.ScoutPoolState()
    tasks1 = orch.plan_initial_tasks(["s1", "s2", "s3"], ["d1", "d2"], state)
    assert tasks1 == {"s1": ["d1", "d2"]}
    tasks2 = orch.plan_initial_tasks(["s1", "s2", "s3"], ["d1", "d2"], state)
    assert tasks2 == {"s2": ["d1", "d2"]}


def test_split_mode_yields_no_tasks_from_plan_initial_tasks():
    """SPLIT mode's task assignment moved entirely into apps/hub/scheduler.py's _drive_scouts
    (2026-08-14, needs wall-clock time + Redis state for the concurrency window this pure module
    deliberately doesn't have) -- plan_initial_tasks is SINGLE-mode only now."""
    state = orch.ScoutPoolState()
    orch.on_slots_found(state)
    assert state.mode == "split"
    assert orch.plan_initial_tasks(["s1", "s2"], ["d1", "d2", "d3", "d4"], state) == {}


def test_no_scouts_or_no_dates_yields_no_tasks():
    state = orch.ScoutPoolState()
    assert orch.plan_initial_tasks([], ["d1"], state) == {}
    assert orch.plan_initial_tasks(["s1"], [], state) == {}


def test_ensure_awakened_caps_at_limit_and_supplements_on_death():
    state = orch.ScoutPoolState()
    orch.ensure_awakened(state, ["s1", "s2", "s3", "s4", "s5"], limit=2)
    assert state.awakened_scouts == ["s1", "s2"]

    # s1 dies -- a spare (s3) is supplemented in, s2 (still alive) is untouched
    orch.ensure_awakened(state, ["s2", "s3", "s4", "s5"], limit=2)
    assert state.awakened_scouts == ["s2", "s3"]


def test_ensure_awakened_limit_zero_or_negative_means_uncapped():
    state = orch.ScoutPoolState()
    orch.ensure_awakened(state, ["s1", "s2", "s3"], limit=0)
    assert state.awakened_scouts == ["s1", "s2", "s3"]


def test_ensure_awakened_never_exceeds_available_scouts():
    state = orch.ScoutPoolState()
    orch.ensure_awakened(state, ["s1", "s2"], limit=10)
    assert state.awakened_scouts == ["s1", "s2"]


def test_on_dates_reported_does_not_prune_on_a_single_empty_report():
    """Multi-strike retirement (default threshold 3, operator spec 2026-08-14): a lone empty report
    must not prune the date anymore -- that was the old (too-eager) single-report behavior."""
    state = orch.ScoutPoolState(remaining_by_scout={"s1": ["found_d", "empty_d", "failed_d"]})
    orch.on_slots_found(state)   # retirement only applies in the slot-found phase
    retired = orch.on_dates_reported(state, "s1", [
        {"date": "found_d", "outcome": "found"},
        {"date": "empty_d", "outcome": "empty"},
        {"date": "failed_d", "outcome": "failed"},
    ], retire_after_misses=3)
    assert retired == []
    assert sorted(state.remaining_by_scout["s1"]) == ["empty_d", "failed_d", "found_d"]


def test_on_dates_reported_retires_after_enough_consecutive_misses():
    state = orch.ScoutPoolState(remaining_by_scout={"s1": ["d1"]})
    orch.on_slots_found(state)
    for _ in range(2):
        retired = orch.on_dates_reported(state, "s1", [{"date": "d1", "outcome": "empty"}],
                                          retire_after_misses=3)
        assert retired == []
    retired = orch.on_dates_reported(state, "s1", [{"date": "d1", "outcome": "empty"}],
                                     retire_after_misses=3)
    assert retired == ["d1"]
    assert state.remaining_by_scout["s1"] == []


def test_on_dates_reported_zero_threshold_never_retires():
    state = orch.ScoutPoolState(remaining_by_scout={"s1": ["d1"]})
    orch.on_slots_found(state)
    for _ in range(10):
        retired = orch.on_dates_reported(state, "s1", [{"date": "d1", "outcome": "empty"}],
                                          retire_after_misses=0)
        assert retired == []
    assert state.remaining_by_scout["s1"] == ["d1"]


def test_on_dates_reported_found_resets_the_miss_streak():
    state = orch.ScoutPoolState(remaining_by_scout={"s1": ["d1"]})
    orch.on_slots_found(state)
    orch.on_dates_reported(state, "s1", [{"date": "d1", "outcome": "empty"}], retire_after_misses=3)
    orch.on_dates_reported(state, "s1", [{"date": "d1", "outcome": "empty"}], retire_after_misses=3)
    orch.on_dates_reported(state, "s1", [{"date": "d1", "outcome": "found"}], retire_after_misses=3)
    assert state.date_miss_counts["d1"] == 0
    for _ in range(2):
        retired = orch.on_dates_reported(state, "s1", [{"date": "d1", "outcome": "empty"}],
                                          retire_after_misses=3)
        assert retired == []
    assert "d1" in state.remaining_by_scout["s1"]   # still not retired -- streak was reset


def test_on_dates_reported_clears_busy_scouts():
    state = orch.ScoutPoolState(remaining_by_scout={"s1": ["d1"]}, busy_scouts={"s1", "s2"})
    orch.on_dates_reported(state, "s1", [{"date": "d1", "outcome": "found"}])
    assert state.busy_scouts == {"s2"}


def test_on_scout_died_rebalances_and_updates_state():
    state = orch.ScoutPoolState(remaining_by_scout={"s1": ["d1"], "s2": ["d2", "d3"]})
    out = orch.on_scout_died(state, "s2", alive_ids=["s1"])
    assert "s2" not in out
    assert sorted(out["s1"]) == ["d1", "d2", "d3"]
    assert state.remaining_by_scout == out


def test_on_scout_died_clears_busy_scouts():
    state = orch.ScoutPoolState(remaining_by_scout={"s1": ["d1"]}, busy_scouts={"s1"})
    orch.on_scout_died(state, "s1", alive_ids=[])
    assert state.busy_scouts == set()


def test_should_send_assign_dedups_outstanding():
    # Phase F2 -- the confirmed 56-unconsumed-commands bug's general fix: never resend to a worker
    # that already has an outstanding, unacked assignment.
    outstanding = {"w1"}
    assert orch.should_send_assign("w1", outstanding) is False
    assert orch.should_send_assign("w2", outstanding) is True


# ── retirement is a SLOT-FOUND-phase feature only (operator, 2026-08-14) ──────
def test_pre_slot_open_empties_never_accrue_retirement_strikes():
    """Before slot-found the scouts probe a portal that has not released its slots yet, so `empty`
    is the expected answer for every date and says nothing about whether it is worth scanning
    later. Counting those strikes was destructive: at the default threshold of 3, three single-mode
    probe rounds put EVERY date at the limit, and the first split-mode report -- the instant
    discovery starts mattering -- retired the whole range and left the scouts with empty slices."""
    state = orch.ScoutPoolState()
    dates = ["d1", "d2", "d3"]
    for _ in range(5):
        retired = orch.on_dates_reported(
            state, "s1", [{"date": d, "outcome": "empty"} for d in dates], retire_after_misses=3)
        assert retired == []
    assert state.date_miss_counts == {}


def test_slot_found_clears_strikes_carried_from_the_single_mode_phase():
    """Belt to the braces above: even if a strike were recorded before the transition, entering the
    slot-found phase starts retirement accounting from zero."""
    state = orch.ScoutPoolState()
    state.date_miss_counts.update({"d1": 9, "d2": 9})
    orch.on_slots_found(state)
    assert state.date_miss_counts == {}


def test_retirement_takes_full_threshold_after_slot_found():
    state = orch.ScoutPoolState()
    dates = ["d1", "d2"]
    for _ in range(4):                      # pre-slot-open: ignored entirely
        orch.on_dates_reported(state, "s1", [{"date": d, "outcome": "empty"} for d in dates],
                               retire_after_misses=3)
    orch.on_slots_found(state)
    state.remaining_by_scout = {"s1": list(dates)}

    assert orch.on_dates_reported(state, "s1", [{"date": d, "outcome": "empty"} for d in dates],
                                  retire_after_misses=3) == []
    assert orch.on_dates_reported(state, "s1", [{"date": d, "outcome": "empty"} for d in dates],
                                  retire_after_misses=3) == []
    assert orch.on_dates_reported(state, "s1", [{"date": d, "outcome": "empty"} for d in dates],
                                  retire_after_misses=3) == ["d1", "d2"]


def test_busy_scouts_is_still_cleared_in_single_mode():
    """The early return must not skip the concurrency-window bookkeeping -- a scout that reported
    still has to be marked not-busy, or split mode would start with a poisoned window."""
    state = orch.ScoutPoolState(busy_scouts={"s1", "s2"})
    orch.on_dates_reported(state, "s1", [{"date": "d1", "outcome": "empty"}])
    assert state.busy_scouts == {"s2"}

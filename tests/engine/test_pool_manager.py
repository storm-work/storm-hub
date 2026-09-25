from datetime import date

from libs.engine import pool_manager as pm


def test_partition_dates_even_split():
    chunks = pm.partition_dates(["d1", "d2", "d3", "d4"], 2)
    assert len(chunks) == 2
    assert sorted(sum(chunks, [])) == ["d1", "d2", "d3", "d4"]


def test_partition_dates_more_scouts_than_dates():
    chunks = pm.partition_dates(["d1", "d2"], 5)
    # never more chunks than dates -- an empty chunk wastes a scout
    assert len(chunks) == 2
    assert all(chunks)


def test_partition_dates_zero_scouts_or_no_dates():
    assert pm.partition_dates([], 5) == []
    assert pm.partition_dates(["d1"], 0) == []


def test_pick_single_prober_rotates():
    ids = ["s1", "s2", "s3"]
    assert pm.pick_single_prober(ids, 0) == "s1"
    assert pm.pick_single_prober(ids, 1) == "s2"
    assert pm.pick_single_prober(ids, 3) == "s1"   # wraps around


def test_pick_single_prober_no_scouts():
    assert pm.pick_single_prober([], 0) is None


def test_dates_to_prune_only_empty_never_failed():
    outcomes = [
        pm.DateOutcome("found_date", "found"),
        pm.DateOutcome("empty_date", "empty"),
        pm.DateOutcome("failed_date", "failed"),
    ]
    assert pm.dates_to_prune(outcomes) == ["empty_date"]


def test_update_date_miss_counts_increments_on_empty():
    counts: dict[str, int] = {}
    pm.update_date_miss_counts([pm.DateOutcome("d1", "empty")], counts)
    pm.update_date_miss_counts([pm.DateOutcome("d1", "empty")], counts)
    assert counts == {"d1": 2}


def test_update_date_miss_counts_found_resets_to_zero():
    counts = {"d1": 2}
    pm.update_date_miss_counts([pm.DateOutcome("d1", "found")], counts)
    assert counts == {"d1": 0}


def test_update_date_miss_counts_failed_is_untouched():
    counts = {"d1": 2}
    pm.update_date_miss_counts([pm.DateOutcome("d1", "failed")], counts)
    assert counts == {"d1": 2}   # unknown, never counted toward retirement -- bug fix #6


def test_dates_to_retire_reaches_threshold():
    counts = {"d1": 3, "d2": 1}
    assert pm.dates_to_retire(counts, ["d1", "d2"], threshold=3) == ["d1"]


def test_dates_to_retire_zero_threshold_disables():
    counts = {"d1": 99}
    assert pm.dates_to_retire(counts, ["d1"], threshold=0) == []


def test_dates_to_retire_only_considers_given_dates():
    counts = {"d1": 5, "d2": 5}
    assert pm.dates_to_retire(counts, ["d1"], threshold=3) == ["d1"]


# ── multi-capacity slot leasing (operator spec 2026-08-14) ────────────────────
def test_resolve_slot_capacity_auto_uses_capacity_estimate():
    assert pm.resolve_slot_capacity("auto", 8) == 8


def test_resolve_slot_capacity_auto_falls_back_to_default_when_no_estimate():
    assert pm.resolve_slot_capacity("auto", None) == pm.DEFAULT_SLOT_CAPACITY
    assert pm.resolve_slot_capacity("auto", 0) == pm.DEFAULT_SLOT_CAPACITY


def test_resolve_slot_capacity_literal_overrides_estimate():
    assert pm.resolve_slot_capacity("3", 8) == 3


def test_resolve_slot_capacity_unparsable_falls_back_to_default():
    assert pm.resolve_slot_capacity("not-a-number", 8) == pm.DEFAULT_SLOT_CAPACITY


def test_resolve_slot_capacity_non_positive_literal_falls_back_to_default():
    assert pm.resolve_slot_capacity("0", 8) == pm.DEFAULT_SLOT_CAPACITY
    assert pm.resolve_slot_capacity("-1", 8) == pm.DEFAULT_SLOT_CAPACITY


def test_plan_slot_assignments_spreads_across_slots_before_doubling_up():
    """Slots first, appointments second (operator spec 2026-08-20). `remaining` is an ESTIMATE --
    the portal never tells us a slot's true remaining capacity -- so stacking k workers on one slot
    bets all k on that estimate, while spreading them buys k independent outcomes.

    Confirmed live on "Test 3059": at 11:14:17 all four free workers were leased onto the SAME slot
    (f94b0b47) while three other slots sat unassigned. One booked; the other three came back
    bounced/session and had to retry those other slots serially over the next two minutes -- and
    each of those then booked."""
    out = pm.plan_slot_assignments([("s1", 2), ("s2", 3)], ["wA", "wB", "wC", "wD"])
    assert out == [("wA", "s1"), ("wB", "s2"), ("wC", "s1"), ("wD", "s2")]


def test_plan_slot_assignments_operator_example_10_slots_15_workers():
    """"when there are 10 slots and 15 apply workers, lease 10 slots to 10 workers each and first 5
    slots again to the remaining 5 workers"."""
    slots = [(f"s{i}", 2) for i in range(1, 11)]
    workers = [f"w{i}" for i in range(1, 16)]
    out = pm.plan_slot_assignments(slots, workers)

    assert len(out) == 15
    first_round = out[:10]
    assert [w for w, _ in first_round] == [f"w{i}" for i in range(1, 11)]
    assert [s for _, s in first_round] == [f"s{i}" for i in range(1, 11)], "one worker per slot first"
    second_round = out[10:]
    assert [s for _, s in second_round] == [f"s{i}" for i in range(1, 6)], "then wrap to slots 1-5"


def test_plan_slot_assignments_skips_slots_whose_capacity_is_exhausted_mid_deal():
    """A shallow slot drops out of later rounds instead of blocking them."""
    out = pm.plan_slot_assignments([("s1", 1), ("s2", 3)], ["wA", "wB", "wC", "wD"])
    assert out == [("wA", "s1"), ("wB", "s2"), ("wC", "s2"), ("wD", "s2")]


def test_plan_slot_assignments_stops_when_capacity_runs_out_not_workers():
    """Must terminate rather than spin once every slot is drained."""
    out = pm.plan_slot_assignments([("s1", 1), ("s2", 1)], ["wA", "wB", "wC", "wD"])
    assert out == [("wA", "s1"), ("wB", "s2")]


def test_plan_slot_assignments_ignores_zero_and_negative_capacity():
    out = pm.plan_slot_assignments([("s1", 0), ("s2", -3), ("s3", 2)], ["wA", "wB"])
    assert out == [("wA", "s3"), ("wB", "s3")]


def test_plan_slot_assignments_stops_when_workers_run_out():
    out = pm.plan_slot_assignments([("s1", 5)], ["wA", "wB"])
    assert out == [("wA", "s1"), ("wB", "s1")]


def test_plan_slot_assignments_no_slots_or_no_workers():
    assert pm.plan_slot_assignments([], ["wA"]) == []
    assert pm.plan_slot_assignments([("s1", 5)], []) == []


def test_rebalance_redistributes_dead_scout_dates():
    remaining = {"s1": ["d1", "d2"], "s2": ["d3"], "s3": ["d4", "d5"]}
    out = pm.rebalance(remaining, dead_ids={"s2"}, alive_ids=["s1", "s3"])
    assert "s2" not in out
    # the orphaned d3 round-robins to the first alive scout (s1)
    assert out["s1"] == ["d1", "d2", "d3"]
    assert out["s3"] == ["d4", "d5"]
    all_dates = sum(out.values(), [])
    assert sorted(all_dates) == ["d1", "d2", "d3", "d4", "d5"]


def test_rebalance_no_alive_scouts_drops_orphaned():
    remaining = {"s1": ["d1"]}
    out = pm.rebalance(remaining, dead_ids={"s1"}, alive_ids=[])
    assert out == {}


def test_enforce_single_lease_reuses_existing_active():
    leases = [
        {"id": "l1", "worker_id": "w1", "released_at": None},
        {"id": "l2", "worker_id": "w2", "released_at": "2026-01-01"},
    ]
    assert pm.enforce_single_lease(leases, "w1") == leases[0]
    assert pm.enforce_single_lease(leases, "w2") is None   # released, safe to create a new one
    assert pm.enforce_single_lease(leases, "w3") is None   # no existing lease at all


def test_default_date_window_skips_weekends_and_respects_bounds():
    # 2026-08-17 is a Monday; ask for a window starting there, 3 weekdays, no explicit end.
    out = pm.default_date_window(
        start_date="2026-08-17", end_date="", num_days=3, today=date(2026, 8, 12)
    )
    assert out == ["2026-08-17", "2026-08-18", "2026-08-19"]


def test_default_date_window_respects_end_date():
    out = pm.default_date_window(
        start_date="2026-08-17", end_date="2026-08-18", num_days=10, today=date(2026, 8, 12)
    )
    assert out == ["2026-08-17", "2026-08-18"]


def test_default_date_window_covers_the_whole_range_when_end_is_beyond_num_days():
    """Regression (booking 0924-3018, 2026-09-25): CAL_START=Oct5 CAL_END=Nov30 with CAL_NUM_DAYS
    unset (defaulted 20) truncated the window at ~Oct 30, so November availability (297 /slots dates
    starting Nov 3) fell OUTSIDE the scan, every date reported 'empty', and slot_found never fired.
    An explicit end date must scan the WHOLE range -- num_days must not cap it."""
    out = pm.default_date_window(
        start_date="2026-10-05", end_date="2026-11-30", num_days=20, today=date(2026, 9, 25)
    )
    assert len(out) > 20, "the window must not be truncated to num_days when an end date is set"
    assert out[0] == "2026-10-05"
    assert out[-1] == "2026-11-30"        # 2026-11-30 is a Monday (weekday)
    assert "2026-11-03" in out            # the availability the old window missed
    assert all(date.fromisoformat(d).weekday() < 5 for d in out)   # weekdays only, still


def test_default_date_window_default_start_is_tomorrow():
    out = pm.default_date_window(start_date="", end_date="", num_days=1, today=date(2026, 8, 12))
    assert out == ["2026-08-13"]   # 2026-08-13 is a Thursday, a weekday

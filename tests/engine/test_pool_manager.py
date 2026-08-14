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


def test_plan_slot_assignments_fills_one_slots_full_capacity_before_the_next():
    out = pm.plan_slot_assignments([("s1", 2), ("s2", 3)], ["wA", "wB", "wC", "wD"])
    assert out == [("wA", "s1"), ("wB", "s1"), ("wC", "s2"), ("wD", "s2")]


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


def test_default_date_window_default_start_is_tomorrow():
    out = pm.default_date_window(start_date="", end_date="", num_days=1, today=date(2026, 8, 12))
    assert out == ["2026-08-13"]   # 2026-08-13 is a Thursday, a weekday

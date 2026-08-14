"""Hub-facing scout/apply orchestration glue.

SINGLE-mode probe (one scout at a time) before the first slot is found; SPLIT-mode date-partitioned
polling after. Deliberately DB-free and Bus-free: every function here takes/returns plain dicts/ids so
it's unit-testable without a database or Redis, matching the existing pure-function pattern in
apps/hub/scheduler.py (plan_booking_actions/plan_slot_assignments). apps/hub/scheduler.py owns all the
actual Bus.send_command/DB-write side effects and is the only caller of this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import pool_manager


@dataclass
class ScoutPoolState:
    """The Hub's in-memory view of one booking's scout discovery state.

    Rebuilt from DB+Redis on Hub restart (nothing here is meant to be the durable source of truth by
    itself -- see the plan's Phase F9 design note: no safety-critical state lives only in Hub-process
    memory. This object is a cache/derivation the scheduler recomputes, not a system of record.)
    """
    mode: str = "single"                 # "single" | "split"
    rotation_idx: int = 0                # SINGLE-mode round-robin cursor (over `awakened_scouts`)
    remaining_by_scout: dict[str, list[str]] = field(default_factory=dict)
    slot_found: bool = False
    # SINGLE mode's "awaken scout limit" (operator spec, 2026-08-14): only up to SCOUT_AWAKEN_LIMIT
    # scouts are ever in the single-mode rotation pool at a time; the rest stay parked in hold,
    # never probed, until slot-found switches to split mode. A dead awakened scout is replaced from
    # the parked pool -- see ensure_awakened().
    awakened_scouts: list[str] = field(default_factory=list)
    # Wall-clock pacing for single-mode probes (SINGLE_MODE_INTERVAL) -- apps/hub/scheduler.py reads
    # time.time() and compares against this; kept here (not computed here) since this module stays
    # DB/Redis/clock-free for testability.
    last_single_probe_at: float = 0.0
    # SPLIT mode's rolling concurrency window (DISCOVERY_CONCURRENCY): scout ids apps/hub/
    # scheduler.py currently considers to have a discover round in flight. Every scout still gets a
    # persistent slice in `remaining_by_scout` (full fleet coverage) -- this only throttles how many
    # are ACTIVELY executing a round at once, cycling fairly across the fleet as rounds finish.
    busy_scouts: set[str] = field(default_factory=set)
    # Shared, date-keyed empty-report strike count (operator spec, 2026-08-14: multi-strike date
    # retirement) -- deliberately NOT per-scout, so a date's history survives being redistributed to
    # a different scout by rebalance()/on_scout_died().
    date_miss_counts: dict[str, int] = field(default_factory=dict)


def plan_initial_tasks(scout_ids: list[str], candidate_dates: list[str],
                       state: ScoutPoolState) -> dict[str, list[str]]:
    """SINGLE mode only (pre slot-found): pick the next scout in rotation from the CALLER-FILTERED
    eligible set (apps/hub/scheduler.py's _drive_scouts already narrows `scout_ids` down to the
    awakened+available subset before calling this) and hand it a probe of the full candidate range
    -- cheap, "do any slots exist at all" check. Returns {} if there's nothing to probe this tick.

    SPLIT mode's task assignment (persistent per-scout slices + concurrency-window throttling) is
    handled entirely in _drive_scouts now, not here -- it needs wall-clock time and Redis state this
    pure module deliberately doesn't have."""
    if not scout_ids or not candidate_dates or state.mode != "single":
        return {}
    chosen = pool_manager.pick_single_prober(scout_ids, state.rotation_idx)
    state.rotation_idx += 1
    return {chosen: candidate_dates} if chosen else {}


def ensure_awakened(state: ScoutPoolState, scout_ids: list[str], limit: int) -> list[str]:
    """SINGLE mode's awaken-scout-limit (operator spec, 2026-08-14): keep `state.awakened_scouts` at
    up to `limit` members, drawn from `scout_ids`. Drops any awakened scout no longer in
    `scout_ids` (dead/reaped) and "supplements" from the parked remainder to refill the pool --
    "if any scout is degraded or dead during discovery, then supplement one from those in hold
    phase." `limit <= 0` means uncapped (every scout is awakened). Returns the updated list (also
    stored onto `state`)."""
    state.awakened_scouts = [sid for sid in state.awakened_scouts if sid in scout_ids]
    if limit <= 0:
        state.awakened_scouts = list(scout_ids)
        return state.awakened_scouts
    spare = [sid for sid in scout_ids if sid not in state.awakened_scouts]
    while len(state.awakened_scouts) < limit and spare:
        state.awakened_scouts.append(spare.pop(0))
    return state.awakened_scouts


def on_slots_found(state: ScoutPoolState) -> None:
    """First slot found for this booking -> switch SINGLE to SPLIT mode (irreversible per booking).

    Clears the per-date miss counts: strikes accrued before this moment describe a portal that had
    not released its slots yet, and must not count toward retiring a date now that it has."""
    state.slot_found = True
    state.mode = "split"
    state.date_miss_counts.clear()


def on_dates_reported(state: ScoutPoolState, scout_id: str, outcomes: list[dict], *,
                      retire_after_misses: int = 3) -> list[str]:
    """A scout finished scanning its chunk (also means it's no longer "busy" -- removed from
    `state.busy_scouts`, freeing a slot in the split-mode concurrency window). Updates the shared
    per-date miss-count from this round's outcomes, then retires (prunes from scout_id's own
    remaining slice) any date that has now reached `retire_after_misses` -- never a 'failed'
    (unknown) date, bug fix #6. `retire_after_misses <= 0` disables retirement entirely (operator
    on/off toggle: "when off, never retire any date"). Returns the retired dates for the caller to
    persist/log."""
    state.busy_scouts.discard(scout_id)
    # Retirement is a SLOT-FOUND-phase feature only (operator, 2026-08-14). Before slot-found the
    # scouts are probing a portal that has not released its slots yet, so `empty` is the expected
    # answer for every date and says nothing about whether that date is worth scanning later.
    # Counting those strikes was actively destructive: with the default threshold of 3, three
    # single-mode probe rounds put EVERY date in the window at the limit, and the first split-mode
    # report -- the instant discovery actually starts mattering -- retired the entire range and left
    # the scouts with empty slices.
    if state.mode != "split":
        return []
    parsed = [pool_manager.DateOutcome(o["date"], o["outcome"]) for o in outcomes]
    pool_manager.update_date_miss_counts(parsed, state.date_miss_counts)
    remaining = state.remaining_by_scout.get(scout_id, [])
    retire = set(pool_manager.dates_to_retire(state.date_miss_counts, remaining, retire_after_misses))
    if retire:
        state.remaining_by_scout[scout_id] = [d for d in remaining if d not in retire]
    return sorted(retire)


def on_scout_died(state: ScoutPoolState, dead_id: str, alive_ids: list[str]) -> dict[str, list[str]]:
    """Redistribute a dead scout's unscanned dates across surviving scouts. Returns the new
    remaining_by_scout mapping (also stored onto `state`)."""
    state.busy_scouts.discard(dead_id)
    state.remaining_by_scout = pool_manager.rebalance(state.remaining_by_scout, {dead_id}, alive_ids)
    return state.remaining_by_scout


def should_send_assign(worker_id: str, outstanding_assignments: set[str]) -> bool:
    """Phase F2 dedup-before-send: never re-send an assign command to a worker that already has one
    outstanding and unacked. `outstanding_assignments` is the set of worker_ids with a pending,
    unconfirmed assign (e.g. derived from Redis XPENDING on that worker's cmd stream) -- this is the
    general-case fix for the confirmed 2026-08-07 bug (56 unconsumed assign commands, 60 open leases
    on one worker), on top of the signal-latch ordering fix (confirm-before-mark)."""
    return worker_id not in outstanding_assignments

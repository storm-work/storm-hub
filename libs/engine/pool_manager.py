"""Pure, DB-free slot-pool + date-partitioning logic for the scout/apply orchestration model.

Confirmed 4-phase workflow (operator spec 2026-08-05, refined 2026-08-14): SINGLE-mode probe (one
scout at a time, drawn from a capped "awakened" subset -- SCOUT_AWAKEN_LIMIT -- of the full scout
fleet, the rest stay parked in hold) before the first slot is found; SPLIT-mode after, where the
FULL date range is partitioned across the FULL scout fleet (every scout gets a persistent slice, no
one is benched), but actual execution is throttled to DISCOVERY_CONCURRENCY scouts running a
discover round at once, rotating through the fleet -- see apps/hub/scheduler.py's _drive_scouts for
both the awaken/rebalance logic and the concurrency-window bookkeeping (both need wall-clock time
and Redis state, so they don't belong in this pure module). See libs/engine/orchestration.py for the
Hub-facing glue that calls these; this module has no I/O so it's unit-testable standalone, same
pattern as apps/hub/scheduler.py's plan_booking_actions/plan_slot_assignments.

Bug fix #6 (confirmed 2026-08-07): a scout's per-date outcome must stay 3-way (found/empty/failed) --
an 'empty' date is genuinely full and safe to prune from the pool; a 'failed' date (WAF-blocked, nav
error, no marker) is UNKNOWN and must be retried, never pruned, never treated as empty.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


def partition_dates(dates: list[str], num_scouts: int) -> list[list[str]]:
    """Split `dates` into up to `num_scouts` roughly-even chunks (one per scout), round-robin.

    Never hands out more chunks than there are dates (an empty chunk wastes a scout); never fewer
    chunks than min(num_scouts, len(dates)) so available parallelism isn't left on the table.
    """
    if not dates or num_scouts <= 0:
        return []
    n = min(num_scouts, len(dates))
    chunks: list[list[str]] = [[] for _ in range(n)]
    for i, d in enumerate(dates):
        chunks[i % n].append(d)
    return [c for c in chunks if c]


def pick_single_prober(scout_ids: list[str], rotation_idx: int) -> str | None:
    """SINGLE-mode: before the first slot is found, only ONE scout probes at a time (sequential
    rotation) -- keeps the pre-slot-open probe cheap. Returns None if there are no scouts."""
    if not scout_ids:
        return None
    return scout_ids[rotation_idx % len(scout_ids)]


@dataclass(frozen=True)
class DateOutcome:
    date: str
    outcome: str   # found | empty | failed


def dates_to_prune(outcomes: list[DateOutcome]) -> list[str]:
    """Only genuinely-empty dates are safe to prune from the active scan range. A 'failed' date is
    UNKNOWN (WAF/nav error) and must stay in rotation for retry -- this is bug fix #6."""
    return [o.date for o in outcomes if o.outcome == "empty"]


def update_date_miss_counts(outcomes: list[DateOutcome], miss_counts: dict[str, int]) -> dict[str, int]:
    """Multi-strike date retirement (operator spec, 2026-08-14): a single 'empty' report used to be
    enough to prune a date outright (dates_to_prune above); that's too eager -- a real slot can
    legitimately not show up on the very first scan of a date range. Increments the miss-count for
    each 'empty' outcome; a 'found' outcome resets it to 0 (the date is unambiguously active again);
    a 'failed' outcome is left untouched -- UNKNOWN, never counted toward retirement, same bug fix #6
    principle. `miss_counts` is a SHARED, date-keyed dict (not per-scout) so a date's strike history
    survives being redistributed to a different scout (rebalance()). Mutates and returns it."""
    for o in outcomes:
        if o.outcome == "empty":
            miss_counts[o.date] = miss_counts.get(o.date, 0) + 1
        elif o.outcome == "found":
            miss_counts[o.date] = 0
    return miss_counts


def dates_to_retire(miss_counts: dict[str, int], dates: list[str], threshold: int) -> list[str]:
    """Dates among `dates` whose miss_count has reached `threshold` -- safe to retire from the
    active scan range. `threshold <= 0` disables retirement entirely (the operator-requested
    on/off toggle: "when off, never retire any date")."""
    if threshold <= 0:
        return []
    return [d for d in dates if miss_counts.get(d, 0) >= threshold]


def rebalance(
    remaining_by_scout: dict[str, list[str]], dead_ids: set[str], alive_ids: list[str]
) -> dict[str, list[str]]:
    """Redistribute a dead scout's still-unscanned dates across the surviving scouts (round-robin).

    Dates belonging to scouts not in `dead_ids` are left untouched. If there are no alive scouts left,
    the orphaned dates are dropped (nobody to hand them to) -- the caller should treat this booking as
    scout-starved and lean on the apply-as-scout fallback instead (see phases.py's _run_apply).
    """
    out: dict[str, list[str]] = {
        sid: list(dates) for sid, dates in remaining_by_scout.items() if sid not in dead_ids
    }
    orphaned: list[str] = [
        d for sid, dates in remaining_by_scout.items() if sid in dead_ids for d in dates
    ]
    if not alive_ids or not orphaned:
        return out
    for i, d in enumerate(orphaned):
        target = alive_ids[i % len(alive_ids)]
        out.setdefault(target, []).append(d)
    return out


def enforce_single_lease(existing_leases: list[dict], worker_id: str) -> dict | None:
    """One-lease-per-worker invariant (Phase F2 -- the confirmed 2026-08-07 bug: a worker accrued 60
    open leases because nothing checked for an existing one before creating another).

    `existing_leases` is a list of {"id":..., "worker_id":..., "released_at": ...|None} dicts already
    scoped to the slot/booking in question. Returns the worker's existing ACTIVE lease if it already
    has one (the caller must reuse/release it, never blindly create a new one), else None (safe to
    create a new lease).
    """
    for lease in existing_leases:
        if lease.get("worker_id") == worker_id and lease.get("released_at") is None:
            return lease
    return None


DEFAULT_SLOT_CAPACITY = 5   # operator's assumption, used when nothing more specific is known


def resolve_slot_capacity(setting: str, capacity_estimate: int | None) -> int:
    """Multi-capacity leasing (operator spec 2026-08-14): how many concurrent apply-worker leases one
    physical (date, period) slot can absorb before it's exhausted -- e.g. 10 open slots at cap=5 is
    50 leasable units fleet-wide, not 10. `setting` is the raw SLOT_CAPACITY_PER_PERIOD value:
    "auto" defers to the discovering scout's own per-slot Slot.capacity_estimate (when it supplied
    one), else DEFAULT_SLOT_CAPACITY; any other value is parsed as a literal integer cap applied
    fleet-wide regardless of per-slot estimates (unparsable falls back to the default rather than
    raising -- a booking should never lock up over a typo in this field)."""
    if setting == "auto":
        return capacity_estimate if capacity_estimate and capacity_estimate > 0 else DEFAULT_SLOT_CAPACITY
    try:
        parsed = int(setting)
    except (TypeError, ValueError):
        return DEFAULT_SLOT_CAPACITY
    return parsed if parsed > 0 else DEFAULT_SLOT_CAPACITY


def plan_slot_assignments(slots: list[tuple[str, int]], holding_apply_worker_ids: list[str]) -> list[tuple[str, str]]:
    """Pair KEEPING apply workers with slot CAPACITY -- BREADTH FIRST across slots, then depth.

    The bookable surface is an n x m matrix: n slots, each carrying m appointments. Priority runs
    slots-first, appointments-second (operator spec 2026-08-20): give every distinct slot one worker
    before giving any slot a second. With 10 slots and 15 workers that is 10 workers onto 10 slots,
    then the remaining 5 onto slots 1-5.

    This used to drain each slot's capacity before moving on, which is the wrong order under the
    uncertainty we actually have: `remaining` is an ESTIMATE. Nobody knows how many appointments a
    given slot still holds at a given instant -- the portal never tells us -- so stacking k workers
    on one slot bets all k on that estimate, while spreading them bets one worker per independent
    outcome.

    Confirmed live on booking "Test 3059": at 11:14:17 all four free apply workers were leased onto
    the SAME slot (f94b0b47) while bd8cfed2, 548b27ab and d61fd58d sat unassigned. One booked; the
    other three came back bounced/session and had to retry other slots serially over the next two
    minutes -- 548b27ab at 11:15:13, d61fd58d at 11:16:35 -- each of which then booked. Spreading
    those four across four slots up front is the same leases against strictly more independent
    chances.

    `slots` is [(slot_id, remaining_capacity), ...] in priority order, already excluding slots with
    no room; a 0 or negative entry simply contributes nothing rather than being second-guessed here.
    Returns [(worker_id, slot_id), ...], one pair per worker up to total capacity, workers consumed
    in the order given.
    """
    out: list[tuple[str, str]] = []
    workers = iter(holding_apply_worker_ids)
    # [slot_id, remaining] pairs we decrement as we deal rounds off the top.
    rows = [[slot_id, max(0, remaining)] for slot_id, remaining in slots]
    while True:
        dealt = False
        for row in rows:
            if row[1] <= 0:
                continue
            worker_id = next(workers, None)
            if worker_id is None:
                return out
            out.append((worker_id, row[0]))
            row[1] -= 1
            dealt = True
        if not dealt:            # every slot exhausted -- capacity, not workers, ran out
            return out


def default_date_window(*, start_date: str, end_date: str, num_days: int, today: date) -> list[str]:
    """A coarse, special-day-unaware candidate date list (weekdays only) for the configured
    CAL_START_DATE/CAL_END_DATE/CAL_NUM_DAYS window.

    Used by: (a) orchestration.py to build the initial scout task pool -- refined live by each
    scout's own gettime/SPECIAL_DAYS read (libs/engine/discovery/gettime.py's candidate_dates, which
    needs a live fetch this module deliberately doesn't have); (b) the apply-as-scout fallback in
    phases.py, which has no live gettime context of its own yet. Occasionally including a
    closed/special day here is a benign inefficiency (the live scan reports it 'empty'), never a
    correctness bug -- this is exactly why bug fix #6 keeps 'empty' distinct from 'failed'.
    """
    start = date.fromisoformat(start_date) if start_date else today + timedelta(days=1)
    end = date.fromisoformat(end_date) if end_date else None
    cur = max(today + timedelta(days=1), start)
    out: list[str] = []
    # When BOTH a start AND an end date are defined, that pair fully specifies the window -- scan the
    # whole [start, end] range and IGNORE CAL_NUM_DAYS entirely (operator preference 2026-09-25).
    # Capping at 20 while both bounds are set silently truncated the window: confirmed live (booking
    # 0924-3018, CAL_START=2026-10-05 CAL_END=2026-11-30, CAL_NUM_DAYS unset -> defaulted 20). The
    # window stopped at ~Oct 30, but ALL availability was in November -- /slots returned 297 dated
    # slots, none of which fell in the 20-day window, so every date reported 'empty' and slot_found
    # never fired even though the portal handed back months of openings. For the /slots path this is
    # pure loss (the full payload arrives in ONE call regardless of the window). A generous hard cap
    # guards against a pathological range on the per-date calendar path.
    #
    # Otherwise num_days applies: an open-ended window (no end date) is a rolling N-weekday lookahead
    # from the start; a lone end date with no start still caps at num_days but is bounded by end.
    _HARD_CAP = 200
    if start_date and end is not None:
        while cur <= end and len(out) < _HARD_CAP:
            if cur.weekday() < 5:
                out.append(cur.isoformat())
            cur += timedelta(days=1)
    else:
        n = num_days or 20
        while len(out) < n and (end is None or cur <= end):
            if cur.weekday() < 5:
                out.append(cur.isoformat())
            cur += timedelta(days=1)
    return out

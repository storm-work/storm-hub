"""Hub-facing scout/apply orchestration glue.

DISCOVERY (rotation model, 2026-09-21 operator redesign -- replaces the old SINGLE-probe/SPLIT-
partition scheme entirely): one scout at a time runs a FULL-range discovery round, and the Hub
rotates round-robin through the healthy scouts. Successive REAL rounds (ones that actually reached
the portal) are spaced at least DISCOVERY_MIN_INTERVAL apart, so with n healthy scouts any one scout
is revisited about every MIN * n. A FAILED round (dead session, nav error, nothing reported, or a
round that never reports within the hang timeout) does NOT count as a real attempt: the scout is
freed immediately and the rotation advances to the next one with no pacing wait -- so a bad session
never stalls the fleet, and when every scout is unhealthy the attempts just slide through with no
results. The loop runs unchanged for the whole booking; there is no slot-found phase transition.

Deliberately DB-free and Bus-free: every function here takes/returns plain values so it's unit-
testable without a database or Redis, matching the existing pure-function pattern in
apps/hub/scheduler.py. apps/hub/scheduler.py owns all the actual Bus.send_command/DB-write side
effects and is the only caller of this module.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ScoutPoolState:
    """The Hub's in-memory view of one booking's scout-rotation state.

    Rebuilt from scratch on Hub restart (a fresh ScoutPoolState just resumes the rotation cleanly --
    nothing here is a durable system of record). All wall-clock values are passed in by the caller
    (apps/hub/scheduler.py reads time.time()); this module stays clock-free for testability.
    """
    rotation_idx: int = 0            # round-robin cursor over the healthy-scout list
    inflight: str | None = None      # the ONE scout currently running a round (one-at-a-time)
    inflight_since: float = 0.0      # wall-clock the in-flight round was dispatched (hang timeout)
    last_real_probe_at: float = 0.0  # wall-clock of the last round that REACHED the portal; MIN
                                     # pacing is measured from here
    last_round_failed: bool = False  # the last completed/reclaimed round did not reach the portal --
                                     # the next dispatch skips the MIN pacing gate ("move on directly")


def _round_reached_portal(outcomes: list[dict]) -> bool:
    """A round counts as REAL (portal-reached) if it reported at least one date whose outcome is not
    'failed'. An empty report (solver outage / our tooling broke before asking) or an all-'failed'
    round reached nothing, so it is a skip, not a paced attempt. 'empty' (portal said 'no slots') is
    a real answer and DOES pace -- only 'failed'/unknown does not."""
    return bool(outcomes) and not all(o.get("outcome") == "failed" for o in outcomes)


def on_round_reported(state: ScoutPoolState, scout_id: str, outcomes: list[dict], *, now: float) -> None:
    """A scout finished its discovery round. Free it from the in-flight slot and record whether the
    round reached the portal: a real round paces the next one (DISCOVERY_MIN_INTERVAL from `now`); a
    failed/empty one frees the rotation to advance immediately."""
    if state.inflight == scout_id:
        state.inflight = None
    if _round_reached_portal(outcomes):
        state.last_real_probe_at = now
        state.last_round_failed = False
    else:
        state.last_round_failed = True


def plan_rotation_round(state: ScoutPoolState, healthy_ids: list[str], *, now: float,
                        min_interval: float, hang_timeout: float) -> str | None:
    """Decide which scout (if any) should run a FULL-range discovery round this tick. Returns a
    scout id to dispatch, or None to wait. One round in flight at a time; the caller sends the
    chosen scout a `discover` command for the whole date window.

    - An in-flight round blocks new dispatch until it reports -- unless the scout has vanished from
      `healthy_ids` (dead/reaped) or has been running past `hang_timeout`, in which case it is
      reclaimed as a FAILED round (freed, rotation advances immediately, no pacing).
    - With no healthy scouts, returns None (attempts slide through with no results).
    - Pacing: a real previous round holds the next dispatch until min_interval has passed; a failed
      previous round (`last_round_failed`) skips that gate entirely -- move to the next one directly.
    """
    if state.inflight is not None:
        if state.inflight not in healthy_ids or (now - state.inflight_since) >= hang_timeout:
            state.inflight = None
            state.last_round_failed = True   # a hang/vanish is a failure -> advance immediately
        else:
            return None
    if not healthy_ids:
        return None
    if (not state.last_round_failed and state.last_real_probe_at
            and (now - state.last_real_probe_at) < min_interval):
        return None
    chosen = healthy_ids[state.rotation_idx % len(healthy_ids)]
    state.rotation_idx += 1
    state.inflight = chosen
    state.inflight_since = now
    return chosen


def should_send_assign(worker_id: str, outstanding_assignments: set[str]) -> bool:
    """Phase F2 dedup-before-send: never re-send an assign command to a worker that already has one
    outstanding and unacked. `outstanding_assignments` is the set of worker_ids with a pending,
    unconfirmed assign (e.g. derived from Redis XPENDING on that worker's cmd stream) -- this is the
    general-case fix for the confirmed 2026-08-07 bug (56 unconsumed assign commands, 60 open leases
    on one worker), on top of the signal-latch ordering fix (confirm-before-mark)."""
    return worker_id not in outstanding_assignments


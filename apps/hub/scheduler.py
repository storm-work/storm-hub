"""Auto-ops scheduler (spec §7 auto-ops) + resilience mechanisms (Phase D).

A background thread ticks every few seconds and drives scheduled bookings with no operator input:
  - at warmup_start_at  -> START (launch all ready workers), booking -> WARMING
  - at slot_open_at      -> latch the slot-open signal (booking -> RUNNING)
  - continuously          -> assign AVAILABLE pooled slots to KEEPING apply workers
  - continuously          -> booking-timeout, hold-respawn, dead/stall reap, self-scout fallback,
                             signal latch, scout task assignment (D1-D6)

The pure decision functions (plan_booking_actions, pool_manager.plan_slot_assignments,
should_timeout_booking) unit-test without a DB/clock/bus; the thread just applies what they return.
The stateful mechanisms (D2-D6) need DB + Redis + Bus + Supervisor + EventHub, so they're methods,
but each is written to recompute its view from those sources every tick rather than trusting only
in-memory state (Phase F9: a Hub restart must not require manual reconciliation).
"""
from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timedelta, timezone

import logging

from sqlalchemy import and_, func, or_, select, update

from libs.db import models as M
from libs.bus.names import manual_open_key, notified_key
from libs.domain.enums import (
    NOT_BOOKABLE_PREFIX, BookingStatus, SlotLeaseOutcome, SlotStatus, WorkerRole, WorkerStatus,
    WorkerSubstate)
from libs.engine import orchestration, pool_manager

logger = logging.getLogger(__name__)


def _aware(dt: datetime | None) -> datetime | None:
    """Normalize a datetime read back from the DB to UTC-aware.

    Not every backend round-trips tzinfo (SQLite drops it), and every comparison in this file is
    against an aware `now`. Mixing the two raises TypeError -- which, inside the tick's catch-all,
    used to mean the ENTIRE scheduler tick died silently for EVERY booking, forever, with nothing
    logged. Found by the full-flow sim (tests/hub/test_full_flow_sim.py)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

# The statuses in which a booking is actively driving workers. STOPPED is deliberately absent: it is
# resumable (a restart moves it back to WARMING/RUNNING), but while it is stopped nothing may
# relaunch, re-signal, or otherwise resurrect its workers.
_ACTIVE_BOOKING_STATUSES = frozenset({
    BookingStatus.WARMING, BookingStatus.ARMED, BookingStatus.RUNNING,
})

# Statuses in which NO worker may be spawned, however the launch was decided. Deliberately a
# blocklist rather than reusing _ACTIVE_BOOKING_STATUSES: an operator legitimately launches workers
# into a DRAFT or SCHEDULED booking (arm-with-template, manual worker restart), so an allowlist of
# the actively-driving statuses would break those. What must never happen is a spawn landing in a
# booking that has been stopped or has already ended.
_LAUNCH_BLOCKED_BOOKING_STATUSES = frozenset({
    BookingStatus.STOPPED, BookingStatus.COMPLETED, BookingStatus.ABORTED,
})

# Marker written into Worker.substate_detail when a launch raises before the process is spawned
# (_launch_worker_fleet). Such a worker correctly stays READY -- nothing was started, so there is
# nothing to fail -- but READY is a status no watchdog looks at, which made it a DEAD END: the
# worker sat there indefinitely, looking healthy rather than broken.
#
# Confirmed live 2026-08-20: 4 of 9 workers on "Test 3018" failed their proxy lease at 05:39 and
# were still sitting at READY when the operator noticed, with the other 5 running normally. The
# operator's question was exactly "others will not launch?" -- and the answer was no, not without
# clicking Start again. A launch failure is transient and retryable (a bad run of proxy candidates),
# so _hold_respawn now picks these up with the same backoff/cap/kill-before-relaunch it already
# applies to FAILED and HELD.
LAUNCH_FAILED_PREFIX = "launch failed:"

# Machine-readable marker on substate_detail, same convention as LAUNCH_FAILED_PREFIX above, for a
# worker that is KEEPING but NOT BOOKABLE: its keepalive found the schedule anchor gone and the
# in-place re-warm failed to rebuild it.
#
# CONFIRMED LIVE 2026-08-21: 147 "form-state lapsed" against only 42 "re-warm restored" -- 110
# failures, every one of which left the worker reporting a bare `holding`, indistinguishable from a
# healthy warm hold. ops_live's keepalive deliberately does not escalate to a re-login before
# slot-found (the LOOSE HOLD anti-attrition rule, which is correct), but it recorded the failure
# ONLY as a log line: no substate, no detail, no counter. So the Hub counted these workers as ready
# capacity, handed them slot leases the moment slot-open fired, and they bounced.
#
# The worker stays in KEEPING on purpose -- it is still authenticated, still keepalive-ing, and still
# recoverable in place, so neither FAILED (respawn: new proxy + new login) nor a transition-table
# change is warranted. What changes is that _assign_slots stops treating it as leasable until a
# re-warm actually succeeds and clears the marker.
# (the literal itself lives in libs/domain/enums.py -- see the import above)


def plan_booking_actions(status: str, warmup_start_at, slot_open_at, now: datetime) -> list[str]:
    """Pure: which timed transitions are due for a booking. Returns subset of {'start','slot_open'}."""
    actions: list[str] = []
    if status == BookingStatus.SCHEDULED and warmup_start_at and now >= warmup_start_at:
        actions.append("start")
    if status in (BookingStatus.WARMING, BookingStatus.ARMED) and slot_open_at and now >= slot_open_at:
        actions.append("slot_open")
    return actions


def should_timeout_booking(status: str, started_at, timeout_hours: float, now: datetime) -> bool:
    """Pure: booking-level hard stop (replaces the removed per-worker hold timeout). A booking with
    no started_at yet (never actually started) can't time out."""
    if status not in (BookingStatus.WARMING, BookingStatus.ARMED, BookingStatus.RUNNING):
        return False
    if not started_at or not timeout_hours or timeout_hours <= 0:
        return False
    return now >= started_at + timedelta(hours=timeout_hours)


# Module constants matching the confirmed engineering-log values (see reconstruction plan).
_DEAD_GRACE = 90.0             # seconds a RUNNING-in-DB-but-no-process worker gets before reaping
_SELF_SCOUT_THROTTLE = 30.0    # seconds between apply-as-scout fallback triggers, per booking

# A scout counts as alive for the apply-as-scout fallback only once it is PAST THE LOGIN WALL.
# `status == RUNNING` was the old test and it is the wrong one: it means "a process exists", not
# "this scout is scouting". MEASURED 2026-09-01 on booking "Test 0901": scout 28b8fe7f cycled
# logging_in -> failed 245 times over four hours and produced ZERO slots_found, yet it was a RUNNING
# row at any given instant (a worker respawning every ~30s is RUNNING roughly half the time), so it
# permanently suppressed the very fallback that exists for when scouts are not working. The booking
# then sat idle with no path to discover anything.
#
# These substates all mean the login wall is behind it, which is the thing that was failing. A scout
# that reaches them and then stalls is caught by the stall/dead-worker reapers, not here -- this
# guard only has to stop a scout that never gets started from looking productive.
_SCOUTING_SUBSTATES = frozenset({
    WorkerSubstate.LOGGED_IN, WorkerSubstate.WARMING_UP, WorkerSubstate.WARMED,
    WorkerSubstate.SWAPPED, WorkerSubstate.KEEPING, WorkerSubstate.DISCOVERING,
    WorkerSubstate.POLLING,
})


def _scout_is_actually_scouting(w) -> bool:
    return w.status == WorkerStatus.RUNNING and w.substate in _SCOUTING_SUBSTATES




class Scheduler:
    def __init__(
        self, session_local, *, on_start, on_slot_open, on_assign, on_relaunch=None,
        supervisor=None, event_hub=None, redis_client=None, bus=None, tick: float = 5.0,
    ):
        self._SessionLocal = session_local
        self._on_start = on_start          # (db, booking) -> None
        self._on_slot_open = on_slot_open  # (db, booking) -> None
        self._on_assign = on_assign        # (db, worker, slot) -> None
        self._on_relaunch = on_relaunch    # (db, worker) -> None -- single-worker respawn (D2)
        self._supervisor = supervisor
        self._event_hub = event_hub
        self._redis = redis_client
        self._bus = bus
        self._tick = tick
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_self_scout: dict[str, float] = {}   # booking_id -> last self-scout trigger time
        self._scout_pools: dict[str, orchestration.ScoutPoolState] = {}   # booking_id -> pool state

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # A healthy tick is a few ms. Anything approaching the tick interval means timed actions
    # (slot-open, launch) will fire late, so the duration is logged when it crosses this budget.
    _SLOW_TICK_MS = 2000.0

    def _run(self) -> None:
        while not self._stop.is_set():
            _t0 = time.monotonic()
            crashed = False
            try:
                self._tick_once()
            except Exception:
                # NEVER silent. A persistent fault here (a naive datetime, a bad column, anything)
                # stops scheduling for EVERY booking on EVERY tick, and swallowing it meant the
                # only symptom was "nothing happens" -- indistinguishable from an idle system.
                # (Per-booking + per-phase fault isolation lives in _tick_once; reaching HERE means
                # the tick scaffolding itself failed, which should now be rare.)
                crashed = True
                logger.exception("scheduler tick failed")
            # TICK LATENCY INSTRUMENTATION (2026-10-01): a time-critical scheduler must make its own
            # timing observable. A tick that approaches the tick interval is why a scheduled event
            # fires late, so surface the duration the moment it crosses the budget.
            dur_ms = (time.monotonic() - _t0) * 1000.0
            if dur_ms >= self._SLOW_TICK_MS and not crashed:
                logger.warning("scheduler tick took %.0fms (budget %.0fms) -- timed actions may be "
                               "late; something in the tick is blocking", dur_ms, self._SLOW_TICK_MS)
            self._stop.wait(self._tick)

    def _tick_once(self) -> None:
        now = datetime.now(timezone.utc)
        db = self._SessionLocal()
        try:
            live = db.scalars(select(M.Booking).where(
                M.Booking.status.notin_([BookingStatus.COMPLETED, BookingStatus.ABORTED,
                                         BookingStatus.DRAFT]))).all()
            for bk in live:
                # FAULT ISOLATION (2026-10-01): drive each booking independently so a failure on
                # ONE booking cannot abort the tick and skip the CRITICAL scheduling (slot-open /
                # start) of every OTHER booking. Before this, any exception in the loop body killed
                # the whole tick -- which is how one supervisor race (dictionary-changed-size in
                # running_ids, reached via _hold_respawn/_reap) made a scheduled slot-open fire 20
                # min late. See scheduler_tick_pids_race.
                try:
                    self._drive_booking(db, bk, now)
                except Exception:
                    logger.exception("scheduler: driving booking %s failed this tick (isolated -- "
                                     "other bookings and maintenance still run)", bk.id)
                    try:
                        db.rollback()   # leave the session usable for the remaining bookings
                    except Exception:
                        pass

            # Maintenance phases, each fault-isolated: a maintenance failure must never stop the
            # NEXT maintenance phase, and (being after the critical per-booking loop) never the
            # scheduling itself. Non-critical work may be skipped for a tick; a scheduled event may not.
            for _phase in (self._reap_dead_workers, self._reap_stalled_workers,
                           self._reap_runaway_workers, self._sweep_orphan_streams,
                           self._reprobe_static_proxies, self._reconcile_orphaned_leases):
                try:
                    _phase(db, now)
                except Exception:
                    logger.exception("scheduler: maintenance %s failed this tick (isolated)",
                                     getattr(_phase, "__name__", _phase))
                    try:
                        db.rollback()
                    except Exception:
                        pass
        finally:
            db.close()

    def _drive_booking(self, db, bk: M.Booking, now: datetime) -> None:
        """Drive ONE booking for one tick: timed transitions (start/slot-open), the cross-booking
        feed, prewarm, timeout/complete, assignment, discovery and worker respawn. Extracted from
        _tick_once so the per-booking loop can isolate a failure to a single booking. `continue` in
        the old inline loop is `return` here -- identical control flow, one booking's worth."""
        for action in plan_booking_actions(bk.status, _aware(bk.warmup_start_at),
                                           _aware(bk.slot_open_at), now):
            if action == "start":
                _late = (now - _aware(bk.warmup_start_at)).total_seconds() if bk.warmup_start_at else 0
                logger.info("booking %s: START scheduled=%s fired=%s lateness=%.1fs",
                            bk.id, bk.warmup_start_at, now.isoformat(), _late)
                bk.started_at = bk.started_at or now
                db.commit()
                self._on_start(db, bk)
            elif action == "slot_open":
                # Scheduled-vs-actual lateness for the single most time-critical event. A big value
                # means the scheduler was blocked/crashing around the scheduled instant (see the
                # tick-duration warning and [[scheduler_tick_pids_race]]), NOT that the fire itself
                # was slow.
                _late = (now - _aware(bk.slot_open_at)).total_seconds() if bk.slot_open_at else 0
                lvl = logger.warning if _late >= 5 else logger.info
                lvl("booking %s: SLOT-OPEN scheduled=%s fired=%s lateness=%.1fs",
                    bk.id, bk.slot_open_at, now.isoformat(), _late)
                self._fire_slot_open(db, bk, now)
        # BLIND-APPLY MODE: the seeded pool needs no discovery, so there is nothing to WAIT to
        # discover -- fire slot-open the instant the booking is warming/armed and let slot-found fire
        # off the seeded pool below. BUT only when the operator did NOT schedule a slot_open_at: if
        # they set one (e.g. the portal opens its real slots at a known future time and the blind grid
        # mirrors them), HOLD the seeded pool until that scheduled instant instead of hammering it
        # early -- applying blind slots before the portal opens them just bounces for hours and burns
        # exits/accounts. With slot_open_at set, the normal timed action above (plan_booking_actions)
        # fires slot-open exactly at the scheduled time; with none set, apply immediately as before.
        if (bk.status in (BookingStatus.WARMING, BookingStatus.ARMED)
                and bk.slot_open_fired_at is None and bk.slot_open_at is None
                and self._is_blind_mode(db, bk)):
            self._fire_slot_open(db, bk, now)
        # Cross-booking feed: mirror a detector booking's finds into this pool (BEFORE the RUNNING
        # branch so injected slots are assignable the same tick). No-op without a feeder.
        self._mirror_feeder_slots(db, bk, now)
        self._maybe_prewarm(db, bk, now)
        if self._settings_timeout_hours(db, bk) and should_timeout_booking(
            bk.status, bk.started_at, self._settings_timeout_hours(db, bk), now
        ):
            self._stop_booking_core(db, bk, reason="timeout")
            return
        if bk.status == BookingStatus.RUNNING and self._maybe_complete_booking(db, bk):
            return   # target reached -- workers stopped, nothing further to drive
        if bk.status == BookingStatus.RUNNING:
            self._reclaim_stale_slot_leases(db, bk, now)
            self._fire_slot_found_if_new(db, bk, now)
            if self._is_blind_mode(db, bk):
                # Blind mode SUPPLEMENTS discovery (a booking can carry scouts alongside a seeded
                # pool): assign seeded-or-discovered slots, keep scouts discovering, latch signals to
                # late-joining workers, respawn. Skips only the discovered-pool-phantom reverts and
                # self-scout, which would wrongly retire an operator-seeded pool.
                self._assign_slots(db, bk)
                self._drive_scouts(db, bk)
                self._latch_signals(db, bk, now)
                self._hold_respawn(db, bk, now)
                return
            if self._maybe_inactivate_slot_open(db, bk, now):
                return   # reverted to WARMING -- no discovery/assignment work this tick
            if self._maybe_revert_false_slot_found(db, bk, now):
                return   # pool declared false -- don't assign the slots we just wiped
            self._assign_slots(db, bk)
            self._drive_scouts(db, bk)
            self._maybe_self_scout(db, bk, now)
        # Only latch/respawn for an ACTIVELY-driving booking -- never a STOPPED one (that was the
        # "stop button doesn't work" class: a stopped booking's _hold_respawn relaunched a worker).
        if bk.status in _ACTIVE_BOOKING_STATUSES:
            self._latch_signals(db, bk, now)
            self._hold_respawn(db, bk, now)

    # ── F1: Redis state must not outlive the workers it belongs to ────────────
    _STREAM_SWEEP_EVERY = 300.0      # seconds; this is reconciliation, not a hot path
    # A stream touched inside this window is never swept, whatever the worker table says. Registration
    # runs (`reg-...`) have a command stream but NEVER a Worker row, so the sweep saw every one as an
    # orphan: it deleted a run's `launch` between the Hub's XADD and the registrar's first read (a few
    # seconds after spawn), and the registrar then waited forever on a command that no longer existed
    # (2026-10-27, run reg-95d33f911358). Real orphans are by definition idle, so a recency guard costs
    # only a slightly later cleanup.
    _STREAM_SWEEP_MIN_IDLE = 600.0

    def _stream_idle_s(self, key: str) -> float | None:
        """Seconds since this stream was last read or written, or None when Redis cannot say (an
        unknown age is treated as young by the caller: a leaked stream is cheap, a deleted launch
        command is a stuck run)."""
        try:
            return float(self._bus.r.object("idletime", key))
        except Exception:
            pass
        try:
            ms = int(str(self._bus.r.xinfo_stream(key).get("last-generated-id", "0-0")).split("-")[0])
            return max(0.0, time.time() - ms / 1000.0) if ms else None
        except Exception:
            return None

    def _sweep_orphan_streams(self, db, now: datetime) -> None:
        """Delete cmd:<worker_id> streams whose worker row no longer exists.

        delete_worker_stream() is already called on reap, on booking stop, and from
        release_worker_resources -- but those are EVENT paths, and an event path only cleans up what
        it witnesses. Anything that removes a worker without going through one of them (a detach,
        a row deleted while the Hub was down, a worker that predates the cleanup) leaves its stream
        and consumer group behind forever.

        Measured 2026-08-20: 2065 cmd:* streams and 2037 consumer groups against 54 worker rows --
        2036 of them (99%) orphaned. Small today (2571 entries, ~9MB of Redis) but strictly
        monotonic, and Redis on this host runs maxmemory=0/noeviction, so nothing bounds it. That
        combination -- unbounded key growth with no eviction policy -- is the shape of the incident
        that cost this project four days.

        Reconciliation rather than another event hook, on the F9 principle already used for
        supervisor state: recompute the truth from DB+Redis periodically instead of trusting that
        every path remembered to clean up after itself.
        """
        if self._bus is None:
            return
        last = getattr(self, "_last_stream_sweep", 0.0)
        if time.time() - last < self._STREAM_SWEEP_EVERY:
            return
        self._last_stream_sweep = time.time()
        try:
            live = set(db.scalars(select(M.Worker.id)).all())   # scalars() yields ids, not rows
            removed = 0
            for key in self._bus.r.scan_iter("cmd:*", count=500):
                worker_id = key.split(":", 1)[1] if ":" in key else ""
                if worker_id and worker_id not in live:
                    idle = self._stream_idle_s(key)
                    if idle is None or idle < self._STREAM_SWEEP_MIN_IDLE:
                        continue            # young or unknowable: a registration run's launch lives here
                    self._bus.delete_worker_stream(worker_id)
                    removed += 1
            if removed:
                logger.info("swept %d orphaned cmd stream(s) from Redis", removed)
        except Exception:
            # Never let housekeeping kill the tick -- that is the failure mode this file's own
            # catch-all comments are about.
            logger.warning("orphan stream sweep failed; continuing", exc_info=True)

    # ── Phase 8 (D-NEW-1): static resource health reactivation ────────────────
    # Operator decision (Phase 8): re-probe every 600s. Deliberately NOT derived from
    # _STREAM_SWEEP_EVERY or _DEAD_GRACE above -- those govern different, unrelated mechanisms.
    _STATIC_REPROBE_EVERY = 600.0

    def _reprobe_static_proxies(self, db, now: datetime, health_check=None) -> None:
        """Re-probe resource_type='static' rows _candidate_query currently excludes outright
        (D33's health scoping) -- the only way, before this, that such a row ever became leasable
        again was the operator's manual, whole-pool POST /proxies/reset-health.

        `health_check` is injectable (defaults to the real libs.proxies.health.check_proxy),
        mirroring apps/hub/events.py's _handle_proxy_request's own health_check parameter -- same
        reasoning: a caller injecting a fake probe is describing a fake environment and must not get
        a real browser/network call underneath it.

        Candidates are read with NO row lock (find_reactivation_candidates is a plain SELECT), and
        the probe itself runs completely outside any transaction -- identical discipline to
        lease_proxy's own probe loop, which never holds FOR UPDATE across a multi-second browser nav.
        A row this sweep flips healthy becomes an ordinary candidate on the very next lease attempt,
        going through the existing, unmodified _claim_locked / FOR UPDATE SKIP LOCKED path -- this
        sweep never claims or leases anything itself."""
        last = getattr(self, "_last_static_reprobe", 0.0)
        if time.time() - last < self._STATIC_REPROBE_EVERY:
            return
        self._last_static_reprobe = time.time()
        try:
            from libs.proxies import apply_result, check_proxy, conn_url, find_reactivation_candidates

            hc = health_check if health_check is not None else check_proxy
            candidates = find_reactivation_candidates(db)
            if not candidates:
                return
            recovered, still_down = 0, 0
            for proxy in candidates:
                ok, detail = hc(conn_url(proxy))
                apply_result(proxy, ok, detail)
                db.commit()
                if ok:
                    recovered += 1
                else:
                    still_down += 1
            logger.info("static reactivation sweep: %d checked, %d recovered, %d still down",
                       len(candidates), recovered, still_down)
        except Exception:
            logger.warning("static reactivation sweep failed; continuing", exc_info=True)

    # ── Phase 8 (D-NEW-2): F5 orphaned proxy-lease reconciliation ──────────────
    # Operator decision (Phase 8): reconcile leases whose worker is non-RUNNING and whose lease is
    # older than 300s; the sweep itself runs no more often than that same 300s -- one operator-
    # supplied number used for both the sweep's own cadence and the query's age threshold, not two
    # independently invented values (see Phase 8 report for the reasoning).
    _LEASE_RECONCILE_GRACE = 300.0
    _LEASE_RECONCILE_EVERY = 300.0

    def _reconcile_orphaned_leases(self, db, now: datetime) -> None:
        """F5 (FAILURE_MODES.md): every existing release path is edge-triggered -- a worker's own
        terminal event, or one of the three RUNNING-only reap paths above. Two confirmed paths leak a
        lease past all of them: `events.py`'s `_release_on_terminal` swallows any exception from
        `release_worker_resources` with no retry, and `_do_launch_worker`'s launch-failure handler
        (apps/hub/main.py) sets a worker FAILED directly after a successful lease WITHOUT releasing
        it -- that worker never touches RUNNING at all, so the reap paths above never see it.

        Reuses `release_worker_resources` (via the existing `_release_worker_resources` wrapper the
        three reap paths already call) -- no second, lease-only release path. Idempotent by
        construction: `release_proxy`'s own `WHERE released_at IS NULL` makes a repeat call on an
        already-released lease a no-op, so a lease released by the worker's own terminal event
        between this sweep's query and its release call is simply not re-released, not double
        counted as an error."""
        last = getattr(self, "_last_lease_reconcile", 0.0)
        if time.time() - last < self._LEASE_RECONCILE_EVERY:
            return
        self._last_lease_reconcile = time.time()
        try:
            from libs.proxies import find_orphaned_worker_ids

            worker_ids = find_orphaned_worker_ids(db, self._LEASE_RECONCILE_GRACE, now=now)
            for worker_id in worker_ids:
                self._release_worker_resources(db, worker_id)
            if worker_ids:
                logger.info("lease reconciliation: released %d orphaned worker(s): %s",
                           len(worker_ids), ", ".join(worker_ids))
        except Exception:
            logger.warning("lease reconciliation sweep failed; continuing", exc_info=True)

    # ── settings helpers ──────────────────────────────────────────────────────
    def _effective_booking_setting(self, db, bk: M.Booking, key: str, default):
        """Booking-layer settings only (timeout/guards live there, not per-template)."""
        return (bk.settings_json or {}).get(key, default)

    def _settings_timeout_hours(self, db, bk: M.Booking) -> float:
        return float(self._effective_booking_setting(db, bk, "BOOKING_TIMEOUT_HOURS", 48.0))

    # ── D1: booking timeout ──────────────────────────────────────────────────
    def _maybe_complete_booking(self, db, bk: M.Booking) -> bool:
        """Wire MAX_BOOKINGS -- until now NOTHING in the codebase ever set a booking COMPLETED. The
        transition existed in the state machine (RUNNING -> COMPLETED) and no code performed it, so
        a run that achieved everything it was asked to kept its scouts discovering and its appliers
        logging in until the 48h timeout, burning proxies and CAPTCHA solves for nothing.

        0 (the catalog default) means unlimited: no auto-complete, the operator stops it. Any
        positive value is a target -- once that many appointments are actually recorded, the run has
        done its job and stops cleanly as COMPLETED rather than ABORTED. Counting BookedAppointment
        rows (not worker outcomes) deliberately: the row is only written once a booking is confirmed
        AND persisted, so a run can never call itself complete on a booking the Hub failed to
        record -- which is exactly what happened on 2026-08-14."""
        target = int(self._effective_booking_setting(db, bk, "MAX_BOOKINGS", 0))
        if target <= 0:
            return False
        made = db.scalar(select(func.count()).select_from(M.BookedAppointment)
                         .where(M.BookedAppointment.booking_id == bk.id)) or 0
        if made < target:
            return False
        logger.info("booking %s: %d/%d appointments booked -- completing the run", bk.id, made, target)
        self._stop_booking_core(db, bk, reason="target reached", final=BookingStatus.COMPLETED)
        return True

    def _stop_booking_core(self, db, bk: M.Booking, *, reason: str,
                           final: BookingStatus = BookingStatus.ABORTED) -> None:
        """Shared stop path for operator-initiated stop (apps/hub/main.py's /stop endpoint, which
        duplicates the worker-stop half inline for now), timeout, and target-reached completion.
        A timeout is a harder give-up than a manual pause -- ABORTED, not the resumable STOPPED --
        while a run that booked everything it was asked for ends COMPLETED."""
        from libs.domain.state_machines import IllegalTransition, booking_transition
        running = db.scalars(select(M.Worker).where(
            M.Worker.booking_id == bk.id, M.Worker.status == WorkerStatus.RUNNING)).all()
        for w in running:
            if self._bus is not None:
                self._bus.send_command(w.id, "stop")
                self._bus.delete_worker_stream(w.id)
            if self._supervisor is not None:
                self._supervisor.stop(w.id)
            w.status = WorkerStatus.STOPPED
            w.substate = None            # see _fail_worker -- substate belongs to a RUNNING worker
            w.substate_detail = f"booking {reason}"
            self._release_worker_resources(db, w.id)
        if bk.status not in (BookingStatus.COMPLETED, BookingStatus.ABORTED):
            try:
                bk.status = booking_transition(bk.status, final)
            except IllegalTransition:
                pass
        db.commit()

    # ── D2: hold-phase respawn ────────────────────────────────────────────────
    def _hold_respawn(self, db, bk: M.Booking, now: datetime) -> None:
        # Defense-in-depth alongside the "stopped" PhaseResult outcome (libs/engine/phases.py): a
        # STOPPED booking must never have its workers auto-relaunched, full stop, regardless of why
        # any individual worker row ended up FAILED. This was a confirmed bug -- an operator's
        # manual stop got silently undone within one tick because a worker reporting "held" (instead
        # of the now-distinct "stopped") got marked FAILED and respawned right back.
        if bk.status == BookingStatus.STOPPED:
            return
        if not self._effective_booking_setting(db, bk, "HOLD_RESPAWN", True):
            return
        if self._redis is None or self._on_start is None:
            return
        # HELD is included alongside FAILED (2026-08-14): an apply worker whose run ended without a
        # slot, or whose apply bounced, is exactly the case hold-respawn exists to retry. It used to
        # land here only because `held` was mis-mapped to FAILED -- now that it has its own status,
        # it has to be named explicitly or the retry would silently disappear with the mis-mapping.
        # READY-with-a-launch-failure joins FAILED/HELD (2026-08-20). A launch that raises before
        # spawn leaves the worker READY -- correct, nothing started -- but no watchdog watches READY,
        # so those workers never retried and never surfaced as a problem. See LAUNCH_FAILED_PREFIX.
        # Scoped to the marker, NOT to READY generally: a plain READY worker is one an operator has
        # armed but not started, and auto-launching those would start a booking nobody asked to run.
        failed = db.scalars(select(M.Worker).where(
            M.Worker.booking_id == bk.id,
            or_(
                M.Worker.status.in_([WorkerStatus.FAILED, WorkerStatus.HELD]),
                and_(M.Worker.status == WorkerStatus.READY,
                     M.Worker.substate_detail.like(f"{LAUNCH_FAILED_PREFIX}%")),
            ))).all()
        if not failed:
            return
        max_attempts = int(self._effective_booking_setting(db, bk, "MAX_RESPAWN_ATTEMPTS", 5))
        base = float((bk.settings_json or {}).get("RESPAWN_BACKOFF_BASE", 2.0))
        cap = float((bk.settings_json or {}).get("RESPAWN_BACKOFF_CAP", 120.0))
        due: list[M.Worker] = []
        for w in failed:
            key = f"respawn:{w.id}"
            attempt = int(self._redis.hget(key, "attempt") or 0)
            next_eligible = float(self._redis.hget(key, "next_eligible") or 0)
            if attempt >= max_attempts:
                continue   # permanently given up on -- surfaced via /ops/status, never retried forever
            if time.time() < next_eligible:
                continue
            # Verified kill before relaunch (F15): never assume the old process is gone just because
            # the DB says FAILED -- explicitly stop it first so a relaunch can never run alongside a
            # still-alive duplicate (the confirmed incident's leading hypothesis for why relaunching
            # didn't stop the runaway).
            if self._supervisor is not None:
                self._supervisor.stop(w.id)
            backoff = min(cap, base * (2 ** attempt)) * random.uniform(0.8, 1.2)
            self._redis.hset(key, mapping={"attempt": attempt + 1, "next_eligible": time.time() + backoff})
            self._redis.expire(key, 86400)
            if self._on_relaunch is not None:
                due.append(w)

        # Dispatch OFF the tick. _do_launch_worker leases a proxy, and lease_proxy's health_check is
        # libs/proxies/health.check_proxy -- a throwaway headless Chromium per candidate, up to
        # max_probes=5 at ~20s each. Its own docstring says callers on a shared thread (it names the
        # Scheduler tick) "must dispatch this to a background thread/pool rather than call it inline,
        # or every other booking's event/tick handling stalls behind it", and this call site was
        # doing exactly that. Confirmed live 2026-08-21: three real bookings sat RUNNING with
        # slot_open_fired_at set while the tick was stuck in health probes, so plan_booking_actions
        # missed the scheduled slot-open and _latch_signals -- which runs immediately BEFORE this in
        # the same per-booking block -- never delivered the signal to a single scout. Discovery
        # never started, and the Hub accumulated 28 defunct browser children.
        #
        # Batched into ONE fleet dispatch rather than one per worker: _begin_fleet_launch is keyed
        # per BOOKING and returns False while a launch is already in flight, so dispatching
        # individually would let the first worker's launch lock out the other eight -- each of whom
        # has already had its attempt counter incremented above, burning MAX_RESPAWN_ATTEMPTS on
        # launches that never happened.
        if due and self._on_relaunch is not None:
            try:
                self._on_relaunch(db, due)
            except Exception:
                    # A relaunch that can't get a proxy is ORDINARY -- the pool is finite and a
                    # booking routinely wants more workers than there are healthy proxies. It must
                    # not be fatal to the tick. Confirmed live 2026-08-15: lease_proxy raised
                    # ProxyLeaseError, _do_launch_worker re-raised it as an HTTPException (an
                    # HTTP-layer error, in a background thread, where nothing can answer it), and it
                    # propagated out of _hold_respawn -> _tick_once -> the catch-all -- killing slot
                    # assignment, signal latching and every watchdog, for EVERY booking, that tick.
                    # The worker's own row is already left FAILED by _do_launch_worker.
                logger.warning("relaunch dispatch failed for %d worker(s); continuing this tick",
                               len(due), exc_info=True)

    # ── D3: dead-process + stall watchdogs ───────────────────────────────────
    def _reap_dead_workers(self, db, now: datetime) -> None:
        if self._supervisor is None:
            return
        running_ids = set(self._supervisor.running_ids())
        db_running = db.scalars(select(M.Worker).where(M.Worker.status == WorkerStatus.RUNNING)).all()
        for w in db_running:
            if w.id in running_ids:
                continue
            # grace period: tolerate spawn-to-process-registration lag. Compared against the tick's
            # own `now`, not a fresh time.time() call -- keeps this deterministic/testable and
            # consistent with every other time comparison in this tick (should_timeout_booking etc).
            last_seen = self._event_hub.last_seen.get(w.id) if self._event_hub else None
            reference = last_seen or (w.created_at.timestamp() if w.created_at else 0)
            if now.timestamp() - reference < _DEAD_GRACE:
                continue
            self._fail_worker(w, "no live process (dead-worker reap)")
            self._release_worker_resources(db, w.id)
        db.commit()

    def _reap_stalled_workers(self, db, now: datetime) -> None:
        if self._event_hub is None:
            return
        db_running = db.scalars(select(M.Worker).where(M.Worker.status == WorkerStatus.RUNNING)).all()
        for w in db_running:
            bk = db.get(M.Booking, w.booking_id)
            stall_timeout = float(self._effective_booking_setting(db, bk, "WORKER_STALL_TIMEOUT", 1500.0)) if bk else 1500.0
            last_seen = self._event_hub.last_seen.get(w.id)
            if last_seen is None:
                continue   # never heard from it yet -- _reap_dead_workers' grace period covers this
            if time.time() - last_seen < stall_timeout:
                continue
            if self._supervisor is not None:
                self._supervisor.stop(w.id)
            if self._bus is not None:
                self._bus.send_command(w.id, "stop")
            self._fail_worker(w, f"silent for >{stall_timeout:.0f}s (stall reap)")
            self._release_worker_resources(db, w.id)
        db.commit()

    def _reap_runaway_workers(self, db, now: datetime) -> None:
        """Phase F16: the confirmed-incident circuit breaker. A worker sustaining an anomalous event
        rate gets auto-killed within its detection window instead of running unthrottled for an hour
        before anyone notices."""
        if self._event_hub is None:
            return
        db_running = db.scalars(select(M.Worker).where(M.Worker.status == WorkerStatus.RUNNING)).all()
        for w in db_running:
            bk = db.get(M.Booking, w.booking_id)
            limit = float(self._effective_booking_setting(db, bk, "WORKER_EVENT_RATE_LIMIT", 5.0)) if bk else 5.0
            rate = self._event_hub.event_rate(w.id)
            if rate <= limit:
                continue
            if self._supervisor is not None:
                self._supervisor.stop(w.id)
            if self._bus is not None:
                self._bus.send_command(w.id, "stop")
            self._fail_worker(w, f"runaway event rate {rate:.1f}/s > limit {limit:.1f}/s")
            self._release_worker_resources(db, w.id)
        db.commit()

    def _fail_worker(self, w: M.Worker, detail: str) -> None:
        """Mark a reaped worker FAILED, clearing the in-flight `substate` and recording WHY.

        Confirmed bug (2026-08-14): the reap paths wrote only `status`, so a killed worker kept the
        substate of whatever it was last doing -- a fleet reaped mid-hold read `status=failed,
        substate=holding` forever, and the runaway reaper compounded it by writing a
        `substate_detail` ("runaway event rate 220/s") that flatly contradicted the substate sitting
        next to it. `substate` describes what a RUNNING worker is doing; a reaped one isn't running.
        Mirrors apps/hub/main.py's _set_worker_status for the endpoint side."""
        w.status = WorkerStatus.FAILED
        w.substate = None
        w.substate_detail = detail

    def _release_worker_resources(self, db, worker_id: str) -> None:
        """A worker being reaped/stopped must never silently hold a proxy lease, a slot lease, or
        Redis bus state forever (Phase F5). Thin wrapper around the shared
        libs.proxies.lease.release_worker_resources -- apps/hub/events.py's normal-termination path
        calls the same function, so there's exactly one place this cleanup is implemented."""
        from libs.proxies.lease import release_worker_resources
        release_worker_resources(db, worker_id, bus=self._bus)

    # ── D4: apply-as-scout fallback (proactive half; the reactive half is
    #        PhaseMachine._run_apply's scouts_invalid() check) ────────────────
    def _maybe_self_scout(self, db, bk: M.Booking, now: datetime) -> None:
        # GATE 0: the operator can forbid the self-scout fallback for this booking entirely. When
        # off, an apply worker only ever books slots a scout found, and holds when there are none.
        if not self._effective_booking_setting(db, bk, "SELF_SCOUT_FALLBACK", True):
            return
        # GATE 1: suppress the fallback while the pool still holds LIVE INVENTORY -- not because a
        # slot was once found.
        #
        # This asked `bk.slot_found_fired_at is not None`, which is a one-shot latch: it is set the
        # first time any slot is ever discovered and never cleared for the life of the booking. So
        # the moment one slot was found -- even a single phantom that was later retired, even a real
        # one that was immediately taken by someone else -- the apply-as-scout fallback was disabled
        # PERMANENTLY, no matter what happened to the pool afterwards. The mechanism that exists for
        # "we have no slots and no working scout" could not fire in exactly the state it is for,
        # because reaching that state requires having had a slot at some point.
        #
        # Worse, it compounds the two paths that legitimately empty a pool: _maybe_revert_false_slot_found
        # wipes a pool it has declared phantom, and slot retirement CLOSEs a slot after N misses.
        # Both leave a booking with zero slots and a latch that says "already found some" forever.
        #
        # Live inventory means AVAILABLE or LEASED. LEASED is deliberately included: while workers
        # are actively applying to every slot there is, the pool is transiently at zero AVAILABLE,
        # and that must not be read as exhaustion or the fallback would fire on every busy tick.
        # CLOSED (retired) and TAKEN (booked) are not inventory -- nothing can be applied to them.
        #
        # BLIND-SEEDED slots are NOT inventory for this purpose (F48, run 1023). They are the
        # operator's guesses, not evidence that anyone can SEE real slots -- and on run 1023 they
        # kept the pool "live" while every submit against them came back 66 KB not-bookable, so the
        # one mechanism that turns 120 logged-in appliers into slot finders could never fire.
        live_inventory = db.scalar(select(func.count(M.Slot.id)).where(
            M.Slot.booking_id == bk.id,
            M.Slot.status.in_([SlotStatus.AVAILABLE, SlotStatus.LEASED]),
            M.Slot.source != "blind",
        )) or 0
        if live_inventory:
            return
        last = self._last_self_scout.get(bk.id, 0)
        if time.time() - last < _SELF_SCOUT_THROTTLE:
            return
        # Scouts that count: this booking's AND its detector feeders' (cross-booking slot feed). A
        # feeder whose scouts are past the login wall is discovering for us; one stuck at the wall
        # (run 1023's Browser detector: 2 logins in 41 attempts, zero discovery) is not.
        scout_booking_ids = [bk.id, *(getattr(bk, "discovery_feeder_booking_ids", None) or [])]
        scouts = db.scalars(select(M.Worker).where(
            M.Worker.booking_id.in_(scout_booking_ids), M.Worker.role == WorkerRole.SCOUT)).all()
        if scouts and any(_scout_is_actually_scouting(s) for s in scouts):
            return   # at least one scout is past the login wall -- no fallback needed
        self._last_self_scout[bk.id] = time.time()
        holding_apply = db.scalars(select(M.Worker).where(
            M.Worker.booking_id == bk.id, M.Worker.role == WorkerRole.APPLY,
            M.Worker.status == WorkerStatus.RUNNING, M.Worker.substate == WorkerSubstate.KEEPING,
        )).first()
        if holding_apply is None or self._bus is None:
            return
        self._bus.send_command(holding_apply.id, "assign", slot=None, scouts_invalid=True)

    # ── D5: signal latch (confirm-before-mark) ───────────────────────────────
    def _latch_signals(self, db, bk: M.Booking, now: datetime) -> None:
        """Re-send slot_open/slot_found to any RUNNING worker not yet CONFIRMED notified. Marks a
        worker notified only after its send returns successfully -- the direct fix for the
        confirmed 2026-08-07 bug (a worker accrued 56 unconsumed assign commands because the
        scheduler marked workers 'seen' before confirming the send).

        The "already notified" set is keyed by worker_id, which is STABLE across a stop/relaunch
        (same DB row, fresh OS process) -- so on its own this durably survives a Hub restart
        (correct: the Hub shouldn't forget who it already told) but ALSO incorrectly survives a
        WORKER's own restart (wrong: a freshly-spawned process has an empty command stream and has
        never actually received the signal in its own lifetime). Confirmed bug (2026-08-13): a
        booking whose signals fired hours ago, restarted after several workers died/were stopped,
        had no way to get those signals to the new worker processes -- the manual fire-slot-open/
        fire-slot-found endpoints are correctly idempotent no-ops once the BOOKING has fired
        (that's the right, durable half), but nothing ever removed a relaunched worker's id from
        this set, so _latch_signals thought it had nothing left to do. The fix lives on the
        caller side (apps/hub/main.py's stop_worker/stop_booking/restart_worker/
        _sched_on_relaunch/start_booking's requeue step all now call
        libs.bus.names.notified_key(...) to strip a worker's membership right before it's about to
        get a fresh process -- see there for the actual removal)."""
        if self._bus is None:
            return
        for signal_name, fired_attr in (("slot_open", "slot_open_fired_at"),
                                        ("slot_found", "slot_found_fired_at")):
            if getattr(bk, fired_attr) is None:
                continue
            running = db.scalars(select(M.Worker).where(
                M.Worker.booking_id == bk.id, M.Worker.status == WorkerStatus.RUNNING)).all()
            if not running:
                continue
            key = notified_key(bk.id, signal_name)
            already = self._redis.smembers(key) if self._redis is not None else set()
            for w in running:
                if w.id in already:
                    continue
                try:
                    self._bus.send_command(w.id, "signal", name=signal_name)
                except Exception:
                    continue   # NOT marked notified -- retried next tick, exactly the confirmed fix
                if self._redis is not None:
                    self._redis.sadd(key, w.id)
                    self._redis.expire(key, 86400)

    def _is_blind_mode(self, db, bk: M.Booking) -> bool:
        return bool(self._effective_booking_setting(db, bk, "BLIND_APPLY_MODE", False))

    def _fire_slot_open(self, db, bk: M.Booking, now: datetime) -> None:
        bk.slot_open_fired_at = bk.slot_open_fired_at or now
        db.commit()
        self._on_slot_open(db, bk)   # publishes the pub/sub broadcast + flips booking status

    def reset_scout_pool(self, booking_id: str) -> None:
        """Drop a booking's in-memory scout discovery state so its next run starts in SINGLE mode
        with no carried-over slices/strike-counts/awakened set. Called by the Hub's start_booking
        (every start is a fresh run) and internally by the false-slot-open revert."""
        self._scout_pools.pop(booking_id, None)

    def _maybe_revert_false_slot_found(self, db, bk: M.Booking, now: datetime) -> bool:
        """False-slot-FOUND revert (operator spec 2026-08-14). The mirror of
        _maybe_inactivate_slot_open: that one handles "slot-open fired but nothing was ever found",
        this one handles "slots WERE found, but every attempt to book them bounces".

        The operator's reasoning, from the field: "even when there are some slots found - but they
        may be false positive. so if applying keeps being failed then regard the slots as false, and
        revert to hold phase. wipe out the slots and slot-found signal state, then make scouts to be
        at slot-open phase." This matches what the portal actually does -- most postos permanently
        advertise 3-4 phantom periods that cannot be booked by anyone.

        Fires when, since slot_found_fired_at, the fleet has accumulated FALSE_SLOT_BOUNCE_LIMIT
        bounced leases and booked NOTHING. Note what protects this from a false conclusion: a single
        degraded session parks itself after APPLY_MAX_ATTEMPTS consecutive bounces (phases._park), so
        the volume counted here has to come from the pool being bad rather than one bad worker.

        Reverting keeps slot_open_fired_at set -- the scouts belong in the slot-open phase, cheaply
        polling in single mode, not in the full-fan-out discovery that a slot-found implies. Returns
        True if it reverted."""
        if bk.slot_found_fired_at is None:
            return False
        # BLIND APPLY DISABLES THE AUTOMATIC REVERT. Once the operator has seeded blind slots they
        # have taken manual control of the pool, so the "these look phantom, give up and go back to
        # holding" heuristic must defer to that deliberate decision -- otherwise the revert, tripped
        # by the ORIGINAL discovered slots' bounces, wipes the freshly-seeded blind grid at step 1
        # (which closes every AVAILABLE/LEASED slot regardless of source), clears slot-found, and
        # inactivates the fleet the operator just pointed at the grid. Blind slots retire
        # individually via BLIND_MAX_BOUNCES; the operator stops the booking by hand. Confirmed as a
        # gap 2026-09-20 while validating blind apply against the scout->fail->blind path.
        if db.scalar(select(M.Slot.id).where(
                M.Slot.booking_id == bk.id, M.Slot.source == "blind").limit(1)):
            return False
        limit = int(self._effective_booking_setting(db, bk, "FALSE_SLOT_BOUNCE_LIMIT", 12))
        if limit <= 0:
            return False
        since = _aware(bk.slot_found_fired_at)

        # Anything actually booked in this window means the slots were real -- never revert.
        booked = db.scalar(select(M.BookedAppointment.id)
                           .where(M.BookedAppointment.booking_id == bk.id).limit(1))
        if booked:
            return False

        bounces = db.scalar(
            select(func.count(M.SlotLease.id))
            .join(M.Slot, M.SlotLease.slot_id == M.Slot.id)
            .where(M.Slot.booking_id == bk.id,
                   # Blind slots are EXCLUDED. The operator seeded them on sight, so bounces off
                   # them are expected -- most of a blind grid is phantom by construction -- and
                   # not evidence that the pool is false. Counting them would wipe the pool after
                   # a single sweep of a 9-combination grid at the default limit of 12. Blind slots
                   # retire individually via BLIND_MAX_BOUNCES in events._release_slot_lease.
                   M.Slot.source != "blind",
                   M.SlotLease.outcome == SlotLeaseOutcome.BOUNCED,
                   M.SlotLease.released_at.is_not(None),
                   M.SlotLease.released_at >= since)) or 0
        if bounces < limit:
            return False

        # 1. Wipe the pool. CLOSED rather than deleted so the history survives -- and so a genuine
        #    rediscovery can still reopen it (_persist_slots does CLOSED -> AVAILABLE) if the portal
        #    starts advertising something real.
        closing = db.scalars(select(M.Slot).where(
            M.Slot.booking_id == bk.id,
            M.Slot.status.in_([SlotStatus.AVAILABLE, SlotStatus.LEASED]))).all()
        for slot in closing:
            # Release any lease still open on it. Closing a slot while leaving its lease open leaves
            # a contradiction the next tick resolves the wrong way: _reclaim_stale_slot_leases
            # reclaims the orphan and (before the guard above) reopened the very slot we just
            # judged phantom.
            for lease in db.scalars(select(M.SlotLease).where(
                    M.SlotLease.slot_id == slot.id, M.SlotLease.released_at.is_(None))).all():
                lease.released_at = now
                lease.outcome = SlotLeaseOutcome.EXPIRED
            slot.status = SlotStatus.CLOSED
        # SessionLocal is autoflush=False, so the releases above stay ORM-level until flushed and
        # any later query in this tick would still see them as open (the same trap called out in
        # _reclaim_stale_slot_leases).
        db.flush()

        # 2. Wipe the slot-found signal state, Hub-side and worker-side. `inactivate` is the proven
        #    path for the worker half: it clears BOTH sticky latches, so no scout keeps behaving as
        #    though the found-phase is still on.
        bk.slot_found_fired_at = None
        running = db.scalars(select(M.Worker).where(
            M.Worker.booking_id == bk.id, M.Worker.status == WorkerStatus.RUNNING)).all()
        if self._bus is not None:
            for w in running:
                try:
                    self._bus.send_command(w.id, "signal", name="inactivate")
                except Exception:
                    pass
        if self._redis is not None:
            # Drop both notified-sets: slot_found so a genuinely new find re-reaches everyone, and
            # slot_open so _latch_signals re-delivers the still-active open that `inactivate` just
            # cleared worker-side -- which is what puts the scouts back in the slot-open phase.
            for signal_name in ("slot_open", "slot_found"):
                self._redis.delete(notified_key(bk.id, signal_name))

        # 3. Scouts back to cheap single-mode polling. slot_open_fired_at stays set on purpose.
        self.reset_scout_pool(bk.id)
        logger.warning("booking %s: %d bounced leases and no booking since slot_found -- treating the "
                    "slot pool as false positives; wiped %s slots, reverted to the slot-open phase",
                    bk.id, bounces, "available/leased")
        return True

    def revert_slot_open(self, db, bk: M.Booking) -> int:
        """MANUAL revert of a fired slot-open (operator 2026-09-26). The same effect as
        _maybe_inactivate_slot_open but without its false-open guards -- an explicit operator undo,
        including of a MANUAL fire (which the auto-guard deliberately leaves alone). Sends every
        RUNNING worker an `inactivate` signal (scouts drop out of discovery back to KEEPING; apply
        workers were already holding), clears the fired timestamps + the signal-latch 'already
        notified' sets + the manual-open flag, resets the scout pool, and puts the booking back to
        WARMING. Returns how many workers were signalled. Runs INSIDE the Hub so the scout pool's
        in-memory reset actually takes effect."""
        from libs.domain.state_machines import IllegalTransition, booking_transition
        running = db.scalars(select(M.Worker).where(
            M.Worker.booking_id == bk.id, M.Worker.status == WorkerStatus.RUNNING)).all()
        if self._bus is not None:
            for w in running:
                try:
                    self._bus.send_command(w.id, "signal", name="inactivate")
                except Exception:
                    pass
        if self._redis is not None:
            for signal_name in ("slot_open", "slot_found"):
                self._redis.delete(notified_key(bk.id, signal_name))
            self._redis.delete(manual_open_key(bk.id))
        self.reset_scout_pool(bk.id)
        bk.slot_open_fired_at = None
        bk.slot_found_fired_at = None
        # ALSO clear the scheduled trigger. plan_booking_actions re-fires slot-open the instant
        # status is WARMING/ARMED and slot_open_at <= now -- so a revert that leaves a past
        # slot_open_at in place is undone on the very next tick (confirmed live 2026-09-26: Booking
        # 0925 re-fired ~1 min after the revert). Clearing it holds the booking until an explicit
        # re-fire / re-schedule; a still-future slot_open_at is left alone.
        if bk.slot_open_at is not None and _aware(bk.slot_open_at) <= datetime.now(timezone.utc):
            bk.slot_open_at = None
        try:
            bk.status = booking_transition(bk.status, BookingStatus.WARMING)
        except IllegalTransition:
            pass
        db.commit()
        logger.warning("booking %s: slot-open MANUALLY REVERTED by operator -- %d worker(s) sent "
                       "inactivate, scouts back to holding, booking -> WARMING", bk.id, len(running))
        return len(running)

    def _maybe_inactivate_slot_open(self, db, bk: M.Booking, now: datetime) -> bool:
        """False-slot-open guard (operator spec 2026-08-14): "if there are no slots during slot-open
        phase for a long time (e.g. 20 mins) then acknowledge the slot-open is false, then revert to
        prev phase: hold. It will save scouts from massive discovering that may cause failures or
        rate limiting."

        Fires only when slot-open has been up for SLOT_OPEN_INACTIVATE_AFTER seconds with ZERO slots
        found in that whole window (slot_found_fired_at set means the open was real -- never revert).
        Reverting means: clear slot_open_fired_at, send every RUNNING worker an `inactivate` signal
        (scouts drop out of discovery back to plain holding; apply workers were already just holding),
        reset the scout pool to single mode, drop the signal-latch "already notified" sets so a
        re-fire genuinely reaches everyone, and put the booking back to WARMING. Returns True if it
        reverted (caller skips the rest of this booking's tick)."""
        if bk.slot_open_fired_at is None or bk.slot_found_fired_at is not None:
            return False
        after = float(self._effective_booking_setting(db, bk, "SLOT_OPEN_INACTIVATE_AFTER", 1200.0))
        if after <= 0:
            return False
        if (now - _aware(bk.slot_open_fired_at)).total_seconds() < after:
            return False
        # A slot found but not yet latched into slot_found_fired_at still counts as a REAL open --
        # _fire_slot_found_if_new runs earlier in the same tick, so this is belt-and-braces against
        # a slot that landed between its query and this one.
        if db.scalar(select(M.Slot.id).where(M.Slot.booking_id == bk.id).limit(1)):
            return False

        # NEVER silently undo an OPERATOR's explicit fire. This guard exists for a SCHEDULED open
        # that turned out to be wrong; applying it to a manual fire overrules a deliberate human
        # decision, and it did so without logging a single line.
        #
        # CONFIRMED 2026-08-23: the operator fired slot-open at 22:38:45; it was cleared at 22:58:45;
        # from the dashboard that was indistinguishable from the fire never having worked. It is also
        # self-perpetuating whenever discovery is broken -- "no slots in 20 minutes" is always true
        # if nothing can successfully scan -- so every manual fire gets undone on a 20-minute cycle.
        #
        # The deeper logic error, worth fixing separately: this infers "the open was FALSE" from
        # "we found nothing", but finding nothing equally means WE NEVER SUCCESSFULLY LOOKED.
        if self._redis is not None and self._redis.get(manual_open_key(bk.id)):
            logger.info("booking %s: slot-open was fired MANUALLY -- not reverting it (%.0fs with "
                        "no slots found)", bk.id,
                        (now - _aware(bk.slot_open_fired_at)).total_seconds())
            return False

        running = db.scalars(select(M.Worker).where(
            M.Worker.booking_id == bk.id, M.Worker.status == WorkerStatus.RUNNING)).all()
        if self._bus is not None:
            for w in running:
                try:
                    self._bus.send_command(w.id, "signal", name="inactivate")
                except Exception:
                    pass   # best-effort: the revert below still stops the Hub driving discovery
        if self._redis is not None:
            for signal_name in ("slot_open", "slot_found"):
                self._redis.delete(notified_key(bk.id, signal_name))
        self.reset_scout_pool(bk.id)
        # LOUD. This used to happen in complete silence, so an operator watching the dashboard saw
        # their fire simply cease to exist with nothing anywhere to explain it.
        logger.warning("booking %s: REVERTING slot-open -- %.0fs elapsed with zero slots found "
                       "(SLOT_OPEN_INACTIVATE_AFTER=%.0fs). Scouts drop back to holding and the "
                       "signal latch is cleared; re-fire to re-open.",
                       bk.id, (now - _aware(bk.slot_open_fired_at)).total_seconds(), after)
        bk.slot_open_fired_at = None
        from libs.domain.state_machines import IllegalTransition, booking_transition
        try:
            bk.status = booking_transition(bk.status, BookingStatus.WARMING)
        except IllegalTransition:
            pass
        db.commit()
        return True

    def _mirror_feeder_slots(self, db, bk: M.Booking, now: datetime) -> int:
        """Cross-booking discovery feed (operator idea 2026-10-01). Mirror a DETECTOR booking's
        discovered AVAILABLE slots into THIS (main) booking's pool, so a disposable detector run
        (fake identities, freely restartable) drives the precious main fleet without a restart.

        A discovered {date, period, posto} is a PORTAL FACT -- bookable by anyone warmed on that
        posto -- so this is INJECTION, not a bare wake-up: apply workers get real pool slots to lease
        immediately, and slot-open is fired to wake idle scouts too. One-directional; returns the
        number of slots injected.

        Guards:
          * only mirrors into an ACTIVE main booking (WARMING/ARMED/RUNNING);
          * POSTO MUST MATCH -- a slot is only bookable by main if main targets the same consulate;
          * dedupe is by the main pool's existing rows of ANY status. The false-slot-found revert
            marks a bounced slot CLOSED (not deleted), so a phantom injected once and reverted is NOT
            re-injected next tick -- no oscillation. Main's OWN scouts can still reopen a CLOSED row
            the normal way (a genuine rediscovery).
          * safe under A<->B feeder cycles: a slot mirrored into main already exists there, so
            mirroring back is skipped.
        """
        feeders = bk.discovery_feeder_booking_ids or []
        if not feeders:
            return 0
        if bk.status not in (BookingStatus.WARMING, BookingStatus.ARMED, BookingStatus.RUNNING):
            return 0
        feeder_rows = db.scalars(select(M.Slot).where(
            M.Slot.booking_id.in_(feeders),
            M.Slot.status == SlotStatus.AVAILABLE,
            # Only REAL discovered slots feed the main pool -- never operator-seeded BLIND phantoms.
            # A blind grid is deliberately mostly-phantom (the operator books it by hand on the
            # detector); mirroring it into the precious main booking would hand its apply workers a
            # wall of slots that bounce. Discovered slots are source='scout'; a slot mirrored from
            # another feeder is source='feeder' and still real, so exclude ONLY 'blind'. NULL-safe
            # (or_ keeps a source-less row) so a future discovered source can't silently be dropped.
            or_(M.Slot.source.is_(None), M.Slot.source != "blind"))).all()
        injected = 0
        for s in feeder_rows:
            if s.posto_id and bk.posto_id and s.posto_id != bk.posto_id:
                continue   # different consulate -- not bookable by this booking's sessions
            exists = db.scalar(select(M.Slot).where(
                M.Slot.booking_id == bk.id, M.Slot.date == s.date, M.Slot.period == s.period))
            if exists is not None:
                continue   # already in main's pool (AVAILABLE/LEASED/TAKEN or reverted-CLOSED)
            db.add(M.Slot(booking_id=bk.id, posto_id=bk.posto_id, date=s.date, period=s.period,
                          status=SlotStatus.AVAILABLE, source="feeder",
                          discovered_by=f"feeder:{s.booking_id}", first_seen=now, last_seen=now))
            injected += 1
        if injected:
            db.commit()
            logger.info("booking %s: injected %d slot(s) from feeder booking(s) %s",
                        bk.id, injected, feeders)
            # Wake the main fleet: fire slot-open so idle scouts discover and the booking goes
            # RUNNING; the RUNNING branch's _fire_slot_found_if_new + _assign_slots (same tick) then
            # hand the injected slots to the apply workers.
            if bk.slot_open_fired_at is None:
                self._fire_slot_open(db, bk, now)
        return injected

    def _fire_slot_found_if_new(self, db, bk: M.Booking, now: datetime) -> None:
        if bk.slot_found_fired_at is not None:
            return
        has_slot = db.scalar(select(M.Slot.id).where(
            M.Slot.booking_id == bk.id, M.Slot.status == SlotStatus.AVAILABLE).limit(1))
        if not has_slot:
            return
        bk.slot_found_fired_at = now
        db.commit()

    def fire_slot_found_manual(self, db, bk: M.Booking) -> None:
        """Operator-triggered override of _fire_slot_found_if_new -- skips the "is there actually an
        AVAILABLE Slot row" precondition, mirroring fire-slot-open's manual-override philosophy
        exactly (apps/hub/main.py's /fire-slot-open endpoint doesn't wait for a real condition
        either -- an operator decision overrides the automatic one). Idempotent: a caller should
        check slot_found_fired_at itself first, same as the endpoint does for slot-open."""
        now = datetime.now(timezone.utc)
        bk.slot_found_fired_at = bk.slot_found_fired_at or now
        db.commit()

    # ── D6: scout discovery orchestration (round-robin full-range rotation) ──
    def _drive_scouts(self, db, bk: M.Booking, *, sample_dates: list[str] | None = None) -> None:
        """Rotation model (operator redesign 2026-09-21, replaces the old single-probe/split-partition
        scheme). ONE scout at a time runs a FULL-range discovery round; the Hub rotates round-robin
        through the healthy scouts. Successive REAL rounds (ones that reached the portal) are spaced
        DISCOVERY_MIN_INTERVAL apart, so with n healthy scouts a given scout is revisited ~MIN*n. A
        FAILED round (dead session/nav error/empty report, or one that never reports within the hang
        timeout) frees the scout immediately and advances the rotation with no pacing wait -- a bad
        session never stalls the fleet, and when everyone is unhealthy the attempts just slide through
        with no results. Identical before and after slot-found; the loop runs the whole booking.

        Phase F2 dedup still holds: a scout with an unconsumed command is not eligible this tick (a
        re-sent `discover` piling up in one stream was the confirmed 2026-08-07 bug's shape)."""
        if self._bus is None:
            return
        scouts = db.scalars(select(M.Worker).where(
            M.Worker.booking_id == bk.id, M.Worker.role == WorkerRole.SCOUT,
            M.Worker.status == WorkerStatus.RUNNING)).all()
        scout_ids = [s.id for s in scouts]
        state = self._scout_pools.setdefault(bk.id, orchestration.ScoutPoolState())
        now = time.time()

        # Drain finished rounds. dates_reported events are buffered by EventHub (a different thread)
        # and drained here each tick -- each completes the reporting scout's round and records whether
        # it reached the portal (paces the next dispatch) or not (frees the rotation to advance now).
        if self._event_hub is not None:
            for sid in scout_ids:
                for outcomes in self._event_hub.pop_dates_reported(sid):
                    orchestration.on_round_reported(state, sid, outcomes, now=now)

        # Healthy = RUNNING (query filter) and no unconsumed command outstanding (F2). A scout's
        # availability is "restored right away" simply by having no benching state to clear -- the
        # rotation never marks a scout unhealthy, it just skips whoever has a pending command.
        healthy = [sid for sid in scout_ids if not self._bus.has_pending_commands(sid)]

        min_interval = float(self._effective_booking_setting(db, bk, "DISCOVERY_MIN_INTERVAL", 30.0))
        # A round that never reports (hung page, killed process mid-scan) must not wedge the single
        # in-flight slot forever. Generous -- a real full-range round is many portal calls -- but
        # finite: MIN plus a floor, so a genuine slow round is not reclaimed out from under itself.
        hang_timeout = max(min_interval * 2.0, 180.0)
        chosen = orchestration.plan_rotation_round(
            state, healthy, now=now, min_interval=min_interval, hang_timeout=hang_timeout)
        if chosen is None:
            return

        # The booking's OWN discovery window -- the WHOLE thing every round (full discovery per scout,
        # not a slice). CAL_START_DATE/CAL_END_DATE/CAL_NUM_DAYS, per booking; a bug in the old path
        # once ignored both ends and scanned 20 default weekdays instead (confirmed live 2026-08-14).
        dates = sample_dates or pool_manager.default_date_window(
            start_date=str(self._effective_booking_setting(db, bk, "CAL_START_DATE", "") or ""),
            end_date=str(self._effective_booking_setting(db, bk, "CAL_END_DATE", "") or ""),
            num_days=int(self._effective_booking_setting(db, bk, "CAL_NUM_DAYS", 20) or 20),
            today=datetime.now(timezone.utc).date(),
        )
        if not dates:
            state.inflight = None   # nothing to scan -- don't hold the in-flight slot on an empty window
            return

        # Shared gettime cache (storm-auto's proven _gettime_cache pattern): if a scout already
        # reported a still-fresh gettime fetch, hand it to this round instead of re-fetching -- "in
        # 300s only one gettime fetch will be done" (operator spec). Absent on the first round or once
        # stale, in which case the scout fetches fresh.
        cached_special_days = None
        if self._event_hub is not None:
            cache_secs = float(self._effective_booking_setting(db, bk, "GETTIME_CACHE_SECS", 300.0))
            cached_special_days = self._event_hub.get_gettime_cache(bk.id, cache_secs)

        # DISCOVERY_SOURCE rides the discover command (2026-09-26), so changing it on the booking
        # takes effect on a RUNNING scout's NEXT round -- no restart / re-login needed. The worker's
        # frozen effective_settings_json is launch-time; this pushes the LIVE booking value each
        # dispatch. do_discover honours it over its own cfg when present (a scout with no bus source
        # keeps its frozen value, so tests/older workers are unaffected).
        source = str(self._effective_booking_setting(db, bk, "DISCOVERY_SOURCE", "slots") or "slots")
        if cached_special_days is not None:
            self._bus.send_command(chosen, "discover", dates=dates,
                                   special_days=cached_special_days, source=source)
        else:
            self._bus.send_command(chosen, "discover", dates=dates, source=source)

    def _maybe_prewarm(self, db, bk: M.Booking, now: datetime) -> None:
        """Tell holding workers to re-warm shortly before a SCHEDULED slot-open.

        The portal's scheduling form state expires on a ~16.1-minute median clock (measured over 28
        keepalive cycles, 2026-08-20). Each worker re-warms on its own KEEPALIVE_INTERVAL, so at any
        given instant the fleet is scattered across that cycle -- and whoever happens to be at the
        wrong end of it enters the decisive window with dead form state.

        That is not theoretical: worker EDER's keepalive re-warm failed 38 seconds AFTER slot-open
        fired and it spent the next eight minutes recovering, while EUNICE, whose state happened to
        be fresh, booked in 47 seconds. slot_open_at is known in advance, so this is free to fix.

        Fires once per booking per run, tracked with the same notified-set the signal latch uses, and
        only when slot_open_at is actually set -- a manually fired slot-open gives no advance notice.
        """
        if self._bus is None or bk.slot_open_fired_at is not None:
            return
        slot_open_at = _aware(bk.slot_open_at)
        if slot_open_at is None:
            return
        lead = float(self._effective_booking_setting(db, bk, "PREWARM_BEFORE_SLOT_OPEN", 120.0))
        if lead <= 0 or now < slot_open_at - timedelta(seconds=lead):
            return
        key = notified_key(bk.id, "prewarm")
        holding = db.scalars(select(M.Worker).where(
            M.Worker.booking_id == bk.id, M.Worker.status == WorkerStatus.RUNNING,
            M.Worker.substate == WorkerSubstate.KEEPING)).all()
        sent = 0
        for w in holding:
            if self._redis is not None and self._redis.sismember(key, w.id):
                continue
            try:
                self._bus.send_command(w.id, "signal", name="prewarm")
            except Exception:
                logger.warning("prewarm send failed for worker %s", w.id, exc_info=True)
                continue
            if self._redis is not None:
                self._redis.sadd(key, w.id)
                self._redis.expire(key, 86400)
            sent += 1
        if sent:
            logger.info("booking %s: pre-warmed %d holding worker(s) %.0fs before slot-open",
                        bk.id, sent, lead)

    def _reclaim_stale_slot_leases(self, db, bk: M.Booking, now: datetime) -> None:
        """Return a slot to the pool when the worker holding it never converted it into an apply.

        A lease is a promise to attempt a booking within seconds. If the holder is no longer RUNNING,
        or has been sitting on it past SLOT_LEASE_TTL without reaching APPLYING/BOOKED, the promise
        is broken and the slot must become bookable by somebody else -- otherwise a single stuck
        worker can sterilise the pool, which is exactly what happened on booking Test2 (one worker
        holding all 120 lease units across all 24 slots, every slot reading LEASED, no assignment
        possible for anyone). Mirrors storm-auto's proven SlotManager lease auto-expiry.

        Belt to _assign_slots's one-lease-per-worker braces: that stops the accumulation, this
        recovers a pool that has already lost units to a worker that died, hung, or bounced without
        emitting a terminal event."""
        ttl = float(self._effective_booking_setting(db, bk, "SLOT_LEASE_TTL", 180.0))
        if ttl <= 0:
            return
        rows = db.execute(
            select(M.SlotLease, M.Worker)
            .join(M.Slot, M.Slot.id == M.SlotLease.slot_id)
            .outerjoin(M.Worker, M.Worker.id == M.SlotLease.worker_id)
            .where(M.Slot.booking_id == bk.id, M.SlotLease.released_at.is_(None))
        ).all()
        reclaimed: set[str] = set()
        for lease, worker in rows:
            # Normalise before comparing: SQLite hands back naive datetimes even for a
            # DateTime(timezone=True) column, and the scheduler tick swallows exceptions -- so a raw
            # subtraction here would raise "can't subtract offset-naive and offset-aware datetimes"
            # and kill the whole tick silently, for every booking, forever.
            leased_at = lease.leased_at
            leased_at = _aware(leased_at)
            aged_out = leased_at is not None and (now - leased_at).total_seconds() > ttl
            gone = worker is None or worker.status != WorkerStatus.RUNNING
            # A worker that is genuinely mid-booking keeps its slot no matter how long it takes.
            busy = worker is not None and worker.substate in (WorkerSubstate.APPLYING,
                                                              WorkerSubstate.BOOKED)
            if busy or not (gone or aged_out):
                continue
            lease.released_at = now
            lease.outcome = SlotLeaseOutcome.EXPIRED
            reclaimed.add(lease.slot_id)
        if not reclaimed:
            return
        # Explicit: the release flags above are ORM-level until flushed, and SessionLocal is built
        # with autoflush=False -- so the capacity count below would otherwise still see the leases
        # this method just released and decline to reopen the slot.
        db.flush()
        # Reopen any slot that now has room again. Recomputed from live lease counts rather than
        # assumed, since multi-capacity means a slot can be partly leased.
        for slot in db.scalars(select(M.Slot).where(M.Slot.id.in_(reclaimed))).all():
            if slot.status == SlotStatus.TAKEN:
                continue          # genuinely booked -- never reopen
            if slot.status == SlotStatus.CLOSED:
                # CLOSED means DELIBERATELY retired -- by /start's fresh-run retirement, by the
                # false-slot revert, or by the operator's Clear. Only a genuine REDISCOVERY may
                # reopen it (events.py's _persist_slots); reclaiming a stale lease must not.
                #
                # Confirmed live 2026-08-14: "Test 3018" restarted at 16:41:53 and had slot_open AND
                # slot_found stamped at 16:41:58.432469 -- the identical microsecond, i.e. one tick,
                # 4.5 minutes before any scout pushed anything. Leases left open on slots the
                # false-slot revert had CLOSED were reclaimed here, this reopened them, and
                # _fire_slot_found_if_new saw an AVAILABLE slot in the same tick. The run skipped
                # its entire slot-open discovery phase on resurrected slots it had already judged
                # phantom.
                continue
            active = db.scalar(select(func.count()).select_from(M.SlotLease).where(
                M.SlotLease.slot_id == slot.id, M.SlotLease.released_at.is_(None))) or 0
            cap = pool_manager.resolve_slot_capacity(
                str(self._effective_booking_setting(db, bk, "SLOT_CAPACITY_PER_PERIOD", "auto")),
                slot.capacity_estimate)
            if active < cap:
                slot.status = SlotStatus.AVAILABLE
        db.commit()

    # ── assignment ─────────────────────────────────────────────────────────────
    def _assign_slots(self, db, bk: M.Booking) -> None:
        """Same Phase F2 dedup as _drive_scouts: a KEEPING worker that was already sent an `assign`
        it hasn't processed yet (DB substate lags the worker's own real-time progress by up to one
        event round-trip) must never be handed a second, different slot in the meantime.

        Multi-capacity leasing (operator spec 2026-08-14): a physical (date, period) slot can absorb
        SLOT_CAPACITY_PER_PERIOD concurrent apply-worker leases before it's exhausted -- 10 open
        slots at cap=5 is 50 leasable units, not 10. AVAILABLE means "has room for at least one more
        lease" (not "zero leases") -- remaining capacity is computed per slot by subtracting its
        currently-active (unreleased) SlotLease count from its resolved cap."""
        # ORDERED, earliest first. plan_slot_assignments documents its input as "in priority order"
        # and deals one worker per slot across that order before doubling up on any -- but this query
        # carried no ORDER BY at all, so the "priority" was whatever Postgres happened to return for
        # an unordered scan, and could differ between ticks. Soonest date first is the right
        # priority: a nearer appointment is both more valuable and the likeliest to be taken by
        # someone else while we deliberate.
        avail = db.scalars(select(M.Slot).where(
            M.Slot.booking_id == bk.id, M.Slot.status == SlotStatus.AVAILABLE)
            .order_by(M.Slot.date.asc(), M.Slot.period.asc())).all()
        if not avail:
            return
        # A slot that just had a SETBACK (form_stale OR bounced) goes to the BACK of the queue.
        #
        # This ordering was strictly `date.asc(), period.asc()`, and form_stale releases WITHOUT a
        # penalty by design (miss_count untouched, status straight back to AVAILABLE) -- so the
        # first candidate came back to the same worker on the very next tick, forever. Measured
        # live 2026-08-27 on booking "E2E VALIDATION 0827": 14 leases over ONE slot
        # (2026-08-28/period 3) while 31 others sat AVAILABLE and freshly seen.
        #
        # BOUNCED is now reordered the same way (2026-10-02). It retires a slot after N misses, but
        # a BOOKED blind slot is retirement-protected until its phantom-capacity budget is spent
        # (events._release_slot_lease) -- so between a bounce and that retirement the slot stays
        # AVAILABLE and, being the earliest date, sat at the FRONT and was re-handed to the same
        # worker every tick. CONFIRMED live (Test 1002): worker 8fac5d27 took one booked slot
        # 18 times in a row while others got ZERO leases. Pushing a just-bounced slot to the back
        # lets every other worker have a turn before it comes round again.
        #
        # Deliberately ORDER ONLY: no miss_count, no status change, no retirement here. A slot we
        # could not use is not a slot that is gone -- charging it would reintroduce the false-slot
        # revert that condemned a real pool over 5082 "bounces". It simply goes last until something
        # else has had a turn.
        recent_stale = dict(db.execute(
            select(M.SlotLease.slot_id, func.max(M.SlotLease.released_at)).where(
                M.SlotLease.slot_id.in_([s.id for s in avail]),
                M.SlotLease.outcome.in_([SlotLeaseOutcome.FORM_STALE, SlotLeaseOutcome.BOUNCED]),
                M.SlotLease.released_at.isnot(None),
            ).group_by(M.SlotLease.slot_id)
        ).all())
        if recent_stale:
            def _staleness(slot) -> float:
                # Postgres hands back tz-aware datetimes and SQLite (tests) naive ones, and the two
                # cannot be compared. Normalise to a float so the key is always orderable.
                ts = recent_stale.get(slot.id)
                if ts is None:
                    return 0.0
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                return ts.timestamp()
            avail.sort(key=lambda s: (_staleness(s), s.date, s.period))
        active_counts = dict(db.execute(
            select(M.SlotLease.slot_id, func.count()).where(
                M.SlotLease.slot_id.in_([s.id for s in avail]), M.SlotLease.released_at.is_(None),
            ).group_by(M.SlotLease.slot_id)
        ).all())
        cap_setting = str(self._effective_booking_setting(db, bk, "SLOT_CAPACITY_PER_PERIOD", "auto"))
        slot_by_id = {s.id: s for s in avail}
        slots_with_room = [
            (s.id, pool_manager.resolve_slot_capacity(cap_setting, s.capacity_estimate)
                   - active_counts.get(s.id, 0))
            for s in avail
        ]
        slots_with_room = [(sid, remaining) for sid, remaining in slots_with_room if remaining > 0]
        if not slots_with_room:
            return
        holding = db.scalars(select(M.Worker).where(
            M.Worker.booking_id == bk.id, M.Worker.role == WorkerRole.APPLY,
            M.Worker.status == WorkerStatus.RUNNING,
            M.Worker.substate == WorkerSubstate.KEEPING,
            # A worker whose re-warm failed is holding but cannot book -- handing it a lease just
            # burns the slot's capacity on a guaranteed bounce. See NOT_BOOKABLE_PREFIX.
            or_(M.Worker.substate_detail.is_(None),
                ~M.Worker.substate_detail.like(f"{NOT_BOOKABLE_PREFIX}%")))).all()
        if self._bus is not None:
            holding = [w for w in holding if not self._bus.has_pending_commands(w.id)]

        # ONE active lease per worker (Phase F2's enforce_single_lease invariant, which was built
        # and unit-tested and then called from nowhere). Confirmed live 2026-08-14 on booking Test2:
        # ERNESTO PIRES alone accumulated 120 active leases -- 5 per slot across all 24 slots, the
        # entire pool -- because a KEEPING worker with no pending command is re-assignable on EVERY
        # 5s tick and nothing checked whether it already held one. Once the pool was consumed every
        # slot read LEASED, `avail` came back empty, and NO worker could be assigned again: the
        # booking deadlocked with one worker owning everything and nobody applying.
        # Multi-capacity leasing turned a bounded version of this bug (one lease per slot, since a
        # slot flipped LEASED on first assignment) into an unbounded one.
        already_leased = {
            wid for (wid,) in db.execute(
                select(M.SlotLease.worker_id).where(
                    M.SlotLease.worker_id.in_([w.id for w in holding] or [""]),
                    M.SlotLease.released_at.is_(None))
            ).all()
        }
        holding = [w for w in holding if w.id not in already_leased]
        for wid, sid in pool_manager.plan_slot_assignments(slots_with_room, [w.id for w in holding]):
            worker = db.get(M.Worker, wid)
            slot = slot_by_id[sid]
            db.add(M.SlotLease(slot_id=sid, worker_id=wid, leased_at=datetime.now(timezone.utc)))
            active_counts[sid] = active_counts.get(sid, 0) + 1
            cap = pool_manager.resolve_slot_capacity(cap_setting, slot.capacity_estimate)
            if active_counts[sid] >= cap:
                slot.status = SlotStatus.LEASED
            db.commit()
            self._on_assign(db, worker, slot)

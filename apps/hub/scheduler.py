"""Auto-ops scheduler (spec §7 auto-ops) + resilience mechanisms (Phase D).

A background thread ticks every few seconds and drives scheduled bookings with no operator input:
  - at warmup_start_at  -> START (launch all ready workers), booking -> WARMING
  - at slot_open_at      -> latch the slot-open signal (booking -> RUNNING)
  - continuously          -> assign AVAILABLE pooled slots to HOLDING apply workers
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

from sqlalchemy import func, select, update

from libs.db import models as M
from libs.bus.names import notified_key
from libs.domain.enums import (
    BookingStatus, SlotLeaseOutcome, SlotStatus, WorkerRole, WorkerStatus, WorkerSubstate,
)
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

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick_once()
            except Exception:
                # NEVER silent. A persistent fault here (a naive datetime, a bad column, anything)
                # stops scheduling for EVERY booking on EVERY tick, and swallowing it meant the
                # only symptom was "nothing happens" -- indistinguishable from an idle system.
                logger.exception("scheduler tick failed")
            self._stop.wait(self._tick)

    def _tick_once(self) -> None:
        now = datetime.now(timezone.utc)
        db = self._SessionLocal()
        try:
            live = db.scalars(select(M.Booking).where(
                M.Booking.status.notin_([BookingStatus.COMPLETED, BookingStatus.ABORTED,
                                         BookingStatus.DRAFT]))).all()
            for bk in live:
                for action in plan_booking_actions(bk.status, bk.warmup_start_at, bk.slot_open_at, now):
                    if action == "start":
                        bk.started_at = bk.started_at or now
                        db.commit()
                        self._on_start(db, bk)
                    elif action == "slot_open":
                        self._fire_slot_open(db, bk, now)

                if self._settings_timeout_hours(db, bk) and should_timeout_booking(
                    bk.status, bk.started_at, self._settings_timeout_hours(db, bk), now
                ):
                    self._stop_booking_core(db, bk, reason="timeout")
                    continue

                if bk.status == BookingStatus.RUNNING and self._maybe_complete_booking(db, bk):
                    continue   # target reached -- workers stopped, nothing further to drive
                if bk.status == BookingStatus.RUNNING:
                    self._reclaim_stale_slot_leases(db, bk, now)
                    self._fire_slot_found_if_new(db, bk, now)
                    if self._maybe_inactivate_slot_open(db, bk, now):
                        continue   # reverted to WARMING -- no discovery/assignment work this tick
                    if self._maybe_revert_false_slot_found(db, bk, now):
                        continue   # pool declared false -- don't assign the slots we just wiped
                    self._assign_slots(db, bk)
                    self._drive_scouts(db, bk)
                    self._maybe_self_scout(db, bk, now)

                # CONFIRMED LIVE 2026-08-14: these two ran for ANY non-terminal booking, including
                # a STOPPED one -- so the operator stopped "Test 3018", every worker went STOPPED,
                # and the next tick's _hold_respawn relaunched a scout anyway (GILSON TAVARES, a
                # process younger than the stop itself). That is the "stop button doesn't work"
                # class all over again, one layer up from the two earlier instances: stop must mean
                # stop, and a booking that is not actively driving workers must not be re-signalled
                # or re-populated behind the operator's back.
                if bk.status in _ACTIVE_BOOKING_STATUSES:
                    self._latch_signals(db, bk, now)
                    self._hold_respawn(db, bk, now)

            self._reap_dead_workers(db, now)
            self._reap_stalled_workers(db, now)
            self._reap_runaway_workers(db, now)
        finally:
            db.close()

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
        failed = db.scalars(select(M.Worker).where(
            M.Worker.booking_id == bk.id,
            M.Worker.status.in_([WorkerStatus.FAILED, WorkerStatus.HELD]))).all()
        if not failed:
            return
        max_attempts = int(self._effective_booking_setting(db, bk, "MAX_RESPAWN_ATTEMPTS", 5))
        base = float((bk.settings_json or {}).get("RESPAWN_BACKOFF_BASE", 2.0))
        cap = float((bk.settings_json or {}).get("RESPAWN_BACKOFF_CAP", 120.0))
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
                self._on_relaunch(db, w)

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
        if bk.slot_found_fired_at is not None:
            return
        last = self._last_self_scout.get(bk.id, 0)
        if time.time() - last < _SELF_SCOUT_THROTTLE:
            return
        scouts = db.scalars(select(M.Worker).where(
            M.Worker.booking_id == bk.id, M.Worker.role == WorkerRole.SCOUT)).all()
        if scouts and any(s.status == WorkerStatus.RUNNING for s in scouts):
            return   # at least one scout alive -- no fallback needed
        self._last_self_scout[bk.id] = time.time()
        holding_apply = db.scalars(select(M.Worker).where(
            M.Worker.booking_id == bk.id, M.Worker.role == WorkerRole.APPLY,
            M.Worker.status == WorkerStatus.RUNNING, M.Worker.substate == WorkerSubstate.HOLDING,
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
        bk.slot_open_fired_at = None
        from libs.domain.state_machines import IllegalTransition, booking_transition
        try:
            bk.status = booking_transition(bk.status, BookingStatus.WARMING)
        except IllegalTransition:
            pass
        db.commit()
        return True

    def _fire_slot_found_if_new(self, db, bk: M.Booking, now: datetime) -> None:
        if bk.slot_found_fired_at is not None:
            return
        has_slot = db.scalar(select(M.Slot.id).where(
            M.Slot.booking_id == bk.id, M.Slot.status == SlotStatus.AVAILABLE).limit(1))
        if not has_slot:
            return
        bk.slot_found_fired_at = now
        db.commit()
        state = self._scout_pools.get(bk.id)
        if state is not None:
            orchestration.on_slots_found(state)

    def fire_slot_found_manual(self, db, bk: M.Booking) -> None:
        """Operator-triggered override of _fire_slot_found_if_new -- skips the "is there actually an
        AVAILABLE Slot row" precondition, mirroring fire-slot-open's manual-override philosophy
        exactly (apps/hub/main.py's /fire-slot-open endpoint doesn't wait for a real condition
        either -- an operator decision overrides the automatic one). Idempotent: a caller should
        check slot_found_fired_at itself first, same as the endpoint does for slot-open."""
        now = datetime.now(timezone.utc)
        bk.slot_found_fired_at = bk.slot_found_fired_at or now
        db.commit()
        state = self._scout_pools.get(bk.id)
        if state is not None:
            orchestration.on_slots_found(state)

    # ── D6: scout discovery orchestration (single-probe -> split-poll) ──────
    def _drive_scouts(self, db, bk: M.Booking, *, sample_dates: list[str] | None = None) -> None:
        """Phase F2 (caught live in Phase E sim validation, not just designed on paper): never send
        a scout another `discover` task while it still has an unconsumed one outstanding -- a scout
        blocked on await_slot_open (or mid-scan) that gets re-sent the same task every tick is
        exactly the confirmed 2026-08-07 bug's shape (unbounded command pileup in one worker's
        stream), just in a different command type than the one it was originally caught in.

        Two DISTINCT participation models, corrected 2026-08-14 (the original 2026-08-13 version
        wired DISCOVERY_MAX_PARALLEL as a hard split-mode cap -- confirmed wrong once the operator
        clarified which knob belongs to which phase):

        SINGLE mode (pre slot-found): only up to SCOUT_AWAKEN_LIMIT scouts are ever "awakened" into
        the round-robin probe rotation (ensure_awakened) -- the rest stay parked in hold, untouched,
        until slot-found. A dead awakened scout is replaced from the parked pool. Probes are also
        PACED: at most one new probe per SINGLE_MODE_INTERVAL seconds fleet-wide, not one per
        scheduler tick (5s) -- "if there are 10 scout workers ... 1 scout will retry discovery per
        300s" only holds if the tick cadence itself doesn't dictate the pace.

        SPLIT mode (post slot-found): the FULL date range is partitioned across the FULL scout
        fleet -- every scout gets a persistent slice (poll_gap-spaced, driven by the scout's own
        loop), nobody is benched. Execution is throttled instead: at most DISCOVERY_CONCURRENCY
        scouts may have a discover round in flight at once (`state.busy_scouts`, tracked via
        dates_reported completion -- NOT has_pending_commands, which acks the instant a scout READS
        a command, long before do_discover() actually finishes, so it can't tell "sent" from "still
        running"). A scout's slice survives a tick where it's throttled out -- it just waits its
        turn in the rotation.

        Dates are retired from a scout's slice after DATE_RETIRE_AFTER_MISSES consecutive empty
        reports (0/off = never retire; see on_dates_reported/dates_to_retire) -- also corrected from
        the original single-empty-report pruning."""
        if self._bus is None:
            return
        scouts = db.scalars(select(M.Worker).where(
            M.Worker.booking_id == bk.id, M.Worker.role == WorkerRole.SCOUT,
            M.Worker.status == WorkerStatus.RUNNING)).all()
        scout_ids = [s.id for s in scouts]
        state = self._scout_pools.setdefault(bk.id, orchestration.ScoutPoolState())

        if bk.slot_found_fired_at is not None and state.mode == "single":
            orchestration.on_slots_found(state)

        # A scout holding a persistent slice that's no longer RUNNING (dead/reaped/stopped) hands
        # its remaining dates to the surviving/spare scouts -- "if one scout failed then replace it
        # with another available one."
        for dead_id in [sid for sid in list(state.remaining_by_scout) if sid not in scout_ids]:
            orchestration.on_scout_died(state, dead_id, scout_ids)

        # Retire confirmed-empty (multi-strike) dates out of each scout's own slice as scan results
        # come in (never a 'failed'/unknown date -- bug fix #6). dates_reported events are buffered
        # by EventHub (a different thread) and drained here each tick, matching last_seen/
        # event_rate's existing "EventHub owns process-local advisory state, Scheduler polls it
        # every tick" pattern. Also clears the reporting scout from `busy_scouts` (it just finished).
        retire_after = 0
        if bool(self._effective_booking_setting(db, bk, "DATE_RETIRE_ENABLED", True)):
            retire_after = int(self._effective_booking_setting(db, bk, "DATE_RETIRE_AFTER_MISSES", 3))
        if self._event_hub is not None:
            for sid in scout_ids:
                for outcomes in self._event_hub.pop_dates_reported(sid):
                    orchestration.on_dates_reported(state, sid, outcomes, retire_after_misses=retire_after)

        available = [sid for sid in scout_ids if not self._bus.has_pending_commands(sid)]
        if not available:
            return

        # The booking's OWN discovery window. Confirmed live 2026-08-14: start/end were hardcoded
        # empty with num_days=20, so CAL_START_DATE/CAL_END_DATE/CAL_NUM_DAYS -- which the operator
        # sets per booking, which the resolver validates, and which EngineConfig.discovery faithfully
        # parses -- were read by nothing on this path. A booking configured for 2026-09-01..09-20
        # discovered 2026-08-28..09-11 instead: the default 20 weekdays from tomorrow, ignoring both
        # ends of the window that was actually asked for.
        dates = sample_dates or pool_manager.default_date_window(
            start_date=str(self._effective_booking_setting(db, bk, "CAL_START_DATE", "") or ""),
            end_date=str(self._effective_booking_setting(db, bk, "CAL_END_DATE", "") or ""),
            num_days=int(self._effective_booking_setting(db, bk, "CAL_NUM_DAYS", 20) or 20),
            today=datetime.now(timezone.utc).date(),
        )

        if state.mode == "single":
            awaken_limit = int(self._effective_booking_setting(db, bk, "DISCOVERY_MAX_PARALLEL", 10))
            orchestration.ensure_awakened(state, scout_ids, awaken_limit)
            interval = float(self._effective_booking_setting(db, bk, "SINGLE_MODE_INTERVAL", 30.0))
            if time.time() - state.last_single_probe_at < interval:
                return
            eligible = [sid for sid in available if sid in state.awakened_scouts]
            tasks = orchestration.plan_initial_tasks(eligible, dates, state)
            if tasks:
                state.last_single_probe_at = time.time()
        else:
            if not state.remaining_by_scout:
                # First tick in split mode (or a fresh Hub restart with no prior in-memory state,
                # Phase F9) -- partition the FULL range across the FULL fleet once; every scout's
                # slice is persistent from here on, not re-derived every tick. No participation cap
                # here -- DISCOVERY_CONCURRENCY throttles EXECUTION below, not slice assignment.
                chunks = pool_manager.partition_dates(dates, len(scout_ids))
                state.remaining_by_scout = dict(zip(scout_ids, chunks))
            concurrency = int(self._effective_booking_setting(db, bk, "DISCOVERY_CONCURRENCY", 5))
            free = max(0, concurrency - len(state.busy_scouts))
            eligible = [sid for sid in available if state.remaining_by_scout.get(sid)][:free]
            tasks = {sid: state.remaining_by_scout[sid] for sid in eligible}
            state.busy_scouts.update(tasks)

        # Shared gettime cache (storm-auto's proven _gettime_cache pattern): if any scout in this
        # booking already reported a still-fresh gettime fetch, hand it to every scout getting a
        # discover command this tick instead of letting each independently re-fetch -- "in 300s only
        # one gettime fetch will be done" (operator spec). Absent when nothing's cached yet (first
        # round) or the cache has gone stale -- those scouts just fetch fresh as before.
        cached_special_days = None
        if self._event_hub is not None and tasks:
            cache_secs = float(self._effective_booking_setting(db, bk, "GETTIME_CACHE_SECS", 300.0))
            cached_special_days = self._event_hub.get_gettime_cache(bk.id, cache_secs)

        for worker_id, chunk in tasks.items():
            if cached_special_days is not None:
                self._bus.send_command(worker_id, "discover", dates=chunk, special_days=cached_special_days)
            else:
                self._bus.send_command(worker_id, "discover", dates=chunk)

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
        """Same Phase F2 dedup as _drive_scouts: a HOLDING worker that was already sent an `assign`
        it hasn't processed yet (DB substate lags the worker's own real-time progress by up to one
        event round-trip) must never be handed a second, different slot in the meantime.

        Multi-capacity leasing (operator spec 2026-08-14): a physical (date, period) slot can absorb
        SLOT_CAPACITY_PER_PERIOD concurrent apply-worker leases before it's exhausted -- 10 open
        slots at cap=5 is 50 leasable units, not 10. AVAILABLE means "has room for at least one more
        lease" (not "zero leases") -- remaining capacity is computed per slot by subtracting its
        currently-active (unreleased) SlotLease count from its resolved cap."""
        avail = db.scalars(select(M.Slot).where(
            M.Slot.booking_id == bk.id, M.Slot.status == SlotStatus.AVAILABLE)).all()
        if not avail:
            return
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
            M.Worker.substate == WorkerSubstate.HOLDING)).all()
        if self._bus is not None:
            holding = [w for w in holding if not self._bus.has_pending_commands(w.id)]

        # ONE active lease per worker (Phase F2's enforce_single_lease invariant, which was built
        # and unit-tested and then called from nowhere). Confirmed live 2026-08-14 on booking Test2:
        # ERNESTO PIRES alone accumulated 120 active leases -- 5 per slot across all 24 slots, the
        # entire pool -- because a HOLDING worker with no pending command is re-assignable on EVERY
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

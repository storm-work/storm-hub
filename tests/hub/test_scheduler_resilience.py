"""Phase D scheduler resilience mechanisms: booking timeout, hold-respawn, dead/stall/runaway
watchdogs (with lease + Redis cleanup), apply-as-scout fallback, signal latch, scout task driving.

Uses a real in-memory SQLite DB (matching tests/hub/test_api.py's pattern) + fakeredis + simple
stub/spy objects for Supervisor/Bus/EventHub, since the point here is the scheduler's DECISION logic,
not real subprocess/Redis-stream mechanics (those are covered in their own test files).
"""
import time
from datetime import datetime, timedelta, timezone

import fakeredis
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.hub.scheduler import Scheduler, should_timeout_booking
from libs.bus.names import notified_key
from libs.db import metadata
from libs.db import models as M
from libs.engine import orchestration as orch
from libs.domain.enums import (
    BookingStatus,
    SlotStatus,
    WorkerRole,
    WorkerStatus,
    WorkerSubstate,
)

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


# ── should_timeout_booking (pure) ─────────────────────────────────────────────
def test_should_timeout_booking_true_past_deadline():
    started = NOW - timedelta(hours=49)
    assert should_timeout_booking(BookingStatus.RUNNING, started, 48.0, NOW) is True


def test_should_timeout_booking_false_within_window():
    started = NOW - timedelta(hours=10)
    assert should_timeout_booking(BookingStatus.RUNNING, started, 48.0, NOW) is False


def test_should_timeout_booking_false_never_started():
    assert should_timeout_booking(BookingStatus.RUNNING, None, 48.0, NOW) is False


def test_should_timeout_booking_false_terminal_status():
    started = NOW - timedelta(hours=100)
    assert should_timeout_booking(BookingStatus.COMPLETED, started, 48.0, NOW) is False


def test_should_timeout_booking_false_zero_timeout_disables():
    started = NOW - timedelta(hours=100)
    assert should_timeout_booking(BookingStatus.RUNNING, started, 0.0, NOW) is False


# ── DB fixture ─────────────────────────────────────────────────────────────────
@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = Session()
    yield s
    s.close()


class _FakeBus:
    def __init__(self):
        self.sent: list[tuple] = []
        self.deleted_streams: list[str] = []
        self.pending: set[str] = set()   # worker_ids with a simulated outstanding command

    def send_command(self, worker_id, type, **payload):
        self.sent.append((worker_id, type, payload))

    def delete_worker_stream(self, worker_id):
        self.deleted_streams.append(worker_id)

    def publish_signal(self, booking_id, name, **payload):
        pass

    def has_pending_commands(self, worker_id):
        return worker_id in self.pending


class _FakeSupervisor:
    def __init__(self):
        self.stopped: list[str] = []
        self._running: set[str] = set()

    def stop(self, worker_id):
        self.stopped.append(worker_id)
        self._running.discard(worker_id)

    def running_ids(self):
        return list(self._running)


class _FakeEventHub:
    def __init__(self):
        self.last_seen: dict[str, float] = {}
        self._rates: dict[str, float] = {}
        self.dates_reported: dict[str, list[list[dict]]] = {}
        self.gettime_cache: dict[str, tuple[dict, float]] = {}

    def event_rate(self, worker_id, window=10.0):
        return self._rates.get(worker_id, 0.0)

    def pop_dates_reported(self, worker_id):
        return self.dates_reported.pop(worker_id, [])

    def get_gettime_cache(self, booking_id, max_age):
        entry = self.gettime_cache.get(booking_id)
        if entry is None:
            return None
        special_days, cached_at = entry
        if time.time() - cached_at >= max_age:
            return None
        return special_days


def _make_booking(db, **overrides):
    bk = M.Booking(name="b", type="real", posto_id="5084", status=BookingStatus.RUNNING,
                   settings_json=overrides.pop("settings_json", {}), **overrides)
    db.add(bk)
    db.commit()
    return bk


def _make_identity(db):
    ident = M.Identity(full_name="Test Person", type="real")
    db.add(ident)
    db.commit()
    return ident


def _make_worker(db, bk, ident, **overrides):
    w = M.Worker(booking_id=bk.id, identity_id=ident.id, role=overrides.pop("role", WorkerRole.APPLY),
                status=overrides.pop("status", WorkerStatus.RUNNING), **overrides)
    db.add(w)
    db.commit()
    return w


def _scheduler(**kwargs):
    defaults = dict(
        session_local=lambda: None, on_start=lambda db, bk: None,
        on_slot_open=lambda db, bk: None, on_assign=lambda db, w, s: None,
    )
    defaults.update(kwargs)
    return Scheduler(**defaults)


# ── D1: _stop_booking_core ────────────────────────────────────────────────────
def test_stop_booking_core_stops_workers_and_aborts(db):
    bus, sup = _FakeBus(), _FakeSupervisor()
    sched = _scheduler(bus=bus, supervisor=sup)
    bk = _make_booking(db)
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident)

    sched._stop_booking_core(db, bk, reason="timeout")

    db.refresh(w)
    db.refresh(bk)
    assert w.status == WorkerStatus.STOPPED
    assert bk.status == BookingStatus.ABORTED
    assert (w.id, "stop", {}) in bus.sent
    assert w.id in sup.stopped
    assert w.id in bus.deleted_streams


# ── D2: _hold_respawn ──────────────────────────────────────────────────────────
def test_hold_respawn_relaunches_failed_worker_and_stops_old_process_first():
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    sup = _FakeSupervisor()
    relaunched = []
    sched = _scheduler(redis_client=r, supervisor=sup, on_relaunch=lambda db, w: relaunched.append(w.id))

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    bk = _make_booking(db)
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, status=WorkerStatus.FAILED)

    sched._hold_respawn(db, bk, NOW)

    assert relaunched == [w.id]
    assert w.id in sup.stopped   # verified-kill-before-relaunch (F15)
    assert int(r.hget(f"respawn:{w.id}", "attempt")) == 1


def test_hold_respawn_respects_backoff_and_stops_after_max_attempts():
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    relaunched = []
    sched = _scheduler(redis_client=r, on_relaunch=lambda db, w: relaunched.append(w.id))

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    bk = _make_booking(db, settings_json={"MAX_RESPAWN_ATTEMPTS": 2})
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, status=WorkerStatus.FAILED)

    # First call relaunches immediately (no prior backoff).
    sched._hold_respawn(db, bk, NOW)
    assert len(relaunched) == 1
    # Second call, same tick timing -- backoff hasn't elapsed, must NOT relaunch again yet.
    sched._hold_respawn(db, bk, NOW)
    assert len(relaunched) == 1

    # Force the backoff to have elapsed, and exhaust the attempt cap.
    r.hset(f"respawn:{w.id}", "next_eligible", 0)
    sched._hold_respawn(db, bk, NOW)
    assert len(relaunched) == 2
    r.hset(f"respawn:{w.id}", "next_eligible", 0)
    sched._hold_respawn(db, bk, NOW)   # attempt count now >= MAX_RESPAWN_ATTEMPTS=2 -> no more
    assert len(relaunched) == 2


def test_hold_respawn_skips_stopped_booking():
    """Regression (X2): an operator's manual stop must never be undone. Before the "stopped" vs
    "held" outcome split (libs/engine/phases.py) and this guard, a worker that reported "held" after
    an explicit stop got marked FAILED by the event consumer, and hold-respawn -- which only
    excluded COMPLETED/ABORTED/DRAFT bookings -- relaunched it right back within one tick."""
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    relaunched = []
    sched = _scheduler(redis_client=r, on_relaunch=lambda db, w: relaunched.append(w.id))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    bk = _make_booking(db)
    bk.status = BookingStatus.STOPPED
    db.commit()
    ident = _make_identity(db)
    _make_worker(db, bk, ident, status=WorkerStatus.FAILED)
    sched._hold_respawn(db, bk, NOW)
    assert relaunched == []


def test_hold_respawn_disabled_by_flag():
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    relaunched = []
    sched = _scheduler(redis_client=r, on_relaunch=lambda db, w: relaunched.append(w.id))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Session()
    bk = _make_booking(db, settings_json={"HOLD_RESPAWN": False})
    ident = _make_identity(db)
    _make_worker(db, bk, ident, status=WorkerStatus.FAILED)
    sched._hold_respawn(db, bk, NOW)
    assert relaunched == []


# ── D3: dead/stall/runaway reap + resource release (F5) ──────────────────────
def test_reap_dead_workers_releases_proxy_and_slot_leases(db):
    bus, sup, eh = _FakeBus(), _FakeSupervisor(), _FakeEventHub()
    sched = _scheduler(bus=bus, supervisor=sup, event_hub=eh)
    bk = _make_booking(db)
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, created_at=NOW - timedelta(minutes=10))

    proxy = M.Proxy(provider="isp", kind="static_res", endpoint="1.2.3.4:80", active=True,
                    is_healthy=True, max_sessions=1)
    db.add(proxy)
    db.commit()
    lease = M.ProxyLease(proxy_id=proxy.id, worker_id=w.id, leased_at=NOW)
    db.add(lease)
    slot = M.Slot(booking_id=bk.id, posto_id="5084", date="2026-08-18", period=4,
                  status=SlotStatus.LEASED)
    db.add(slot)
    db.commit()
    slot_lease = M.SlotLease(slot_id=slot.id, worker_id=w.id, leased_at=NOW)
    db.add(slot_lease)
    db.commit()

    # supervisor has no running processes at all -> w is dead-in-DB-but-not-in-process-list
    sched._reap_dead_workers(db, NOW + timedelta(minutes=5))   # past _DEAD_GRACE (90s)

    db.refresh(w)
    db.refresh(lease)
    db.refresh(slot)
    db.refresh(slot_lease)
    assert w.status == WorkerStatus.FAILED
    assert lease.released_at is not None
    assert slot_lease.released_at is not None
    assert slot.status == SlotStatus.AVAILABLE
    assert w.id in bus.deleted_streams


def test_reap_dead_workers_respects_grace_period(db):
    sup = _FakeSupervisor()
    sched = _scheduler(supervisor=sup)
    bk = _make_booking(db)
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, created_at=NOW)

    sched._reap_dead_workers(db, NOW + timedelta(seconds=10))   # well within _DEAD_GRACE

    db.refresh(w)
    assert w.status == WorkerStatus.RUNNING   # not reaped yet


def test_reap_stalled_workers_kills_and_marks_failed(db):
    bus, sup, eh = _FakeBus(), _FakeSupervisor(), _FakeEventHub()
    sched = _scheduler(bus=bus, supervisor=sup, event_hub=eh)
    bk = _make_booking(db, settings_json={"WORKER_STALL_TIMEOUT": 100.0})
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident)
    eh.last_seen[w.id] = 0.0   # "ages ago" in wall-clock time.time() terms

    sched._reap_stalled_workers(db, NOW)

    db.refresh(w)
    assert w.status == WorkerStatus.FAILED
    assert w.id in sup.stopped


def test_reap_stalled_workers_leaves_recently_seen_alone(db):
    import time as _time
    bus, sup, eh = _FakeBus(), _FakeSupervisor(), _FakeEventHub()
    sched = _scheduler(bus=bus, supervisor=sup, event_hub=eh)
    bk = _make_booking(db, settings_json={"WORKER_STALL_TIMEOUT": 1500.0})
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident)
    eh.last_seen[w.id] = _time.time()   # just now

    sched._reap_stalled_workers(db, NOW)

    db.refresh(w)
    assert w.status == WorkerStatus.RUNNING


def test_reap_runaway_workers_kills_high_rate_worker(db):
    bus, sup, eh = _FakeBus(), _FakeSupervisor(), _FakeEventHub()
    sched = _scheduler(bus=bus, supervisor=sup, event_hub=eh)
    bk = _make_booking(db, settings_json={"WORKER_EVENT_RATE_LIMIT": 5.0})
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident)
    eh._rates[w.id] = 200.0   # the confirmed-incident magnitude (~150-280/s)

    sched._reap_runaway_workers(db, NOW)

    db.refresh(w)
    assert w.status == WorkerStatus.FAILED
    assert "runaway" in (w.substate_detail or "")
    assert w.id in sup.stopped


def test_reap_runaway_workers_leaves_normal_rate_alone(db):
    bus, sup, eh = _FakeBus(), _FakeSupervisor(), _FakeEventHub()
    sched = _scheduler(bus=bus, supervisor=sup, event_hub=eh)
    bk = _make_booking(db, settings_json={"WORKER_EVENT_RATE_LIMIT": 5.0})
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident)
    eh._rates[w.id] = 0.09   # ~1 event/11s, the confirmed-healthy baseline

    sched._reap_runaway_workers(db, NOW)

    db.refresh(w)
    assert w.status == WorkerStatus.RUNNING


# ── D4: apply-as-scout fallback ────────────────────────────────────────────────
def test_maybe_self_scout_triggers_when_no_scouts_alive(db):
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db)
    ident = _make_identity(db)
    apply_w = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.HOLDING)

    sched._maybe_self_scout(db, bk, NOW)

    assert any(t == "assign" and p.get("scouts_invalid") is True
              for _wid, t, p in bus.sent)


def test_maybe_self_scout_does_nothing_when_scout_alive(db):
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db)
    ident = _make_identity(db)
    _make_worker(db, bk, ident, role=WorkerRole.SCOUT, status=WorkerStatus.RUNNING)
    _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.HOLDING)

    sched._maybe_self_scout(db, bk, NOW)

    assert bus.sent == []


def test_maybe_self_scout_throttled(db):
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db)
    ident = _make_identity(db)
    _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.HOLDING)

    sched._maybe_self_scout(db, bk, NOW)
    first_len = len(bus.sent)
    sched._maybe_self_scout(db, bk, NOW)   # immediately again -- throttle window not elapsed
    assert len(bus.sent) == first_len


def test_maybe_self_scout_skips_once_slot_found(db):
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db, slot_found_fired_at=NOW)
    ident = _make_identity(db)
    _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.HOLDING)

    sched._maybe_self_scout(db, bk, NOW)

    assert bus.sent == []


# ── D5: signal latch (confirm-before-mark) ────────────────────────────────────
def test_latch_signals_sends_and_marks_notified(db):
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    bus = _FakeBus()
    sched = _scheduler(bus=bus, redis_client=r)
    bk = _make_booking(db, slot_open_fired_at=NOW)
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident)

    sched._latch_signals(db, bk, NOW)

    assert (w.id, "signal", {"name": "slot_open"}) in bus.sent
    assert r.sismember(f"notified:{bk.id}:slot_open", w.id)

    # second tick: already notified -> must NOT resend (the confirmed bug's exact shape: repeated
    # unconfirmed resends piling up in the worker's command stream)
    bus.sent.clear()
    sched._latch_signals(db, bk, NOW)
    assert bus.sent == []


def test_latch_signals_reaches_worker_that_joins_after_signal_fired(db):
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    bus = _FakeBus()
    sched = _scheduler(bus=bus, redis_client=r)
    bk = _make_booking(db, slot_open_fired_at=NOW)
    ident = _make_identity(db)
    w1 = _make_worker(db, bk, ident)
    sched._latch_signals(db, bk, NOW)
    bus.sent.clear()

    # a second worker starts AFTER the signal already fired (e.g. a respawn) -- must still get it
    w2 = _make_worker(db, bk, ident)
    sched._latch_signals(db, bk, NOW)
    assert any(wid == w2.id for wid, _t, _p in bus.sent)
    assert not any(wid == w1.id for wid, _t, _p in bus.sent)   # w1 already confirmed, not re-sent


def test_latch_signals_does_not_mark_notified_when_send_fails(db):
    r = fakeredis.FakeStrictRedis(decode_responses=True)

    class _FailingBus(_FakeBus):
        def send_command(self, worker_id, type, **payload):
            raise RuntimeError("redis down")

    bus = _FailingBus()
    sched = _scheduler(bus=bus, redis_client=r)
    bk = _make_booking(db, slot_open_fired_at=NOW)
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident)

    sched._latch_signals(db, bk, NOW)   # send raises -- must not be silently marked notified

    assert not r.sismember(f"notified:{bk.id}:slot_open", w.id)


def test_latch_signals_noop_when_not_yet_fired(db):
    bus = _FakeBus()
    sched = _scheduler(bus=bus, redis_client=fakeredis.FakeStrictRedis(decode_responses=True))
    bk = _make_booking(db)   # slot_open_fired_at is None
    ident = _make_identity(db)
    _make_worker(db, bk, ident)

    sched._latch_signals(db, bk, NOW)

    assert bus.sent == []


# ── D6: scout discovery orchestration ─────────────────────────────────────────
def test_fire_slot_found_sets_timestamp_once_a_slot_exists(db):
    sched = _scheduler()
    bk = _make_booking(db)
    ident = _make_identity(db)
    assert bk.slot_found_fired_at is None

    sched._fire_slot_found_if_new(db, bk, NOW)   # no slots yet -> no-op
    db.refresh(bk)
    assert bk.slot_found_fired_at is None

    db.add(M.Slot(booking_id=bk.id, posto_id="5084", date="2026-08-18", period=4,
                  status=SlotStatus.AVAILABLE))
    db.commit()

    sched._fire_slot_found_if_new(db, bk, NOW)
    db.refresh(bk)
    # SQLite (unlike Postgres) doesn't round-trip tzinfo on DateTime columns -- compare naively.
    assert bk.slot_found_fired_at.replace(tzinfo=None) == NOW.replace(tzinfo=None)


def test_fire_slot_found_only_fires_once(db):
    sched = _scheduler()
    bk = _make_booking(db, slot_found_fired_at=NOW - timedelta(minutes=5))
    db.add(M.Slot(booking_id=bk.id, posto_id="5084", date="2026-08-18", period=4,
                  status=SlotStatus.AVAILABLE))
    db.commit()

    sched._fire_slot_found_if_new(db, bk, NOW)

    db.refresh(bk)
    expected = (NOW - timedelta(minutes=5)).replace(tzinfo=None)
    assert bk.slot_found_fired_at.replace(tzinfo=None) == expected   # unchanged, not re-stamped


def test_drive_scouts_single_mode_picks_one_scout_at_a_time(db):
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db)
    ident = _make_identity(db)
    s1 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)
    s2 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)

    sched._drive_scouts(db, bk, sample_dates=["2026-08-18", "2026-08-19"])

    discover_sends = [(wid, p) for wid, t, p in bus.sent if t == "discover"]
    assert len(discover_sends) == 1   # SINGLE mode: only one scout gets a task this tick
    assert discover_sends[0][0] in (s1.id, s2.id)


def test_drive_scouts_split_mode_after_slot_found(db):
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db, slot_found_fired_at=NOW)
    ident = _make_identity(db)
    s1 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)
    s2 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)

    sched._drive_scouts(db, bk, sample_dates=["d1", "d2", "d3", "d4"])

    discover_sends = [wid for wid, t, p in bus.sent if t == "discover"]
    assert set(discover_sends) == {s1.id, s2.id}   # SPLIT mode: both scouts get a chunk


def test_drive_scouts_noop_with_no_scouts(db):
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db)
    sched._drive_scouts(db, bk, sample_dates=["d1"])
    assert bus.sent == []


def test_drive_scouts_skips_scout_with_outstanding_command(db):
    # Phase F2, caught live in Phase E validation: a scout that hasn't consumed its previous
    # discover task yet must not be sent another one every tick.
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db)
    ident = _make_identity(db)
    s1 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)
    bus.pending.add(s1.id)   # simulate: still has an unconsumed discover command

    sched._drive_scouts(db, bk, sample_dates=["d1"])

    assert bus.sent == []


def test_drive_scouts_split_mode_never_benches_a_scout(db):
    """Corrected 2026-08-14: DISCOVERY_MAX_PARALLEL was briefly wired as a hard SPLIT-mode cap
    (2026-08-13), which the operator clarified was wrong -- split mode gives EVERY scout a
    persistent slice, no matter how many there are; only EXECUTION is throttled
    (DISCOVERY_CONCURRENCY), never participation."""
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db, slot_found_fired_at=NOW, settings_json={"DISCOVERY_MAX_PARALLEL": 2})
    ident = _make_identity(db)
    s1 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)
    s2 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)
    s3 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)

    sched._drive_scouts(db, bk, sample_dates=["d1", "d2", "d3", "d4"])

    state = sched._scout_pools[bk.id]
    assert set(state.remaining_by_scout) == {s1.id, s2.id, s3.id}   # nobody benched


def test_drive_scouts_split_mode_concurrency_window_throttles_execution(db):
    """DISCOVERY_CONCURRENCY (not DISCOVERY_MAX_PARALLEL) throttles how many scouts may have a
    discover round in flight at once -- every scout still gets a persistent slice, but only up to
    the concurrency window is actually SENT a task this tick."""
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db, slot_found_fired_at=NOW, settings_json={"DISCOVERY_CONCURRENCY": 2})
    ident = _make_identity(db)
    s1 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)
    s2 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)
    s3 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)

    sched._drive_scouts(db, bk, sample_dates=["d1", "d2", "d3", "d4"])

    state = sched._scout_pools[bk.id]
    assert set(state.remaining_by_scout) == {s1.id, s2.id, s3.id}   # everyone has a slice
    discover_sends = {wid for wid, t, p in bus.sent if t == "discover"}
    assert len(discover_sends) == 2   # but only the concurrency window actually got sent a round
    assert discover_sends <= {s1.id, s2.id, s3.id}
    assert state.busy_scouts == discover_sends


def test_drive_scouts_single_mode_awaken_limit_caps_rotation_pool(db):
    """DISCOVERY_MAX_PARALLEL's corrected meaning: SINGLE mode's awaken-scout-limit. Only up to the
    limit are ever probed; the rest stay parked."""
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db, settings_json={"DISCOVERY_MAX_PARALLEL": 2})
    ident = _make_identity(db)
    s1 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)
    s2 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)
    s3 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)

    sched._drive_scouts(db, bk, sample_dates=["d1"])

    state = sched._scout_pools[bk.id]
    assert len(state.awakened_scouts) == 2
    assert set(state.awakened_scouts) <= {s1.id, s2.id, s3.id}
    discover_sends = {wid for wid, t, p in bus.sent if t == "discover"}
    assert discover_sends <= set(state.awakened_scouts)


def test_drive_scouts_single_mode_paces_by_interval_not_scheduler_tick(db):
    """SINGLE_MODE_INTERVAL must gate successive probes fleet-wide -- not fire on every 5s
    scheduler tick."""
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db, settings_json={"SINGLE_MODE_INTERVAL": 30.0})
    ident = _make_identity(db)
    _make_worker(db, bk, ident, role=WorkerRole.SCOUT)
    _make_worker(db, bk, ident, role=WorkerRole.SCOUT)

    sched._drive_scouts(db, bk, sample_dates=["d1"])
    assert len([1 for _, t, _ in bus.sent if t == "discover"]) == 1
    bus.sent.clear()

    # immediately again -- must NOT send another probe, the interval hasn't elapsed
    sched._drive_scouts(db, bk, sample_dates=["d1"])
    assert bus.sent == []

    # simulate the interval having elapsed
    sched._scout_pools[bk.id].last_single_probe_at -= 31.0
    sched._drive_scouts(db, bk, sample_dates=["d1"])
    assert len([1 for _, t, _ in bus.sent if t == "discover"]) == 1


def test_drive_scouts_split_mode_slice_is_persistent_across_ticks(db):
    """A scout's date range must NOT be re-partitioned from scratch every tick -- it keeps
    re-polling its own persistent slice (poll_gap-spaced) until pruned or redistributed. Confirmed
    this was previously broken: every tick re-split the FULL, unpruned range across whichever
    scouts looked available, so pruning/dead-scout-redistribution were structurally unreachable."""
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db, slot_found_fired_at=NOW)
    ident = _make_identity(db)
    s1 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)
    s2 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)

    sched._drive_scouts(db, bk, sample_dates=["d1", "d2", "d3", "d4"])
    first_slices = dict(sched._scout_pools[bk.id].remaining_by_scout)
    bus.sent.clear()

    # A DIFFERENT sample_dates on the next tick must be ignored for slice assignment -- the
    # scout's OWN persistent slice is what gets re-sent, not a fresh split of the new range.
    sched._drive_scouts(db, bk, sample_dates=["x1", "x2"])

    second_sends = {wid: chunk for wid, t, p in bus.sent if t == "discover" for chunk in [p["dates"]]}
    assert second_sends == first_slices
    assert sched._scout_pools[bk.id].remaining_by_scout == first_slices


def test_drive_scouts_includes_cached_special_days_in_discover_payload(db):
    """Shared gettime cache (storm-auto's proven _gettime_cache pattern): when the Hub already has a
    fresh cached fetch for this booking, every discover command sent this tick carries it -- so the
    scout can skip its own gettime fetch entirely."""
    bus = _FakeBus()
    event_hub = _FakeEventHub()
    cached = {"2026": {"8": [15, 16]}}
    event_hub.gettime_cache["test-booking"] = (cached, time.time())
    sched = _scheduler(bus=bus, event_hub=event_hub)
    bk = _make_booking(db, id="test-booking")
    ident = _make_identity(db)
    _make_worker(db, bk, ident, role=WorkerRole.SCOUT)
    _make_worker(db, bk, ident, role=WorkerRole.SCOUT)

    sched._drive_scouts(db, bk, sample_dates=["2026-08-18", "2026-08-19"])

    discover_sends = [(wid, p) for wid, t, p in bus.sent if t == "discover"]
    assert discover_sends   # SINGLE mode still only sends to one scout, but it must carry the cache
    for _wid, payload in discover_sends:
        assert payload.get("special_days") == cached


def test_drive_scouts_omits_special_days_when_nothing_cached(db):
    bus = _FakeBus()
    event_hub = _FakeEventHub()   # no cache entry at all
    sched = _scheduler(bus=bus, event_hub=event_hub)
    bk = _make_booking(db)
    ident = _make_identity(db)
    _make_worker(db, bk, ident, role=WorkerRole.SCOUT)

    sched._drive_scouts(db, bk, sample_dates=["d1"])

    discover_sends = [(wid, p) for wid, t, p in bus.sent if t == "discover"]
    assert discover_sends
    for _wid, payload in discover_sends:
        assert "special_days" not in payload


def test_drive_scouts_prunes_empty_dates_from_dates_reported(db):
    """dates_reported outcomes buffered on EventHub must get drained into the scout's own slice,
    retiring confirmed-empty dates (never 'failed'/unknown ones -- bug fix #6). Overrides
    DATE_RETIRE_AFTER_MISSES to 1 to keep this test about the WIRING (drain -> on_dates_reported ->
    prune), not the multi-strike counting itself (covered directly in test_orchestration.py)."""
    bus = _FakeBus()
    hub = _FakeEventHub()
    sched = _scheduler(bus=bus, event_hub=hub)
    bk = _make_booking(db, slot_found_fired_at=NOW, settings_json={"DATE_RETIRE_AFTER_MISSES": 1})
    ident = _make_identity(db)
    s1 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)

    sched._drive_scouts(db, bk, sample_dates=["d1", "d2", "d3"])
    assert sorted(sched._scout_pools[bk.id].remaining_by_scout[s1.id]) == ["d1", "d2", "d3"]

    hub.dates_reported[s1.id] = [[
        {"date": "d1", "outcome": "empty"},
        {"date": "d2", "outcome": "failed"},
        {"date": "d3", "outcome": "found"},
    ]]
    bus.sent.clear()
    sched._drive_scouts(db, bk, sample_dates=["d1", "d2", "d3"])

    assert sorted(sched._scout_pools[bk.id].remaining_by_scout[s1.id]) == ["d2", "d3"]   # d1 pruned
    assert hub.dates_reported.get(s1.id, []) == []   # drained


def test_drive_scouts_redistributes_dead_scouts_slice(db):
    """"if one scout failed then replace it with another available one" -- a scout that dies while
    holding a persistent slice must have its remaining dates picked up by a surviving/spare scout,
    not lost."""
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db, slot_found_fired_at=NOW)
    ident = _make_identity(db)
    s1 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)
    s2 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)

    sched._drive_scouts(db, bk, sample_dates=["d1", "d2", "d3", "d4"])
    state = sched._scout_pools[bk.id]
    assert set(state.remaining_by_scout) == {s1.id, s2.id}

    # s1 dies (no longer RUNNING) -- a fresh, previously-idle spare scout s3 shows up as its
    # available replacement.
    s1.status = WorkerStatus.FAILED
    db.commit()
    s3 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)

    sched._drive_scouts(db, bk, sample_dates=["d1", "d2", "d3", "d4"])
    state = sched._scout_pools[bk.id]

    assert s1.id not in state.remaining_by_scout
    all_remaining_dates = sorted(sum(state.remaining_by_scout.values(), []))
    assert all_remaining_dates == ["d1", "d2", "d3", "d4"]   # nothing lost


# ── status/substate coherence on the reap paths (review batch 2026-08-14) ─────
def test_reap_paths_clear_the_in_flight_substate(db):
    """The reapers wrote only `status`, so a worker killed mid-hold read `status=failed,
    substate=holding` forever -- and the runaway reaper compounded it by writing a substate_detail
    that flatly contradicted the substate sitting next to it."""
    sup, hub = _FakeSupervisor(), _FakeEventHub()
    sched = _scheduler(supervisor=sup, event_hub=hub, bus=_FakeBus())
    bk = _make_booking(db)
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, substate=WorkerSubstate.HOLDING)
    hub._rates[w.id] = 99.0   # trip the runaway breaker

    sched._reap_runaway_workers(db, NOW)

    db.refresh(w)
    assert w.status == WorkerStatus.FAILED
    assert w.substate is None                       # not still "holding"
    assert "runaway event rate" in w.substate_detail  # and the reason is recorded


def test_stop_booking_core_clears_substate_and_records_reason(db):
    sched = _scheduler(bus=_FakeBus(), supervisor=_FakeSupervisor())
    bk = _make_booking(db)
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, substate=WorkerSubstate.APPLYING)

    sched._stop_booking_core(db, bk, reason="timeout")

    db.refresh(w)
    assert w.status == WorkerStatus.STOPPED
    assert w.substate is None
    assert w.substate_detail == "booking timeout"


def test_hold_respawn_also_retries_held_workers():
    """HELD is exactly what hold-respawn exists for (an apply that got no slot / bounced). It used
    to land here only via the `held`->FAILED mis-mapping; now it must be named explicitly."""
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    relaunched = []
    sched = _scheduler(redis_client=r, on_relaunch=lambda db, w: relaunched.append(w.id))

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    bk = _make_booking(db)
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, status=WorkerStatus.HELD)

    sched._hold_respawn(db, bk, NOW)

    assert relaunched == [w.id]


# ── false-slot-open revert (operator spec 2026-08-14) ─────────────────────────
def test_inactivate_slot_open_reverts_to_warming_and_signals_workers(db):
    bus = _FakeBus()
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    sched = _scheduler(bus=bus, redis_client=redis)
    bk = _make_booking(db, slot_open_fired_at=NOW - timedelta(seconds=1300),
                       settings_json={"SLOT_OPEN_INACTIVATE_AFTER": 1200.0})
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)
    redis.sadd(notified_key(bk.id, "slot_open"), w.id)
    sched._scout_pools[bk.id] = orch.ScoutPoolState()

    assert sched._maybe_inactivate_slot_open(db, bk, NOW) is True

    assert bk.slot_open_fired_at is None
    assert bk.status == BookingStatus.WARMING
    assert (w.id, "signal", {"name": "inactivate"}) in bus.sent
    assert redis.smembers(notified_key(bk.id, "slot_open")) == set()   # a re-fire reaches everyone
    assert bk.id not in sched._scout_pools                              # discovery state reset


def test_inactivate_slot_open_does_nothing_before_the_timeout(db):
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db, slot_open_fired_at=NOW - timedelta(seconds=100),
                       settings_json={"SLOT_OPEN_INACTIVATE_AFTER": 1200.0})

    assert sched._maybe_inactivate_slot_open(db, bk, NOW) is False
    assert bk.slot_open_fired_at is not None
    assert bus.sent == []


def test_inactivate_slot_open_never_fires_once_slot_found_fired(db):
    """slot_found means the open was REAL -- there are slots. Never revert."""
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db, slot_open_fired_at=NOW - timedelta(seconds=9999),
                       slot_found_fired_at=NOW - timedelta(seconds=9000),
                       settings_json={"SLOT_OPEN_INACTIVATE_AFTER": 1200.0})

    assert sched._maybe_inactivate_slot_open(db, bk, NOW) is False
    assert bk.slot_open_fired_at is not None


def test_inactivate_slot_open_never_fires_when_a_slot_row_exists(db):
    """Belt-and-braces: a slot found but not yet latched into slot_found_fired_at still means the
    open was real."""
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db, slot_open_fired_at=NOW - timedelta(seconds=9999),
                       settings_json={"SLOT_OPEN_INACTIVATE_AFTER": 1200.0})
    db.add(M.Slot(booking_id=bk.id, posto_id="5084", date="2026-08-18", period=4,
                  status=SlotStatus.AVAILABLE))
    db.commit()

    assert sched._maybe_inactivate_slot_open(db, bk, NOW) is False
    assert bk.slot_open_fired_at is not None


def test_inactivate_slot_open_disabled_by_zero_setting(db):
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db, slot_open_fired_at=NOW - timedelta(seconds=99999),
                       settings_json={"SLOT_OPEN_INACTIVATE_AFTER": 0.0})

    assert sched._maybe_inactivate_slot_open(db, bk, NOW) is False
    assert bk.slot_open_fired_at is not None


def test_assign_slots_skips_worker_with_outstanding_command(db):
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db)
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.HOLDING)
    bus.pending.add(w.id)
    db.add(M.Slot(booking_id=bk.id, posto_id="5084", date="2026-08-18", period=4,
                  status=SlotStatus.AVAILABLE))
    db.commit()

    sched._assign_slots(db, bk)

    db.refresh(w)
    assert bus.sent == []
    assigned_slot = db.scalars(select(M.Slot).where(M.Slot.booking_id == bk.id)).first()
    assert assigned_slot.status == SlotStatus.AVAILABLE   # not leased -- worker was skipped


def test_assign_slots_multi_capacity_fills_one_slot_before_the_next(db):
    """Multi-capacity leasing (operator spec 2026-08-14): a physical slot absorbs
    SLOT_CAPACITY_PER_PERIOD concurrent leases before it's exhausted -- 2 workers can both land on
    the SAME slot when its cap is 2, and the slot only flips to LEASED once genuinely full."""
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db, settings_json={"SLOT_CAPACITY_PER_PERIOD": "2"})
    ident = _make_identity(db)
    w1 = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.HOLDING)
    w2 = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.HOLDING)
    w3 = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.HOLDING)
    slot = M.Slot(booking_id=bk.id, posto_id="5084", date="2026-08-18", period=4,
                  status=SlotStatus.AVAILABLE)
    db.add(slot)
    db.commit()

    sched._assign_slots(db, bk)

    leases = db.scalars(select(M.SlotLease).where(M.SlotLease.slot_id == slot.id)).all()
    assert len(leases) == 2   # only 2 of 3 workers fit -- the slot's cap is 2
    assert {le.worker_id for le in leases} == {w1.id, w2.id}
    db.refresh(slot)
    assert slot.status == SlotStatus.LEASED   # now genuinely full
    unassigned = db.get(M.Worker, w3.id)
    assert unassigned is not None   # w3 simply wasn't paired this tick, nothing broke


def test_assign_slots_respects_already_active_leases_when_computing_remaining_capacity(db):
    """A slot with 1 active lease out of a cap of 2 has exactly 1 unit of room left -- confirms
    remaining capacity is computed from CURRENT active SlotLease rows, not just the cap alone."""
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db, settings_json={"SLOT_CAPACITY_PER_PERIOD": "2"})
    ident = _make_identity(db)
    w1 = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.HOLDING)
    w2 = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.HOLDING)
    slot = M.Slot(booking_id=bk.id, posto_id="5084", date="2026-08-18", period=4,
                  status=SlotStatus.AVAILABLE)
    db.add(slot)
    db.commit()
    other_worker = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.APPLYING)
    db.add(M.SlotLease(slot_id=slot.id, worker_id=other_worker.id))
    db.commit()

    sched._assign_slots(db, bk)

    leases = db.scalars(select(M.SlotLease).where(M.SlotLease.slot_id == slot.id)).all()
    assert len(leases) == 2   # the pre-existing lease + exactly 1 new one (room for only 1 more)
    new_worker_ids = {le.worker_id for le in leases} - {other_worker.id}
    assert new_worker_ids == {w1.id}   # w1 assigned first (in query order), w2 left unpaired


def test_drive_scouts_uses_the_bookings_own_calendar_window(db):
    """Confirmed live 2026-08-14: _drive_scouts hardcoded start=""/end=""/num_days=20, so
    CAL_START_DATE/CAL_END_DATE/CAL_NUM_DAYS -- set per booking, validated by the resolver, parsed
    into EngineConfig.discovery -- were read by nothing on this path. A booking configured for
    2026-09-01..09-20 discovered 2026-08-28..09-11 instead."""
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db, settings_json={"CAL_START_DATE": "2026-09-01",
                                          "CAL_END_DATE": "2026-09-04",
                                          "CAL_NUM_DAYS": 20})
    ident = _make_identity(db)
    _make_worker(db, bk, ident, role=WorkerRole.SCOUT)

    sched._drive_scouts(db, bk)   # no sample_dates -- exercise the real window computation

    sent = [p["dates"] for _wid, t, p in bus.sent if t == "discover"]
    assert sent, "no discover command was sent"
    dates = sent[0]
    assert min(dates) >= "2026-09-01"
    assert max(dates) <= "2026-09-04"


def test_drive_scouts_falls_back_to_a_default_window_when_unset(db):
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db)          # no CAL_* settings at all
    ident = _make_identity(db)
    _make_worker(db, bk, ident, role=WorkerRole.SCOUT)

    sched._drive_scouts(db, bk)

    dates = [p["dates"] for _wid, t, p in bus.sent if t == "discover"][0]
    assert len(dates) == 20         # CAL_NUM_DAYS default


# ── one active slot lease per worker (confirmed live 2026-08-14) ──────────────
def test_assign_slots_never_gives_a_worker_a_second_active_lease(db):
    """Phase F2's enforce_single_lease invariant, which was built, unit-tested, and then called
    from nowhere. On booking Test2 one worker (ERNESTO PIRES) accumulated 120 active leases -- 5 per
    slot across all 24 slots, the whole pool -- because a HOLDING worker with no pending command is
    re-assignable on EVERY 5s tick. Once consumed, every slot read LEASED, `avail` came back empty,
    and no worker could be assigned again: deadlock with one worker owning everything."""
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db, settings_json={"SLOT_CAPACITY_PER_PERIOD": "5"})
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.HOLDING)
    for period in (1, 2, 3):
        db.add(M.Slot(booking_id=bk.id, posto_id="5084", date="2026-09-01", period=period,
                      status=SlotStatus.AVAILABLE))
    db.commit()

    for _ in range(5):            # five scheduler ticks, worker never leaves HOLDING
        sched._assign_slots(db, bk)

    active = db.scalars(select(M.SlotLease).where(
        M.SlotLease.worker_id == w.id, M.SlotLease.released_at.is_(None))).all()
    assert len(active) == 1       # not 5, and certainly not one per slot-capacity-unit


# ── stale lease reclaim ───────────────────────────────────────────────────────
def test_reclaim_returns_a_slot_held_by_a_worker_that_is_no_longer_running(db):
    sched = _scheduler(bus=_FakeBus())
    bk = _make_booking(db, settings_json={"SLOT_CAPACITY_PER_PERIOD": "1"})
    ident = _make_identity(db)
    dead = _make_worker(db, bk, ident, role=WorkerRole.APPLY, status=WorkerStatus.FAILED)
    slot = M.Slot(booking_id=bk.id, posto_id="5084", date="2026-09-01", period=1,
                  status=SlotStatus.LEASED)
    db.add(slot); db.commit()
    db.add(M.SlotLease(slot_id=slot.id, worker_id=dead.id, leased_at=NOW))
    db.commit()

    sched._reclaim_stale_slot_leases(db, bk, NOW)

    db.refresh(slot)
    assert slot.status == SlotStatus.AVAILABLE
    assert db.scalars(select(M.SlotLease).where(M.SlotLease.released_at.is_(None))).all() == []


def test_reclaim_expires_a_lease_the_holder_never_converted(db):
    sched = _scheduler(bus=_FakeBus())
    bk = _make_booking(db, settings_json={"SLOT_LEASE_TTL": 180.0, "SLOT_CAPACITY_PER_PERIOD": "1"})
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.HOLDING)
    slot = M.Slot(booking_id=bk.id, posto_id="5084", date="2026-09-01", period=1,
                  status=SlotStatus.LEASED)
    db.add(slot); db.commit()
    db.add(M.SlotLease(slot_id=slot.id, worker_id=w.id, leased_at=NOW - timedelta(seconds=600)))
    db.commit()

    sched._reclaim_stale_slot_leases(db, bk, NOW)

    db.refresh(slot)
    assert slot.status == SlotStatus.AVAILABLE


def test_reclaim_never_touches_a_worker_that_is_actually_applying(db):
    """However long a real booking attempt takes, its slot is not up for grabs."""
    sched = _scheduler(bus=_FakeBus())
    bk = _make_booking(db, settings_json={"SLOT_LEASE_TTL": 1.0})
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.APPLYING)
    slot = M.Slot(booking_id=bk.id, posto_id="5084", date="2026-09-01", period=1,
                  status=SlotStatus.LEASED)
    db.add(slot); db.commit()
    db.add(M.SlotLease(slot_id=slot.id, worker_id=w.id, leased_at=NOW - timedelta(seconds=9999)))
    db.commit()

    sched._reclaim_stale_slot_leases(db, bk, NOW)

    db.refresh(slot)
    assert slot.status == SlotStatus.LEASED
    assert len(db.scalars(select(M.SlotLease).where(M.SlotLease.released_at.is_(None))).all()) == 1


def test_drive_scouts_does_not_retire_dates_before_slot_found(db):
    """End-to-end phase scoping: during the slot-open phase the portal has not released its slots,
    so every date reports empty -- and none of it may count toward retirement. Before this was
    scoped, three single-mode rounds at the default threshold retired the entire window the moment
    split mode began."""
    bus = _FakeBus()
    hub = _FakeEventHub()
    sched = _scheduler(bus=bus, event_hub=hub)
    bk = _make_booking(db, settings_json={"DATE_RETIRE_AFTER_MISSES": 1})   # NO slot_found_fired_at
    ident = _make_identity(db)
    s1 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)

    for _ in range(4):
        hub.dates_reported[s1.id] = [[{"date": d, "outcome": "empty"} for d in ("d1", "d2", "d3")]]
        sched._drive_scouts(db, bk, sample_dates=["d1", "d2", "d3"])

    state = sched._scout_pools[bk.id]
    assert state.mode == "single"
    assert state.date_miss_counts == {}          # nothing accrued in the slot-open phase

    # now slots are found -- the full range must still be available to scan
    bk.slot_found_fired_at = NOW
    db.commit()
    sched._drive_scouts(db, bk, sample_dates=["d1", "d2", "d3"])
    assert sorted(sum(sched._scout_pools[bk.id].remaining_by_scout.values(), [])) == ["d1", "d2", "d3"]

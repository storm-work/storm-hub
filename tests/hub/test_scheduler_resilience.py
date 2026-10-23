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
from libs.bus.names import manual_open_key, notified_key
from libs.db import metadata
from libs.db import models as M
from libs.engine import orchestration as orch
from libs.domain.enums import (
    BookingStatus,
    SlotLeaseOutcome,
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
    sched = _scheduler(redis_client=r, supervisor=sup, on_relaunch=lambda db, ws: relaunched.extend(x.id for x in ws))

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
    sched = _scheduler(redis_client=r, on_relaunch=lambda db, ws: relaunched.extend(x.id for x in ws))

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
    sched = _scheduler(redis_client=r, on_relaunch=lambda db, ws: relaunched.extend(x.id for x in ws))
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
    sched = _scheduler(redis_client=r, on_relaunch=lambda db, ws: relaunched.extend(x.id for x in ws))
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
    apply_w = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)

    sched._maybe_self_scout(db, bk, NOW)

    assert any(t == "assign" and p.get("scouts_invalid") is True
              for _wid, t, p in bus.sent)


def test_maybe_self_scout_does_nothing_when_scout_alive(db):
    """A scout that is genuinely scouting still suppresses the fallback.

    Updated 2026-09-01: this scout used to be created with substate=None, which passed under the
    old `status == RUNNING` gate. That gate is what let a scout thrashing at the login wall count
    as alive (see test_a_scout_stuck_in_login_does_not_suppress_the_fallback), so "alive" now means
    a scouting substate -- and the fixture has to say which one it is."""
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db)
    ident = _make_identity(db)
    _make_worker(db, bk, ident, role=WorkerRole.SCOUT, status=WorkerStatus.RUNNING,
                 substate=WorkerSubstate.KEEPING)
    _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)

    sched._maybe_self_scout(db, bk, NOW)

    assert bus.sent == []


def test_a_scout_with_no_substate_yet_does_not_suppress_the_fallback(db):
    """Deliberate, and the one behaviour change with a real cost: a RUNNING scout that has not yet
    reported a substate (mid-launch) no longer counts as alive, so the fallback can fire during a
    healthy scout's login. That costs at most ONE command to one holding apply worker per
    _SELF_SCOUT_THROTTLE (30s) window, and the apply worker only self-discovers. Weighed against
    the measured alternative -- four booking runs, zero bookings, the fallback permanently
    suppressed -- firing early is the cheaper error. Pinned here so it cannot change silently."""
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db)
    ident = _make_identity(db)
    _make_worker(db, bk, ident, role=WorkerRole.SCOUT, status=WorkerStatus.RUNNING, substate=None)
    _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)

    sched._maybe_self_scout(db, bk, NOW)

    assert any(t == "assign" and p.get("scouts_invalid") for _w, t, p in bus.sent)


def test_maybe_self_scout_throttled(db):
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db)
    ident = _make_identity(db)
    _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)

    sched._maybe_self_scout(db, bk, NOW)
    first_len = len(bus.sent)
    sched._maybe_self_scout(db, bk, NOW)   # immediately again -- throttle window not elapsed
    assert len(bus.sent) == first_len


def test_maybe_self_scout_skips_while_found_slots_are_still_there(db):
    """Renamed and corrected 2026-09-01 (was test_maybe_self_scout_skips_once_slot_found).

    It asserted that setting slot_found_fired_at alone suppresses the fallback, with NO slot rows
    in the booking at all -- which is the one-shot-latch defect stated as a requirement. What the
    gate is actually for is "we already have slots to work", so the fixture now has one. The
    exhausted-pool half of the old assertion moved to
    test_an_exhausted_pool_re_enables_the_fallback_even_though_slot_found_fired."""
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db, slot_found_fired_at=NOW)
    ident = _make_identity(db)
    db.add(M.Slot(booking_id=bk.id, posto_id="5084", date="2026-09-14", period=3,
                  status=SlotStatus.AVAILABLE))
    _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)
    db.commit()

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


def test_drive_scouts_dispatches_one_scout_per_round(db):
    """Rotation model: one scout at a time runs a FULL-range round. A single tick dispatches exactly
    one discover command, and while that round is in flight no other scout is dispatched."""
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db)
    ident = _make_identity(db)
    s1 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)
    s2 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)

    sched._drive_scouts(db, bk, sample_dates=["2026-08-18", "2026-08-19"])
    sends = [(wid, p) for wid, t, p in bus.sent if t == "discover"]
    assert len(sends) == 1 and sends[0][0] in (s1.id, s2.id)
    # the whole window goes every round -- full discovery per scout, not a slice
    assert sends[0][1]["dates"] == ["2026-08-18", "2026-08-19"]

    # next tick, the first round still in flight (no report drained) -> nothing new dispatched
    bus.sent.clear()
    sched._drive_scouts(db, bk, sample_dates=["2026-08-18", "2026-08-19"])
    assert bus.sent == []


def test_drive_scouts_runs_the_same_before_and_after_slot_found(db):
    """There is no slot-found phase transition any more -- the rotation is identical throughout."""
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db, slot_found_fired_at=NOW)
    ident = _make_identity(db)
    _make_worker(db, bk, ident, role=WorkerRole.SCOUT)
    _make_worker(db, bk, ident, role=WorkerRole.SCOUT)

    sched._drive_scouts(db, bk, sample_dates=["d1", "d2", "d3", "d4"])
    sends = [wid for wid, t, p in bus.sent if t == "discover"]
    assert len(sends) == 1   # still one-at-a-time, not a fan-out split


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


def test_drive_scouts_rotates_and_a_failed_round_advances_immediately(db):
    """A FAILED round (all-'failed' / empty report) frees the scout and advances the rotation to the
    next scout with NO MIN wait -- a bad session never stalls the fleet."""
    bus = _FakeBus()
    hub = _FakeEventHub()
    sched = _scheduler(bus=bus, event_hub=hub)
    bk = _make_booking(db, settings_json={"DISCOVERY_MIN_INTERVAL": 30.0})
    ident = _make_identity(db)
    s1 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)
    s2 = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)

    sched._drive_scouts(db, bk, sample_dates=["d1"])
    first = [wid for wid, t, p in bus.sent if t == "discover"]
    assert len(first) == 1
    inflight = first[0]

    # that round comes back FAILED -> next tick immediately dispatches the OTHER scout, no pacing
    hub.dates_reported[inflight] = [[{"date": "d1", "outcome": "failed"}]]
    bus.sent.clear()
    sched._drive_scouts(db, bk, sample_dates=["d1"])
    second = [wid for wid, t, p in bus.sent if t == "discover"]
    assert len(second) == 1 and second[0] != inflight   # advanced, immediately


def test_drive_scouts_paces_real_rounds_by_min_interval(db):
    """A REAL round (portal reached, even 'empty') holds the next dispatch until DISCOVERY_MIN_INTERVAL
    has elapsed -- pacing real portal traffic, not the 5s scheduler tick."""
    bus = _FakeBus()
    hub = _FakeEventHub()
    sched = _scheduler(bus=bus, event_hub=hub)
    bk = _make_booking(db, settings_json={"DISCOVERY_MIN_INTERVAL": 30.0})
    ident = _make_identity(db)
    _make_worker(db, bk, ident, role=WorkerRole.SCOUT)
    _make_worker(db, bk, ident, role=WorkerRole.SCOUT)

    sched._drive_scouts(db, bk, sample_dates=["d1"])
    inflight = [wid for wid, t, p in bus.sent if t == "discover"][0]

    # a REAL round (portal said 'empty') completes -> the interval gate now applies
    hub.dates_reported[inflight] = [[{"date": "d1", "outcome": "empty"}]]
    bus.sent.clear()
    sched._drive_scouts(db, bk, sample_dates=["d1"])
    assert bus.sent == []   # inside the interval -- must wait

    # simulate the interval having elapsed, then it dispatches the next scout
    sched._scout_pools[bk.id].last_real_probe_at -= 31.0
    sched._drive_scouts(db, bk, sample_dates=["d1"])
    assert len([1 for _, t, _ in bus.sent if t == "discover"]) == 1


def test_drive_scouts_includes_cached_special_days_in_discover_payload(db):
    """Shared gettime cache (storm-auto's proven _gettime_cache pattern): when the Hub already has a
    fresh cached fetch for this booking, the discover round sent this tick carries it -- so the scout
    can skip its own gettime fetch entirely."""
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
    assert discover_sends
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


# ── status/substate coherence on the reap paths (review batch 2026-08-14) ─────
def test_reap_paths_clear_the_in_flight_substate(db):
    """The reapers wrote only `status`, so a worker killed mid-hold read `status=failed,
    substate=holding` forever -- and the runaway reaper compounded it by writing a substate_detail
    that flatly contradicted the substate sitting next to it."""
    sup, hub = _FakeSupervisor(), _FakeEventHub()
    sched = _scheduler(supervisor=sup, event_hub=hub, bus=_FakeBus())
    bk = _make_booking(db)
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, substate=WorkerSubstate.KEEPING)
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
    sched = _scheduler(redis_client=r, on_relaunch=lambda db, ws: relaunched.extend(x.id for x in ws))

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


def test_manual_revert_slot_open_signals_workers_even_for_a_manual_fire(db):
    """Operator manual revert (2026-09-26): unlike the auto-guard, it undoes a MANUAL fire too and
    needs no timeout -- inactivate every RUNNING worker (scouts -> KEEPING), clear the fired stamps +
    latch sets + manual flag, reset the pool, booking -> WARMING."""
    bus = _FakeBus()
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    sched = _scheduler(bus=bus, redis_client=redis)
    bk = _make_booking(db, slot_open_fired_at=NOW - timedelta(seconds=10),
                       slot_open_at=NOW - timedelta(seconds=20))   # a PAST scheduled trigger
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)
    redis.sadd(notified_key(bk.id, "slot_open"), w.id)
    redis.set(manual_open_key(bk.id), "1")           # a MANUAL fire the auto-guard would skip
    sched._scout_pools[bk.id] = orch.ScoutPoolState()

    n = sched.revert_slot_open(db, bk)

    assert n == 1
    assert bk.slot_open_fired_at is None and bk.status == BookingStatus.WARMING
    assert bk.slot_open_at is None, "a PAST slot_open_at must be cleared or it re-fires next tick"
    assert (w.id, "signal", {"name": "inactivate"}) in bus.sent
    assert redis.smembers(notified_key(bk.id, "slot_open")) == set()
    assert not redis.get(manual_open_key(bk.id))       # manual flag cleared -> re-fire is clean
    assert bk.id not in sched._scout_pools


def test_assign_slots_skips_worker_with_outstanding_command(db):
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db)
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)
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
    w1 = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)
    w2 = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)
    w3 = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)
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


def test_assign_slots_sends_a_recently_bounced_slot_to_the_back(db):
    """A just-BOUNCED blind slot must yield to a fresh one, so a single bouncing slot cannot
    monopolize assignment while other workers starve. CONFIRMED live Test 1002 (2026-10-02): worker
    8fac5d27 took one booked-but-exhausted slot 18 times in a row (it stayed at the FRONT, being the
    earliest date) while another worker got ZERO leases. Mirrors the long-standing form_stale
    reorder, now extended to BOUNCED."""
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db)
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)
    # `a` is the EARLIER date (normally assigned first) but was just bounced; `b` is fresh.
    a = M.Slot(booking_id=bk.id, posto_id="5084", date="2026-08-18", period=4,
               status=SlotStatus.AVAILABLE, source="blind")
    b = M.Slot(booking_id=bk.id, posto_id="5084", date="2026-08-19", period=4,
               status=SlotStatus.AVAILABLE, source="blind")
    db.add_all([a, b]); db.commit()
    other = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.APPLYING)
    db.add(M.SlotLease(slot_id=a.id, worker_id=other.id, outcome=SlotLeaseOutcome.BOUNCED,
                       released_at=datetime.now(timezone.utc)))
    db.commit()

    sched._assign_slots(db, bk)

    leases = db.scalars(select(M.SlotLease).where(
        M.SlotLease.worker_id == w.id, M.SlotLease.released_at.is_(None))).all()
    assert len(leases) == 1
    assert leases[0].slot_id == b.id, "the fresh slot is assigned before the just-bounced earlier one"


def test_assign_slots_respects_already_active_leases_when_computing_remaining_capacity(db):
    """A slot with 1 active lease out of a cap of 2 has exactly 1 unit of room left -- confirms
    remaining capacity is computed from CURRENT active SlotLease rows, not just the cap alone."""
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db, settings_json={"SLOT_CAPACITY_PER_PERIOD": "2"})
    ident = _make_identity(db)
    w1 = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)
    w2 = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)
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
    2026-09-01..09-20 discovered 2026-08-28..09-11 instead.

    The window is computed RELATIVE TO TODAY rather than hardcoded. It was originally pinned to
    2026-09-01..09-04, which silently became a time bomb: default_date_window clamps the window to
    `today`, so once that end date fell into the past the window came back empty, no discover
    command was sent, and the test failed on a date boundary rather than on any real regression --
    which it duly did from 2026-09-05, masking this assertion for every run after that."""
    from datetime import date, timedelta

    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    # +7/+10 days out: comfortably future (so nothing is clamped away) and inside the 20-weekday
    # default, so the CAL_START/END bounds -- not CAL_NUM_DAYS -- are what constrain the result.
    start = (date.today() + timedelta(days=7)).isoformat()
    end = (date.today() + timedelta(days=10)).isoformat()
    bk = _make_booking(db, settings_json={"CAL_START_DATE": start,
                                          "CAL_END_DATE": end,
                                          "CAL_NUM_DAYS": 20})
    ident = _make_identity(db)
    _make_worker(db, bk, ident, role=WorkerRole.SCOUT)

    sched._drive_scouts(db, bk)   # no sample_dates -- exercise the real window computation

    sent = [p["dates"] for _wid, t, p in bus.sent if t == "discover"]
    assert sent, "no discover command was sent"
    dates = sent[0]
    assert min(dates) >= start
    assert max(dates) <= end


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
    w = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)
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
    w = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)
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


# ── false-slot-FOUND revert (operator spec 2026-08-14) ────────────────────────
# "even when there are some slots found - but they may be false positive. so if applying keeps being
# failed then regard the slots as false, and revert to hold phase. wipe out the slots and slot-found
# signal state, then make scouts to be at slot-open phase."
def _bounced_pool(db, bk, worker, *, n_bounces, since, source="scout"):
    """A slot pool whose leases all came back BOUNCED after slot_found fired."""
    from libs.domain.enums import SlotLeaseOutcome
    slots = []
    for i in range(max(1, n_bounces)):
        # (date, period) must stay unique -- slots has a UNIQUE(booking_id, date, period)
        s = M.Slot(booking_id=bk.id, posto_id="5084", date=f"2026-09-{i // 4 + 1:02d}",
                   period=i % 4 + 1, status=SlotStatus.AVAILABLE, source=source)
        db.add(s); slots.append(s)
    db.commit()
    for i in range(n_bounces):
        db.add(M.SlotLease(slot_id=slots[i].id, worker_id=worker.id,
                           leased_at=since, released_at=since + timedelta(seconds=10 + i),
                           outcome=SlotLeaseOutcome.BOUNCED))
    db.commit()
    return slots


def test_false_slot_found_wipes_the_pool_and_returns_scouts_to_the_slot_open_phase(db):
    bus = _FakeBus()
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    sched = _scheduler(bus=bus, redis_client=redis)
    found_at = NOW - timedelta(minutes=10)
    bk = _make_booking(db, slot_open_fired_at=NOW - timedelta(minutes=30),
                       slot_found_fired_at=found_at,
                       settings_json={"FALSE_SLOT_BOUNCE_LIMIT": 5})
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)
    _bounced_pool(db, bk, w, n_bounces=5, since=found_at)
    redis.sadd(notified_key(bk.id, "slot_open"), w.id)
    redis.sadd(notified_key(bk.id, "slot_found"), w.id)
    sched._scout_pools[bk.id] = orch.ScoutPoolState()

    assert sched._maybe_revert_false_slot_found(db, bk, NOW) is True

    # the pool is wiped...
    assert {s.status for s in db.scalars(select(M.Slot)).all()} == {SlotStatus.CLOSED}
    # ...the slot-found signal state is gone, Hub-side and worker-side...
    assert bk.slot_found_fired_at is None
    assert (w.id, "signal", {"name": "inactivate"}) in bus.sent
    assert redis.smembers(notified_key(bk.id, "slot_found")) == set()
    # ...and the scouts land back in the SLOT-OPEN phase, not in warming: slot_open stays fired and
    # its notified-set is cleared so _latch_signals re-delivers it after `inactivate` cleared the
    # worker-side latch.
    assert bk.slot_open_fired_at is not None
    assert bk.status == BookingStatus.RUNNING
    assert redis.smembers(notified_key(bk.id, "slot_open")) == set()
    assert bk.id not in sched._scout_pools          # back to cheap single-mode polling


def test_false_slot_revert_ignores_bounces_off_blind_slots(db):
    """THE SEAM for blind apply in the scheduler (2026-09-18). An operator-seeded grid is mostly
    phantom BY CONSTRUCTION -- 9 combinations, maybe 2 real -- so bouncing off it is expected, not
    evidence the pool is false. Counted, a single sweep would hit the default limit of 12 and wipe
    the whole pool, ending blind apply after one pass. Blind slots retire individually via
    BLIND_MAX_BOUNCES instead."""
    import fakeredis
    from libs.bus.names import manual_open_key, notified_key
    from libs.engine import orchestration as orch

    bus = _FakeBus()
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    sched = _scheduler(bus=bus, redis_client=redis)
    found_at = NOW - timedelta(minutes=10)
    bk = _make_booking(db, slot_open_fired_at=NOW - timedelta(minutes=30),
                       slot_found_fired_at=found_at,
                       settings_json={"FALSE_SLOT_BOUNCE_LIMIT": 5})
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)
    _bounced_pool(db, bk, w, n_bounces=9, since=found_at, source="blind")   # well past the limit
    redis.sadd(notified_key(bk.id, "slot_found"), w.id)
    sched._scout_pools[bk.id] = orch.ScoutPoolState()

    assert sched._maybe_revert_false_slot_found(db, bk, NOW) is False
    assert bk.slot_found_fired_at is not None, "blind bounces must not revert slot-found"
    assert {s.status for s in db.scalars(select(M.Slot)).all()} == {SlotStatus.AVAILABLE}


def test_seeding_blind_slots_disables_the_false_slot_revert(db):
    """THE scout->fail->blind interaction (operator question 2026-09-20). Scouts find a few
    mostly-phantom slots, apply workers bounce them PAST the limit, the operator then seeds a blind
    grid. The revert is tripped by the ORIGINAL discovered bounces -- but its step-1 wipe closes
    every AVAILABLE/LEASED slot regardless of source, so without this guard it would destroy the
    freshly-seeded blind grid, clear slot-found, and inactivate the fleet the operator just pointed
    at it. Once blind slots exist the operator owns the pool: the automatic revert must stand down."""
    import fakeredis
    from libs.bus.names import manual_open_key, notified_key
    from libs.engine import orchestration as orch

    bus = _FakeBus()
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    sched = _scheduler(bus=bus, redis_client=redis)
    found_at = NOW - timedelta(minutes=10)
    bk = _make_booking(db, slot_open_fired_at=NOW - timedelta(minutes=30),
                       slot_found_fired_at=found_at, settings_json={"FALSE_SLOT_BOUNCE_LIMIT": 5})
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, role=WorkerRole.SCOUT)
    # DISCOVERED slots that bounced well past the limit -- would revert on their own.
    _bounced_pool(db, bk, w, n_bounces=8, since=found_at, source="scout")
    # ...but the operator has since seeded a blind grid.
    db.add(M.Slot(booking_id=bk.id, posto_id="5084", date="2026-10-12", period=3,
                  status=SlotStatus.AVAILABLE, source="blind"))
    db.commit()
    sched._scout_pools[bk.id] = orch.ScoutPoolState()

    assert sched._maybe_revert_false_slot_found(db, bk, NOW) is False, "blind slots must veto the revert"
    assert bk.slot_found_fired_at is not None
    # the blind slot is untouched, and the discovered slots are NOT wiped either (no revert ran)
    statuses = {s.source: s.status for s in db.scalars(select(M.Slot).where(M.Slot.booking_id == bk.id)).all()}
    assert statuses["blind"] == SlotStatus.AVAILABLE
    assert SlotStatus.CLOSED not in statuses.values()
    assert (w.id, "signal", {"name": "inactivate"}) not in bus.sent


def test_false_slot_found_does_not_fire_below_the_bounce_limit(db):
    sched = _scheduler(bus=_FakeBus())
    found_at = NOW - timedelta(minutes=10)
    bk = _make_booking(db, slot_found_fired_at=found_at,
                       settings_json={"FALSE_SLOT_BOUNCE_LIMIT": 5})
    w = _make_worker(db, bk, _make_identity(db))
    _bounced_pool(db, bk, w, n_bounces=4, since=found_at)

    assert sched._maybe_revert_false_slot_found(db, bk, NOW) is False
    assert bk.slot_found_fired_at == found_at
    assert {s.status for s in db.scalars(select(M.Slot)).all()} == {SlotStatus.AVAILABLE}


def test_false_slot_found_never_fires_once_something_was_actually_booked(db):
    """A real booking proves the pool was real, however many other periods bounced."""
    sched = _scheduler(bus=_FakeBus())
    found_at = NOW - timedelta(minutes=10)
    bk = _make_booking(db, slot_found_fired_at=found_at,
                       settings_json={"FALSE_SLOT_BOUNCE_LIMIT": 5})
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident)
    _bounced_pool(db, bk, w, n_bounces=20, since=found_at)
    sub = M.SubAccount(identity_id=ident.id, username="u1", password="p1")
    db.add(sub); db.commit()
    db.add(M.BookedAppointment(booking_id=bk.id, identity_id=ident.id, sub_account_id=sub.id))
    db.commit()

    assert sched._maybe_revert_false_slot_found(db, bk, NOW) is False


def test_false_slot_found_ignores_bounces_from_a_previous_found_window(db):
    """The count window restarts at each slot_found -- stale bounces from an earlier, already-
    reverted window must not instantly re-trip the revert on the next genuine find."""
    sched = _scheduler(bus=_FakeBus())
    old_window = NOW - timedelta(hours=3)
    bk = _make_booking(db, slot_found_fired_at=NOW - timedelta(minutes=1),
                       settings_json={"FALSE_SLOT_BOUNCE_LIMIT": 5})
    w = _make_worker(db, bk, _make_identity(db))
    _bounced_pool(db, bk, w, n_bounces=20, since=old_window)

    assert sched._maybe_revert_false_slot_found(db, bk, NOW) is False


def test_false_slot_found_disabled_by_zero_setting(db):
    sched = _scheduler(bus=_FakeBus())
    found_at = NOW - timedelta(minutes=10)
    bk = _make_booking(db, slot_found_fired_at=found_at,
                       settings_json={"FALSE_SLOT_BOUNCE_LIMIT": 0})
    w = _make_worker(db, bk, _make_identity(db))
    _bounced_pool(db, bk, w, n_bounces=50, since=found_at)

    assert sched._maybe_revert_false_slot_found(db, bk, NOW) is False


def test_false_slot_revert_ignores_form_stale_releases(db):
    """The false-slot verdict must rest on evidence the POOL is phantom. form_stale releases are
    evidence our own sessions lapsed -- counting them would let a run of bad logins condemn a
    perfectly real slot pool and send the scouts back to square one."""
    from libs.domain.enums import SlotLeaseOutcome
    sched = _scheduler(bus=_FakeBus())
    found_at = NOW - timedelta(minutes=10)
    bk = _make_booking(db, slot_found_fired_at=found_at,
                       settings_json={"FALSE_SLOT_BOUNCE_LIMIT": 5})
    w = _make_worker(db, bk, _make_identity(db))
    slots = _bounced_pool(db, bk, w, n_bounces=0, since=found_at)
    for i in range(20):
        s = M.Slot(booking_id=bk.id, posto_id="5084", date=f"2026-10-{i + 1:02d}", period=1,
                   status=SlotStatus.AVAILABLE)
        db.add(s); db.commit()
        db.add(M.SlotLease(slot_id=s.id, worker_id=w.id, leased_at=found_at,
                           released_at=found_at + timedelta(seconds=10 + i),
                           outcome=SlotLeaseOutcome.FORM_STALE))
    db.commit()

    assert sched._maybe_revert_false_slot_found(db, bk, NOW) is False


# ── MAX_BOOKINGS: nothing in the codebase ever completed a booking (2026-08-14) ───────────────
def _appointment(db, bk, ident):
    sub = M.SubAccount(identity_id=ident.id, username=f"u{ident.id[:4]}", password="p")
    db.add(sub); db.commit()
    db.add(M.BookedAppointment(booking_id=bk.id, identity_id=ident.id, sub_account_id=sub.id))
    db.commit()


def test_booking_completes_once_it_hits_its_booking_target(db):
    """The RUNNING -> COMPLETED transition existed in the state machine and NO code ever performed
    it, so a run that achieved everything asked of it kept its scouts discovering and its appliers
    logging in until the 48h timeout -- burning proxies and CAPTCHA solves for nothing."""
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db, settings_json={"MAX_BOOKINGS": 2})
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident)
    _appointment(db, bk, ident)
    _appointment(db, bk, _make_identity(db))

    assert sched._maybe_complete_booking(db, bk) is True
    assert bk.status == BookingStatus.COMPLETED       # not ABORTED -- it succeeded
    assert (w.id, "stop", {}) in bus.sent or any(c[1] == "stop" for c in bus.sent)
    assert w.status == WorkerStatus.STOPPED


def test_booking_does_not_complete_below_its_target(db):
    sched = _scheduler(bus=_FakeBus())
    bk = _make_booking(db, settings_json={"MAX_BOOKINGS": 3})
    ident = _make_identity(db)
    _make_worker(db, bk, ident)
    _appointment(db, bk, ident)

    assert sched._maybe_complete_booking(db, bk) is False
    assert bk.status == BookingStatus.RUNNING


def test_max_bookings_zero_means_unlimited(db):
    """0 is the catalog default and must stay "no auto-complete" -- the operator ends the run."""
    sched = _scheduler(bus=_FakeBus())
    bk = _make_booking(db, settings_json={"MAX_BOOKINGS": 0})
    ident = _make_identity(db)
    _appointment(db, bk, ident)

    assert sched._maybe_complete_booking(db, bk) is False
    assert bk.status == BookingStatus.RUNNING


# ── stop must mean stop (confirmed live 2026-08-14) ──────────────────────────────────────────
def test_stopped_booking_is_never_re_signalled_or_respawned(db, monkeypatch):
    """_latch_signals and _hold_respawn ran for ANY non-terminal booking, outside the RUNNING guard,
    so a STOPPED booking kept being re-signalled every tick. (_hold_respawn had its own internal
    STOPPED guard; _latch_signals had none.) Confirmed live: the operator stopped "Test 3018" and a
    scout process came up 23 seconds later."""
    called = []
    sched = _scheduler(session_local=lambda: db, bus=_FakeBus())
    monkeypatch.setattr(sched, "_latch_signals", lambda *a: called.append("latch"))
    monkeypatch.setattr(sched, "_hold_respawn", lambda *a: called.append("respawn"))
    monkeypatch.setattr(sched, "_reap_dead_workers", lambda *a: None)
    monkeypatch.setattr(sched, "_reap_stalled_workers", lambda *a: None)
    monkeypatch.setattr(sched, "_reap_runaway_workers", lambda *a: None)
    bk = _make_booking(db)
    bk.status = BookingStatus.STOPPED
    db.commit()

    sched._tick_once()

    assert called == [], "a stopped booking was still being driven"


def test_running_booking_is_still_signalled_and_respawned(db, monkeypatch):
    """The guard must not break the feature it guards."""
    called = []
    sched = _scheduler(session_local=lambda: db, bus=_FakeBus())
    monkeypatch.setattr(sched, "_latch_signals", lambda *a: called.append("latch"))
    monkeypatch.setattr(sched, "_hold_respawn", lambda *a: called.append("respawn"))
    for m in ("_reclaim_stale_slot_leases", "_fire_slot_found_if_new", "_assign_slots",
              "_drive_scouts", "_maybe_self_scout", "_reap_dead_workers",
              "_reap_stalled_workers", "_reap_runaway_workers"):
        monkeypatch.setattr(sched, m, lambda *a, **k: None)
    monkeypatch.setattr(sched, "_maybe_inactivate_slot_open", lambda *a: False)
    monkeypatch.setattr(sched, "_maybe_revert_false_slot_found", lambda *a: False)
    monkeypatch.setattr(sched, "_maybe_complete_booking", lambda *a: False)
    _make_booking(db)          # the helper already creates it RUNNING

    sched._tick_once()

    assert set(called) == {"latch", "respawn"}


# ── a retired slot must never be resurrected by lease bookkeeping (confirmed live 2026-08-14) ────
def test_reclaiming_a_stale_lease_never_reopens_a_closed_slot(db):
    """CLOSED means DELIBERATELY retired -- by /start's fresh-run retirement, the false-slot revert,
    or the operator's Clear. Only a genuine rediscovery may reopen it.

    Live: "Test 3018" restarted at 16:41:53 and had slot_open AND slot_found stamped at
    16:41:58.432469 -- the identical microsecond, one tick, and 4.5 MINUTES before any scout pushed
    anything. Leases left open on slots the false-slot revert had CLOSED got reclaimed here, that
    reopened them, and _fire_slot_found_if_new saw an AVAILABLE slot in the same tick. The run
    skipped its whole slot-open discovery phase on slots it had already judged phantom."""
    from libs.domain.enums import SlotLeaseOutcome

    sched = _scheduler(bus=_FakeBus())
    bk = _make_booking(db, settings_json={"SLOT_LEASE_TTL": 1})
    w = _make_worker(db, bk, _make_identity(db))
    closed = M.Slot(booking_id=bk.id, posto_id="5084", date="2026-09-01", period=1,
                    status=SlotStatus.CLOSED)
    db.add(closed); db.commit()
    db.add(M.SlotLease(slot_id=closed.id, worker_id=w.id,
                       leased_at=NOW - timedelta(hours=1)))       # long stale
    db.commit()

    sched._reclaim_stale_slot_leases(db, bk, NOW)

    assert db.get(M.Slot, closed.id).status == SlotStatus.CLOSED, "a retired slot was resurrected"
    lease = db.scalars(select(M.SlotLease)).one()
    assert lease.released_at is not None and lease.outcome == SlotLeaseOutcome.EXPIRED


def test_reclaiming_a_stale_lease_still_reopens_a_live_leased_slot(db):
    """The guard must not break the reclaim it guards: a LEASED slot whose holder went silent is
    exactly what this path exists to free."""
    sched = _scheduler(bus=_FakeBus())
    bk = _make_booking(db, settings_json={"SLOT_LEASE_TTL": 1})
    w = _make_worker(db, bk, _make_identity(db))
    leased = M.Slot(booking_id=bk.id, posto_id="5084", date="2026-09-02", period=2,
                    status=SlotStatus.LEASED)
    db.add(leased); db.commit()
    db.add(M.SlotLease(slot_id=leased.id, worker_id=w.id, leased_at=NOW - timedelta(hours=1)))
    db.commit()

    sched._reclaim_stale_slot_leases(db, bk, NOW)

    assert db.get(M.Slot, leased.id).status == SlotStatus.AVAILABLE


def test_false_slot_revert_releases_the_leases_of_the_slots_it_closes(db):
    """Closing a slot while leaving its lease open leaves a contradiction the next tick resolves the
    wrong way -- an orphan lease on a retired slot."""
    from libs.domain.enums import SlotLeaseOutcome

    sched = _scheduler(bus=_FakeBus(), redis_client=fakeredis.FakeStrictRedis(decode_responses=True))
    found_at = NOW - timedelta(minutes=10)
    bk = _make_booking(db, slot_found_fired_at=found_at,
                       settings_json={"FALSE_SLOT_BOUNCE_LIMIT": 5})
    w = _make_worker(db, bk, _make_identity(db))
    slots = _bounced_pool(db, bk, w, n_bounces=5, since=found_at)
    # an extra slot still actively leased when the verdict lands
    db.add(M.SlotLease(slot_id=slots[0].id, worker_id=w.id, leased_at=NOW))
    slots[0].status = SlotStatus.LEASED
    db.commit()

    assert sched._maybe_revert_false_slot_found(db, bk, NOW) is True

    assert db.get(M.Slot, slots[0].id).status == SlotStatus.CLOSED
    open_leases = db.scalars(select(M.SlotLease).where(
        M.SlotLease.released_at.is_(None))).all()
    assert open_leases == [], "left an orphan lease on a slot it retired"


# ── one worker's failed relaunch must not kill the tick (confirmed live 2026-08-15) ───────────
def test_a_relaunch_failure_does_not_abort_the_whole_tick(db):
    """A relaunch that can't get a proxy is ORDINARY -- the pool is finite and a booking routinely
    wants more workers than there are healthy proxies.

    Live: lease_proxy raised ProxyLeaseError, _do_launch_worker re-raised it as an HTTPException (an
    HTTP-layer error, raised in a background thread where nothing can answer it), and it propagated
    out of _hold_respawn -> _tick_once -> the catch-all. That killed slot assignment, signal
    latching and every watchdog, for EVERY booking, on that tick."""
    r = fakeredis.FakeStrictRedis(decode_responses=True)

    def _boom(db_, w):
        raise RuntimeError("no healthy webshare proxy available after probing 5 candidate(s)")

    sched = _scheduler(redis_client=r, supervisor=_FakeSupervisor(), bus=_FakeBus(),
                       on_relaunch=_boom)
    bk = _make_booking(db)
    _make_worker(db, bk, _make_identity(db), status=WorkerStatus.FAILED)

    sched._hold_respawn(db, bk, NOW)      # must return normally, not raise


def test_every_due_worker_is_dispatched_in_one_batch(db):
    """One unlucky worker must not deny the rest their retry -- now enforced one layer down.

    _hold_respawn used to call on_relaunch once PER worker, inline on the tick, so this test made a
    callback raise for the first worker and checked the other two still got their turn. Since
    2026-08-21 the whole due set is handed over in ONE dispatch that runs on a background thread
    (_begin_fleet_launch -> _launch_worker_fleet), because the inline version ran a real headless
    Chromium proxy probe per candidate and stalled every booking's tick behind it.

    Per-worker isolation did not disappear, it moved: _launch_worker_fleet gives each worker its own
    thread and DB session precisely so one worker's failure cannot abort the others. What
    _hold_respawn still owes is that every eligible worker reaches that dispatch -- which is what
    this now asserts."""
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    batches = []

    sched = _scheduler(redis_client=r, supervisor=_FakeSupervisor(), bus=_FakeBus(),
                       on_relaunch=lambda db_, ws: batches.append([w.id for w in ws]))
    bk = _make_booking(db)
    made = [_make_worker(db, bk, _make_identity(db), status=WorkerStatus.FAILED) for _ in range(3)]

    sched._hold_respawn(db, bk, NOW)

    assert len(batches) == 1, "one dispatch for the whole due set, not one per worker"
    assert sorted(batches[0]) == sorted(w.id for w in made)


def test_a_dispatch_failure_does_not_abort_the_tick(db):
    """The dispatch itself raising must not kill the tick for every other booking."""
    r = fakeredis.FakeStrictRedis(decode_responses=True)

    def _boom(db_, ws):
        raise RuntimeError("thread pool exhausted")

    sched = _scheduler(redis_client=r, supervisor=_FakeSupervisor(), bus=_FakeBus(),
                       on_relaunch=_boom)
    bk = _make_booking(db)
    _make_worker(db, bk, _make_identity(db), status=WorkerStatus.FAILED)

    sched._hold_respawn(db, bk, NOW)   # must not raise


# ── a SCHEDULED start must launch, not just transition (confirmed live 2026-08-19) ────────────
def test_scheduled_start_requeues_stopped_workers_before_launching(db, monkeypatch):
    """THE reported bug. The manual /start requeues STOPPED/HELD/FAILED to READY first (fixed
    2026-08-13), but _sched_on_start selected READY workers directly. A scheduled run is normally
    the SECOND or later run of a booking, so its workers are sitting STOPPED from last time and that
    query returned nothing: warmup fired at 01:10:01 exactly on time, the booking moved to WARMING,
    and ZERO of seven workers launched."""
    import apps.hub.main as main

    launched = {}
    monkeypatch.setattr(main, "_begin_fleet_launch",
                        lambda bid, ids, jitter_secs=0.0: launched.update(ids=list(ids)))
    monkeypatch.setattr(main, "SessionLocal", lambda: db)

    bk = _make_booking(db)
    bk.status = BookingStatus.SCHEDULED
    db.commit()
    stopped = [_make_worker(db, bk, _make_identity(db), status=WorkerStatus.STOPPED)
               for _ in range(3)]
    done = _make_worker(db, bk, _make_identity(db), status=WorkerStatus.COMPLETED)

    main._sched_on_start(db, bk)

    assert set(launched.get("ids", [])) == {w.id for w in stopped}, launched
    assert done.id not in launched.get("ids", []), "a COMPLETED worker is done, not stale"
    assert bk.status == BookingStatus.WARMING


def test_scheduled_start_leaves_a_completed_worker_alone(db, monkeypatch):
    """A worker that already booked must not be dragged back into the next run."""
    import apps.hub.main as main

    launched = {}
    monkeypatch.setattr(main, "_begin_fleet_launch",
                        lambda bid, ids, jitter_secs=0.0: launched.update(ids=list(ids)))
    bk = _make_booking(db)
    bk.status = BookingStatus.SCHEDULED
    db.commit()
    done = _make_worker(db, bk, _make_identity(db), status=WorkerStatus.COMPLETED)

    main._sched_on_start(db, bk)

    assert launched.get("ids", []) == []
    assert done.status == WorkerStatus.COMPLETED


# ── launch-failure retry (live incident 2026-08-20) ──────────────────────────
def _launch_failed_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def test_hold_respawn_retries_a_worker_whose_LAUNCH_failed():
    """A launch that raises before spawn leaves the worker READY -- correct, nothing started -- but
    READY is a status no watchdog looked at, so it was a dead end.

    Confirmed live 2026-08-20: 4 of 9 workers on "Test 3018" failed their proxy lease and sat at
    READY indefinitely while the other 5 ran. Nothing would ever have retried them.
    """
    from apps.hub.scheduler import LAUNCH_FAILED_PREFIX

    r = fakeredis.FakeStrictRedis(decode_responses=True)
    relaunched = []
    sched = _scheduler(redis_client=r, on_relaunch=lambda db, ws: relaunched.extend(x.id for x in ws))

    db = _launch_failed_db()
    bk = _make_booking(db)
    w = _make_worker(db, bk, _make_identity(db), status=WorkerStatus.READY)
    w.substate_detail = f"{LAUNCH_FAILED_PREFIX} no soax proxy leased after 5 probe(s)"
    db.commit()

    sched._hold_respawn(db, bk, NOW)

    assert relaunched == [w.id]


def test_hold_respawn_leaves_a_plain_READY_worker_alone():
    """Scoped to the marker, NOT to READY generally. A plain READY worker is one an operator armed
    but deliberately has not started -- auto-launching those would start a booking nobody ran."""
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    relaunched = []
    sched = _scheduler(redis_client=r, on_relaunch=lambda db, ws: relaunched.extend(x.id for x in ws))

    db = _launch_failed_db()
    bk = _make_booking(db)
    _make_worker(db, bk, _make_identity(db), status=WorkerStatus.READY)

    sched._hold_respawn(db, bk, NOW)

    assert relaunched == []


def test_launch_failure_retry_still_honours_the_respawn_cap():
    """It reuses the same backoff/cap machinery -- a permanently unleasable worker must not spin."""
    from apps.hub.scheduler import LAUNCH_FAILED_PREFIX

    r = fakeredis.FakeStrictRedis(decode_responses=True)
    relaunched = []
    sched = _scheduler(redis_client=r, on_relaunch=lambda db, ws: relaunched.extend(x.id for x in ws))

    db = _launch_failed_db()
    bk = _make_booking(db)
    w = _make_worker(db, bk, _make_identity(db), status=WorkerStatus.READY)
    w.substate_detail = f"{LAUNCH_FAILED_PREFIX} pool exhausted"
    db.commit()

    r.hset(f"respawn:{w.id}", mapping={"attempt": 5, "next_eligible": 0})
    sched._hold_respawn(db, bk, NOW)

    assert relaunched == []


# ── F1: Redis state must not outlive its workers (audit finding 2026-08-20) ──
def test_orphan_cmd_streams_are_swept():
    """delete_worker_stream is called on reap/stop/release, but those are EVENT paths and only clean
    up what they witness. Measured 2026-08-20: 2065 cmd:* streams against 54 worker rows -- 2036
    (99%) orphaned, and Redis here runs maxmemory=0/noeviction so nothing bounds it."""
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    from libs.bus.bus import Bus
    bus = Bus(r)
    sched = _scheduler(redis_client=r, bus=bus)

    db = _launch_failed_db()
    bk = _make_booking(db)
    w = _make_worker(db, bk, _make_identity(db), status=WorkerStatus.RUNNING)

    bus.send_command(w.id, "ping")                 # live worker's stream
    bus.send_command("ghost-worker-1", "ping")     # orphan: no DB row
    bus.send_command("ghost-worker-2", "ping")
    assert len(list(r.scan_iter("cmd:*"))) == 3

    sched._sweep_orphan_streams(db, NOW)

    remaining = {k.split(":", 1)[1] for k in r.scan_iter("cmd:*")}
    assert remaining == {w.id}, f"expected only the live worker's stream, got {remaining}"


def test_the_sweep_is_time_gated_so_it_is_not_a_hot_path():
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    from libs.bus.bus import Bus
    bus = Bus(r)
    sched = _scheduler(redis_client=r, bus=bus)
    db = _launch_failed_db()

    sched._sweep_orphan_streams(db, NOW)           # first call runs and stamps
    bus.send_command("ghost-worker-3", "ping")
    sched._sweep_orphan_streams(db, NOW)           # immediate second call must be skipped
    assert list(r.scan_iter("cmd:*")), "a second sweep inside the window should not have run"


def test_a_sweep_failure_never_kills_the_tick():
    """Housekeeping must not be able to stop scheduling -- the failure mode this file exists about."""
    class _BoomBus:
        class _R:
            def scan_iter(self, *a, **k): raise RuntimeError("redis down")
        r = _R()
        def delete_worker_stream(self, wid): pass

    sched = _scheduler(redis_client=None, bus=_BoomBus())
    db = _launch_failed_db()
    sched._sweep_orphan_streams(db, NOW)           # must not raise


# ── Phase 2: pre-warm before a scheduled slot-open ───────────────────────────
def _prewarm_sched(r, bus):
    return _scheduler(redis_client=r, bus=bus)


def test_prewarm_fires_inside_the_lead_window_only():
    """The portal's form state dies on a ~16.1-minute clock, so each worker sits at a random point
    in its own keepalive cycle when slot-open arrives. slot_open_at is known in advance."""
    import fakeredis
    from libs.bus.bus import Bus
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    sent = []
    bus = Bus(r)
    orig = bus.send_command
    bus.send_command = lambda wid, t, **p: (sent.append((wid, t, p)), orig(wid, t, **p))[1]

    db = _launch_failed_db()
    bk = _make_booking(db)
    bk.slot_open_at = NOW + timedelta(seconds=600)     # well outside a 120s lead
    bk.settings_json = {"PREWARM_BEFORE_SLOT_OPEN": 120.0}
    w = _make_worker(db, bk, _make_identity(db), status=WorkerStatus.RUNNING)
    w.substate = WorkerSubstate.KEEPING
    db.commit()

    sched = _prewarm_sched(r, bus)
    sched._maybe_prewarm(db, bk, NOW)
    assert sent == [], "must not pre-warm 10 minutes early"

    sched._maybe_prewarm(db, bk, NOW + timedelta(seconds=540))   # 60s before -> inside the lead
    assert [t for _, t, _ in sent] == ["signal"]
    assert sent[0][2]["name"] == "prewarm"


def test_prewarm_is_sent_once_per_worker():
    import fakeredis
    from libs.bus.bus import Bus
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    sent = []
    bus = Bus(r)
    orig = bus.send_command
    bus.send_command = lambda wid, t, **p: (sent.append(wid), orig(wid, t, **p))[1]

    db = _launch_failed_db()
    bk = _make_booking(db)
    bk.slot_open_at = NOW + timedelta(seconds=60)
    w = _make_worker(db, bk, _make_identity(db), status=WorkerStatus.RUNNING)
    w.substate = WorkerSubstate.KEEPING
    db.commit()

    sched = _prewarm_sched(r, bus)
    sched._maybe_prewarm(db, bk, NOW)
    sched._maybe_prewarm(db, bk, NOW)      # a tick later, same window
    assert len(sent) == 1, f"pre-warmed {len(sent)} times; every tick would re-warm the fleet"


def test_prewarm_skips_a_booking_whose_slot_open_already_fired():
    import fakeredis
    from libs.bus.bus import Bus
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    sent = []
    bus = Bus(r); orig = bus.send_command
    bus.send_command = lambda wid, t, **p: (sent.append(wid), orig(wid, t, **p))[1]

    db = _launch_failed_db()
    bk = _make_booking(db)
    bk.slot_open_at = NOW + timedelta(seconds=60)
    bk.slot_open_fired_at = NOW
    w = _make_worker(db, bk, _make_identity(db), status=WorkerStatus.RUNNING)
    w.substate = WorkerSubstate.KEEPING
    db.commit()

    _prewarm_sched(r, bus)._maybe_prewarm(db, bk, NOW)
    assert sent == []


def test_prewarm_needs_a_scheduled_time_to_act_on():
    """A manually fired slot-open gives no advance notice -- there is nothing to be early for."""
    import fakeredis
    from libs.bus.bus import Bus
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    sent = []
    bus = Bus(r); orig = bus.send_command
    bus.send_command = lambda wid, t, **p: (sent.append(wid), orig(wid, t, **p))[1]

    db = _launch_failed_db()
    bk = _make_booking(db)
    bk.slot_open_at = None
    w = _make_worker(db, bk, _make_identity(db), status=WorkerStatus.RUNNING)
    w.substate = WorkerSubstate.KEEPING
    db.commit()

    _prewarm_sched(r, bus)._maybe_prewarm(db, bk, NOW)
    assert sent == []


def test_prewarm_can_be_disabled_with_zero():
    import fakeredis
    from libs.bus.bus import Bus
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    sent = []
    bus = Bus(r); orig = bus.send_command
    bus.send_command = lambda wid, t, **p: (sent.append(wid), orig(wid, t, **p))[1]

    db = _launch_failed_db()
    bk = _make_booking(db)
    bk.slot_open_at = NOW + timedelta(seconds=10)
    bk.settings_json = {"PREWARM_BEFORE_SLOT_OPEN": 0.0}
    w = _make_worker(db, bk, _make_identity(db), status=WorkerStatus.RUNNING)
    w.substate = WorkerSubstate.KEEPING
    db.commit()

    _prewarm_sched(r, bus)._maybe_prewarm(db, bk, NOW)
    assert sent == []


# ── a holding worker that cannot book must not be handed a slot ──────────────
def test_assign_skips_a_holding_worker_marked_not_bookable(db):
    """CONFIRMED LIVE 2026-08-21. A keepalive that finds the schedule anchor gone re-warms in place;
    110 of those re-warms failed against 42 that succeeded. The failure was recorded ONLY as a log
    line, so the worker kept reporting a bare `holding` -- the Hub counted it as ready capacity, and
    it bounced the first slot it was handed. It now carries NOT_BOOKABLE_PREFIX on substate_detail
    and is skipped here until a re-warm succeeds and clears it."""
    from libs.domain.enums import NOT_BOOKABLE_PREFIX

    r = fakeredis.FakeStrictRedis(decode_responses=True)
    bus = _FakeBus()
    sched = _scheduler(redis_client=r, bus=bus)
    bk = _make_booking(db)
    bk.status = BookingStatus.RUNNING
    good = _make_worker(db, bk, _make_identity(db), status=WorkerStatus.RUNNING,
                        role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)
    bad = _make_worker(db, bk, _make_identity(db), status=WorkerStatus.RUNNING,
                       role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)
    bad.substate_detail = f"{NOT_BOOKABLE_PREFIX} form-state lapsed, re-warm failed x3"
    db.add(M.Slot(booking_id=bk.id, posto_id="5084", date="2026-08-18", period=4,
                  status=SlotStatus.AVAILABLE))
    db.commit()

    sched._assign_slots(db, bk)

    leased = {l.worker_id for l in db.scalars(select(M.SlotLease)).all()}
    assert good.id in leased, "a healthy holding worker must still be assignable"
    assert bad.id not in leased, "a not-bookable worker must never be handed a slot"


def test_a_healthy_holding_worker_with_a_detail_is_still_assignable(db):
    """The filter keys on the PREFIX, not on 'has any detail' -- an ordinary detail must not
    accidentally bench a perfectly good worker."""
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    sched = _scheduler(redis_client=r, bus=_FakeBus())
    bk = _make_booking(db)
    bk.status = BookingStatus.RUNNING
    w = _make_worker(db, bk, _make_identity(db), status=WorkerStatus.RUNNING,
                     role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)
    w.substate_detail = "re-warm restored the schedule form"
    db.add(M.Slot(booking_id=bk.id, posto_id="5084", date="2026-08-18", period=4,
                  status=SlotStatus.AVAILABLE))
    db.commit()

    sched._assign_slots(db, bk)

    assert {l.worker_id for l in db.scalars(select(M.SlotLease)).all()} == {w.id}


# ── a manual slot-open must not be silently reverted (2026-08-23) ───────────
def test_a_manually_fired_slot_open_is_not_reverted(db):
    """CONFIRMED live: the operator fired slot-open at 22:38:45; the false-slot-open guard cleared
    it at 22:58:45 and logged NOTHING, so it was indistinguishable from the fire never having
    worked. It is self-perpetuating whenever discovery is broken -- "no slots in 20 minutes" is
    always true if nothing can successfully scan."""
    from datetime import timedelta

    from libs.bus.names import manual_open_key

    r = fakeredis.FakeStrictRedis(decode_responses=True)
    sched = _scheduler(redis_client=r, bus=_FakeBus())
    bk = _make_booking(db)
    bk.status = BookingStatus.RUNNING
    bk.slot_open_fired_at = NOW - timedelta(seconds=5000)     # far past the 1200s window
    db.commit()

    r.set(manual_open_key(bk.id), "1")
    assert sched._maybe_inactivate_slot_open(db, bk, NOW) is False
    assert bk.slot_open_fired_at is not None, "an operator's explicit fire must survive"


def test_a_scheduled_slot_open_is_still_reverted(db):
    """The guard still does its job for a SCHEDULED open that turned out to be wrong."""
    from datetime import timedelta

    r = fakeredis.FakeStrictRedis(decode_responses=True)
    sched = _scheduler(redis_client=r, bus=_FakeBus())
    bk = _make_booking(db)
    bk.status = BookingStatus.RUNNING
    bk.slot_open_fired_at = NOW - timedelta(seconds=5000)
    db.commit()

    assert sched._maybe_inactivate_slot_open(db, bk, NOW) is True
    assert bk.slot_open_fired_at is None


# ── Phase 8 (D-NEW-1): static resource health reactivation ──────────────────────────────────────
def _static_proxy(db, *, pid, healthy, active=True, last_used_at=None):
    p = M.Proxy(id=pid, provider="isp", kind="static_res", endpoint=f"{pid}:80",
               credentials_ref="u:p", resource_type="static", active=active,
               is_healthy=healthy, last_used_at=last_used_at, max_sessions=1)
    db.add(p); db.commit()
    return p


def test_reprobe_static_proxies_failing_probe_leaves_it_unhealthy(db):
    p = _static_proxy(db, pid="s1", healthy=False)
    sched = _scheduler()
    calls = []

    def fake_check(url):
        calls.append(url)
        return False, "ERR_CONNECTION_CLOSED"

    sched._reprobe_static_proxies(db, NOW, health_check=fake_check)

    db.refresh(p)
    assert len(calls) == 1
    assert p.is_healthy is False
    assert p.health == "ERR_CONNECTION_CLOSED"


def test_reprobe_static_proxies_successful_probe_reactivates_and_is_then_leasable(db):
    """The hand-off to normal leasing: this test does NOT reimplement _candidate_query's logic --
    it proves the reactivated row becomes selectable by calling the real, unmodified lease_proxy."""
    from libs.proxies import lease_proxy

    _static_proxy(db, pid="s1", healthy=False)
    sched = _scheduler()

    sched._reprobe_static_proxies(db, NOW, health_check=lambda url: (True, "ok"))

    px, _ = lease_proxy(db, "w1", "isp", resource_type="static")
    assert px.id == "s1"
    assert px.is_healthy is True


def test_reprobe_static_proxies_never_touches_rotating_resources(db):
    """Regression guard for D19: a pool of only unhealthy ROTATING rows must produce zero probes and
    zero mutations from this sweep -- rotating health exclusion stays exactly as it was."""
    rot = M.Proxy(id="r1", provider="soax", kind="dc", endpoint="r1:80", credentials_ref="u:p",
                 resource_type="rotating", active=True, is_healthy=False, health="old-verdict")
    db.add(rot); db.commit()
    sched = _scheduler()
    calls = []

    sched._reprobe_static_proxies(db, NOW, health_check=lambda url: (calls.append(url), (True, "ok"))[1])

    db.refresh(rot)
    assert calls == []
    assert rot.is_healthy is False           # untouched
    assert rot.health == "old-verdict"       # untouched


def test_reprobe_static_proxies_is_time_gated():
    db = _launch_failed_db()
    _static_proxy(db, pid="s1", healthy=False)
    sched = _scheduler()
    calls = []
    probe = lambda url: (calls.append(url), (True, "ok"))[1]

    sched._reprobe_static_proxies(db, NOW, health_check=probe)      # first call runs
    assert len(calls) == 1
    sched._reprobe_static_proxies(db, NOW, health_check=probe)      # immediate second call: skipped
    assert len(calls) == 1


def test_reprobe_static_proxies_failure_never_kills_the_tick():
    db = _launch_failed_db()
    sched = _scheduler()

    def boom(url):
        raise RuntimeError("browser crashed")

    _static_proxy(db, pid="s1", healthy=False)
    sched._reprobe_static_proxies(db, NOW, health_check=boom)   # must not raise


# ── Phase 8 (D-NEW-2): F5 orphaned proxy-lease reconciliation ───────────────────────────────────
def test_reconcile_orphaned_leases_recovers_launch_failure_after_lease_worker(db):
    """Phase 7's confirmed path 2: a worker whose launch fails (spawn/command-group/send-command)
    AFTER a successful proxy lease is set FAILED directly by apps/hub/main.py's except block and
    NEVER touches RUNNING -- invisible to _reap_dead_workers/_reap_stalled_workers/
    _reap_runaway_workers, all three of which only ever scan status==RUNNING. This test reproduces
    that exact STATE (not the launch code path itself, which lives in main.py and is out of this
    phase's scope) and proves the new sweep is the first mechanism that can ever recover it."""
    bk = _make_booking(db)
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, status=WorkerStatus.FAILED)   # never was RUNNING
    proxy = M.Proxy(provider="isp", kind="static_res", endpoint="1.2.3.4:80", active=True,
                    is_healthy=True, max_sessions=1)
    db.add(proxy); db.commit()
    lease = M.ProxyLease(proxy_id=proxy.id, worker_id=w.id, leased_at=NOW - timedelta(seconds=400))
    db.add(lease); db.commit()

    sched = _scheduler(bus=_FakeBus())
    sched._reconcile_orphaned_leases(db, NOW)

    db.refresh(lease)
    assert lease.released_at is not None


def test_reconcile_orphaned_leases_recovers_terminal_release_failure_worker(db):
    """Phase 7's confirmed path 1: a worker reaches a normal terminal status (its own `terminal`
    bus event correctly applied `status`), but `_release_on_terminal`'s call to
    release_worker_resources raised and was silently swallowed by the `_step()` wrapper in
    events.py -- the lease is left active with no retry. Reproduces the resulting STATE: a
    COMPLETED worker with a still-active lease."""
    bk = _make_booking(db)
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, status=WorkerStatus.COMPLETED)
    proxy = M.Proxy(provider="isp", kind="static_res", endpoint="5.6.7.8:80", active=True,
                    is_healthy=True, max_sessions=1)
    db.add(proxy); db.commit()
    lease = M.ProxyLease(proxy_id=proxy.id, worker_id=w.id, leased_at=NOW - timedelta(seconds=400))
    db.add(lease); db.commit()

    sched = _scheduler(bus=_FakeBus())
    sched._reconcile_orphaned_leases(db, NOW)

    db.refresh(lease)
    assert lease.released_at is not None


def test_reconcile_orphaned_leases_also_releases_slot_leases(db):
    """release_worker_resources releases proxy AND slot leases in one call -- confirms the sweep
    reuses the real function (with its full cleanup), not a lease-only shortcut."""
    bk = _make_booking(db)
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, status=WorkerStatus.FAILED)
    proxy = M.Proxy(provider="isp", kind="static_res", endpoint="1.2.3.4:80", active=True,
                    is_healthy=True, max_sessions=1)
    db.add(proxy); db.commit()
    db.add(M.ProxyLease(proxy_id=proxy.id, worker_id=w.id, leased_at=NOW - timedelta(seconds=400)))
    slot = M.Slot(booking_id=bk.id, posto_id="5084", date="2026-08-20", period=3,
                  status=SlotStatus.LEASED)
    db.add(slot); db.commit()
    slot_lease = M.SlotLease(slot_id=slot.id, worker_id=w.id, leased_at=NOW - timedelta(seconds=400))
    db.add(slot_lease); db.commit()

    sched = _scheduler(bus=_FakeBus())
    sched._reconcile_orphaned_leases(db, NOW)

    db.refresh(slot_lease)
    db.refresh(slot)
    assert slot_lease.released_at is not None
    assert slot.status == SlotStatus.AVAILABLE


def test_reconcile_orphaned_leases_never_touches_running_workers(db):
    bk = _make_booking(db)
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, status=WorkerStatus.RUNNING)
    proxy = M.Proxy(provider="isp", kind="static_res", endpoint="1.2.3.4:80", active=True,
                    is_healthy=True, max_sessions=1)
    db.add(proxy); db.commit()
    lease = M.ProxyLease(proxy_id=proxy.id, worker_id=w.id, leased_at=NOW - timedelta(hours=10))
    db.add(lease); db.commit()

    sched = _scheduler(bus=_FakeBus())
    sched._reconcile_orphaned_leases(db, NOW)

    db.refresh(lease)
    assert lease.released_at is None


def test_reconcile_orphaned_leases_respects_grace_period_for_in_flight_launches(db):
    """A worker that leased moments ago and hasn't flipped to RUNNING yet (a launch genuinely in
    progress) must not be reconciled -- this is the false-positive the grace period exists to
    prevent."""
    bk = _make_booking(db)
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, status=WorkerStatus.READY)   # about to become RUNNING
    proxy = M.Proxy(provider="isp", kind="static_res", endpoint="1.2.3.4:80", active=True,
                    is_healthy=True, max_sessions=1)
    db.add(proxy); db.commit()
    lease = M.ProxyLease(proxy_id=proxy.id, worker_id=w.id, leased_at=NOW - timedelta(seconds=10))
    db.add(lease); db.commit()

    sched = _scheduler(bus=_FakeBus())
    sched._reconcile_orphaned_leases(db, NOW)

    db.refresh(lease)
    assert lease.released_at is None


def test_reconcile_orphaned_leases_is_idempotent_across_two_runs(db):
    bk = _make_booking(db)
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, status=WorkerStatus.FAILED)
    proxy = M.Proxy(provider="isp", kind="static_res", endpoint="1.2.3.4:80", active=True,
                    is_healthy=True, max_sessions=1)
    db.add(proxy); db.commit()
    lease = M.ProxyLease(proxy_id=proxy.id, worker_id=w.id, leased_at=NOW - timedelta(seconds=400))
    db.add(lease); db.commit()

    sched = _scheduler(bus=_FakeBus())
    sched._reconcile_orphaned_leases(db, NOW)
    db.refresh(lease)
    first_released_at = lease.released_at
    assert first_released_at is not None

    # Force the throttle open again to actually re-run the query (not just skip on the timer).
    sched._last_lease_reconcile = 0.0
    sched._reconcile_orphaned_leases(db, NOW)   # must not raise, must not double-release

    db.refresh(lease)
    assert lease.released_at == first_released_at


def test_reconcile_orphaned_leases_is_time_gated():
    db = _launch_failed_db()
    bk = _make_booking(db)
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, status=WorkerStatus.FAILED)
    proxy = M.Proxy(provider="isp", kind="static_res", endpoint="1.2.3.4:80", active=True,
                    is_healthy=True, max_sessions=1)
    db.add(proxy); db.commit()
    lease = M.ProxyLease(proxy_id=proxy.id, worker_id=w.id, leased_at=NOW - timedelta(seconds=400))
    db.add(lease); db.commit()

    sched = _scheduler(bus=_FakeBus())
    sched._reconcile_orphaned_leases(db, NOW)          # first call runs
    db.refresh(lease)
    assert lease.released_at is not None

    w2 = _make_worker(db, bk, ident, status=WorkerStatus.FAILED)
    lease2 = M.ProxyLease(proxy_id=proxy.id, worker_id=w2.id, leased_at=NOW - timedelta(seconds=400))
    db.add(lease2); db.commit()
    sched._reconcile_orphaned_leases(db, NOW)          # immediate second call: skipped by the throttle
    db.refresh(lease2)
    assert lease2.released_at is None


def test_reconcile_orphaned_leases_failure_never_kills_the_tick(db, monkeypatch):
    sched = _scheduler(bus=_FakeBus())

    def boom(*a, **k):
        raise RuntimeError("db exploded")

    import libs.proxies as proxies_pkg
    monkeypatch.setattr(proxies_pkg, "find_orphaned_worker_ids", boom)
    sched._reconcile_orphaned_leases(db, NOW)   # must not raise


# ── form_stale must not send the same slot straight back (2026-08-27) ────────────────────────
# Measured live on booking "E2E VALIDATION 0827": 14 leases over ONE slot (2026-08-28/period 3)
# while 31 others sat AVAILABLE and freshly seen. _assign_slots ordered date.asc(),period.asc() and
# form_stale releases WITHOUT penalty, so the first candidate came back on every tick, forever.

def _stale_lease(db, slot, worker, when):
    db.add(M.SlotLease(slot_id=slot.id, worker_id=worker.id, leased_at=when,
                       released_at=when, outcome=SlotLeaseOutcome.FORM_STALE))
    db.commit()


def test_a_slot_that_just_went_form_stale_is_offered_last(db):
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db)
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)
    # first by date/period -- the one the old ordering handed back every single time
    first = M.Slot(booking_id=bk.id, posto_id="5084", date="2026-08-28", period=3,
                   status=SlotStatus.AVAILABLE)
    later = M.Slot(booking_id=bk.id, posto_id="5084", date="2026-08-28", period=4,
                   status=SlotStatus.AVAILABLE)
    db.add_all([first, later]); db.commit()
    _stale_lease(db, first, w, NOW - timedelta(seconds=5))

    sched._assign_slots(db, bk)

    lease = db.scalars(select(M.SlotLease).where(
        M.SlotLease.worker_id == w.id, M.SlotLease.released_at.is_(None))).first()
    assert lease is not None, "no assignment was made at all"
    assert lease.slot_id == later.id, (
        "the just-form_stale'd slot was handed straight back -- this is the live "
        "14-leases-on-one-slot loop")


def test_form_stale_ordering_does_not_retire_or_penalise_the_slot(db):
    """Order only. A slot we could not use is not a slot that is gone -- charging it is what
    produced 5082 bounced leases against a pool that was never phantom."""
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db)
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)
    s = M.Slot(booking_id=bk.id, posto_id="5084", date="2026-08-28", period=3,
               status=SlotStatus.AVAILABLE, miss_count=0)
    db.add(s); db.commit()
    _stale_lease(db, s, w, NOW - timedelta(seconds=5))

    sched._assign_slots(db, bk)

    db.refresh(s)
    assert s.miss_count == 0, "form_stale charged the slot a miss"
    assert s.status != SlotStatus.CLOSED, "form_stale retired the slot"


def test_a_single_stale_slot_is_still_offered(db):
    """With only one slot in the pool, it must still be assigned -- back of the queue is still
    in the queue. Otherwise a one-slot pool would deadlock."""
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db)
    ident = _make_identity(db)
    w = _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)
    only = M.Slot(booking_id=bk.id, posto_id="5084", date="2026-08-28", period=3,
                  status=SlotStatus.AVAILABLE)
    db.add(only); db.commit()
    _stale_lease(db, only, w, NOW - timedelta(seconds=5))

    sched._assign_slots(db, bk)

    lease = db.scalars(select(M.SlotLease).where(
        M.SlotLease.worker_id == w.id, M.SlotLease.released_at.is_(None))).first()
    assert lease is not None and lease.slot_id == only.id, "a one-slot pool was never assigned"


# ── a scout that never gets past login is not "alive" (2026-09-01) ───────────────────────────
# MEASURED on booking "Test 0901": scout 28b8fe7f cycled logging_in -> failed 245 times over four
# hours with ZERO slots_found, yet `status == RUNNING` was true at any instant (a worker respawning
# every ~30s is RUNNING roughly half the time). That permanently suppressed the apply-as-scout
# fallback -- the mechanism that exists precisely for when scouts are not working -- and the booking
# sat idle with no path to discover anything.

def test_a_scout_stuck_in_login_does_not_suppress_the_fallback(db):
    from apps.hub.scheduler import _scout_is_actually_scouting
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db)
    ident = _make_identity(db)
    scout = _make_worker(db, bk, ident, role=WorkerRole.SCOUT, substate=WorkerSubstate.LOGGING_IN)
    scout.status = WorkerStatus.RUNNING
    applier = _make_worker(db, bk, _make_identity(db), role=WorkerRole.APPLY,
                           substate=WorkerSubstate.KEEPING)
    db.commit()

    assert _scout_is_actually_scouting(scout) is False, \
        "a scout stuck at the login wall counted as alive -- this is the 245-failure case"

    sched._maybe_self_scout(db, bk, NOW)

    # _FakeBus records (worker_id, type, payload)
    assert any(t == "assign" and p.get("scouts_invalid") for _w, t, p in bus.sent), \
        f"the apply-as-scout fallback never fired for a booking whose only scout cannot log in: {bus.sent}"


def test_a_scout_that_is_past_login_still_suppresses_the_fallback(db):
    """The guard must not become 'always fall back' -- a working scout is still a working scout."""
    from apps.hub.scheduler import _scout_is_actually_scouting
    from libs.domain.enums import WorkerSubstate as S
    ident_bk = _make_booking(db)
    for sub in (S.LOGGED_IN, S.WARMING_UP, S.WARMED, S.KEEPING, S.DISCOVERING, S.POLLING):
        w = _make_worker(db, ident_bk, _make_identity(db), role=WorkerRole.SCOUT, substate=sub)
        w.status = WorkerStatus.RUNNING
        assert _scout_is_actually_scouting(w) is True, f"{sub} should count as scouting"


def test_a_dead_or_failed_scout_never_counts(db):
    from apps.hub.scheduler import _scout_is_actually_scouting
    from libs.domain.enums import WorkerSubstate as S
    bk = _make_booking(db)
    for sub in (S.LOGGING_IN, S.FAILED, S.IDLE):
        w = _make_worker(db, bk, _make_identity(db), role=WorkerRole.SCOUT, substate=sub)
        w.status = WorkerStatus.RUNNING
        assert _scout_is_actually_scouting(w) is False, f"{sub} must not count as scouting"
    w = _make_worker(db, bk, _make_identity(db), role=WorkerRole.SCOUT,
                     substate=WorkerSubstate.DISCOVERING)
    w.status = WorkerStatus.FAILED
    assert _scout_is_actually_scouting(w) is False, "a FAILED worker must never count"


# ── gate 1 was a one-shot latch, not a question about the pool (2026-09-01) ───────────────────
# `if bk.slot_found_fired_at is not None: return` is set the first time ANY slot is ever discovered
# and never cleared. So one phantom slot -- later retired by _maybe_revert_false_slot_found or by
# miss_count -- permanently disabled the apply-as-scout fallback for the rest of the booking. The
# mechanism could not fire in the exact state it exists for, because reaching that state requires
# having had a slot at some point.

def _self_scout_case(db, bus, *, slot_statuses, fired=True):
    sched = _scheduler(bus=bus)
    bk = _make_booking(db)
    if fired:
        bk.slot_found_fired_at = NOW - timedelta(hours=2)
    for i, st in enumerate(slot_statuses):
        db.add(M.Slot(booking_id=bk.id, posto_id="5084", date=f"2026-09-1{i}",
                      period=3, status=st))
    _make_worker(db, bk, _make_identity(db), role=WorkerRole.APPLY,
                 substate=WorkerSubstate.KEEPING)
    db.commit()
    sched._maybe_self_scout(db, bk, NOW)
    return [p for _w, t, p in bus.sent if t == "assign" and p.get("scouts_invalid")]


def test_an_exhausted_pool_re_enables_the_fallback_even_though_slot_found_fired(db):
    """The whole defect: slot_found fired hours ago, every slot has since been retired, no scout is
    working -- and the fallback stayed off because of a latch that only remembers the past."""
    fired = _self_scout_case(db, _FakeBus(),
                             slot_statuses=[SlotStatus.CLOSED, SlotStatus.CLOSED])
    assert fired, "a booking with zero live slots could not re-enable its own discovery fallback"


def test_a_wiped_pool_re_enables_the_fallback(db):
    """_maybe_revert_false_slot_found deletes the slots it declares phantom, leaving zero rows and
    a latch that still says 'already found some'."""
    fired = _self_scout_case(db, _FakeBus(), slot_statuses=[])
    assert fired, "a booking whose phantom pool was wiped was left with no way to discover again"


def test_available_inventory_still_suppresses_the_fallback(db):
    """The gate must not become 'always fall back' -- real slots mean discovery is not the problem."""
    fired = _self_scout_case(db, _FakeBus(), slot_statuses=[SlotStatus.CLOSED, SlotStatus.AVAILABLE])
    assert fired == [], "the fallback fired while the pool still had an applyable slot"


def test_all_leased_is_inventory_not_exhaustion(db):
    """Every slot leased out to a busy worker is the pool at its most productive, not its emptiest.
    Counting only AVAILABLE would fire the fallback on every busy tick."""
    fired = _self_scout_case(db, _FakeBus(), slot_statuses=[SlotStatus.LEASED, SlotStatus.LEASED])
    assert fired == [], "a fully-leased pool was mistaken for an exhausted one"


def test_a_taken_slot_is_not_inventory(db):
    """TAKEN is an exclusive-mode booking already made -- nothing can be applied to it."""
    fired = _self_scout_case(db, _FakeBus(), slot_statuses=[SlotStatus.TAKEN])
    assert fired, "a booked-out slot was counted as applyable inventory"


# ── BLIND_APPLY_MODE: seed-before-start, discovery-free run (operator 2026-09-21) ────────────
def test_blind_mode_fires_slot_open_on_start_without_a_timer(db):
    """The operator's intended flow: seed the grid before starting, and on start the signals fire
    themselves. A blind-mode booking that is WARMING with slot-open NOT yet fired must have it
    fired by the tick immediately -- no slot_open_at timer -- so it reaches RUNNING and slot-found
    can fire off the seeded pool."""
    fired = []
    sched = _scheduler(on_slot_open=lambda db, bk: fired.append(bk.id))
    bk = _make_booking(db, slot_open_at=None, settings_json={"BLIND_APPLY_MODE": True})
    bk.status = BookingStatus.WARMING
    db.commit()
    assert sched._is_blind_mode(db, bk) is True

    sched._SessionLocal = lambda: _SingletonSession(db)
    sched._tick_once()
    assert bk.slot_open_fired_at is not None, "blind mode must fire slot-open on start"
    assert fired == [bk.id]


def test_blind_mode_assigns_and_drives_scouts_but_skips_phantom_reverts(db, monkeypatch):
    """Blind mode SUPPLEMENTS discovery. A RUNNING blind booking assigns the seeded pool AND keeps
    the scout fleet discovering (a booking may carry scouts alongside a seed -- "0921-3059"), and
    still delivers the signals via _latch_signals. It skips ONLY the machinery that would harm a
    seeded pool: _maybe_inactivate_slot_open, _maybe_revert_false_slot_found (both judge a DISCOVERED
    pool phantom) and _maybe_self_scout (turns apply workers into discoverers off the seed)."""
    sched = _scheduler()
    bk = _make_booking(db, slot_open_fired_at=NOW, slot_found_fired_at=NOW,
                       settings_json={"BLIND_APPLY_MODE": True})   # _make_booking defaults RUNNING
    called = {"assign": 0, "scouts": 0, "self_scout": 0, "inactivate": 0, "revert": 0, "latch": 0}
    monkeypatch.setattr(sched, "_assign_slots", lambda *a: called.__setitem__("assign", called["assign"] + 1))
    monkeypatch.setattr(sched, "_drive_scouts", lambda *a, **k: called.__setitem__("scouts", called["scouts"] + 1))
    monkeypatch.setattr(sched, "_maybe_self_scout", lambda *a: called.__setitem__("self_scout", called["self_scout"] + 1))
    monkeypatch.setattr(sched, "_maybe_inactivate_slot_open", lambda *a: called.__setitem__("inactivate", called["inactivate"] + 1) or False)
    monkeypatch.setattr(sched, "_maybe_revert_false_slot_found", lambda *a: called.__setitem__("revert", called["revert"] + 1) or False)
    monkeypatch.setattr(sched, "_latch_signals", lambda *a: called.__setitem__("latch", called["latch"] + 1))
    monkeypatch.setattr(sched, "_hold_respawn", lambda *a: None)
    monkeypatch.setattr(sched, "_reclaim_stale_slot_leases", lambda *a: None)
    monkeypatch.setattr(sched, "_fire_slot_found_if_new", lambda *a: None)
    monkeypatch.setattr(sched, "_maybe_complete_booking", lambda *a: False)
    monkeypatch.setattr(sched, "_settings_timeout_hours", lambda *a: 0)
    sched._SessionLocal = lambda: _SingletonSession(db)

    sched._tick_once()
    assert called["assign"] == 1, "the seeded pool must be assigned"
    # THE regression this locks in: blind mode must still DRIVE SCOUTS. Skipping it left a hybrid
    # booking's scouts idle (confirmed live "0921-3059": 7 scouts warmed, awaiting a discover task
    # that never came). _drive_scouts is a clean no-op without scouts, so pure-blind is unaffected.
    assert called["scouts"] == 1, "blind mode must still drive the scout fleet"
    # ...but NOT the seed-hostile machinery: self-scout (apply workers must work the seed) and the
    # two phantom judgments (which would retire an operator-seeded pool).
    assert called["self_scout"] == 0, "blind mode must not turn apply workers into self-scouts"
    assert called["inactivate"] == 0 and called["revert"] == 0, "no phantom-revert machinery in blind mode"
    # _latch_signals is the only per-tick delivery of slot_open/slot_found to a worker that reached
    # KEEPING after the broadcast; without it they hold a leased slot and block in await_slot_open()
    # forever (confirmed live "0921-3059").
    assert called["latch"] == 1, "blind mode must still deliver the signals via _latch_signals"


class _SingletonSession:
    """Hand the scheduler tick the SAME db session the test built, and no-op its close()."""
    def __init__(self, db): self._db = db
    def __call__(self): return self
    def __getattr__(self, n): return getattr(self._db, n)
    def close(self): pass


# CROSS-BOOKING DISCOVERY FEED tests (cp000, 2026-10-01)
def _utcnow():
    return datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _add_slot(db, bk, date, period, status=SlotStatus.AVAILABLE, source="scout", posto=None):
    db.add(M.Slot(booking_id=bk.id, posto_id=posto or bk.posto_id, date=date, period=period,
                  status=status, source=source))
    db.commit()


def _mk_booking(db, *, posto="5084", status=BookingStatus.RUNNING, feeders=None):
    # _make_booking hardcodes name/type/posto_id/status/settings_json, so set what we vary here
    # via attributes after creation rather than as colliding kwargs.
    bk = _make_booking(db)
    bk.posto_id = posto
    bk.status = status
    if feeders is not None:
        bk.discovery_feeder_booking_ids = feeders
    db.commit()
    return bk


def _detector_and_main(db, *, detector_posto="5084", main_status=BookingStatus.RUNNING):
    detector = _mk_booking(db, posto=detector_posto, status=BookingStatus.RUNNING)
    main = _mk_booking(db, posto="5084", status=main_status, feeders=[detector.id])
    return detector, main


def test_mirror_feeder_injects_matching_posto_and_fires_slot_open(db):
    detector, main = _detector_and_main(db, main_status=BookingStatus.WARMING)
    _add_slot(db, detector, "2026-10-20", 3)
    opened = []
    sch = _scheduler(on_slot_open=lambda db, bk: opened.append(bk.id))

    n = sch._mirror_feeder_slots(db, main, _utcnow())

    assert n == 1
    rows = db.scalars(select(M.Slot).where(M.Slot.booking_id == main.id)).all()
    assert [(r.date, r.period, r.source) for r in rows] == [("2026-10-20", 3, "feeder")]
    assert main.slot_open_fired_at is not None
    assert opened == [main.id]


def test_mirror_feeder_skips_a_different_posto(db):
    detector, main = _detector_and_main(db, detector_posto="9999")
    _add_slot(db, detector, "2026-10-20", 3)

    n = _scheduler()._mirror_feeder_slots(db, main, _utcnow())

    assert n == 0
    assert db.scalars(select(M.Slot).where(M.Slot.booking_id == main.id)).all() == []


def test_mirror_feeder_is_idempotent_and_does_not_re_inject_a_reverted_slot(db):
    detector, main = _detector_and_main(db)
    _add_slot(db, detector, "2026-10-20", 3)
    sch = _scheduler()

    assert sch._mirror_feeder_slots(db, main, _utcnow()) == 1
    assert sch._mirror_feeder_slots(db, main, _utcnow()) == 0

    row = db.scalar(select(M.Slot).where(M.Slot.booking_id == main.id))
    row.status = SlotStatus.CLOSED
    db.commit()
    assert sch._mirror_feeder_slots(db, main, _utcnow()) == 0


def test_mirror_feeder_only_mirrors_available_detector_slots(db):
    detector, main = _detector_and_main(db)
    _add_slot(db, detector, "2026-10-20", 3, status=SlotStatus.AVAILABLE)
    _add_slot(db, detector, "2026-10-21", 3, status=SlotStatus.TAKEN)
    _add_slot(db, detector, "2026-10-22", 3, status=SlotStatus.CLOSED)

    assert _scheduler()._mirror_feeder_slots(db, main, _utcnow()) == 1
    rows = db.scalars(select(M.Slot).where(M.Slot.booking_id == main.id)).all()
    assert [(r.date, r.period) for r in rows] == [("2026-10-20", 3)]


def test_mirror_feeder_excludes_blind_seeded_slots(db):
    """The feed carries REAL discovered slots only. A detector's operator-seeded BLIND grid is
    deliberately mostly-phantom; mirroring it into the precious main booking would hand its apply
    workers a wall of bouncing slots. Discovered (source='scout') feeds; source='blind' does not."""
    detector, main = _detector_and_main(db)
    _add_slot(db, detector, "2026-10-20", 3, source="scout")   # real find -> feeds
    _add_slot(db, detector, "2026-10-21", 4, source="blind")   # phantom seed -> excluded
    _add_slot(db, detector, "2026-10-22", 5, source="blind")

    n = _scheduler()._mirror_feeder_slots(db, main, _utcnow())

    assert n == 1, "only the real discovered slot should feed"
    rows = db.scalars(select(M.Slot).where(M.Slot.booking_id == main.id)).all()
    assert [(r.date, r.period) for r in rows] == [("2026-10-20", 3)]


def test_mirror_feeder_is_a_noop_without_feeders_or_when_inactive(db):
    detector = _mk_booking(db, status=BookingStatus.RUNNING)
    _add_slot(db, detector, "2026-10-20", 3)
    sch = _scheduler()

    plain = _mk_booking(db, status=BookingStatus.RUNNING)
    assert sch._mirror_feeder_slots(db, plain, _utcnow()) == 0
    assert plain.slot_open_fired_at is None

    done = _mk_booking(db, status=BookingStatus.COMPLETED, feeders=[detector.id])
    assert sch._mirror_feeder_slots(db, done, _utcnow()) == 0
    assert db.scalars(select(M.Slot).where(M.Slot.booking_id == done.id)).all() == []


# ── tick fault isolation (2026-10-01): one failure must not abort the whole tick ──
def test_tick_isolates_a_failing_booking_and_still_runs_maintenance(db):
    b1 = _mk_booking(db, status=BookingStatus.RUNNING)
    b2 = _mk_booking(db, status=BookingStatus.RUNNING)
    sch = _scheduler(session_local=lambda: db)
    driven = []
    def fake_drive(d, bk, now):
        if bk.id == b1.id:
            raise RuntimeError("boom in b1")      # one booking blows up
        driven.append(bk.id)
    sch._drive_booking = fake_drive
    ran = []
    for name in ("_reap_dead_workers", "_reap_stalled_workers", "_reap_runaway_workers",
                 "_sweep_orphan_streams", "_reprobe_static_proxies", "_reconcile_orphaned_leases"):
        setattr(sch, name, (lambda n: (lambda d, now: ran.append(n)))(name))
    sch._tick_once()      # must NOT raise
    assert b2.id in driven, "a failing booking must not skip the others' scheduling"
    assert len(ran) == 6, "all maintenance phases must still run after a booking failure"


def test_tick_isolates_a_failing_maintenance_phase_from_the_rest(db):
    _mk_booking(db, status=BookingStatus.RUNNING)
    sch = _scheduler(session_local=lambda: db)
    sch._drive_booking = lambda d, bk, now: None
    ran = []
    sch._reap_dead_workers = lambda d, now: (_ for _ in ()).throw(RuntimeError("reap boom"))
    for name in ("_reap_stalled_workers", "_reap_runaway_workers", "_sweep_orphan_streams",
                 "_reprobe_static_proxies", "_reconcile_orphaned_leases"):
        setattr(sch, name, (lambda n: (lambda d, now: ran.append(n)))(name))
    sch._tick_once()      # must NOT raise
    assert len(ran) == 5, "a failing maintenance phase must not stop the following phases"


# ── blind apply: respect a scheduled slot_open_at (operator 2026-10-16) ───────────────────────────
def test_blind_mode_fires_slot_open_immediately_when_unscheduled(db):
    """Blind apply with NO slot_open_at: the seeded pool needs no discovery, so slot-open fires the
    instant the booking is warming -- apply now."""
    fired = []
    sched = _scheduler(on_slot_open=lambda db, bk: fired.append(bk.id))
    bk = _make_booking(db, settings_json={"BLIND_APPLY_MODE": True})
    bk.status = BookingStatus.WARMING; bk.slot_open_at = None; db.commit()
    sched._drive_booking(db, bk, NOW)
    db.refresh(bk)
    assert bk.slot_open_fired_at is not None and fired == [bk.id]


def test_blind_mode_holds_seeded_pool_until_scheduled_slot_open_at(db):
    """Blind apply WITH a future slot_open_at: hold the seeded pool until the scheduled instant
    instead of hammering it early (which would bounce for hours before the portal opens)."""
    fired = []
    sched = _scheduler(on_slot_open=lambda db, bk: fired.append(bk.id))
    bk = _make_booking(db, settings_json={"BLIND_APPLY_MODE": True})
    bk.status = BookingStatus.WARMING; bk.slot_open_at = NOW + timedelta(hours=4); db.commit()
    # well before the scheduled time: must NOT fire
    sched._drive_booking(db, bk, NOW)
    db.refresh(bk)
    assert bk.slot_open_fired_at is None and fired == [], "blind must hold until scheduled slot_open_at"
    # at/after the scheduled time: the normal timed action fires it
    sched._drive_booking(db, bk, NOW + timedelta(hours=4, seconds=1))
    db.refresh(bk)
    assert bk.slot_open_fired_at is not None and fired == [bk.id]


# ── F48 (run 1023): seeded guesses must not silence the self-scout fallback ──────────────────────
def test_blind_seeded_slots_do_not_suppress_the_self_scout_fallback(db):
    """Run 1023: 50 blind slots kept the pool 'live' while every submit against them came back
    not-bookable, so 120 logged-in appliers never looked for a real slot."""
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _make_booking(db, slot_found_fired_at=NOW)
    ident = _make_identity(db)
    db.add(M.Slot(booking_id=bk.id, posto_id="5084", date="2026-11-09", period=3,
                  status=SlotStatus.AVAILABLE, source="blind"))
    _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)
    db.commit()

    sched._maybe_self_scout(db, bk, NOW)

    assert any(t == "assign" and p.get("scouts_invalid") for _w, t, p in bus.sent)


def test_a_feeder_bookings_scouting_detector_counts_as_a_scout(db):
    """Cross-booking feed: a detector booking that is actually scouting is discovering for us."""
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    det = _make_booking(db)
    ident = _make_identity(db)
    _make_worker(db, det, ident, role=WorkerRole.SCOUT, substate=WorkerSubstate.KEEPING)
    bk = _make_booking(db, discovery_feeder_booking_ids=[det.id])
    _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)

    sched._maybe_self_scout(db, bk, NOW)

    assert bus.sent == []


def test_a_feeder_detector_stuck_at_the_login_wall_does_not(db):
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    det = _make_booking(db)
    ident = _make_identity(db)
    _make_worker(db, det, ident, role=WorkerRole.SCOUT, substate=WorkerSubstate.LOGGING_IN)
    bk = _make_booking(db, discovery_feeder_booking_ids=[det.id])
    _make_worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)

    sched._maybe_self_scout(db, bk, NOW)

    assert any(t == "assign" and p.get("scouts_invalid") for _w, t, p in bus.sent)

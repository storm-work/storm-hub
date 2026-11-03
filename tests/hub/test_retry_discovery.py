"""Retry discovery/apply (2026-11-03): un-stick a fleet parked in KEEPING. Covers the scheduler core
(retry_discovery + its helpers), the scheduled auto-retry tick (_maybe_retry_slot_open), the
inactivation-arming, and the per-worker strip (retry_worker). Same in-memory SQLite + fakeredis +
stub-bus harness as test_scheduler_resilience.py."""
import time
from datetime import datetime, timedelta, timezone

import fakeredis
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.hub.scheduler import Scheduler
from libs.bus.names import manual_open_key, notified_key
from libs.db import metadata
from libs.db import models as M
from libs.domain.enums import (
    BookingStatus,
    SlotStatus,
    WorkerRole,
    WorkerStatus,
    WorkerSubstate,
)

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


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

    def send_command(self, worker_id, type, **payload):
        self.sent.append((worker_id, type, payload))


def _scheduler(**kwargs):
    defaults = dict(
        session_local=lambda: None, on_start=lambda db, bk: None,
        on_slot_open=lambda db, bk: None, on_assign=lambda db, w, s: None,
    )
    defaults.update(kwargs)
    return Scheduler(**defaults)


def _booking(db, *, status=BookingStatus.WARMING, fired=False, found=False, **settings):
    bk = M.Booking(name="b", type="real", posto_id="5084", status=status, settings_json=settings)
    db.add(bk)
    db.commit()
    if fired:
        bk.slot_open_fired_at = NOW - timedelta(minutes=1)
    if found:
        bk.slot_found_fired_at = NOW - timedelta(minutes=1)
    db.commit()
    return bk


def _ident(db):
    i = M.Identity(full_name="P", type="real")
    db.add(i)
    db.commit()
    return i


def _worker(db, bk, ident, *, role=WorkerRole.APPLY, status=WorkerStatus.RUNNING, substate=None):
    w = M.Worker(booking_id=bk.id, identity_id=ident.id, role=role, status=status, substate=substate)
    db.add(w)
    db.commit()
    return w


def _slot(db, bk, *, status=SlotStatus.AVAILABLE, source="scout", date="2026-12-01", period=1,
          miss=0):
    s = M.Slot(booking_id=bk.id, posto_id=bk.posto_id, date=date, period=period,
               status=status, source=source, miss_count=miss)
    db.add(s)
    db.commit()
    return s


# ── retry_discovery core ───────────────────────────────────────────────────────
def test_retry_discovery_refires_slot_open_when_inactivated(db):
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    flips = []
    sched = _scheduler(bus=_FakeBus(), redis_client=r,
                       on_slot_open=lambda db, bk: flips.append(bk.id))
    bk = _booking(db)                                   # WARMING, slot_open NOT fired (inactivated)
    r.sadd(notified_key(bk.id, "slot_open"), "w1")      # a stale latch entry

    counts = sched.retry_discovery(db, bk, NOW)

    assert counts["refired"] is True
    assert flips == [bk.id]                             # _on_slot_open was invoked
    db.refresh(bk)
    assert bk.slot_open_fired_at is not None
    assert not r.exists(notified_key(bk.id, "slot_open"))   # latch cleared so _latch_signals re-sends


def test_retry_discovery_running_booking_is_not_refired(db):
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    flips = []
    sched = _scheduler(bus=_FakeBus(), redis_client=r,
                       on_slot_open=lambda db, bk: flips.append(bk.id))
    bk = _booking(db, status=BookingStatus.RUNNING, fired=True)
    before = bk.slot_open_fired_at
    r.sadd(notified_key(bk.id, "slot_found"), "w1")

    counts = sched.retry_discovery(db, bk, NOW)

    assert counts["refired"] is False
    assert flips == []
    db.refresh(bk)
    assert bk.slot_open_fired_at is not None             # unchanged (not re-fired)
    assert not r.exists(notified_key(bk.id, "slot_found"))


# ── _reactivate_retired_slots ───────────────────────────────────────────────────
def test_reactivate_only_blind_closed_never_taken_or_scout(db):
    sched = _scheduler()
    bk = _booking(db, status=BookingStatus.RUNNING, fired=True)
    b_closed = _slot(db, bk, status=SlotStatus.CLOSED, source="blind", period=1, miss=5)
    b_taken = _slot(db, bk, status=SlotStatus.TAKEN, source="blind", period=3)       # booked/taken
    scout_closed = _slot(db, bk, status=SlotStatus.CLOSED, source="scout", period=5)

    n = sched._reactivate_retired_slots(db, bk)
    db.commit()                                          # retry_discovery commits; the helper doesn't

    assert n == 1
    db.refresh(b_closed)
    assert b_closed.status == SlotStatus.AVAILABLE and b_closed.miss_count == 0
    for s in (b_taken, scout_closed):
        db.refresh(s)
        assert s.status != SlotStatus.AVAILABLE          # untouched


# ── _nudge_apply_self_scout ─────────────────────────────────────────────────────
def test_nudge_sends_self_scout_to_keeping_apply_when_pool_empty(db):
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _booking(db, status=BookingStatus.RUNNING, fired=True)
    ident = _ident(db)
    keep = _worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)
    _worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.APPLYING)   # not KEEPING
    _worker(db, bk, ident, role=WorkerRole.SCOUT, substate=WorkerSubstate.KEEPING)    # not APPLY

    n = sched._nudge_apply_self_scout(db, bk)

    assert n == 1
    assert (keep.id, "assign", {"slot": None, "scouts_invalid": True}) in bus.sent


def test_nudge_suppressed_when_live_nonblind_inventory_exists(db):
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _booking(db, status=BookingStatus.RUNNING, fired=True)
    ident = _ident(db)
    _worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)
    _slot(db, bk, status=SlotStatus.AVAILABLE, source="scout")      # live inventory

    assert sched._nudge_apply_self_scout(db, bk) == 0
    assert bus.sent == []


def test_nudge_ignores_blind_inventory(db):
    bus = _FakeBus()
    sched = _scheduler(bus=bus)
    bk = _booking(db, status=BookingStatus.RUNNING, fired=True)
    ident = _ident(db)
    _worker(db, bk, ident, role=WorkerRole.APPLY, substate=WorkerSubstate.KEEPING)
    _slot(db, bk, status=SlotStatus.AVAILABLE, source="blind")      # blind is NOT inventory here

    assert sched._nudge_apply_self_scout(db, bk) == 1


# ── _maybe_retry_slot_open (scheduled) ──────────────────────────────────────────
def test_scheduled_retry_off_by_default(db):
    sched = _scheduler(bus=_FakeBus(), redis_client=fakeredis.FakeStrictRedis(decode_responses=True))
    bk = _booking(db)                                   # no SLOT_OPEN_RETRY_EVERY_S
    sched._retry_state[bk.id] = {"count": 0, "last": 0.0, "armed": True}
    sched._maybe_retry_slot_open(db, bk, NOW)
    db.refresh(bk)
    assert bk.slot_open_fired_at is None                 # nothing fired


def test_scheduled_retry_requires_arming(db):
    sched = _scheduler(bus=_FakeBus(), redis_client=fakeredis.FakeStrictRedis(decode_responses=True))
    bk = _booking(db, SLOT_OPEN_RETRY_EVERY_S=60.0)      # on, but NOT armed (no inactivation)
    sched._maybe_retry_slot_open(db, bk, NOW)
    db.refresh(bk)
    assert bk.slot_open_fired_at is None


def test_scheduled_retry_fires_when_armed_and_respects_max(db):
    flips = []
    sched = _scheduler(bus=_FakeBus(), redis_client=fakeredis.FakeStrictRedis(decode_responses=True),
                       on_slot_open=lambda db, bk: flips.append(1))
    bk = _booking(db, SLOT_OPEN_RETRY_EVERY_S=60.0, SLOT_OPEN_RETRY_MAX=1)
    sched._retry_state[bk.id] = {"count": 0, "last": 0.0, "armed": True}

    sched._maybe_retry_slot_open(db, bk, NOW)           # first retry fires
    db.refresh(bk)
    assert bk.slot_open_fired_at is not None
    assert sched._retry_state[bk.id] == {"count": 1, "last": pytest.approx(sched._retry_state[bk.id]["last"]), "armed": False}

    # arm again (as a second inactivation would) but MAX=1 is reached -> no second fire
    bk.slot_open_fired_at = None
    db.commit()
    sched._retry_state[bk.id]["armed"] = True
    sched._maybe_retry_slot_open(db, bk, NOW)
    db.refresh(bk)
    assert bk.slot_open_fired_at is None
    assert len(flips) == 1


def test_scheduled_retry_noop_while_slot_found(db):
    sched = _scheduler(bus=_FakeBus(), redis_client=fakeredis.FakeStrictRedis(decode_responses=True))
    bk = _booking(db, SLOT_OPEN_RETRY_EVERY_S=60.0, found=True)
    sched._retry_state[bk.id] = {"count": 0, "last": 0.0, "armed": True}
    sched._maybe_retry_slot_open(db, bk, NOW)
    db.refresh(bk)
    assert bk.slot_open_fired_at is None


# ── inactivation arms the retry ─────────────────────────────────────────────────
def test_inactivation_arms_retry_state(db):
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    sched = _scheduler(bus=_FakeBus(), redis_client=r)
    bk = _booking(db, status=BookingStatus.RUNNING, SLOT_OPEN_INACTIVATE_AFTER=1.0)
    bk.slot_open_fired_at = NOW - timedelta(seconds=10)     # fired long enough ago, nothing found
    db.commit()

    reverted = sched._maybe_inactivate_slot_open(db, bk, NOW)

    assert reverted is True
    db.refresh(bk)
    assert bk.status == BookingStatus.WARMING
    assert sched._retry_state[bk.id]["armed"] is True


# ── retry_worker (per-worker strip) ─────────────────────────────────────────────
def test_retry_worker_strips_from_notified_sets(db):
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    sched = _scheduler(bus=_FakeBus(), redis_client=r)
    bk = _booking(db, status=BookingStatus.RUNNING, fired=True)
    ident = _ident(db)
    w = _worker(db, bk, ident)
    r.sadd(notified_key(bk.id, "slot_open"), w.id)

    assert sched.retry_worker(db, w) is True
    assert not r.sismember(notified_key(bk.id, "slot_open"), w.id)
    # nothing left to strip -> False
    assert sched.retry_worker(db, w) is False

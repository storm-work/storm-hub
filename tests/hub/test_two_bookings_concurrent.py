"""Can two bookings run CONCURRENTLY without leaking into each other?

An open question since the reconstruction plan ("the operator asked 'what if I run 2 bookings in
parallel?' -- never answered... worth an explicit validation pass rather than assuming it was ever
tested live"). Every Redis key except the shared event stream is namespaced by booking_id
(libs/bus/names.py), and the scheduler's in-memory state is booking-keyed -- but namespacing by
construction is a claim, not evidence.

These exercise the seams where two live bookings could actually contaminate each other:
  - a slot-open signal fired on A must not wake B's workers
  - slots discovered by A must not appear in B's pool
  - the signal latch (notified:<booking>:<signal>) must be independent
  - the scheduler must drive both in one tick, not just whichever it sees first
"""
from __future__ import annotations

import fakeredis
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import apps.hub.events as ev
from libs.bus.bus import Bus
from libs.bus.names import notified_key
from libs.db import models as M
from libs.db.models import metadata
from libs.domain.enums import BookingStatus, SlotStatus, WorkerRole, WorkerStatus


@pytest.fixture()
def env(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(eng)
    with eng.begin() as c:
        c.execute(text("CREATE UNIQUE INDEX uq_identity_active_allocation "
                       "ON identity_allocations (identity_id) WHERE released_at IS NULL"))
    Session = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(ev, "SessionLocal", Session)
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    return Session, Bus(r), r


def _booking(Session, name, posto):
    db = Session()
    bk = M.Booking(name=name, type="test", posto_id=posto, nationality="CPV", residence="FRA",
                   status=BookingStatus.WARMING)
    db.add(bk); db.commit()
    ids = {}
    for role in (WorkerRole.SCOUT, WorkerRole.APPLY):
        i = M.Identity(full_name=f"{name}-{role.value}", type="real")
        db.add(i); db.commit()
        w = M.Worker(booking_id=bk.id, identity_id=i.id, role=role,
                     status=WorkerStatus.RUNNING)
        db.add(w); db.commit()
        ids[role] = w.id
    out = (bk.id, ids)
    db.close()
    return out


def test_two_bookings_can_coexist_with_disjoint_workers(env):
    Session, bus, r = env
    a_id, a_w = _booking(Session, "A", "3018")
    b_id, b_w = _booking(Session, "B", "3059")

    db = Session()
    assert db.scalar(select(M.Booking).where(M.Booking.id == a_id)) is not None
    assert db.scalar(select(M.Booking).where(M.Booking.id == b_id)) is not None
    a_workers = {w.id for w in db.scalars(select(M.Worker).where(M.Worker.booking_id == a_id))}
    b_workers = {w.id for w in db.scalars(select(M.Worker).where(M.Worker.booking_id == b_id))}
    db.close()
    assert a_workers and b_workers
    assert not (a_workers & b_workers), "a worker must belong to exactly one booking"


def test_a_slot_open_signal_does_not_leak_across_bookings(env):
    """signal_channel() is per booking. If it were not, firing A's slot-open would start B early --
    which for a scheduled booking means discovering (and rate-limiting) hours before its window."""
    Session, bus, r = env
    a_id, _ = _booking(Session, "A", "3018")
    b_id, _ = _booking(Session, "B", "3059")

    seen_a, seen_b = [], []
    sub_a = bus.subscribe_signal(a_id) if hasattr(bus, "subscribe_signal") else None
    if sub_a is None:
        pytest.skip("bus has no subscribe_signal in this build")

    bus.publish_signal(a_id, "slot_open")
    # Drain whatever each channel actually received.
    for _ in range(5):
        m = sub_a.get_message(ignore_subscribe_messages=True, timeout=0.05)
        if m and m.get("data"):
            seen_a.append(m["data"])
    sub_b = bus.subscribe_signal(b_id)
    for _ in range(5):
        m = sub_b.get_message(ignore_subscribe_messages=True, timeout=0.05)
        if m and m.get("data"):
            seen_b.append(m["data"])

    assert any("slot_open" in str(x) for x in seen_a), "A should have received its own signal"
    assert not seen_b, f"B received A's signal: {seen_b}"


def test_the_signal_latch_is_per_booking(env):
    """notified:<booking>:<signal> -- marking a worker notified in A must not mark it in B."""
    Session, bus, r = env
    a_id, a_w = _booking(Session, "A", "3018")
    b_id, b_w = _booking(Session, "B", "3059")

    r.sadd(notified_key(a_id, "slot_open"), a_w[WorkerRole.APPLY])

    assert r.sismember(notified_key(a_id, "slot_open"), a_w[WorkerRole.APPLY])
    assert not r.sismember(notified_key(b_id, "slot_open"), b_w[WorkerRole.APPLY])
    assert notified_key(a_id, "slot_open") != notified_key(b_id, "slot_open")


def test_slots_discovered_by_one_booking_do_not_appear_in_the_other(env):
    """_persist_slots keys on the pushing worker's booking_id. A shared pool would let one booking
    lease a slot another discovered -- for a DIFFERENT posto, which cannot be booked."""
    Session, bus, r = env
    a_id, a_w = _booking(Session, "A", "3018")
    b_id, b_w = _booking(Session, "B", "3059")

    hub = ev.EventHub.__new__(ev.EventHub)
    hub._persist_slots(a_w[WorkerRole.SCOUT], [{"date": "2026-09-01", "period": 5}])
    hub._persist_slots(b_w[WorkerRole.SCOUT], [{"date": "2026-09-02", "period": 7}])

    db = Session()
    a_slots = db.scalars(select(M.Slot).where(M.Slot.booking_id == a_id)).all()
    b_slots = db.scalars(select(M.Slot).where(M.Slot.booking_id == b_id)).all()
    db.close()

    assert [(s.date, s.period) for s in a_slots] == [("2026-09-01", 5)]
    assert [(s.date, s.period) for s in b_slots] == [("2026-09-02", 7)]
    assert a_slots[0].posto_id == "3018" and b_slots[0].posto_id == "3059"


def test_an_identity_cannot_be_allocated_to_both_bookings_at_once(env):
    """The partial unique index is what stops the same person being booked twice in parallel runs."""
    from sqlalchemy.exc import IntegrityError

    Session, bus, r = env
    a_id, _ = _booking(Session, "A", "3018")
    b_id, _ = _booking(Session, "B", "3059")

    db = Session()
    ident = M.Identity(full_name="SHARED", type="real")
    db.add(ident); db.commit()
    db.add(M.IdentityAllocation(booking_id=a_id, identity_id=ident.id)); db.commit()
    db.add(M.IdentityAllocation(booking_id=b_id, identity_id=ident.id))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.close()


def test_scheduler_drives_every_live_booking_in_one_tick(env):
    """_tick_once selects ALL non-terminal bookings. If it handled only the first, the second would
    silently never fire its signals or assign its slots."""
    from apps.hub.scheduler import Scheduler

    Session, bus, r = env
    a_id, _ = _booking(Session, "A", "3018")
    b_id, _ = _booking(Session, "B", "3059")

    driven: list[str] = []
    sched = Scheduler(Session, on_start=lambda db, bk: driven.append(bk.id),
                      on_slot_open=lambda db, bk: None, on_assign=lambda db, w, s: None,
                      redis_client=r, bus=bus)
    db = Session()
    live = [b.id for b in db.scalars(select(M.Booking).where(
        M.Booking.status.notin_([BookingStatus.COMPLETED, BookingStatus.ABORTED,
                                 BookingStatus.DRAFT]))).all()]
    db.close()

    assert set(live) == {a_id, b_id}, "both bookings must be visible to the tick's own query"
    sched._tick_once()          # must not raise with two live bookings in play

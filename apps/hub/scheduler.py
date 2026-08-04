"""Auto-ops scheduler (spec §7 auto-ops).

A background thread ticks every few seconds and drives scheduled bookings with no operator input:
  - at warmup_start_at  -> START (launch all ready workers), booking -> WARMING
  - at slot_open_at      -> fire SLOT-OPEN signal, booking -> RUNNING
  - continuously          -> assign AVAILABLE pooled slots to HOLDING apply workers (slot-found routing)

The decision (`plan_booking_actions`) and the assignment (`plan_slot_assignments`) are PURE functions so
they unit-test without a DB/clock/bus. The thread just applies what they return.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select

from libs.db import models as M
from libs.domain.enums import BookingStatus, SlotStatus, WorkerRole, WorkerStatus, WorkerSubstate


def plan_booking_actions(status: str, warmup_start_at, slot_open_at, now: datetime) -> list[str]:
    """Pure: which timed transitions are due for a booking. Returns subset of {'start','slot_open'}."""
    actions: list[str] = []
    if status == BookingStatus.SCHEDULED and warmup_start_at and now >= warmup_start_at:
        actions.append("start")
    if status in (BookingStatus.WARMING, BookingStatus.ARMED) and slot_open_at and now >= slot_open_at:
        actions.append("slot_open")
    return actions


def plan_slot_assignments(available_slot_ids: list[str], holding_apply_worker_ids: list[str]) -> list[tuple]:
    """Pure: pair AVAILABLE pooled slots with HOLDING apply workers, 1:1, in order.
    Returns [(worker_id, slot_id), ...]. (Multi-capacity broadcast is a later refinement.)"""
    return list(zip(holding_apply_worker_ids, available_slot_ids))


class Scheduler:
    def __init__(self, session_local, *, on_start, on_slot_open, on_assign, tick: float = 5.0):
        self._SessionLocal = session_local
        self._on_start = on_start          # (db, booking) -> None
        self._on_slot_open = on_slot_open  # (db, booking) -> None
        self._on_assign = on_assign        # (db, worker, slot) -> None
        self._tick = tick
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

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
                pass
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
                        self._on_start(db, bk)
                    elif action == "slot_open":
                        self._on_slot_open(db, bk)
                # slot-found routing: assign available pooled slots -> holding apply workers
                if bk.status == BookingStatus.RUNNING:
                    self._assign_slots(db, bk)
        finally:
            db.close()

    def _assign_slots(self, db, bk: M.Booking) -> None:
        avail = db.scalars(select(M.Slot).where(
            M.Slot.booking_id == bk.id, M.Slot.status == SlotStatus.AVAILABLE)).all()
        if not avail:
            return
        holding = db.scalars(select(M.Worker).where(
            M.Worker.booking_id == bk.id, M.Worker.role == WorkerRole.APPLY,
            M.Worker.status == WorkerStatus.RUNNING,
            M.Worker.substate == WorkerSubstate.HOLDING)).all()
        for wid, sid in plan_slot_assignments([s.id for s in avail], [w.id for w in holding]):
            worker = db.get(M.Worker, wid)
            slot = db.get(M.Slot, sid)
            slot.status = SlotStatus.LEASED
            db.add(M.SlotLease(slot_id=sid, worker_id=wid, leased_at=datetime.now(timezone.utc)))
            db.commit()
            self._on_assign(db, worker, slot)

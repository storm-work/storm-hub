"""Event consumer + WebSocket broadcaster.

A background thread tails the shared `evt` Redis stream. For each worker event it (1) updates the
worker's status/substate in the DB and (2) fans the event out to every connected dashboard WebSocket.
The status mapping is a pure function so it's unit-tested without Redis or a DB.
"""
from __future__ import annotations

import asyncio
import threading

from libs.bus import Bus, connect
from libs.domain.enums import WorkerStatus, WorkerSubstate

from .config import settings
from .db import SessionLocal
from libs.db import models as M

# outcome -> terminal worker status
_TERMINAL_STATUS = {
    "booked": WorkerStatus.COMPLETED,
    "discovered": WorkerStatus.COMPLETED,
    "dry_run": WorkerStatus.COMPLETED,
    "warmed": WorkerStatus.COMPLETED,
    "stopped": WorkerStatus.STOPPED,
    "failed": WorkerStatus.FAILED,
}


def map_event_to_update(event_type: str, payload: dict) -> dict:
    """Return the Worker fields to update for an event (empty dict = no DB change). Pure."""
    if event_type == "started":
        return {"status": WorkerStatus.RUNNING, "substate": WorkerSubstate.IDLE}
    if event_type == "status_change":
        sub = payload.get("substate")
        upd: dict = {}
        if sub:
            upd["substate"] = sub
        if payload.get("detail"):
            upd["substate_detail"] = payload["detail"]
        return upd
    if event_type == "terminal":
        outcome = payload.get("outcome", "")
        upd = {"status": _TERMINAL_STATUS.get(outcome, WorkerStatus.FAILED)}
        if payload.get("substate"):
            upd["substate"] = payload["substate"]
        if payload.get("detail"):
            upd["substate_detail"] = payload["detail"]
        return upd
    return {}


class EventHub:
    def __init__(self, redis_url: str | None = None):
        self._redis_url = redis_url or settings.redis_url
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ── WS subscriptions ──────────────────────────────────────────────────────
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def _broadcast(self, item: dict) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                pass

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def start(self) -> None:
        self._loop = asyncio.get_event_loop()
        self._thread = threading.Thread(target=self._consume, name="event-consumer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _apply_db(self, worker_id: str, upd: dict) -> None:
        if not upd:
            return
        db = SessionLocal()
        try:
            w = db.get(M.Worker, worker_id)
            if w:
                for k, v in upd.items():
                    setattr(w, k, v)
                db.commit()
        finally:
            db.close()

    def _persist_slots(self, worker_id: str, slots: list) -> None:
        """A scout pushed slots -> upsert into the booking's DB pool (scheduler then assigns them)."""
        from sqlalchemy import select
        from libs.domain.enums import SlotStatus
        db = SessionLocal()
        try:
            w = db.get(M.Worker, worker_id)
            if not w:
                return
            bk = db.get(M.Booking, w.booking_id)
            for s in slots or []:
                date, period = s.get("date"), s.get("period")
                if date is None or period is None:
                    continue
                exists = db.scalar(select(M.Slot).where(
                    M.Slot.booking_id == w.booking_id, M.Slot.date == str(date),
                    M.Slot.period == int(period)))
                if exists:
                    continue
                db.add(M.Slot(booking_id=w.booking_id, posto_id=bk.posto_id if bk else "",
                              date=str(date), period=int(period), status=SlotStatus.AVAILABLE,
                              discovered_by=worker_id))
            db.commit()
        finally:
            db.close()

    def _consume(self) -> None:
        bus = Bus(connect(self._redis_url))
        last_id = "$"   # only new events from Hub start
        while not self._stop.is_set():
            try:
                msgs = bus.read_events(last_id=last_id, block_ms=1000, count=200)
            except Exception:
                continue
            for m in msgs:
                last_id = m.id
                wid = m.payload.get("worker_id")
                if wid:
                    self._apply_db(wid, map_event_to_update(m.type, m.payload))
                    if m.type == "slots_found":
                        self._persist_slots(wid, m.payload.get("slots", []))
                item = {"id": m.id, "type": m.type, "ts": m.ts, **m.payload}
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(self._broadcast, item)

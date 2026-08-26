"""End-to-end SIM of the real booking flow: long hold -> discovery on slot-open -> applying.

Operator request 2026-08-14: "we need to sim real booking flow and then the apply flow still works
fine in that situation. long hold, discovery on slot-open, then applying."

What makes this different from tests/engine/test_config_and_phases.py: that drives PhaseMachine
against a stub signals object, with no bus and no Hub. Every bug this project actually hit today
lived in a SEAM -- a payload key dropped between Hub and worker, a signal acked and discarded, a
lease left open on a closed slot, a status update swallowed by an unrelated handler. So this test
wires the REAL components together and only fakes the two things that must not be real:

  real: PhaseMachine (scout + apply), BusSignals, Bus over fakeredis, Scheduler, SQLite DB,
        the slot/lease/appointment persistence in apps.hub.events
  fake: the engine itself (SimEngineOps -- no portal, no browser) and the clock for the long hold

The workers run as THREADS rather than subprocesses: the seam under test is the bus/DB one, and a
subprocess fleet makes the test slow and flaky without exercising anything more of it.
"""
from __future__ import annotations

import json
import threading
import time

import fakeredis
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.hub.scheduler import Scheduler
from apps.worker.bus_signals import BusSignals
from apps.worker.ops_sim import SimEngineOps
from apps.worker.runtime import WorkerRuntime
from libs.bus import Bus
from libs.db import metadata
from libs.db import models as M
from libs.domain.enums import (
    BookingStatus,
    SettingsLayer as L,
    SlotLeaseOutcome,
    SlotStatus,
    WorkerRole,
    WorkerStatus,
    WorkerSubstate,
)
from libs.domain.settings import resolve
from libs.engine.config import EngineConfig

POSTO = "3018"
DATES = ["2026-09-08", "2026-09-09"]


@pytest.fixture()
def env(monkeypatch):
    """Real bus + real DB + the Hub's real event consumer, all in-process."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(eng)
    Session = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)

    import apps.hub.events as ev
    monkeypatch.setattr(ev, "SessionLocal", Session)

    r = fakeredis.FakeStrictRedis(decode_responses=True)
    bus = Bus(r)
    fleets: list = []
    yield Session, bus, r, fleets
    for f in fleets:
        f.shutdown()


class _Fleet:
    """Runs each worker's PhaseMachine on a thread, exactly as WorkerRuntime does in the real
    process, and pumps their events into the Hub's persistence the way EventHub._consume does."""

    def __init__(self, Session, bus, r):
        self.Session, self.bus, self.r = Session, bus, r
        self.threads: list[threading.Thread] = []
        self.workers: list[BusSignals] = []
        self.results: dict[str, str] = {}

    def start(self, worker_id: str, cfg: EngineConfig, ops_kwargs: dict) -> SimEngineOps:
        signals = BusSignals(self.bus, worker_id, consumer=f"c-{worker_id[:6]}")
        self.workers.append(signals)
        rt = WorkerRuntime(self.bus, worker_id)
        ops = SimEngineOps(signals=signals, emit=rt._emit, **ops_kwargs)

        def _run():
            try:
                self.results[worker_id] = rt.run(cfg, ops).outcome
            except Exception as e:                      # surfaced by the assertions below
                self.results[worker_id] = f"EXC:{e}"

        t = threading.Thread(target=_run, name=f"w-{worker_id[:6]}", daemon=True)
        t.start()
        self.threads.append(t)
        return ops

    def shutdown(self, timeout: float = 3.0) -> None:
        """Stop every worker thread and wait for it. Without this the phase machines keep looping
        on the bus for the rest of the pytest session -- daemon threads don't die when a test ends,
        and a fleet of them spinning behind every later test is both a big slowdown and a source of
        cross-test interference."""
        # Send the REAL stop command the Hub sends, not just the in-process flag -- that is the
        # path a stopped worker actually takes, so the teardown exercises it too.
        for w in self.workers:
            try:
                self.bus.send_command(w.wid, "stop")
            except Exception:
                pass
            w.stopped = True
        for t in self.threads:
            t.join(timeout=timeout)

    def pump(self, rounds: int = 1) -> None:
        """Drain the shared event stream into the DB, as the Hub's event consumer does."""
        import apps.hub.events as ev
        hub = ev.EventHub.__new__(ev.EventHub)
        for _ in range(rounds):
            for m in self.bus.read_events(last_id="0", block_ms=1, count=500):
                wid = m.payload.get("worker_id")
                if not wid:
                    continue
                upd = ev.map_event_to_update(m.type, m.payload)
                if upd:
                    db = self.Session()
                    try:
                        w = db.get(M.Worker, wid)
                        if w:
                            for k, v in upd.items():
                                setattr(w, k, v)
                            db.commit()
                    finally:
                        db.close()
                if m.type == "slots_found":
                    hub._persist_slots(wid, m.payload.get("slots", []))
                if m.type == "slot_released":
                    hub._release_slot_lease(wid, m.payload.get("slot") or {},
                                            m.payload.get("reason", "bounce"))
                if m.type == "booked":
                    hub._persist_booking(wid, m.payload.get("slot"))
                if m.type == "booked_pdf" and m.payload.get("pdf_path"):
                    hub._persist_booked_pdf(wid, m.payload["pdf_path"], m.payload.get("slot"))
            time.sleep(0.02)


def _seed(Session):
    db = Session()
    bk = M.Booking(name="SimFlow", type="test", posto_id=POSTO, nationality="CPV", residence="FRA",
                   status=BookingStatus.WARMING, settings_json={"MAX_BOOKINGS": 1})
    db.add(bk); db.commit()
    ids = {}
    for name, role in (("SCOUT ONE", WorkerRole.SCOUT), ("APPLY ONE", WorkerRole.APPLY)):
        i = M.Identity(full_name=name, type="real")
        db.add(i); db.commit()
        sa = M.SubAccount(identity_id=i.id, username=name.replace(" ", "").lower(), password="p")
        db.add(sa); db.commit()
        w = M.Worker(booking_id=bk.id, identity_id=i.id, role=role, status=WorkerStatus.RUNNING,
                     effective_settings_json={"_sub_account_id": sa.id})
        db.add(w); db.commit()
        ids[role] = w.id
    bid = bk.id
    db.close()
    return bid, ids


def _cfg(role: str, **booking):
    return EngineConfig.from_settings(resolve({
        L.BOOKING: {"CAL_START_DATE": DATES[0], "CAL_END_DATE": DATES[-1],
                    "CAL_NUM_DAYS": len(DATES), **booking},
        L.TEMPLATE: {"ROLE": role},
    }))


def _wait(pred, timeout=8.0, pump=None):
    end = time.time() + timeout
    while time.time() < end:
        if pump:
            pump.pump()
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_long_hold_then_slot_open_discovery_then_apply_books(env):
    """The full operator flow, in order, with the REAL bus and Hub persistence in the middle.

    1. Both workers log in, warm, and HOLD -- for a long stretch with no slot-open. The point of
       the hold is that it costs nothing and breaks nothing: no failures, no terminal outcomes, and
       the apply worker must NOT have consumed a slot or ended its run.
    2. slot-open fires. The scout starts discovering and pushes what it finds.
    3. The Hub persists the slots, slot_found fires, the scheduler assigns one to the apply worker.
    4. The apply worker books it -- AFTER the long hold, which is the part the operator asked to
       confirm still works."""
    Session, bus, r, fleets = env
    bid, wids = _seed(Session)
    scout_id, apply_id = wids[WorkerRole.SCOUT], wids[WorkerRole.APPLY]
    fleet = _Fleet(Session, bus, r); fleets.append(fleet)

    sched = Scheduler(session_local=Session, on_start=lambda db, bk: None,
                      on_slot_open=lambda db, bk: None,
                      # Exactly apps/hub/main.py's _sched_on_assign -- the slot must cross the bus
                      # as the same plain dict the real worker receives, not an ORM object.
                      on_assign=lambda db, w, s: bus.send_command(
                          w.id, "assign",
                          slot={"id": s.id, "date": s.date, "period": s.period}),
                      bus=bus, redis_client=r)

    # The scout discovers both dates on its first round, then has nothing more to do.
    fleet.start(scout_id, _cfg("scout"), dict(
        discover_tasks=[DATES],
        discover_outcomes={DATES[0]: {"outcome": "found", "periods": [8]},
                           DATES[1]: {"outcome": "empty"}},
        stop_after_polls=40,
    ))
    apply_ops = fleet.start(apply_id, _cfg("apply"), dict(
        assignment=None, apply_outcome="booked", stop_after_polls=200,
    ))

    # ── 1. LONG HOLD ─────────────────────────────────────────────────────────
    assert _wait(lambda: _substate(Session, apply_id) == WorkerSubstate.HOLDING, pump=fleet), \
        f"apply worker never reached HOLDING (got {_substate(Session, apply_id)})"
    for _ in range(25):                       # many scheduler ticks with no slot-open at all
        sched._tick_once()
        fleet.pump()
    assert apply_ops.applied == 0, "applied something during the hold, with no slot open"
    assert fleet.results.get(apply_id) is None, "the apply worker ENDED during the hold"
    assert fleet.results.get(scout_id) is None, "the scout ENDED during the hold"
    db = Session()
    assert db.scalar(select(func.count()).select_from(M.Slot)) == 0   # nothing discovered yet
    db.close()

    # ── 2. SLOT-OPEN -> DISCOVERY ────────────────────────────────────────────
    db = Session()
    bk = db.get(M.Booking, bid)
    bk.status = BookingStatus.RUNNING
    bk.slot_open_fired_at = _now()
    db.commit(); db.close()
    for _ in range(6):                        # _latch_signals delivers slot_open per-worker
        sched._tick_once()
        fleet.pump()

    assert _wait(lambda: _slot_count(Session) > 0, pump=fleet), "scout never pushed a slot"

    # ── 3. SLOT-FOUND -> ASSIGNMENT ──────────────────────────────────────────
    assert _wait(lambda: _booking(Session, bid).slot_found_fired_at is not None,
                 pump=fleet) or _tick_until(sched, fleet,
                                            lambda: _booking(Session, bid).slot_found_fired_at), \
        "slot_found never fired despite a persisted AVAILABLE slot"

    # ── 4. APPLY AFTER THE HOLD ──────────────────────────────────────────────
    assert _tick_until(sched, fleet, lambda: _appointments(Session) == 1, ticks=60), (
        f"no booking after the hold; apply outcome={fleet.results.get(apply_id)!r} "
        f"applied={apply_ops.applied} substate={_substate(Session, apply_id)}")

    db = Session()
    appt = db.scalars(select(M.BookedAppointment)).one()
    slot = db.get(M.Slot, appt.slot_id)
    lease = db.scalars(select(M.SlotLease).where(M.SlotLease.slot_id == slot.id)).one()
    assert slot.status == SlotStatus.TAKEN
    assert lease.outcome == SlotLeaseOutcome.BOOKED and lease.released_at is not None
    # No PDF in sim -- and the appointment must exist regardless, which is the whole point:
    # a booking is real the moment the portal confirms it, PDF or no PDF.
    assert appt.pdf_artifact_id is None
    assert (slot.date, slot.period) == (DATES[0], 8)  # the slot the scout actually found
    db.close()
    assert apply_ops.applied == 1, "booked more than once"


def test_a_long_hold_does_not_consume_the_apply_workers_run(env):
    """Narrower regression on the same flow: holding is not a terminal state. Before the apply loop
    was rewritten, a worker with no assignment reported "held", the Hub mapped that to FAILED, and
    hold-respawn relaunched it -- paying a full login+warmup to sit and hold again."""
    Session, bus, r, fleets = env
    bid, wids = _seed(Session)
    fleet = _Fleet(Session, bus, r); fleets.append(fleet)
    apply_id = wids[WorkerRole.APPLY]

    apply_ops = fleet.start(apply_id, _cfg("apply"), dict(
        assignment=None, apply_outcome="booked", stop_after_polls=200))

    assert _wait(lambda: _substate(Session, apply_id) == WorkerSubstate.HOLDING, pump=fleet)
    for _ in range(40):
        fleet.pump()
        time.sleep(0.01)

    assert fleet.results.get(apply_id) is None, "the hold ended the run"
    assert _status(Session, apply_id) == WorkerStatus.RUNNING
    assert apply_ops.applied == 0


# ── small readers, kept out of the flow above so it reads as the operator's sequence ──────────
def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _booking(Session, bid):
    db = Session()
    try:
        return db.get(M.Booking, bid)
    finally:
        db.close()


def _substate(Session, wid):
    db = Session()
    try:
        w = db.get(M.Worker, wid)
        return w.substate if w else None
    finally:
        db.close()


def _status(Session, wid):
    db = Session()
    try:
        return db.get(M.Worker, wid).status
    finally:
        db.close()


def _slot_count(Session):
    db = Session()
    try:
        return db.scalar(select(func.count()).select_from(M.Slot)) or 0
    finally:
        db.close()


def _appointments(Session):
    db = Session()
    try:
        return db.scalar(select(func.count()).select_from(M.BookedAppointment)) or 0
    finally:
        db.close()


def _tick_until(sched, fleet, pred, ticks=30):
    for _ in range(ticks):
        sched._tick_once()
        fleet.pump()
        if pred():
            return True
        time.sleep(0.05)
    return bool(pred())


def test_pipeline_runs_to_the_apply_boundary_and_stops_there(env):
    """PRE-APPLY PROOF: every production stage up to the `do_apply` call, and not one step past it.

    This is the validation that matters when the final submission is irreversible -- it books a real
    public appointment in a real applicant's name -- so the pipeline has to be provable without
    performing it. `no_book` is the production setting for exactly that (catalog: "test dry-run:
    apply workers never submit"); it gates in phases.py's _run_apply AFTER the worker has taken the
    assignment and entered APPLYING, and returns PhaseResult("dry_run") instead of calling do_apply.

    So this asserts the whole chain fired -- scout discovery -> push_slots -> Hub persistence ->
    slot_found -> scheduler assignment -> SlotLease -> the slot crossing the bus to the worker ->
    the worker entering APPLYING -- while do_apply itself was NEVER invoked and no appointment
    exists. The difference from test_long_hold_...: that one proves the pipeline BOOKS; this proves
    the pipeline is complete and correct right up to the boundary, which is the strongest statement
    available without a real submission.
    """
    Session, bus, r, fleets = env
    bid, wids = _seed(Session)
    scout_id, apply_id = wids[WorkerRole.SCOUT], wids[WorkerRole.APPLY]
    fleet = _Fleet(Session, bus, r); fleets.append(fleet)

    sched = Scheduler(session_local=Session, on_start=lambda db, bk: None,
                      on_slot_open=lambda db, bk: None,
                      on_assign=lambda db, w, s: bus.send_command(
                          w.id, "assign", slot={"id": s.id, "date": s.date, "period": s.period}),
                      bus=bus, redis_client=r)

    fleet.start(scout_id, _cfg("scout"), dict(
        discover_tasks=[DATES],
        discover_outcomes={DATES[0]: {"outcome": "found", "periods": [8]},
                           DATES[1]: {"outcome": "empty"}},
        stop_after_polls=40,
    ))
    # apply_outcome is deliberately "booked": if the no_book gate ever regressed, this worker WOULD
    # book and the appointment assertion below would fail loudly rather than passing by accident.
    apply_ops = fleet.start(apply_id, _cfg("apply", no_book=True), dict(
        assignment=None, apply_outcome="booked", stop_after_polls=200,
    ))

    assert _wait(lambda: _substate(Session, apply_id) == WorkerSubstate.HOLDING, pump=fleet)

    db = Session(); bk = db.get(M.Booking, bid)
    bk.status = BookingStatus.RUNNING; bk.slot_open_fired_at = _now()
    db.commit(); db.close()
    for _ in range(6):
        sched._tick_once(); fleet.pump()

    # discovery -> push_slots -> Hub persistence
    assert _wait(lambda: _slot_count(Session) > 0, pump=fleet), "scout never pushed a slot"

    # scheduler assignment -> SlotLease -> handoff -> worker enters APPLYING and stops at the gate
    assert _tick_until(sched, fleet,
                       lambda: fleet.results.get(apply_id) is not None, ticks=60), (
        f"apply worker never reached a terminal state; substate={_substate(Session, apply_id)}")

    # _Fleet records PhaseResult.outcome (a str), not the PhaseResult itself.
    assert fleet.results[apply_id] == "dry_run", \
        f"expected the no_book boundary, got {fleet.results[apply_id]!r}"

    # THE POINT: the boundary was reached, and not crossed.
    assert apply_ops.applied == 0, "do_apply was invoked despite no_book"

    db = Session()
    # the scheduler really did assign and lease -- this is not a test that simply never got there
    lease = db.scalars(select(M.SlotLease).where(M.SlotLease.worker_id == apply_id)).all()
    assert len(lease) == 1, f"expected exactly one slot lease from the scheduler, got {len(lease)}"
    slot = db.get(M.Slot, lease[0].slot_id)
    assert (slot.date, slot.period) == (DATES[0], 8)
    # APPLYING was entered: the substate hop is emitted over the real bus before the gate returns.
    applying_seen = any(
        m.type == "status_change" and m.payload.get("substate") == str(WorkerSubstate.APPLYING)
        and m.payload.get("worker_id") == apply_id
        for m in bus.read_events(last_id="0", block_ms=1, count=2000))
    assert applying_seen, "worker never entered APPLYING -- the boundary was not actually reached"
    # and nothing irreversible happened
    assert db.scalar(select(func.count()).select_from(M.BookedAppointment)) == 0
    db.close()

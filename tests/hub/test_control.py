"""Tests for the worker-control plumbing: event->status mapping (pure) + supervisor tree-kill."""
import subprocess
import time

import pytest

from apps.hub.events import EventHub, map_event_to_update
from apps.hub.supervisor import _alive, kill_tree
from libs.domain.enums import WorkerStatus, WorkerSubstate


# ── event -> worker-status mapping ────────────────────────────────────────────
def test_started_sets_running_idle():
    assert map_event_to_update("started", {}) == {
        "status": WorkerStatus.RUNNING, "substate": WorkerSubstate.IDLE}


def test_status_change_sets_substate_and_detail():
    upd = map_event_to_update("status_change", {"substate": "warming_up", "detail": "attempt 2"})
    assert upd == {"substate": "warming_up", "substate_detail": "attempt 2"}


def test_terminal_booked_completes():
    upd = map_event_to_update("terminal", {"outcome": "booked", "substate": "booked"})
    assert upd["status"] == WorkerStatus.COMPLETED and upd["substate"] == "booked"


def test_terminal_failed():
    assert map_event_to_update("terminal", {"outcome": "failed"})["status"] == WorkerStatus.FAILED


def test_terminal_stopped():
    assert map_event_to_update("terminal", {"outcome": "stopped"})["status"] == WorkerStatus.STOPPED


def test_terminal_held_is_its_own_status_not_a_failure():
    """Confirmed bug 2026-08-14: `held` (emitted by _run_apply for "no slot assigned" and "apply
    bounced") had no entry in _TERMINAL_STATUS, so it hit the FAILED default -- a worker that did
    nothing wrong was shown red, counted as failed, and burned MAX_RESPAWN_ATTEMPTS."""
    assert map_event_to_update("terminal", {"outcome": "held"})["status"] == WorkerStatus.HELD


def test_every_phase_outcome_has_an_explicit_terminal_status():
    """Drift guard -- this is the test that would have caught the `held` bug. Any outcome
    libs/engine/phases.py can emit MUST be an explicit key in _TERMINAL_STATUS; falling through to
    the FAILED default silently mislabels a worker."""
    import re
    from pathlib import Path

    from apps.hub.events import _TERMINAL_STATUS

    src = Path("libs/engine/phases.py").read_text()
    emitted = set(re.findall(r'PhaseResult\(\s*"(\w+)"', src))
    assert emitted, "failed to parse any PhaseResult outcomes -- fix this test's regex"
    missing = emitted - set(_TERMINAL_STATUS)
    assert not missing, f"outcomes with no explicit terminal status (would silently become FAILED): {missing}"


def test_log_line_no_db_change():
    assert map_event_to_update("log_line", {"text": "hi"}) == {}


# ── EventHub.dates_reported buffer (D6) ────────────────────────────────────────
def test_pop_dates_reported_returns_and_clears_buffered_reports():
    hub = EventHub.__new__(EventHub)   # bypass __init__ -- no Redis needed for this
    hub.dates_reported = {}

    assert hub.pop_dates_reported("s1") == []   # nothing buffered yet

    hub.dates_reported.setdefault("s1", []).append([{"date": "d1", "outcome": "empty"}])
    hub.dates_reported.setdefault("s1", []).append([{"date": "d2", "outcome": "found"}])
    hub.dates_reported.setdefault("s2", []).append([{"date": "d3", "outcome": "failed"}])

    popped = hub.pop_dates_reported("s1")
    assert popped == [[{"date": "d1", "outcome": "empty"}], [{"date": "d2", "outcome": "found"}]]
    assert hub.pop_dates_reported("s1") == []          # drained
    assert hub.pop_dates_reported("s2") == [[{"date": "d3", "outcome": "failed"}]]   # untouched by s1's pop


# ── supervisor kill_tree ──────────────────────────────────────────────────────
def test_kill_tree_reaps_process_and_children():
    # parent shell spawns a child sleep; killing the tree must reap both
    p = subprocess.Popen(["bash", "-c", "sleep 30 & sleep 30"], start_new_session=True)
    time.sleep(0.3)
    assert _alive(p.pid)
    kill_tree(p.pid, grace=2.0)
    p.wait(timeout=3)   # reap the killed parent so it isn't a lingering zombie
    assert p.poll() is not None


# ── rediscovery must reopen a CLOSED slot (confirmed live 2026-08-14) ─────────
@pytest.fixture()
def slots_db(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from libs.db import metadata

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(eng)
    S = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    import apps.hub.events as ev
    monkeypatch.setattr(ev, "SessionLocal", S)
    return S


def _seed_scout(S):
    from libs.db import models as M
    from libs.domain.enums import BookingStatus, WorkerRole, WorkerStatus

    db = S()
    bk = M.Booking(name="b", type="real", posto_id="5084", status=BookingStatus.RUNNING)
    i = M.Identity(full_name="X", type="real")
    db.add_all([bk, i]); db.commit()
    w = M.Worker(booking_id=bk.id, identity_id=i.id, role=WorkerRole.SCOUT,
                 status=WorkerStatus.RUNNING)
    db.add(w); db.commit()
    ids = (bk.id, w.id)
    db.close()
    return ids


def test_rediscovery_reopens_a_closed_slot(slots_db):
    """SlotStatus.CLOSED means "absent from N consecutive scans" and the transition table has said
    CLOSED -> AVAILABLE ("reappearance reopens") from the start -- but _persist_slots just
    `continue`d on ANY existing row, so nothing ever performed that transition.

    Harmless while slots were only created; fatal once /start began retiring the previous run's
    slots to CLOSED. Confirmed live on booking Test2: a scout pushed 24 slots at 09:21 and again at
    09:23, every one matching a CLOSED row from the earlier run -- all 48 dropped, slot_found never
    fired, and the appliers held with nothing to do while the pool read "24 slots, all closed"."""
    from sqlalchemy import select

    import apps.hub.events as ev
    from libs.db import models as M
    from libs.domain.enums import SlotStatus

    bid, wid = _seed_scout(slots_db)
    db = slots_db()
    db.add(M.Slot(booking_id=bid, posto_id="5084", date="2026-09-01", period=3,
                  status=SlotStatus.CLOSED, miss_count=3))
    db.commit(); db.close()

    ev.EventHub.__new__(ev.EventHub)._persist_slots(wid, [{"date": "2026-09-01", "period": 3}])

    db = slots_db()
    slot = db.scalars(select(M.Slot)).one()
    assert slot.status == SlotStatus.AVAILABLE
    assert slot.miss_count == 0          # the strike history that closed it is cleared
    assert slot.last_seen is not None
    db.close()


def test_rediscovery_never_disturbs_a_leased_or_taken_slot(slots_db):
    """A slot being worked, or already booked, is not something a discovery report may reset."""
    from sqlalchemy import select

    import apps.hub.events as ev
    from libs.db import models as M
    from libs.domain.enums import SlotStatus

    bid, wid = _seed_scout(slots_db)
    db = slots_db()
    db.add(M.Slot(booking_id=bid, posto_id="5084", date="2026-09-01", period=3,
                  status=SlotStatus.TAKEN))
    db.add(M.Slot(booking_id=bid, posto_id="5084", date="2026-09-02", period=4,
                  status=SlotStatus.LEASED))
    db.commit(); db.close()

    ev.EventHub.__new__(ev.EventHub)._persist_slots(
        wid, [{"date": "2026-09-01", "period": 3}, {"date": "2026-09-02", "period": 4}])

    db = slots_db()
    got = {(s.date, str(s.status)) for s in db.scalars(select(M.Slot)).all()}
    assert got == {("2026-09-01", "taken"), ("2026-09-02", "leased")}
    db.close()


def test_slot_released_frees_the_lease_and_reopens_the_slot(slots_db):
    """A worker that keeps its warm session after a bounce (phases.py's apply loop) must also hand
    the slot back -- otherwise _assign_slots' one-active-lease rule leaves it warm and idle until
    SLOT_LEASE_TTL reclaims it minutes later."""
    from sqlalchemy import select

    import apps.hub.events as ev
    from libs.db import models as M
    from libs.domain.enums import SlotLeaseOutcome, SlotStatus

    bid, wid = _seed_scout(slots_db)
    db = slots_db()
    slot = M.Slot(booking_id=bid, posto_id="5084", date="2026-09-03", period=3,
                  status=SlotStatus.LEASED)
    db.add(slot); db.commit()
    db.add(M.SlotLease(slot_id=slot.id, worker_id=wid)); db.commit()
    db.close()

    ev.EventHub.__new__(ev.EventHub)._release_slot_lease(wid, {"date": "2026-09-03", "period": 3})

    db = slots_db()
    lease = db.scalars(select(M.SlotLease)).one()
    assert lease.released_at is not None
    assert lease.outcome == SlotLeaseOutcome.BOUNCED
    assert db.scalars(select(M.Slot)).one().status == SlotStatus.AVAILABLE
    db.close()


def test_form_stale_release_is_recorded_distinctly_from_a_bounce(slots_db):
    """storm-auto's two-tier release (slot_manager.release(lease, "form_stale") vs "bounce"): a
    form_stale means OUR form-state expired, so it must never count against the slot -- neither
    toward retiring it nor toward the Hub's false-slot verdict, which has to be evidence the POOL is
    phantom rather than evidence our sessions keep lapsing."""
    from sqlalchemy import select

    import apps.hub.events as ev
    from libs.db import models as M
    from libs.domain.enums import SlotLeaseOutcome, SlotStatus

    bid, wid = _seed_scout(slots_db)
    db = slots_db()
    slot = M.Slot(booking_id=bid, posto_id="5084", date="2026-09-05", period=2,
                  status=SlotStatus.LEASED)
    db.add(slot); db.commit()
    db.add(M.SlotLease(slot_id=slot.id, worker_id=wid)); db.commit()
    db.close()

    ev.EventHub.__new__(ev.EventHub)._release_slot_lease(
        wid, {"date": "2026-09-05", "period": 2}, "form_stale")

    db = slots_db()
    assert db.scalars(select(M.SlotLease)).one().outcome == SlotLeaseOutcome.FORM_STALE
    assert db.scalars(select(M.Slot)).one().status == SlotStatus.AVAILABLE   # still freed
    db.close()


# ── the success path had never once run end to end (confirmed live 2026-08-14) ────────────────
def _seed_apply_worker(S, sub_account=True):
    from libs.db import models as M
    from libs.domain.enums import BookingStatus, WorkerRole, WorkerStatus

    db = S()
    bk = M.Booking(name="b", type="real", posto_id="3018", status=BookingStatus.RUNNING)
    i = M.Identity(full_name="CLAUDIA BORGES", type="real")
    db.add_all([bk, i]); db.commit()
    sa = M.SubAccount(identity_id=i.id, username="clabor4612", password="p")
    db.add(sa); db.commit()
    w = M.Worker(booking_id=bk.id, identity_id=i.id, role=WorkerRole.APPLY,
                 status=WorkerStatus.RUNNING,
                 effective_settings_json={"_sub_account_id": sa.id} if sub_account else {})
    db.add(w); db.commit()
    ids = (bk.id, w.id)
    db.close()
    return ids


def test_booked_pdf_writes_the_artifact_appointment_and_marks_the_slot_taken(slots_db):
    """This whole handler had never executed successfully -- it referenced SlotStatus.BOOKED, which
    is not a member of SlotStatus (TAKEN is the booked state). A bug reachable ONLY on the success
    path survives every failing run: it first fired on 2026-08-14 when a worker booked posto 3018 /
    2026-09-10 / period 7 with a verified 250KB PDF, and the AttributeError rolled the transaction
    back -- no Artifact, no BookedAppointment, slot never marked."""
    from sqlalchemy import select

    import apps.hub.events as ev
    from libs.db import models as M
    from libs.domain.enums import SlotStatus

    bid, wid = _seed_apply_worker(slots_db)
    db = slots_db()
    db.add(M.Slot(booking_id=bid, posto_id="3018", date="2026-09-10", period=7,
                  status=SlotStatus.LEASED))
    db.commit(); db.close()

    ev.EventHub.__new__(ev.EventHub)._persist_booked_pdf(
        wid, "/var/artifacts/x.pdf", {"date": "2026-09-10", "period": 7})

    db = slots_db()
    art = db.scalars(select(M.Artifact)).one()
    appt = db.scalars(select(M.BookedAppointment)).one()
    assert art.kind == "pdf" and art.path == "/var/artifacts/x.pdf"
    assert appt.pdf_artifact_id == art.id and appt.booking_id == bid
    assert appt.booked_at is not None
    assert db.scalars(select(M.Slot)).one().status == SlotStatus.TAKEN
    db.close()


def test_booked_pdf_closes_the_lease_as_booked(slots_db):
    """SlotLeaseOutcome.BOOKED existed in the enum from day one and was never once written: the
    terminal path sets released_at but no outcome, so every real booking left its lease reading
    None -- indistinguishable from one still in flight."""
    from sqlalchemy import select

    import apps.hub.events as ev
    from libs.db import models as M
    from libs.domain.enums import SlotLeaseOutcome, SlotStatus

    bid, wid = _seed_apply_worker(slots_db)
    db = slots_db()
    slot = M.Slot(booking_id=bid, posto_id="3018", date="2026-09-10", period=7,
                  status=SlotStatus.LEASED)
    db.add(slot); db.commit()
    db.add(M.SlotLease(slot_id=slot.id, worker_id=wid)); db.commit()
    db.close()

    ev.EventHub.__new__(ev.EventHub)._persist_booked_pdf(
        wid, "/var/artifacts/x.pdf", {"date": "2026-09-10", "period": 7})

    db = slots_db()
    lease = db.scalars(select(M.SlotLease)).one()
    assert lease.outcome == SlotLeaseOutcome.BOOKED and lease.released_at is not None
    db.close()


def test_a_failing_side_effect_never_swallows_the_rest_of_the_event(monkeypatch, caplog):
    """The real cost of the crash above wasn't only the missing rows: it aborted the rest of that
    message's handling, so the `terminal` branch never ran and the worker still showed
    running/applying after it had booked and exited. Handlers must be isolated and loud."""
    import logging

    import apps.hub.events as ev

    hub = ev.EventHub.__new__(ev.EventHub)
    hub.last_seen, hub._event_times, hub.dates_reported = {}, {}, {}
    hub._stop = __import__("threading").Event()
    hub._loop = None
    hub._redis_url = "redis://unused"
    calls = []

    def _boom(*a, **k):
        raise AttributeError("BOOKED")

    monkeypatch.setattr(hub, "_apply_db", _boom, raising=False)
    monkeypatch.setattr(hub, "_release_on_terminal", lambda *a: calls.append("terminal"),
                        raising=False)

    class _M:
        id, type, ts = "1-0", "terminal", 0.0
        payload = {"worker_id": "w1", "outcome": "booked"}

    class _Bus:
        def read_events(self, **kw):
            hub._stop.set()
            return [_M()]

    monkeypatch.setattr(ev, "Bus", lambda *a, **k: _Bus())
    monkeypatch.setattr(ev, "connect", lambda *a, **k: None)
    with caplog.at_level(logging.ERROR):
        hub._consume()

    assert calls == ["terminal"], "a failing handler swallowed the terminal status update"
    assert any("apply_db" in r.message for r in caplog.records), "the failure was not logged"


def test_a_confirmed_booking_is_recorded_even_when_the_pdf_never_arrives(slots_db):
    """The appointment is real the moment the portal confirms it; the PDF is an ARTIFACT, not the
    proof. Recording used to be reachable only via `booked_pdf`, so a real booking whose PDF fetch
    failed -- or returned a non-PDF, which do_apply deliberately refuses to save -- produced NO
    BookedAppointment. do_apply's own log said "booking IS confirmed, PDF missing" and the Hub then
    recorded nothing: the one artefact this system exists to produce, lost to a failed follow-up
    fetch. Found by the full-flow sim."""
    from sqlalchemy import select

    import apps.hub.events as ev
    from libs.db import models as M
    from libs.domain.enums import SlotStatus

    bid, wid = _seed_apply_worker(slots_db)
    db = slots_db()
    db.add(M.Slot(booking_id=bid, posto_id="3018", date="2026-09-10", period=7,
                  status=SlotStatus.LEASED))
    db.commit(); db.close()

    ev.EventHub.__new__(ev.EventHub)._persist_booking(
        wid, {"date": "2026-09-10", "period": 7})       # no pdf_path at all

    db = slots_db()
    appt = db.scalars(select(M.BookedAppointment)).one()
    assert appt.pdf_artifact_id is None
    assert db.get(M.Slot, appt.slot_id).status == SlotStatus.TAKEN
    assert db.scalars(select(M.Artifact)).all() == []
    db.close()


def test_the_pdf_event_attaches_to_the_existing_appointment_not_a_duplicate(slots_db):
    """`booked` and `booked_pdf` both land for a healthy live booking. The second must attach what
    it adds to the first's row -- two appointment rows for one real appointment would be worse than
    the missing artifact."""
    from sqlalchemy import select

    import apps.hub.events as ev
    from libs.db import models as M
    from libs.domain.enums import SlotStatus

    bid, wid = _seed_apply_worker(slots_db)
    db = slots_db()
    db.add(M.Slot(booking_id=bid, posto_id="3018", date="2026-09-10", period=7,
                  status=SlotStatus.LEASED))
    db.commit(); db.close()

    hub = ev.EventHub.__new__(ev.EventHub)
    slot = {"date": "2026-09-10", "period": 7}
    hub._persist_booking(wid, slot)
    hub._persist_booked_pdf(wid, "/var/artifacts/x.pdf", slot)

    db = slots_db()
    appt = db.scalars(select(M.BookedAppointment)).one()      # exactly one
    art = db.scalars(select(M.Artifact)).one()
    assert appt.pdf_artifact_id == art.id
    db.close()


def test_a_session_release_is_recorded_distinctly_from_a_bounce(slots_db):
    """A worker whose own re-warm failed never spoke to the portal about that slot. Recording it as
    BOUNCED both penalises a blameless slot and feeds the Hub's false-slot revert with evidence of
    OUR failure -- which is exactly how a real pool got condemned repeatedly on 2026-08-15."""
    from sqlalchemy import select

    import apps.hub.events as ev
    from libs.db import models as M
    from libs.domain.enums import SlotLeaseOutcome, SlotStatus

    bid, wid = _seed_scout(slots_db)
    db = slots_db()
    slot = M.Slot(booking_id=bid, posto_id="5084", date="2026-09-06", period=1,
                  status=SlotStatus.LEASED)
    db.add(slot); db.commit()
    db.add(M.SlotLease(slot_id=slot.id, worker_id=wid)); db.commit()
    db.close()

    ev.EventHub.__new__(ev.EventHub)._release_slot_lease(
        wid, {"date": "2026-09-06", "period": 1}, "session")

    db = slots_db()
    assert db.scalars(select(M.SlotLease)).one().outcome == SlotLeaseOutcome.SESSION
    assert db.scalars(select(M.Slot)).one().status == SlotStatus.AVAILABLE
    db.close()


def test_a_confirmed_booking_marks_the_identity_booked_and_logs_it(slots_db):
    """The worker row is NOT a durable record of a booking. Detaching an identity deletes its worker
    outright and nulls the artifact's worker_id, so re-attaching gives a fresh READY worker with no
    trace -- exactly what was reported for RICARDO RONALDO CORREIA MATOS: a booked PDF on disk, an
    appointment row, and a worker showing "ready" whose log had never seen a booking.

    IdentityStatus.BOOKED and the identity_events table both existed for this and neither had a
    single writer anywhere in the codebase."""
    from sqlalchemy import select

    import apps.hub.events as ev
    from libs.db import models as M
    from libs.domain.enums import IdentityEvent, IdentityStatus, SlotStatus

    bid, wid = _seed_apply_worker(slots_db)
    db = slots_db()
    ident = db.scalars(select(M.Identity)).one()
    ident.status = IdentityStatus.ACTIVE
    db.add(M.Slot(booking_id=bid, posto_id="3018", date="2026-09-14", period=3,
                  status=SlotStatus.LEASED))
    db.commit(); db.close()

    ev.EventHub.__new__(ev.EventHub)._persist_booking(wid, {"date": "2026-09-14", "period": 3})

    db = slots_db()
    assert db.scalars(select(M.Identity)).one().status == IdentityStatus.BOOKED
    e = db.scalars(select(M.IdentityEvent)).one()
    assert e.event == IdentityEvent.BOOKED and e.booking_id == bid
    db.close()


def test_identity_bookkeeping_never_costs_the_booking_record(slots_db, monkeypatch):
    """If the status/history write fails, the APPOINTMENT must still be recorded -- it is the thing
    the system exists to produce, and bookkeeping is not allowed to take it down."""
    from sqlalchemy import select

    import apps.hub.events as ev
    from libs.db import models as M
    from libs.domain.enums import SlotStatus

    bid, wid = _seed_apply_worker(slots_db)
    db = slots_db()
    db.add(M.Slot(booking_id=bid, posto_id="3018", date="2026-09-14", period=3,
                  status=SlotStatus.LEASED))
    db.commit(); db.close()

    hub = ev.EventHub.__new__(ev.EventHub)
    monkeypatch.setattr(hub, "_mark_identity_booked",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
                        raising=False)
    try:
        hub._persist_booking(wid, {"date": "2026-09-14", "period": 3})
    except RuntimeError:
        pass

    db = slots_db()
    assert len(db.scalars(select(M.BookedAppointment)).all()) == 1
    db.close()

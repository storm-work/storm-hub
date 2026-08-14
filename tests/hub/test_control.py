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

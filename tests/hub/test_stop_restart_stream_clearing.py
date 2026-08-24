"""stop_worker/stop_booking/restart_worker/start_booking must never leave stale per-worker bus
state behind when a worker is about to get a fresh process incarnation. Two confirmed bugs
(2026-08-13), both fixed by apps/hub/main.py's shared _reset_worker_bus_state:

1. "I click start now ... no workers start" -- a leftover unconsumed command (most commonly an
   earlier "stop") sitting in a worker's command stream ahead of its next `launch`.
   BusSignals.wait_launch() drains its whole batch and returns on the first match in stream order,
   so the stale stop makes a freshly spawned worker treat itself as told-to-stop before it ever
   looks at its own launch payload. See tests/worker/test_bus_signals.py for the mechanism itself.

2. "I can't fire any signal after restart" -- apps/hub/scheduler.py's _latch_signals tracks
   "already notified" per worker_id in a Redis set that's meant to survive a HUB restart but was
   ALSO (wrongly) surviving the WORKER's own restart: a relaunched worker has a fresh, empty
   command stream and has never actually received slot_open/slot_found in its own lifetime, but
   the sticky set made the scheduler think it had nothing left to send -- and the booking-level
   fire-slot-open/fire-slot-found endpoints are correctly idempotent no-ops once the booking has
   already fired, so there was no way to get the signal to the new process at all.

This file covers the Hub endpoints that must prevent both."""
import fakeredis
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.hub.db import get_db
from apps.hub.main import app
from libs.db import metadata


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(text("CREATE UNIQUE INDEX uq_identity_active_allocation "
                       "ON identity_allocations (identity_id) WHERE released_at IS NULL"))
        c.execute(text("CREATE UNIQUE INDEX uq_single_active_operate "
                       "ON user_sessions (purpose) WHERE purpose='operate' AND status='active'"))
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _override():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override

    import apps.hub.main as hub_main
    from libs.bus.bus import Bus
    fake_redis = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(hub_main, "_bus", Bus(fake_redis))
    monkeypatch.setattr(hub_main.supervisor, "spawn", lambda wid: 0)
    monkeypatch.setattr(hub_main.supervisor, "stop", lambda wid: True)
    monkeypatch.setattr(hub_main, "SessionLocal", TestSession)

    from fastapi.testclient import TestClient
    yield TestClient(app)
    app.dependency_overrides.clear()


def _operate_headers(client, username="admin", password="pw"):
    client.post("/auth/bootstrap", json={"username": username, "password": password})
    r = client.post("/auth/login", json={"username": username, "password": password, "purpose": "operate"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _bk_with_worker(client, h, name="b"):
    bid = client.post("/bookings", json={"name": name, "type": "real", "posto_id": "5084"},
                      headers=h).json()["id"]
    iid = client.post("/identities", json={"full_name": "X", "type": "real"}, headers=h).json()["id"]
    w = client.post(f"/bookings/{bid}/identities", json={"identity_id": iid}, headers=h).json()
    return bid, w["id"]


def _stream_len(worker_id):
    import apps.hub.main as hub_main
    return hub_main._bus.r.xlen(f"cmd:{worker_id}")


def _mark_notified(bid, worker_id, signal_name):
    import apps.hub.main as hub_main
    from libs.bus.names import notified_key
    hub_main._bus.r.sadd(notified_key(bid, signal_name), worker_id)


def _is_notified(bid, worker_id, signal_name):
    import apps.hub.main as hub_main
    from libs.bus.names import notified_key
    return hub_main._bus.r.sismember(notified_key(bid, signal_name), worker_id)


def test_stop_worker_clears_the_stream(client):
    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)
    client.post(f"/bookings/{bid}/start", headers=h)
    assert _stream_len(wid) > 0   # the "launch" command that was just sent

    r = client.post(f"/workers/{wid}/stop", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "stopped"
    assert _stream_len(wid) == 0   # the just-sent "stop" doesn't linger either


def test_stop_booking_clears_every_stopped_workers_stream(client):
    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)
    client.post(f"/bookings/{bid}/start", headers=h)

    r = client.post(f"/bookings/{bid}/stop", headers=h)
    assert r.status_code == 200 and r.json()["stopped"] == 1
    assert _stream_len(wid) == 0


def test_restart_worker_clears_stream_before_relaunching(client):
    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)
    client.post(f"/bookings/{bid}/start", headers=h)

    r = client.post(f"/workers/{wid}/restart", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "running"
    # the stream now holds exactly the NEW launch, not an old stop sitting ahead of it
    assert _stream_len(wid) == 1


def test_start_booking_requeues_stopped_worker_and_relaunches_it(client):
    """The reported bug: a booking that's been running a while drifts to STOPPED/FAILED workers,
    and "Start Instantly" used to only ever look at READY ones -- clicking it did nothing."""
    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)
    client.post(f"/bookings/{bid}/start", headers=h)
    client.post(f"/workers/{wid}/stop", headers=h)

    got = client.get(f"/bookings/{bid}/workers", headers=h).json()
    assert got[0]["status"] == "stopped"

    r = client.post(f"/bookings/{bid}/start", headers=h)
    assert r.status_code == 200
    assert r.json()["started"] == 1   # the requeued worker was launched, not skipped

    got = client.get(f"/bookings/{bid}/workers", headers=h).json()
    assert got[0]["status"] == "running"


def test_start_booking_requeues_failed_worker(client):
    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)

    import apps.hub.main as hub_main
    from libs.db import models as M
    from libs.domain.enums import WorkerStatus
    db = hub_main.SessionLocal()
    try:
        w = db.get(M.Worker, wid)
        w.status = WorkerStatus.FAILED
        db.commit()
    finally:
        db.close()

    r = client.post(f"/bookings/{bid}/start", headers=h)
    assert r.status_code == 200
    assert r.json()["started"] == 1

    got = client.get(f"/bookings/{bid}/workers", headers=h).json()
    assert got[0]["status"] == "running"


def test_start_booking_never_requeues_a_completed_worker(client):
    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)

    import apps.hub.main as hub_main
    from libs.db import models as M
    from libs.domain.enums import WorkerStatus
    db = hub_main.SessionLocal()
    try:
        w = db.get(M.Worker, wid)
        w.status = WorkerStatus.COMPLETED
        db.commit()
    finally:
        db.close()

    r = client.post(f"/bookings/{bid}/start", headers=h)
    assert r.status_code == 200
    assert r.json()["started"] == 0   # a worker that already finished must never be re-run

    got = client.get(f"/bookings/{bid}/workers", headers=h).json()
    assert got[0]["status"] == "completed"


# ── signal-latch membership must not survive a WORKER's own restart ────────────────────────────
def test_stop_worker_clears_signal_notified_membership(client):
    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)
    client.post(f"/bookings/{bid}/start", headers=h)
    _mark_notified(bid, wid, "slot_open")
    _mark_notified(bid, wid, "slot_found")

    client.post(f"/workers/{wid}/stop", headers=h)

    assert not _is_notified(bid, wid, "slot_open")
    assert not _is_notified(bid, wid, "slot_found")


def test_stop_booking_clears_signal_notified_membership(client):
    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)
    client.post(f"/bookings/{bid}/start", headers=h)
    _mark_notified(bid, wid, "slot_open")

    client.post(f"/bookings/{bid}/stop", headers=h)

    assert not _is_notified(bid, wid, "slot_open")


def test_restart_worker_clears_signal_notified_membership(client):
    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)
    client.post(f"/bookings/{bid}/start", headers=h)
    _mark_notified(bid, wid, "slot_open")
    _mark_notified(bid, wid, "slot_found")

    client.post(f"/workers/{wid}/restart", headers=h)

    assert not _is_notified(bid, wid, "slot_open")
    assert not _is_notified(bid, wid, "slot_found")


def test_start_booking_requeue_clears_signal_notified_membership(client):
    """The reported bug: a booking whose slot_open/slot_found fired hours ago, restarted after its
    workers died/were stopped, could not get either signal to the new worker processes -- the
    "already notified" set never forgot the (stable) worker_id from its previous incarnation."""
    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)
    client.post(f"/bookings/{bid}/start", headers=h)
    client.post(f"/workers/{wid}/stop", headers=h)
    _mark_notified(bid, wid, "slot_open")   # simulate: this worker's PREVIOUS incarnation was
    _mark_notified(bid, wid, "slot_found")  # already notified, hours before this restart

    client.post(f"/bookings/{bid}/start", headers=h)   # requeues the STOPPED worker and relaunches it

    assert not _is_notified(bid, wid, "slot_open")
    assert not _is_notified(bid, wid, "slot_found")


def test_sched_on_relaunch_clears_signal_notified_membership(client):
    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)
    client.post(f"/bookings/{bid}/start", headers=h)
    _mark_notified(bid, wid, "slot_open")

    import apps.hub.main as hub_main
    from libs.db import models as M
    db = hub_main.SessionLocal()
    try:
        w = db.get(M.Worker, wid)
        # Takes a LIST since 2026-08-21: the respawn dispatch is batched and backgrounded so a
        # proxy health probe can't stall the scheduler tick.
        hub_main._sched_on_relaunch(db, [w])
    finally:
        db.close()

    assert not _is_notified(bid, wid, "slot_open")

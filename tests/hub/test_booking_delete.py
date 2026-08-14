"""DELETE /bookings/{id} -- there was previously no way to remove a booking at all (only nested
resources: identities-within-a-booking, templates). Refuses to touch a booking that ever actually
booked something (a real BookedAppointment row); otherwise removes everything scoped to it (workers
and their proxy/slot leases/artifacts, slots, identity attachments/allocations), stopping any
RUNNING worker first."""
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
    stopped = []
    monkeypatch.setattr(hub_main.supervisor, "spawn", lambda wid: 0)
    monkeypatch.setattr(hub_main.supervisor, "stop", lambda wid: stopped.append(wid) or True)
    monkeypatch.setattr(hub_main, "SessionLocal", TestSession)

    from fastapi.testclient import TestClient
    c = TestClient(app)
    c._stopped = stopped   # expose for assertions
    yield c, TestSession
    app.dependency_overrides.clear()


def _operate_headers(client, username="admin", password="pw"):
    client.post("/auth/bootstrap", json={"username": username, "password": password})
    r = client.post("/auth/login", json={"username": username, "password": password, "purpose": "operate"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_delete_nonexistent_booking_404s(client):
    c, _ = client
    h = _operate_headers(c, "a1")
    assert c.delete("/bookings/nope", headers=h).status_code == 404


def test_delete_requires_operator(client):
    c, _ = client
    h = _operate_headers(c, "a1")
    bid = c.post("/bookings", json={"name": "b", "type": "test", "posto_id": "5084"}, headers=h).json()["id"]
    assert c.delete(f"/bookings/{bid}").status_code == 401


def test_delete_removes_booking_workers_and_identity_allocation(client):
    c, TestSession = client
    h = _operate_headers(c, "a1")
    bid = c.post("/bookings", json={"name": "b", "type": "real", "posto_id": "5084"}, headers=h).json()["id"]
    iid = c.post("/identities", json={"full_name": "X", "type": "real"}, headers=h).json()["id"]
    w = c.post(f"/bookings/{bid}/identities", json={"identity_id": iid}, headers=h).json()
    c.post(f"/bookings/{bid}/start", headers=h)   # worker now RUNNING

    r = c.delete(f"/bookings/{bid}", headers=h)
    assert r.status_code == 204

    assert c.get(f"/bookings/{bid}", headers=h).status_code == 404
    assert w["id"] in c._stopped   # the running worker's process was killed first

    db = TestSession()
    try:
        from libs.db import models as M
        assert db.get(M.Worker, w["id"]) is None
        assert db.query(M.BookingIdentity).filter(M.BookingIdentity.booking_id == bid).count() == 0
        assert db.query(M.IdentityAllocation).filter(M.IdentityAllocation.booking_id == bid).count() == 0
    finally:
        db.close()

    # the identity itself survives and is free to be attached to a NEW booking
    bid2 = c.post("/bookings", json={"name": "b2", "type": "real", "posto_id": "5084"}, headers=h).json()["id"]
    r2 = c.post(f"/bookings/{bid2}/identities", json={"identity_id": iid}, headers=h)
    assert r2.status_code == 201


def test_delete_removes_slots_and_leases(client):
    c, TestSession = client
    h = _operate_headers(c, "a1")
    bid = c.post("/bookings", json={"name": "b", "type": "test", "posto_id": "5084"}, headers=h).json()["id"]

    db = TestSession()
    try:
        from libs.db import models as M
        from libs.domain.enums import SlotStatus
        db.add(M.Slot(booking_id=bid, posto_id="5084", date="2026-08-20", period=4,
                      status=SlotStatus.AVAILABLE))
        db.commit()
    finally:
        db.close()

    r = c.delete(f"/bookings/{bid}", headers=h)
    assert r.status_code == 204

    db = TestSession()
    try:
        from libs.db import models as M
        assert db.query(M.Slot).filter(M.Slot.booking_id == bid).count() == 0
    finally:
        db.close()


def test_delete_refuses_a_booking_with_a_real_booked_appointment(client):
    c, TestSession = client
    h = _operate_headers(c, "a1")
    bid = c.post("/bookings", json={"name": "b", "type": "real", "posto_id": "5084"}, headers=h).json()["id"]
    iid = c.post("/identities", json={"full_name": "X", "type": "real"}, headers=h).json()["id"]
    c.post("/identities/gen-subaccounts", json={"identity_ids": [iid], "count": 1}, headers=h)
    sub_id = next(s["id"] for s in c.get("/sub-accounts", headers=h).json() if s["identity_id"] == iid)

    db = TestSession()
    try:
        from libs.db import models as M
        db.add(M.BookedAppointment(booking_id=bid, identity_id=iid, sub_account_id=sub_id))
        db.commit()
    finally:
        db.close()

    r = c.delete(f"/bookings/{bid}", headers=h)
    assert r.status_code == 409
    assert c.get(f"/bookings/{bid}", headers=h).status_code == 200   # untouched


def test_delete_nulls_identity_event_booking_id_instead_of_deleting(client):
    c, TestSession = client
    h = _operate_headers(c, "a1")
    bid = c.post("/bookings", json={"name": "b", "type": "test", "posto_id": "5084"}, headers=h).json()["id"]
    iid = c.post("/identities", json={"full_name": "X", "type": "test"}, headers=h).json()["id"]

    db = TestSession()
    try:
        from libs.db import models as M
        from libs.domain.enums import IdentityEvent as IdentityEventEnum
        ev = M.IdentityEvent(identity_id=iid, booking_id=bid, event=IdentityEventEnum.LOGIN_OK)
        db.add(ev)
        db.commit()
        ev_id = ev.id
    finally:
        db.close()

    r = c.delete(f"/bookings/{bid}", headers=h)
    assert r.status_code == 204

    db = TestSession()
    try:
        from libs.db import models as M
        kept = db.get(M.IdentityEvent, ev_id)
        assert kept is not None          # the identity's own history log survives
        assert kept.booking_id is None   # just detached from the now-gone booking
    finally:
        db.close()

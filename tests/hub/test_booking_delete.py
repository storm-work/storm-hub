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
    # Pin sim: ENGINE_MODE's default flipped to "live" (2026-08-21) and a live launch needs
    # accounts/proxies, so this worker would never reach RUNNING. This test is about DELETE
    # semantics, not engine selection, so it states the mode it needs rather than inheriting one.
    c.put(f"/bookings/{bid}", json={"settings": {"ENGINE_MODE": "sim"}}, headers=h)
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


# ── booking duplication (operator, 2026-08-21) ──────────────────────────────
def test_duplicate_copies_config_but_no_identities(client):
    client, _ = client
    """Setting up a run means getting posto, nationality, residence, the calendar window and a dozen
    settings right. Re-typing that to run the same config against new identities is where
    transcription mistakes come from."""
    h = _operate_headers(client, "a1")
    src = client.post("/bookings", json={"name": "orig", "type": "real", "posto_id": "3018",
                                         "nationality": "CPV", "residence": "FRA"}, headers=h).json()
    client.put(f"/bookings/{src['id']}", headers=h,
               json={"settings": {"PROXY_KIND": "rotating", "CAL_START_DATE": "2026-09-01"}})
    iid = client.post("/identities", json={"full_name": "A", "type": "real"}, headers=h).json()["id"]
    client.post(f"/bookings/{src['id']}/identities", json={"identity_id": iid}, headers=h)

    r = client.post(f"/bookings/{src['id']}/duplicate", headers=h)
    assert r.status_code == 201, r.text
    dup = r.json()

    assert dup["id"] != src["id"]
    assert dup["posto_id"] == "3018" and dup["nationality"] == "CPV" and dup["residence"] == "FRA"
    assert dup["settings_json"]["PROXY_KIND"] == "rotating"
    assert dup["status"] == "draft"
    workers = client.get(f"/bookings/{dup['id']}/workers", headers=h).json()
    assert workers == [], "identities must NOT be carried over -- one identity, one live allocation"


def test_duplicate_does_not_carry_run_state(client):
    client, _ = client
    """A duplicate is a NEW run. Carrying a fired-signal timestamp is exactly the bug that made
    restarted bookings skip their whole discovery phase."""
    h = _operate_headers(client, "a1")
    src = client.post("/bookings", json={"name": "o2", "type": "real", "posto_id": "3018"},
                      headers=h).json()
    dup = client.post(f"/bookings/{src['id']}/duplicate", headers=h).json()
    assert dup["started_at"] is None
    assert dup["slot_open_fired_at"] is None
    assert dup["slot_found_fired_at"] is None


def test_duplicate_names_are_unique(client):
    client, _ = client
    h = _operate_headers(client, "a1")
    src = client.post("/bookings", json={"name": "dup-me", "type": "real", "posto_id": "3018"},
                      headers=h).json()
    a = client.post(f"/bookings/{src['id']}/duplicate", headers=h).json()
    b = client.post(f"/bookings/{src['id']}/duplicate", headers=h).json()
    assert a["name"] == "dup-me (copy)"
    assert b["name"] == "dup-me (copy) 2", b["name"]


def test_duplicate_404s_on_unknown_booking(client):
    client, _ = client
    h = _operate_headers(client, "a1")
    assert client.post("/bookings/nope/duplicate", headers=h).status_code == 404


# ── re-targeting a booking's posto (2026-08-22) ──────────────────────────────
def test_posto_can_be_changed_on_a_booking(client):
    client, _ = client
    h = _operate_headers(client, "a1")
    bid = client.post("/bookings", json={"name": "b", "type": "real", "posto_id": "5084"},
                      headers=h).json()["id"]

    r = client.put(f"/bookings/{bid}", json={"posto_id": "3018"}, headers=h)
    assert r.status_code == 200
    assert r.json()["posto_id"] == "3018"
    assert client.get(f"/bookings/{bid}", headers=h).json()["posto_id"] == "3018"


def test_changing_posto_retires_slots_found_at_the_old_one(client):
    """Slots carry their own posto_id, so a pool discovered at the old consulate is not bookable at
    the new one -- an apply worker handed one would bounce every attempt."""
    from sqlalchemy import select

    from libs.db import models as M
    from libs.domain.enums import SlotStatus

    c, TestSession = client
    h = _operate_headers(c, "a1")
    bid = c.post("/bookings", json={"name": "b", "type": "real", "posto_id": "5084"},
                 headers=h).json()["id"]
    db = TestSession()
    try:
        db.add(M.Slot(booking_id=bid, posto_id="5084", date="2026-09-01", period=4,
                      status=SlotStatus.AVAILABLE))
        db.commit()
    finally:
        db.close()

    assert c.put(f"/bookings/{bid}", json={"posto_id": "3018"}, headers=h).status_code == 200

    db = TestSession()
    try:
        slots = list(db.scalars(select(M.Slot).where(M.Slot.booking_id == bid)).all())
        assert [s.status for s in slots] == [SlotStatus.CLOSED], \
            "a slot found at the old posto must not stay AVAILABLE at the new one"
        bk = db.get(M.Booking, bid)
        assert bk.slot_found_fired_at is None, "the found-latch belonged to the old posto"
    finally:
        db.close()


def test_changing_posto_is_refused_while_workers_are_running(client):
    """A RUNNING worker warmed against the OLD posto and has it frozen in its launch payload and its
    Schedule.jsp anchor. Moving the column under it does not re-target it, it desyncs it."""
    client, _ = client
    h = _operate_headers(client, "a1")
    bid = client.post("/bookings", json={"name": "b", "type": "real", "posto_id": "5084"},
                      headers=h).json()["id"]
    iid = client.post("/identities", json={"full_name": "X", "type": "real"}, headers=h).json()["id"]
    client.post(f"/bookings/{bid}/identities", json={"identity_id": iid}, headers=h)
    client.put(f"/bookings/{bid}", json={"settings": {"ENGINE_MODE": "sim"}}, headers=h)
    client.post(f"/bookings/{bid}/start", headers=h)          # worker now RUNNING

    r = client.put(f"/bookings/{bid}", json={"posto_id": "3018"}, headers=h)
    assert r.status_code == 409
    assert "RUNNING" in r.json()["detail"]
    assert client.get(f"/bookings/{bid}", headers=h).json()["posto_id"] == "5084"


def test_setting_the_same_posto_is_a_no_op(client):
    """Re-sending the current value must not retire slots or trip the running-worker guard."""
    client, _ = client
    h = _operate_headers(client, "a1")
    bid = client.post("/bookings", json={"name": "b", "type": "real", "posto_id": "5084"},
                      headers=h).json()["id"]
    r = client.put(f"/bookings/{bid}", json={"posto_id": "5084", "name": "renamed"}, headers=h)
    assert r.status_code == 200
    assert r.json()["name"] == "renamed"


# ── booking type is switchable (2026-08-22) ──────────────────────────────────
def test_switching_test_to_real_rederives_worker_roles(client):
    """The two types have different ROLE rules, so the fleet has to come with the switch. Under TEST
    the operator assigns roles; under REAL they are derived from identity type. A test identity that
    was assigned "apply" under test rules must become a scout."""
    from sqlalchemy import select

    from libs.db import models as M

    c, TestSession = client
    h = _operate_headers(c, "a1")
    bid = c.post("/bookings", json={"name": "b", "type": "test", "posto_id": "5084"},
                 headers=h).json()["id"]
    iid = c.post("/identities", json={"full_name": "T", "type": "test"}, headers=h).json()["id"]
    w = c.post(f"/bookings/{bid}/identities",
               json={"identity_id": iid, "assigned_role": "apply"}, headers=h).json()
    assert w["role"] == "apply"          # legal under TEST rules

    r = c.put(f"/bookings/{bid}", json={"type": "real"}, headers=h)
    assert r.status_code == 200 and r.json()["type"] == "real"

    db = TestSession()
    try:
        assert db.get(M.Worker, w["id"]).role == "scout", \
            "a test identity on a REAL booking must be a scout"
    finally:
        db.close()


def test_switching_real_to_test_is_refused_with_real_identities_attached(client):
    """A TEST booking may only contain TEST identities -- and silently detaching someone's fleet is
    not this endpoint's call to make."""
    client, _ = client
    h = _operate_headers(client, "a1")
    bid = client.post("/bookings", json={"name": "b", "type": "real", "posto_id": "5084"},
                      headers=h).json()["id"]
    iid = client.post("/identities", json={"full_name": "REALPERSON", "type": "real"},
                      headers=h).json()["id"]
    client.post(f"/bookings/{bid}/identities", json={"identity_id": iid}, headers=h)

    r = client.put(f"/bookings/{bid}", json={"type": "test"}, headers=h)
    assert r.status_code == 409
    assert "REALPERSON" in r.json()["detail"]
    assert client.get(f"/bookings/{bid}", headers=h).json()["type"] == "real"


def test_switching_real_to_test_is_allowed_with_only_test_identities(client):
    client, _ = client
    h = _operate_headers(client, "a1")
    bid = client.post("/bookings", json={"name": "b", "type": "real", "posto_id": "5084"},
                      headers=h).json()["id"]
    iid = client.post("/identities", json={"full_name": "T", "type": "test"}, headers=h).json()["id"]
    client.post(f"/bookings/{bid}/identities", json={"identity_id": iid}, headers=h)

    r = client.put(f"/bookings/{bid}", json={"type": "test"}, headers=h)
    assert r.status_code == 200 and r.json()["type"] == "test"


def test_type_switch_refused_while_workers_running(client):
    client, _ = client
    h = _operate_headers(client, "a1")
    bid = client.post("/bookings", json={"name": "b", "type": "real", "posto_id": "5084"},
                      headers=h).json()["id"]
    iid = client.post("/identities", json={"full_name": "T", "type": "test"}, headers=h).json()["id"]
    client.post(f"/bookings/{bid}/identities", json={"identity_id": iid}, headers=h)
    client.put(f"/bookings/{bid}", json={"settings": {"ENGINE_MODE": "sim"}}, headers=h)
    client.post(f"/bookings/{bid}/start", headers=h)

    r = client.put(f"/bookings/{bid}", json={"type": "test"}, headers=h)
    assert r.status_code == 409 and "RUNNING" in r.json()["detail"]


def test_already_booked_event_retires_the_identity(client):
    """The portal reporting an existing future appointment must retire the identity so it stops
    consuming a worker slot, a proxy and a LOGIN every run -- but must NOT invent a booking record,
    since we have no idea when that appointment is."""
    from sqlalchemy import select

    from libs.db import models as M

    c, TestSession = client
    h = _operate_headers(c, "a1")
    bid = c.post("/bookings", json={"name": "b", "type": "real", "posto_id": "5084"},
                 headers=h).json()["id"]
    iid = c.post("/identities", json={"full_name": "X", "type": "real"}, headers=h).json()["id"]
    w = c.post(f"/bookings/{bid}/identities", json={"identity_id": iid}, headers=h).json()

    # A fresh identity is EMPTY (no sub-accounts) and EMPTY -> BOOKED is correctly illegal, so put
    # it in the state a real attached identity is actually in.
    db = TestSession()
    try:
        db.get(M.Identity, iid).status = "active"
        db.commit()
    finally:
        db.close()

    import apps.hub.events as hub_events
    import apps.hub.main as hub_main
    # events.py opens its own module-level SessionLocal, so point that at this test's engine.
    hub_events.SessionLocal = TestSession
    try:
        hub_main.event_hub._mark_already_booked(
            w["id"], "This applicant already has an application")
    finally:
        pass

    db = TestSession()
    try:
        assert db.get(M.Identity, iid).status == "booked"
        assert db.scalar(select(M.BookedAppointment).where(
            M.BookedAppointment.identity_id == iid)) is None, "must not invent an appointment"
        ev = db.scalars(select(M.IdentityEvent).where(M.IdentityEvent.identity_id == iid)).all()
        assert len(ev) == 1 and ev[0].step == "already_booked_at_portal"
    finally:
        db.close()

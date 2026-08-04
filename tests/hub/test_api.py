"""Hub API tests over an in-memory SQLite DB (no Postgres needed) via FastAPI TestClient.

Exercises the domain rules through HTTP: settings-catalog, identity/booking CRUD, attach→worker
role derivation, C2 exclusivity, and settings-layer validation.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.hub.db import get_db
from apps.hub.main import app
from libs.db import metadata


@pytest.fixture()
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    metadata.create_all(engine)
    # partial unique indexes (PG/SQLite compatible) so C2 is enforced in the test DB too
    with engine.begin() as c:
        from sqlalchemy import text
        c.execute(text("CREATE UNIQUE INDEX uq_identity_active_allocation "
                       "ON identity_allocations (identity_id) WHERE released_at IS NULL"))
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _override():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_health_and_catalog(client):
    assert client.get("/health").json()["status"] == "ok"
    cat = client.get("/settings/catalog").json()
    assert cat["_meta"]["total"] > 90
    assert {"global", "booking", "template"} <= set(cat)


def test_real_booking_attach_derives_apply(client):
    iid = client.post("/identities", json={"full_name": "R", "type": "real",
                                           "passport_no": "PA1"}).json()["id"]
    bid = client.post("/bookings", json={"name": "b", "type": "real", "posto_id": "5084"}).json()["id"]
    w = client.post(f"/bookings/{bid}/identities", json={"identity_id": iid})
    assert w.status_code == 201 and w.json()["role"] == "apply"


def test_test_booking_requires_role_and_derives_scout_or_apply(client):
    iid = client.post("/identities", json={"full_name": "T", "type": "test"}).json()["id"]
    bid = client.post("/bookings", json={"name": "t", "type": "test", "posto_id": "2032"}).json()["id"]
    # no role -> 400
    assert client.post(f"/bookings/{bid}/identities", json={"identity_id": iid}).status_code == 400
    # explicit scout -> ok
    w = client.post(f"/bookings/{bid}/identities", json={"identity_id": iid, "assigned_role": "scout"})
    assert w.status_code == 201 and w.json()["role"] == "scout"


def test_test_booking_rejects_real_identity(client):
    iid = client.post("/identities", json={"full_name": "R", "type": "real"}).json()["id"]
    bid = client.post("/bookings", json={"name": "t", "type": "test", "posto_id": "2032"}).json()["id"]
    r = client.post(f"/bookings/{bid}/identities", json={"identity_id": iid, "assigned_role": "apply"})
    assert r.status_code == 400


def test_c2_identity_exclusivity_across_active_bookings(client):
    iid = client.post("/identities", json={"full_name": "R", "type": "real"}).json()["id"]
    b1 = client.post("/bookings", json={"name": "b1", "type": "real", "posto_id": "5084"}).json()["id"]
    b2 = client.post("/bookings", json={"name": "b2", "type": "real", "posto_id": "5084"}).json()["id"]
    assert client.post(f"/bookings/{b1}/identities", json={"identity_id": iid}).status_code == 201
    # second active booking must reject the same identity
    assert client.post(f"/bookings/{b2}/identities", json={"identity_id": iid}).status_code == 409


def test_booking_settings_layer_validation(client):
    # NAV_TIMEOUT_SCALE is a template knob -> illegal at booking layer
    r = client.post("/bookings", json={"name": "x", "type": "test", "posto_id": "2032",
                                       "settings": {"NAV_TIMEOUT_SCALE": 3}})
    assert r.status_code == 400
    # SCOUT_POOL is a booking knob -> accepted
    r = client.post("/bookings", json={"name": "y", "type": "test", "posto_id": "2032",
                                       "settings": {"SCOUT_POOL": True}})
    assert r.status_code == 201

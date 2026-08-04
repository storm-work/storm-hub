"""Hub API tests over in-memory SQLite (StaticPool) via FastAPI TestClient.

Covers the domain rules through HTTP AND the auth / View-Operate session model (§12c):
settings-catalog, identity/booking CRUD, attach→worker role derivation, C2 exclusivity,
settings-layer validation, operate-seat guard, and the single-operator invariant.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.hub.db import get_db
from apps.hub.main import app
from libs.db import metadata


@pytest.fixture()
def client():
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
    yield TestClient(app)
    app.dependency_overrides.clear()


def _operate_headers(client, username="admin", password="pw"):
    """Bootstrap the first admin and log in as OPERATE; return auth headers."""
    client.post("/auth/bootstrap", json={"username": username, "password": password})
    r = client.post("/auth/login", json={"username": username, "password": password, "purpose": "operate"})
    assert r.status_code == 200 and r.json()["session_status"] == "active"
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ── open endpoints ────────────────────────────────────────────────────────────
def test_health_and_catalog(client):
    assert client.get("/health").json()["status"] == "ok"
    cat = client.get("/settings/catalog").json()
    assert cat["_meta"]["total"] > 90
    assert {"global", "booking", "template"} <= set(cat)


# ── auth guards ───────────────────────────────────────────────────────────────
def test_mutations_require_operate_seat(client):
    # no token -> 401
    assert client.post("/identities", json={"full_name": "X", "type": "real"}).status_code == 401
    # view session -> 423 (operate seat required)
    client.post("/auth/bootstrap", json={"username": "a", "password": "p"})
    tok = client.post("/auth/login", json={"username": "a", "password": "p", "purpose": "view"}).json()["token"]
    r = client.post("/identities", json={"full_name": "X", "type": "real"},
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 423


def test_bootstrap_only_once(client):
    assert client.post("/auth/bootstrap", json={"username": "a", "password": "p"}).status_code == 201
    assert client.post("/auth/bootstrap", json={"username": "b", "password": "p"}).status_code == 409


def test_single_operator_second_login_paused(client):
    h = _operate_headers(client)  # admin operating
    # create an operator user, then try to operate concurrently -> paused
    client.post("/auth/users", json={"username": "op2", "password": "p", "role": "operator"}, headers=h)
    r = client.post("/auth/login", json={"username": "op2", "password": "p", "purpose": "operate"})
    assert r.status_code == 200
    assert r.json()["session_status"] == "paused"
    assert "handover" in (r.json()["notice"] or "").lower()


def test_handover_grant_transfers_the_seat(client):
    h_admin = _operate_headers(client)  # admin holds the seat
    # make op2, log in operate -> paused
    client.post("/auth/users", json={"username": "op2", "password": "p", "role": "operator"}, headers=h_admin)
    login2 = client.post("/auth/login", json={"username": "op2", "password": "p", "purpose": "operate"}).json()
    h2 = {"Authorization": f"Bearer {login2['token']}"}
    assert login2["session_status"] == "paused"
    # op2 requests handover
    req = client.post("/sessions/handover/request", headers=h2).json()
    assert req["status"] == "pending"
    # op2 cannot mutate yet
    assert client.post("/identities", json={"full_name": "z", "type": "real"}, headers=h2).status_code == 423
    # admin grants
    g = client.post(f"/sessions/handover/{req['request_id']}/grant", headers=h_admin)
    assert g.status_code == 200 and g.json()["status"] == "granted"
    # now op2 can mutate; admin can't (seat ended)
    assert client.post("/identities", json={"full_name": "z", "type": "real"}, headers=h2).status_code == 201
    assert client.post("/identities", json={"full_name": "q", "type": "real"}, headers=h_admin).status_code == 401


def test_admin_force_release(client):
    h_admin = _operate_headers(client)
    sessions = client.get("/sessions", headers=h_admin).json()
    op_sid = next(s["session_id"] for s in sessions if s["status"] == "active")
    assert client.post(f"/sessions/{op_sid}/force-release", headers=h_admin).status_code == 200


def test_viewer_role_cannot_operate(client):
    h = _operate_headers(client)
    client.post("/auth/users", json={"username": "v", "password": "p", "role": "viewer"}, headers=h)
    r = client.post("/auth/login", json={"username": "v", "password": "p", "purpose": "operate"})
    assert r.status_code == 403


# ── domain rules through HTTP (now authenticated) ─────────────────────────────
def test_real_booking_attach_derives_apply(client):
    h = _operate_headers(client)
    iid = client.post("/identities", json={"full_name": "R", "type": "real", "passport_no": "PA1"}, headers=h).json()["id"]
    bid = client.post("/bookings", json={"name": "b", "type": "real", "posto_id": "5084"}, headers=h).json()["id"]
    w = client.post(f"/bookings/{bid}/identities", json={"identity_id": iid}, headers=h)
    assert w.status_code == 201 and w.json()["role"] == "apply"


def test_test_booking_requires_role_and_derives_scout(client):
    h = _operate_headers(client)
    iid = client.post("/identities", json={"full_name": "T", "type": "test"}, headers=h).json()["id"]
    bid = client.post("/bookings", json={"name": "t", "type": "test", "posto_id": "2032"}, headers=h).json()["id"]
    assert client.post(f"/bookings/{bid}/identities", json={"identity_id": iid}, headers=h).status_code == 400
    w = client.post(f"/bookings/{bid}/identities", json={"identity_id": iid, "assigned_role": "scout"}, headers=h)
    assert w.status_code == 201 and w.json()["role"] == "scout"


def test_test_booking_rejects_real_identity(client):
    h = _operate_headers(client)
    iid = client.post("/identities", json={"full_name": "R", "type": "real"}, headers=h).json()["id"]
    bid = client.post("/bookings", json={"name": "t", "type": "test", "posto_id": "2032"}, headers=h).json()["id"]
    r = client.post(f"/bookings/{bid}/identities", json={"identity_id": iid, "assigned_role": "apply"}, headers=h)
    assert r.status_code == 400


def test_c2_identity_exclusivity(client):
    h = _operate_headers(client)
    iid = client.post("/identities", json={"full_name": "R", "type": "real"}, headers=h).json()["id"]
    b1 = client.post("/bookings", json={"name": "b1", "type": "real", "posto_id": "5084"}, headers=h).json()["id"]
    b2 = client.post("/bookings", json={"name": "b2", "type": "real", "posto_id": "5084"}, headers=h).json()["id"]
    assert client.post(f"/bookings/{b1}/identities", json={"identity_id": iid}, headers=h).status_code == 201
    assert client.post(f"/bookings/{b2}/identities", json={"identity_id": iid}, headers=h).status_code == 409


def test_booking_settings_layer_validation(client):
    h = _operate_headers(client)
    r = client.post("/bookings", json={"name": "x", "type": "test", "posto_id": "2032",
                                       "settings": {"NAV_TIMEOUT_SCALE": 3}}, headers=h)
    assert r.status_code == 400
    r = client.post("/bookings", json={"name": "y", "type": "test", "posto_id": "2032",
                                       "settings": {"SCOUT_POOL": True}}, headers=h)
    assert r.status_code == 201

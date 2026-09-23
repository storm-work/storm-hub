"""GET /bookings/{id}/portal — the portal-liveness readout that replaced the booking breaker."""
import fakeredis
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.hub.db import get_db
from apps.hub.main import app
from libs.db import metadata
from libs.bus.portal_liveness import PortalLiveness


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(text("CREATE UNIQUE INDEX uq_single_active_operate "
                       "ON user_sessions (purpose) WHERE purpose='operate' AND status='active'"))
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    app.dependency_overrides[get_db] = lambda: (lambda db: (yield db))(TestSession())

    def _override():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()
    app.dependency_overrides[get_db] = _override

    import apps.hub.main as hub_main
    from libs.bus.bus import Bus
    fr = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(hub_main, "_bus", Bus(fr))
    monkeypatch.setattr(hub_main, "SessionLocal", TestSession)

    from fastapi.testclient import TestClient
    yield TestClient(app), fr
    app.dependency_overrides.clear()


def _op(c):
    c.post("/auth/bootstrap", json={"username": "a", "password": "pw"})
    r = c.post("/auth/login", json={"username": "a", "password": "pw", "purpose": "operate"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_portal_endpoint_reports_unknown_then_down(client):
    c, fr = client
    h = _op(c)
    bid = c.post("/bookings", json={"name": "b", "type": "test", "posto_id": "5084"}, headers=h).json()["id"]

    assert c.get(f"/bookings/{bid}/portal", headers=h).json()["state"] == "unknown"

    PortalLiveness(fr, bid).record_down("acct-A")   # a worker sees the maintenance page
    body = c.get(f"/bookings/{bid}/portal", headers=h).json()
    assert body["state"] == "down" and body["down_accounts"] == 1


def test_portal_endpoint_404s_for_a_missing_booking(client):
    c, _ = client
    assert c.get("/bookings/nope/portal", headers=_op(c)).status_code == 404

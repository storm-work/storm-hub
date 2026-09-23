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


def test_workers_endpoint_surfaces_not_bookable_substate_detail(client):
    """A KEEPING worker whose session went dead is marked with NOT_BOOKABLE_PREFIX in
    substate_detail. That column was never RETURNED by the workers endpoint, so a dead 'keeping'
    worker looked identical to a healthy one on the dashboard (the 'holding is a lie' gap)."""
    from libs.db import models as M
    from libs.domain.enums import NOT_BOOKABLE_PREFIX
    c, _ = client
    h = _op(c)
    bid = c.post("/bookings", json={"name": "b", "type": "test", "posto_id": "5084"}, headers=h).json()["id"]
    iid = c.post("/identities", json={"full_name": "X", "type": "test"}, headers=h).json()["id"]
    wid = c.post(f"/bookings/{bid}/identities",
                 json={"identity_id": iid, "assigned_role": "apply"}, headers=h).json()["id"]

    import apps.hub.main as hub_main
    db = hub_main.SessionLocal()
    try:
        w = db.get(M.Worker, wid)
        w.status = "running"; w.substate = "keeping"
        w.substate_detail = f"{NOT_BOOKABLE_PREFIX} keepalive dead-trip (await_assignment)"
        db.commit()
    finally:
        db.close()

    rows = c.get(f"/bookings/{bid}/workers", headers=h).json()
    row = next(r for r in rows if r["id"] == wid)
    assert row["substate"] == "keeping"
    assert (row.get("substate_detail") or "").startswith(NOT_BOOKABLE_PREFIX), \
        "the not-bookable marker must reach the client so a dead hold is visibly distinct"


def test_clear_booking_checkpoints_is_scoped_to_that_booking(client, tmp_path, monkeypatch):
    """DELETE /bookings/{id}/checkpoints must clear ONLY this booking's identities' checkpoints --
    never another booking's, never the whole dir (the ad-hoc-rm-against-production habit, F8)."""
    import libs.engine.checkpoint as ckpt
    monkeypatch.setattr(ckpt, "_CHECKPOINTS_DIR", tmp_path)
    c, _ = client
    h = _op(c)

    # booking A with an identity + a saved checkpoint; booking B with its own
    def _booking_with_ckpt(name):
        bid = c.post("/bookings", json={"name": name, "type": "test", "posto_id": "5084"}, headers=h).json()["id"]
        iid = c.post("/identities", json={"full_name": name, "type": "test"}, headers=h).json()["id"]
        c.post(f"/bookings/{bid}/identities", json={"identity_id": iid, "assigned_role": "apply"}, headers=h)
        ckpt.save_checkpoint(iid, cookies=[], stage="alive", sub_account_id="s", username="u")
        return bid, iid

    bidA, iidA = _booking_with_ckpt("A")
    bidB, iidB = _booking_with_ckpt("B")
    assert (tmp_path / f"{iidA}.json").exists() and (tmp_path / f"{iidB}.json").exists()

    r = c.delete(f"/bookings/{bidA}/checkpoints", headers=h).json()
    assert r["cleared"] == 1 and r["identities"] == 1
    assert not (tmp_path / f"{iidA}.json").exists(), "booking A's checkpoint must be cleared"
    assert (tmp_path / f"{iidB}.json").exists(), "booking B's checkpoint must be UNTOUCHED"


def test_clear_booking_checkpoints_404s_for_a_missing_booking(client):
    c, _ = client
    assert c.delete("/bookings/nope/checkpoints", headers=_op(c)).status_code == 404

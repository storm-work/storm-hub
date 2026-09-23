"""DELETE /templates/{id} must be FK-safe. workers.template_id and template_versions.template_id
reference a template with ON DELETE NO ACTION, so a bare db.delete(tpl) on a template that was ever
launched raised an IntegrityError that surfaced as a plain-text "Internal Server Error" 500 -- the
dashboard tried to JSON.parse it ("Unexpected token 'I'...") and the Delete button silently failed
for every used template (confirmed 2026-09-23). The fix nulls the provenance FK on workers (keeping
their history), deletes the template's own versions, then deletes the template.

FK enforcement is turned ON for the sqlite engine here (it is off by default), so the OLD bare-delete
genuinely fails this test and the fix genuinely passes it.
"""
import fakeredis
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.hub.db import get_db
from apps.hub.main import app
from libs.db import metadata, models as M


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):            # sqlite enforces FKs only when asked
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    metadata.create_all(engine)
    with engine.begin() as c:
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
    monkeypatch.setattr(hub_main, "_bus", Bus(fakeredis.FakeStrictRedis(decode_responses=True)))
    monkeypatch.setattr(hub_main, "SessionLocal", TestSession)

    from fastapi.testclient import TestClient
    yield TestClient(app), TestSession
    app.dependency_overrides.clear()


def _operate_headers(c, username="admin", password="pw"):
    c.post("/auth/bootstrap", json={"username": username, "password": password})
    r = c.post("/auth/login", json={"username": username, "password": password, "purpose": "operate"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_delete_a_template_that_has_launched_workers_succeeds_and_keeps_the_workers(client):
    c, TestSession = client
    h = _operate_headers(c)
    tid = c.post("/templates", json={"name": "T headful", "role": "apply", "settings": {}},
                 headers=h).json()["id"]

    # a booking + a worker that references the template (the FK that used to block the delete)
    bid = c.post("/bookings", json={"name": "b", "type": "test", "posto_id": "5084"},
                 headers=h).json()["id"]
    iid = c.post("/identities", json={"full_name": "X", "type": "test"}, headers=h).json()["id"]
    db = TestSession()
    try:
        db.add(M.Worker(booking_id=bid, identity_id=iid, role="apply", template_id=tid))
        db.commit()
    finally:
        db.close()

    r = c.delete(f"/templates/{tid}", headers=h)
    assert r.status_code == 204, f"FK-safe delete must not 500: {r.status_code} {r.text!r}"

    db = TestSession()
    try:
        assert db.get(M.WorkerTemplate, tid) is None, "template should be gone"
        survivors = db.query(M.Worker).filter(M.Worker.booking_id == bid).all()
        assert len(survivors) == 1, "the worker history must survive the template delete"
        assert survivors[0].template_id is None, "its template_id must be nulled, not dangling"
    finally:
        db.close()


def test_delete_an_unused_template_still_works(client):
    c, _ = client
    h = _operate_headers(c)
    tid = c.post("/templates", json={"name": "T unused", "role": "scout", "settings": {}},
                 headers=h).json()["id"]
    assert c.delete(f"/templates/{tid}", headers=h).status_code == 204


def test_delete_missing_template_is_a_noop(client):
    c, _ = client
    h = _operate_headers(c)
    assert c.delete("/templates/does-not-exist", headers=h).status_code == 204

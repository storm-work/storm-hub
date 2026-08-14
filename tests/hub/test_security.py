"""Production-hardening tests: login rate limiting, audit logging, docs lockdown.

Uses the same in-memory-SQLite `client` fixture pattern as test_api.py. The login rate limiter is
Redis-backed against the real Redis instance (same as the rest of the Hub's bus/scheduler tests),
keyed by client IP -- TestClient's fixed synthetic IP ("testclient") means the fail counter must be
flushed before/after each test that touches it, or runs would bleed into each other.
"""
from sqlalchemy import select
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import apps.hub.main as hub_main
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
    # _launch_worker_fleet (POST /bookings/{id}/start) opens its own SessionLocal() per background
    # thread instead of using Depends(get_db) -- point it at this test's isolated engine too.
    monkeypatch.setattr(hub_main, "SessionLocal", TestSession)
    hub_main._bus.r.delete("loginfail:testclient")
    yield TestClient(app), TestSession
    hub_main._bus.r.delete("loginfail:testclient")
    app.dependency_overrides.clear()


def test_login_rate_limit_locks_out_after_max_attempts(client):
    c, _ = client
    c.post("/auth/bootstrap", json={"username": "admin", "password": "correct-horse"})
    for _ in range(hub_main._LOGIN_MAX_ATTEMPTS):
        r = c.post("/auth/login", json={"username": "admin", "password": "wrong", "purpose": "view"})
        assert r.status_code == 401
    # one more, still within the same window -> locked out at the app level, not just 401
    r = c.post("/auth/login", json={"username": "admin", "password": "wrong", "purpose": "view"})
    assert r.status_code == 429
    assert "too many failed" in r.json()["detail"]
    # even the CORRECT password is blocked while locked out
    r = c.post("/auth/login", json={"username": "admin", "password": "correct-horse", "purpose": "view"})
    assert r.status_code == 429


def test_successful_login_clears_the_fail_counter(client):
    c, _ = client
    c.post("/auth/bootstrap", json={"username": "admin", "password": "correct-horse"})
    for _ in range(hub_main._LOGIN_MAX_ATTEMPTS - 1):
        assert c.post("/auth/login", json={"username": "admin", "password": "wrong",
                                           "purpose": "view"}).status_code == 401
    ok = c.post("/auth/login", json={"username": "admin", "password": "correct-horse", "purpose": "view"})
    assert ok.status_code == 200
    # counter reset -- another wrong attempt right after a success is just a normal 401, not 429
    r = c.post("/auth/login", json={"username": "admin", "password": "wrong", "purpose": "view"})
    assert r.status_code == 401


def test_failed_and_successful_logins_are_audited(client):
    c, TestSession = client
    c.post("/auth/bootstrap", json={"username": "admin", "password": "correct-horse"})
    c.post("/auth/login", json={"username": "admin", "password": "wrong", "purpose": "view"})
    c.post("/auth/login", json={"username": "admin", "password": "correct-horse", "purpose": "view"})

    from libs.db import models as M
    from sqlalchemy import select
    db = TestSession()
    try:
        rows = list(db.scalars(select(M.AuditRow).order_by(M.AuditRow.ts.asc())).all())
    finally:
        db.close()
    actions = [r.action for r in rows]
    assert "login_failed" in actions
    assert "login_ok" in actions
    failed = next(r for r in rows if r.action == "login_failed")
    ok = next(r for r in rows if r.action == "login_ok")
    assert failed.actor == "admin" and ok.actor == "admin"
    assert failed.payload["ip"] and ok.payload["ip"]


def test_mutating_endpoint_is_audited_generically(client):
    c, TestSession = client
    c.post("/auth/bootstrap", json={"username": "admin", "password": "pw"})
    tok = c.post("/auth/login", json={"username": "admin", "password": "pw",
                                       "purpose": "operate"}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    r = c.post("/identities", json={"full_name": "Audit Test", "type": "real"}, headers=h)
    assert r.status_code == 201

    from libs.db import models as M
    from sqlalchemy import select
    db = TestSession()
    try:
        rows = list(db.scalars(select(M.AuditRow).where(M.AuditRow.target == "/identities")).all())
    finally:
        db.close()
    assert len(rows) == 1
    assert rows[0].actor == "admin"
    assert rows[0].action == "POST"
    assert rows[0].target == "/identities"
    assert rows[0].payload["status"] == 201


def test_audit_survives_a_long_resolved_path(client):
    """Regression: AuditRow.action is VARCHAR(64) -- some routes' resolved paths (a real 36-char
    uuid substituted for {booking_id}) exceed 64 chars combined with an HTTP-method prefix, which
    would silently fail to insert (caught by the middleware's bare except) if action ever went back
    to "METHOD path". Exercises the longest route in the app end to end."""
    c, TestSession = client
    c.post("/auth/bootstrap", json={"username": "admin", "password": "pw"})
    tok = c.post("/auth/login", json={"username": "admin", "password": "pw",
                                       "purpose": "operate"}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    bid = c.post("/bookings", json={"name": "b", "type": "real", "posto_id": "5084"},
                 headers=h).json()["id"]
    r = c.post(f"/bookings/{bid}/start", headers=h)
    assert r.status_code == 200

    from libs.db import models as M
    from sqlalchemy import select
    db = TestSession()
    try:
        row = db.scalars(
            select(M.AuditRow).where(M.AuditRow.target == f"/bookings/{bid}/start")
        ).first()
    finally:
        db.close()
    assert row is not None
    assert row.action == "POST"


def test_get_requests_are_not_audited(client):
    c, TestSession = client
    c.post("/auth/bootstrap", json={"username": "admin", "password": "pw"})  # 1 audit row (POST)
    c.get("/identities")  # GET -- must add none

    from libs.db import models as M
    from sqlalchemy import select, func
    db = TestSession()
    try:
        n = db.scalar(select(func.count()).select_from(M.AuditRow))
    finally:
        db.close()
    assert n == 1


def test_docs_are_disabled_by_default(client):
    c, _ = client
    assert c.get("/docs").status_code == 404
    assert c.get("/redoc").status_code == 404
    assert c.get("/openapi.json").status_code == 404


# ── viewer role (2026-08-14) ──────────────────────────────────────────────────
def _admin(c, username="admin", password="pw-admin"):
    c.post("/auth/bootstrap", json={"username": username, "password": password})
    r = c.post("/auth/login", json={"username": username, "password": password, "purpose": "operate"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_admin_can_create_a_viewer_and_viewer_is_blocked_from_admin_surfaces(client):
    """The dashboard hides templates/settings/ops/audit/sessions from a viewer, but hiding a tab is
    not a security boundary -- the server is. This pins the half that actually protects them."""
    c, _ = client
    h = _admin(c)
    r = c.post("/auth/users",
               json={"username": "watcher", "password": "pw-viewer", "role": "viewer"}, headers=h)
    assert r.status_code == 201 and r.json()["role"] == "viewer"

    lr = c.post("/auth/login",
                json={"username": "watcher", "password": "pw-viewer", "purpose": "view"})
    assert lr.status_code == 200
    vh = {"Authorization": f"Bearer {lr.json()['token']}"}

    who = c.get("/auth/whoami", headers=vh).json()
    assert who["role"] == "viewer" and who["is_operating"] is False

    assert c.post("/auth/users", json={"username": "x", "password": "y", "role": "viewer"},
                  headers=vh).status_code == 403          # admin-only
    # 423 Locked in practice: a viewer logs in with purpose=view and so never holds the operate
    # seat, and the seat check fires before the role check. 403 would be equally correct if the
    # ordering changed -- what matters is that the mutation is refused.
    assert c.post("/bookings", json={"name": "n", "type": "real", "posto_id": "5084"},
                  headers=vh).status_code in (403, 423)
    assert c.get("/bookings", headers=vh).status_code == 200   # reading is fine


def test_viewer_never_holds_the_operate_seat(client):
    c, _ = client
    h = _admin(c)
    c.post("/auth/users", json={"username": "watcher2", "password": "pw", "role": "viewer"}, headers=h)
    r = c.post("/auth/login", json={"username": "watcher2", "password": "pw", "purpose": "operate"})
    if r.status_code == 200:
        vh = {"Authorization": f"Bearer {r.json()['token']}"}
        assert c.get("/auth/whoami", headers=vh).json()["is_operating"] is False
    else:
        assert r.status_code in (403, 409)


def test_dashboard_hides_exactly_the_pages_a_viewer_cannot_use(client):
    """Pins the JS gate list against this file's server-side expectations, so hiding a tab and
    blocking its endpoints can't drift apart."""
    import re
    from pathlib import Path

    src = Path("apps/dashboard/src/App.jsx").read_text()
    m = re.search(r"const HIDDEN_FOR_VIEWER = new Set\(\[(.*?)\]\)", src, re.S)
    assert m, "HIDDEN_FOR_VIEWER not found in App.jsx -- renamed?"
    hidden = set(re.findall(r'"(\w+)"', m.group(1)))
    assert hidden == {"templates", "settings", "ops", "audit", "sessions"}


# ── stale operate seat (confirmed live 2026-08-14) ────────────────────────────
def test_a_quiet_operate_seat_is_released_and_a_waiting_login_takes_over(client):
    """An operate seat is held by an ACTIVE session and only one may exist, but nothing ever expired
    one -- so a browser tab closed hours earlier kept the lock and every later login sat PAUSED
    behind it. Confirmed live: a session last seen at 03:36 still held the seat at 09:16, so the
    operator's own 08:47 login was read-only. Every settings field and Save button silently
    disabled, which reads as "saving is broken"."""
    from datetime import timedelta

    import apps.hub.main as hub_main
    from libs.db import models as M
    from libs.domain.enums import SessionPurpose, SessionStatus

    c, TestSession = client
    h = _admin(c)          # holds the seat, ACTIVE

    db = TestSession()
    try:
        held = db.scalars(select(M.UserSession).where(
            M.UserSession.purpose == SessionPurpose.OPERATE,
            M.UserSession.status == SessionStatus.ACTIVE)).one()
        held.last_seen = hub_main._now() - timedelta(seconds=hub_main.OPERATE_SEAT_STALE_AFTER + 60)
        db.commit()
        stale_id = held.id
    finally:
        db.close()

    # a second operate login is paused behind the stale seat
    r = c.post("/auth/login", json={"username": "admin", "password": "pw-admin", "purpose": "operate"})
    assert r.status_code == 200
    h2 = {"Authorization": f"Bearer {r.json()['token']}"}

    who = c.get("/auth/whoami", headers=h2).json()      # polling whoami reclaims it
    assert who["is_operating"] is True
    assert who["session_status"] == "active"

    db = TestSession()
    try:
        assert db.get(M.UserSession, stale_id).status == SessionStatus.ENDED
    finally:
        db.close()


def test_a_live_operator_is_never_bumped(client):
    """Only a seat that has gone quiet is reclaimable -- an operator actively polling keeps it."""
    import apps.hub.main as hub_main
    from libs.db import models as M
    from libs.domain.enums import SessionPurpose, SessionStatus

    c, TestSession = client
    h = _admin(c)
    assert c.get("/auth/whoami", headers=h).json()["is_operating"] is True

    r = c.post("/auth/login", json={"username": "admin", "password": "pw-admin", "purpose": "operate"})
    h2 = {"Authorization": f"Bearer {r.json()['token']}"}
    assert c.get("/auth/whoami", headers=h2).json()["is_operating"] is False   # still paused

    db = TestSession()
    try:
        actives = db.scalars(select(M.UserSession).where(
            M.UserSession.purpose == SessionPurpose.OPERATE,
            M.UserSession.status == SessionStatus.ACTIVE)).all()
        assert len(actives) == 1      # the invariant holds
    finally:
        db.close()

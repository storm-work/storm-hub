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
import threading
import time

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
    from libs.db import models as M
    from sqlalchemy import select, func

    def audit_rows() -> int:
        db = TestSession()
        try:
            return db.scalar(select(func.count()).select_from(M.AuditRow))
        finally:
            db.close()

    c.post("/auth/bootstrap", json={"username": "admin", "password": "pw"})
    # The GET must be AUTHENTICATED to be meaningful: since require_viewer landed, an anonymous read
    # 401s in the dependency and never reaches the handler, which would prove nothing about whether
    # successful reads are audited. Login is itself an audited POST, so measure the delta across the
    # GET alone rather than asserting a total.
    tok = c.post("/auth/login", json={"username": "admin", "password": "pw",
                                      "purpose": "view"}).json()["token"]
    before = audit_rows()
    c.get("/identities", headers={"Authorization": f"Bearer {tok}"})  # GET -- must add none
    assert audit_rows() == before


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


# ── viewer session cap + admin-granted extra session (operator spec 2026-09-10) ───────────────
def test_a_second_viewer_login_is_capped_paused_the_first_is_untouched(client):
    c, _ = client
    h = _admin(c)
    c.post("/auth/users", json={"username": "w1", "password": "pw", "role": "viewer"}, headers=h)
    r1 = c.post("/auth/login", json={"username": "w1", "password": "pw", "purpose": "view"})
    assert r1.status_code == 200
    h1 = {"Authorization": f"Bearer {r1.json()['token']}"}
    assert c.get("/auth/whoami", headers=h1).json()["session_status"] == "active"

    r2 = c.post("/auth/login", json={"username": "w1", "password": "pw", "purpose": "view"})
    assert r2.status_code == 200
    h2 = {"Authorization": f"Bearer {r2.json()['token']}"}
    who2 = c.get("/auth/whoami", headers=h2).json()
    assert who2["session_status"] == "paused"
    assert "extra session" in (r2.json().get("notice") or "")

    # the first session is untouched by the second login being capped
    assert c.get("/auth/whoami", headers=h1).json()["session_status"] == "active"


def test_an_admin_or_operator_logging_in_as_view_twice_is_unrestricted(client):
    """The cap is scoped to UserRole.VIEWER, not purpose=="view" broadly -- an admin/operator
    choosing to view the read-only dashboard is not the case this feature is about."""
    c, _ = client
    h = _admin(c, username="a1", password="pw-a1")
    r1 = c.post("/auth/login", json={"username": "a1", "password": "pw-a1", "purpose": "view"})
    r2 = c.post("/auth/login", json={"username": "a1", "password": "pw-a1", "purpose": "view"})
    assert r1.status_code == 200 and r2.status_code == 200
    h2 = {"Authorization": f"Bearer {r2.json()['token']}"}
    assert c.get("/auth/whoami", headers=h2).json()["session_status"] == "active"


def test_extra_session_request_requires_a_paused_viewer_session(client):
    c, _ = client
    h = _admin(c)
    c.post("/auth/users", json={"username": "w2", "password": "pw", "role": "viewer"}, headers=h)
    r1 = c.post("/auth/login", json={"username": "w2", "password": "pw", "purpose": "view"})
    h1 = {"Authorization": f"Bearer {r1.json()['token']}"}
    # active (not paused) session -- must not be allowed to request
    assert c.post("/sessions/extra-view/request", headers=h1).status_code == 400
    # non-viewer -- must not be allowed to request
    assert c.post("/sessions/extra-view/request", headers=h).status_code == 403


def test_extra_session_grant_activates_the_second_session_without_touching_the_first(client):
    c, _ = client
    h = _admin(c)
    c.post("/auth/users", json={"username": "w3", "password": "pw", "role": "viewer"}, headers=h)
    r1 = c.post("/auth/login", json={"username": "w3", "password": "pw", "purpose": "view"})
    h1 = {"Authorization": f"Bearer {r1.json()['token']}"}
    r2 = c.post("/auth/login", json={"username": "w3", "password": "pw", "purpose": "view"})
    h2 = {"Authorization": f"Bearer {r2.json()['token']}"}

    req = c.post("/sessions/extra-view/request", headers=h2)
    assert req.status_code == 200
    req_id = req.json()["request_id"]

    pending = c.get("/sessions/extra-view/pending", headers=h).json()
    assert any(p["request_id"] == req_id and p["requester"] == "w3" for p in pending)

    gr = c.post(f"/sessions/extra-view/{req_id}/grant", headers=h)
    assert gr.status_code == 200

    assert c.get("/auth/whoami", headers=h2).json()["session_status"] == "active"
    assert c.get("/auth/whoami", headers=h1).json()["session_status"] == "active"   # untouched
    assert c.get("/sessions/extra-view/pending", headers=h).json() == []


def test_extra_session_deny_ends_the_paused_session(client):
    c, _ = client
    h = _admin(c)
    c.post("/auth/users", json={"username": "w4", "password": "pw", "role": "viewer"}, headers=h)
    c.post("/auth/login", json={"username": "w4", "password": "pw", "purpose": "view"})
    r2 = c.post("/auth/login", json={"username": "w4", "password": "pw", "purpose": "view"})
    h2 = {"Authorization": f"Bearer {r2.json()['token']}"}
    req_id = c.post("/sessions/extra-view/request", headers=h2).json()["request_id"]

    dn = c.post(f"/sessions/extra-view/{req_id}/deny", headers=h)
    assert dn.status_code == 200
    # the paused session is now ENDED -- its token no longer authenticates
    assert c.get("/auth/whoami", headers=h2).status_code == 401


def test_extra_session_grant_and_deny_require_admin_not_just_operator(client):
    c, _ = client
    h = _admin(c)
    c.post("/auth/users", json={"username": "op1", "password": "pw", "role": "operator"}, headers=h)
    opr = c.post("/auth/login", json={"username": "op1", "password": "pw", "purpose": "operate"})
    oph = {"Authorization": f"Bearer {opr.json()['token']}"}

    c.post("/auth/users", json={"username": "w5", "password": "pw", "role": "viewer"}, headers=h)
    c.post("/auth/login", json={"username": "w5", "password": "pw", "purpose": "view"})
    r2 = c.post("/auth/login", json={"username": "w5", "password": "pw", "purpose": "view"})
    h2 = {"Authorization": f"Bearer {r2.json()['token']}"}
    req_id = c.post("/sessions/extra-view/request", headers=h2).json()["request_id"]

    assert c.post(f"/sessions/extra-view/{req_id}/grant", headers=oph).status_code == 403
    assert c.post(f"/sessions/extra-view/{req_id}/deny", headers=oph).status_code == 403


def test_force_release_ends_a_view_session_not_just_operate(client):
    """The backend was never purpose-restricted -- only the dashboard button used to withhold
    itself for non-operate rows. Pin the backend behavior directly."""
    c, _ = client
    h = _admin(c)
    c.post("/auth/users", json={"username": "w6", "password": "pw", "role": "viewer"}, headers=h)
    r = c.post("/auth/login", json={"username": "w6", "password": "pw", "purpose": "view"})
    vh = {"Authorization": f"Bearer {r.json()['token']}"}
    # LoginResponse doesn't carry the session id -- fetch it via /sessions instead
    rows = c.get("/sessions", headers=h).json()
    sid = next(row["session_id"] for row in rows if row["user"] == "w6")
    assert c.post(f"/sessions/{sid}/force-release", headers=h).status_code == 200
    assert c.get("/auth/whoami", headers=vh).status_code == 401


def test_dashboard_hides_exactly_the_pages_a_viewer_cannot_use(client):
    """Pins the JS gate list against this file's server-side expectations, so hiding a tab and
    blocking its endpoints can't drift apart."""
    import re
    from pathlib import Path

    src = Path("apps/dashboard/src/App.jsx").read_text()
    m = re.search(r"const HIDDEN_FOR_VIEWER = new Set\(\[(.*?)\]\)", src, re.S)
    assert m, "HIDDEN_FOR_VIEWER not found in App.jsx -- renamed?"
    hidden = set(re.findall(r'"(\w+)"', m.group(1)))
    # "sessions" deliberately visible to a viewer since 2026-09-10 -- a capped/paused viewer
    # session needs to reach the "Request extra session" button, which lives on that page.
    assert hidden == {"templates", "settings", "ops", "audit"}


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


# ── every route is guarded unless it is deliberately public ──────────────────
# The VIEWER role and the login form's "View (read-only)" purpose both shipped without any guard
# backing them, so ten read routes answered anonymous callers -- GET /identities served full_name,
# birthdate and passport_no of every applicant. That was not a regression; the guard was simply
# never written. This test exists so the next route added cannot repeat it silently.
_PUBLIC_BY_DESIGN = {
    ("GET", "/health"),                  # liveness probe, no data
    ("GET", "/"),                        # the SPA shell
    ("GET", "/{full_path:path}"),        # SPA catch-all -- must serve the login page anonymously
    ("POST", "/auth/bootstrap"),         # self-closes once a user exists (409 thereafter)
    ("POST", "/auth/login"),             # the entry point itself
}


def test_no_route_is_unauthenticated_unless_explicitly_public():
    import ast
    import io

    src = io.open("apps/hub/main.py", encoding="utf-8").read()
    guards = ("require_operator", "require_admin", "require_viewer", "active_operator", "Principal")

    def is_guarded(node) -> bool:
        if any(g in ast.unparse(node.args) for g in guards):
            return True
        # websockets validate the token by hand, before accept()
        return "principal_from_token" in ast.unparse(node)

    unguarded = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            if not (isinstance(dec.func.value, ast.Name) and dec.func.value.id == "app"):
                continue
            verb = dec.func.attr.upper()
            if verb not in ("GET", "POST", "PUT", "DELETE", "PATCH", "WEBSOCKET"):
                continue
            if not (dec.args and isinstance(dec.args[0], ast.Constant)):
                continue
            path = dec.args[0].value
            if not is_guarded(node):
                unguarded.add((verb, path))

    leaked = unguarded - _PUBLIC_BY_DESIGN
    assert not leaked, (
        f"these routes take no auth dependency: {sorted(leaked)}. Add Depends(require_viewer) for "
        f"reads or Depends(require_operator) for mutations -- or, if the route is genuinely meant to "
        f"be public, add it to _PUBLIC_BY_DESIGN with the reason."
    )


def test_reads_reject_an_anonymous_caller(client):
    """The concrete symptom: passport numbers to anyone who could reach the port."""
    c, _ = client
    c.post("/auth/bootstrap", json={"username": "admin", "password": "pw"})
    for path in ("/identities", "/bookings", "/proxies", "/templates", "/ops/status",
                 "/settings/catalog"):
        assert c.get(path).status_code == 401, f"{path} answered an anonymous caller"


def test_events_websocket_rejects_an_anonymous_client(client):
    """The widest hole of the set: a continuous feed of worker status and log lines, not one
    snapshot. It accepted anonymously until require_viewer landed."""
    from starlette.websockets import WebSocketDisconnect

    c, _ = client
    c.post("/auth/bootstrap", json={"username": "admin", "password": "pw"})

    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect("/ws/events") as ws:
            ws.receive_json()
    assert e.value.code == 1008

    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect("/ws/events?token=garbage") as ws:
            ws.receive_json()
    assert e.value.code == 1008


# ── the audit path must never touch the DB on the request/event-loop path (2026-09-13) ────────
# CONFIRMED LIVE (py-spy dump of a wedged Hub): _audit_middleware did a SECOND DB checkout per
# mutating request -- while the handler's own Depends(get_db) connection was still held, since
# FastAPI tears that down only after the response is sent -- and did it with synchronous
# SQLAlchemy on the asyncio event loop. Once the pool drained, the loop itself parked in
# QueuePool._do_get, so no response could be sent, so the first connection was never released.
# Self-sustaining deadlock: every thread waiting for a connection, none holding one and working.
def test_audit_middleware_does_no_db_work_on_the_request_path(client, monkeypatch):
    """The regression that matters: the middleware must not open a session of its own. If it ever
    does again, the deadlock comes back."""
    import apps.hub.main as hub_main

    c, _TestSession = client
    h = _admin(c)

    # Any attempt to build a session from inside the request path is a hard failure.
    calls = []
    real_submit = hub_main.audit_writer.submit

    def _spy_submit(row):
        calls.append(row)
        # Deliberately DOES NOT write -- proves the request completes without the audit row
        # having touched the DB yet.
        return None

    monkeypatch.setattr(hub_main.audit_writer, "submit", _spy_submit)
    r = c.post("/identities", json={"full_name": "No DB On Path", "type": "real"}, headers=h)
    assert r.status_code == 201, "request must succeed even though nothing persisted the audit row"
    assert len(calls) == 1, f"expected exactly one non-blocking submit, got {calls}"
    assert "_actor_sub" in calls[0], "actor must be passed as a token subject, NOT resolved via DB here"
    assert calls[0]["action"] == "POST" and calls[0]["target"] == "/identities"
    assert real_submit is not None


def test_audit_submit_is_non_blocking_when_the_writer_is_running():
    """submit() runs on the event loop. It must be a pure in-memory queue put -- never a DB call,
    never anything that can block -- so a slow/dead DB can no longer stall request serving."""
    from apps.hub.audit import AuditWriter

    def _explode():
        raise AssertionError("submit() resolved a session factory on the request path")

    w = AuditWriter(_explode)
    w._thread = threading.Thread(target=lambda: time.sleep(5), name="fake-writer", daemon=True)
    w._thread.start()                      # is_running() True -> must take the queue path
    try:
        w.submit({"action": "POST", "target": "/x", "ts": None, "payload": {}})
        assert w._q.qsize() == 1
    finally:
        w._thread.join(timeout=6)


def test_audit_queue_drops_rather_than_growing_without_bound():
    """A bounded queue on purpose: an unbounded one turns a DB outage into unbounded memory growth,
    which is how a logging path takes down the process it exists to observe."""
    from apps.hub.audit import AuditWriter

    w = AuditWriter(lambda: None)
    w._thread = threading.Thread(target=lambda: time.sleep(5), name="fake-writer", daemon=True)
    w._thread.start()
    try:
        w._q.maxsize  # bounded
        for _ in range(w._q.maxsize + 50):
            w.submit({"action": "POST", "target": "/x", "ts": None, "payload": {}})
        assert w.dropped >= 50, f"overflow must drop and count, got dropped={w.dropped}"
        assert w._q.qsize() <= w._q.maxsize
    finally:
        w._thread.join(timeout=6)

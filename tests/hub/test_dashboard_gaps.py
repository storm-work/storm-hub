"""Backend endpoints added to close the dashboard-feature gaps identified against the recovered
conversation history (identity edit/archive/delete, proxy toggle, booking detach, worker restart,
audit viewer, booked-PDF persistence/serving)."""
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
    yield TestClient(app), TestSession
    app.dependency_overrides.clear()


def _op_headers(c, username="admin", password="pw"):
    c.post("/auth/bootstrap", json={"username": username, "password": password})
    r = c.post("/auth/login", json={"username": username, "password": password, "purpose": "operate"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ── identities: edit / archive / delete ────────────────────────────────────────
def test_identity_response_includes_birthdate_and_gender(client):
    """Regression: birthdate/gender were stored (IdentityCreate accepted + persisted them) but
    IdentityOut never returned them, so the dashboard's identity detail panel had no data to show
    for two fields it otherwise rendered a label for -- confirmed missing by the user, not a
    frontend rendering bug."""
    c, _ = client
    h = _op_headers(c)
    r = c.post("/identities", json={"full_name": "Ana Silva", "type": "real",
                                    "birthdate": "1995-04-02", "gender": "F"}, headers=h)
    assert r.status_code == 201
    body = r.json()
    assert body["birthdate"] == "1995-04-02"
    assert body["gender"] == "F"

    listed = next(i for i in c.get("/identities", headers=h).json() if i["id"] == body["id"])
    assert listed["birthdate"] == "1995-04-02"
    assert listed["gender"] == "F"


def test_identity_update_comment_and_target_posto(client):
    c, _ = client
    h = _op_headers(c)
    iid = c.post("/identities", json={"full_name": "Real One", "type": "real"}, headers=h).json()["id"]
    r = c.patch(f"/identities/{iid}", json={"comment": "vip", "target_posto": "5084"}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["comment"] == "vip" and body["target_posto"] == "5084"


def test_identity_target_posto_rejected_for_test_identity(client):
    c, _ = client
    h = _op_headers(c)
    iid = c.post("/identities", json={"full_name": "Test One", "type": "test"}, headers=h).json()["id"]
    r = c.patch(f"/identities/{iid}", json={"target_posto": "5084"}, headers=h)
    assert r.status_code == 400


def test_identity_archive(client):
    c, _ = client
    h = _op_headers(c)
    iid = c.post("/identities", json={"full_name": "X", "type": "test"}, headers=h).json()["id"]
    r = c.post(f"/identities/{iid}/archive", headers=h)
    assert r.status_code == 200
    assert r.json()["status"] == "archived"


def test_identity_delete_blocked_when_actively_allocated(client):
    c, _ = client
    h = _op_headers(c)
    iid = c.post("/identities", json={"full_name": "X", "type": "test"}, headers=h).json()["id"]
    bid = c.post("/bookings", json={"name": "b", "type": "test", "posto_id": "2032"}, headers=h).json()["id"]
    c.post(f"/bookings/{bid}/identities", json={"identity_id": iid, "assigned_role": "scout"}, headers=h)
    r = c.delete(f"/identities/{iid}", headers=h)
    assert r.status_code == 409


def test_identity_delete_when_unallocated(client):
    c, _ = client
    h = _op_headers(c)
    iid = c.post("/identities", json={"full_name": "X", "type": "test"}, headers=h).json()["id"]
    r = c.delete(f"/identities/{iid}", headers=h)
    assert r.status_code == 204
    assert c.get("/identities", headers=h).json() == []


# ── proxies: toggle active ──────────────────────────────────────────────────────
def test_proxy_toggle_active(client):
    c, _ = client
    h = _op_headers(c)
    pid = c.post("/proxies", json={"provider": "isp", "kind": "static_res",
                                   "endpoint": "1.2.3.4:8080"}, headers=h).json()["id"]
    assert c.get("/proxies", headers=h).json()[0]["active"] is True
    r = c.post(f"/proxies/{pid}/toggle-active", headers=h)
    assert r.status_code == 200 and r.json()["active"] is False
    r2 = c.post(f"/proxies/{pid}/toggle-active", headers=h)
    assert r2.json()["active"] is True


# ── bookings: detach identity ────────────────────────────────────────────────────
def test_detach_identity_removes_worker_and_frees_allocation(client):
    c, _ = client
    h = _op_headers(c)
    iid = c.post("/identities", json={"full_name": "X", "type": "test"}, headers=h).json()["id"]
    bid = c.post("/bookings", json={"name": "b", "type": "test", "posto_id": "2032"}, headers=h).json()["id"]
    c.post(f"/bookings/{bid}/identities", json={"identity_id": iid, "assigned_role": "scout"}, headers=h)
    assert len(c.get(f"/bookings/{bid}/workers", headers=h).json()) == 1

    r = c.delete(f"/bookings/{bid}/identities/{iid}", headers=h)
    assert r.status_code == 200
    assert c.get(f"/bookings/{bid}/workers", headers=h).json() == []

    # freed -- can now delete the identity outright, and can re-attach to a fresh booking
    assert c.delete(f"/identities/{iid}", headers=h).status_code == 204


def test_detach_identity_with_a_proxy_lease_does_not_500(client):
    """Regression, confirmed live 2026-08-14: release_proxy() only sets released_at -- it does NOT
    delete the ProxyLease row -- so the row still held a live FK to worker.id. On the real Postgres
    DB, the very next line (db.delete(worker)) always raised a ForeignKeyViolation for any worker
    that had ever actually leased a proxy, coming back as a raw 500 with a non-JSON body
    (Starlette's default, since nothing caught it) -- which is what the dashboard's "Unexpected
    token" error actually was. This test runs on SQLite (this file's fixture), which doesn't
    enforce FKs by default and so can't reproduce the crash itself -- but it still catches the
    underlying bug's other half: the ProxyLease row being silently leaked/orphaned instead of
    cleaned up, which the assertion below checks directly."""
    c, TestSession = client
    h = _op_headers(c)
    iid = c.post("/identities", json={"full_name": "X", "type": "test"}, headers=h).json()["id"]
    bid = c.post("/bookings", json={"name": "b", "type": "test", "posto_id": "2032"}, headers=h).json()["id"]
    wid = c.post(f"/bookings/{bid}/identities",
                json={"identity_id": iid, "assigned_role": "apply"}, headers=h).json()["id"]

    from libs.db import models as M
    from libs.proxies import lease_proxy
    db = TestSession()
    try:
        db.add(M.Proxy(id="px1", provider="soax", kind="static", endpoint="h:1",
                       credentials_ref="u:p", max_sessions=1))
        db.commit()
        lease_proxy(db, wid, "soax")
    finally:
        db.close()

    r = c.delete(f"/bookings/{bid}/identities/{iid}", headers=h)
    assert r.status_code == 200
    assert c.get(f"/bookings/{bid}/workers", headers=h).json() == []

    db = TestSession()
    try:
        assert db.query(M.ProxyLease).filter(M.ProxyLease.worker_id == wid).count() == 0
    finally:
        db.close()


# ── workers: restart ──────────────────────────────────────────────────────────
def test_worker_restart_relaunches(client):
    c, _ = client
    h = _op_headers(c)
    iid = c.post("/identities", json={"full_name": "X", "type": "test"}, headers=h).json()["id"]
    bid = c.post("/bookings", json={"name": "b", "type": "test", "posto_id": "2032"}, headers=h).json()["id"]
    wid = c.post(f"/bookings/{bid}/identities",
                 json={"identity_id": iid, "assigned_role": "scout"}, headers=h).json()["id"]
    r = c.post(f"/workers/{wid}/restart", headers=h)
    assert r.status_code == 200
    assert r.json()["status"] == "running"


# ── audit ────────────────────────────────────────────────────────────────────
def test_audit_listing_requires_auth_and_returns_recent_rows(client):
    c, _ = client
    assert c.get("/audit").status_code == 401
    h = _op_headers(c)
    c.post("/identities", json={"full_name": "X", "type": "test"}, headers=h)
    rows = c.get("/audit", headers=h).json()
    assert len(rows) >= 1
    assert any(row["target"] == "/identities" for row in rows)


# ── artifacts / booked appointments ─────────────────────────────────────────────
def test_artifact_and_appointment_endpoints_require_auth(client):
    c, _ = client
    h = _op_headers(c)
    bid = c.post("/bookings", json={"name": "b", "type": "test", "posto_id": "2032"}, headers=h).json()["id"]
    assert c.get(f"/bookings/{bid}/appointments").status_code == 401
    assert c.get(f"/bookings/{bid}/appointments", headers=h).json() == []
    assert c.get("/artifacts/nonexistent").status_code == 401
    assert c.get("/artifacts/nonexistent", headers=h).status_code == 404


def test_slots_endpoint_requires_auth_and_reflects_persisted_slots(client):
    """Regression: libs.db.models.Slot has been written by events.py's _persist_slots since the
    scout pool was built, but no read endpoint ever exposed those rows -- the dashboard's Slots tab
    is the first consumer of GET /bookings/{id}/slots."""
    c, TestSession = client
    h = _op_headers(c)
    iid = c.post("/identities", json={"full_name": "X", "type": "test"}, headers=h).json()["id"]
    bid = c.post("/bookings", json={"name": "b", "type": "test", "posto_id": "2032"}, headers=h).json()["id"]
    wid = c.post(f"/bookings/{bid}/identities",
                 json={"identity_id": iid, "assigned_role": "scout"}, headers=h).json()["id"]

    assert c.get(f"/bookings/{bid}/slots").status_code == 401
    assert c.get(f"/bookings/{bid}/slots", headers=h).json() == []

    import apps.hub.db as hub_db
    import apps.hub.events as events_mod
    orig = hub_db.SessionLocal
    hub_db.SessionLocal = TestSession
    events_mod.SessionLocal = TestSession
    try:
        hub = events_mod.EventHub.__new__(events_mod.EventHub)
        hub._persist_slots(wid, [{"date": "2026-08-20", "period": 4}, {"date": "2026-08-21", "period": 1}])
    finally:
        hub_db.SessionLocal = orig
        events_mod.SessionLocal = orig

    slots = c.get(f"/bookings/{bid}/slots", headers=h).json()
    assert len(slots) == 2
    assert slots[0]["date"] == "2026-08-20" and slots[0]["period"] == 4
    assert slots[0]["status"] == "available"
    assert slots[0]["discovered_by"] == wid
    assert slots[0]["posto_id"] == "2032"


def test_release_on_terminal_frees_proxy_lease(client):
    """Regression (P1): a worker reaching ANY terminal outcome must release its proxy lease, not
    just the ones the scheduler's dead/stall/runaway reap paths forcibly kill. Before
    _release_on_terminal existed, a worker that finished its own run normally (the common case)
    leaked its lease permanently."""
    c, TestSession = client
    h = _op_headers(c)
    iid = c.post("/identities", json={"full_name": "X", "type": "test"}, headers=h).json()["id"]
    bid = c.post("/bookings", json={"name": "b", "type": "test", "posto_id": "2032"}, headers=h).json()["id"]
    wid = c.post(f"/bookings/{bid}/identities",
                 json={"identity_id": iid, "assigned_role": "apply"}, headers=h).json()["id"]

    from libs.db import models as M
    from libs.proxies import lease_proxy
    db = TestSession()
    try:
        db.add(M.Proxy(id="px1", provider="soax", kind="static", endpoint="h:1",
                       credentials_ref="u:p", max_sessions=1))
        db.commit()
        lease_proxy(db, wid, "soax")
    finally:
        db.close()

    import apps.hub.db as hub_db
    import apps.hub.events as events_mod
    orig = hub_db.SessionLocal
    hub_db.SessionLocal = TestSession
    events_mod.SessionLocal = TestSession
    try:
        hub = events_mod.EventHub.__new__(events_mod.EventHub)
        hub._release_on_terminal(wid, bus=None)
    finally:
        hub_db.SessionLocal = orig
        events_mod.SessionLocal = orig

    db = TestSession()
    try:
        lease = db.query(M.ProxyLease).filter(M.ProxyLease.worker_id == wid).first()
        assert lease.released_at is not None
    finally:
        db.close()


def test_handle_proxy_request_leases_from_pool_matching_phase(client):
    """P3 -- proxy rotation, spec'd by the operator: a worker's `proxy_request` event must be
    answered with a proxy drawn from the pool matching its CURRENT phase (PROXY_PROVIDER/KIND for
    "warmup", HOLD_PROXY_PROVIDER/KIND for "hold"), and its old same-pool lease released once the
    new one is confirmed -- but a pre-existing lease from the OTHER pool must be left untouched."""
    c, TestSession = client
    h = _op_headers(c)
    iid = c.post("/identities", json={"full_name": "X", "type": "test"}, headers=h).json()["id"]
    bid = c.post("/bookings", json={"name": "b", "type": "test", "posto_id": "2032"}, headers=h).json()["id"]
    wid = c.post(f"/bookings/{bid}/identities",
                 json={"identity_id": iid, "assigned_role": "apply"}, headers=h).json()["id"]

    from libs.db import models as M
    from libs.proxies import lease_proxy
    db = TestSession()
    try:
        bk = db.get(M.Booking, bid)
        bk.settings_json = {
            **bk.settings_json, "PROXY_PROVIDER": "soax", "PROXY_KIND": "rotating",
            "HOLD_PROXY_PROVIDER": "soax", "HOLD_PROXY_KIND": "static",
        }
        db.add(M.Proxy(id="px-warm-old", provider="soax", kind="rotating", endpoint="h:1",
                       credentials_ref="u:p", max_sessions=5))
        db.add(M.Proxy(id="px-warm-new", provider="soax", kind="rotating", endpoint="h:2",
                       credentials_ref="u:p", max_sessions=5))
        db.add(M.Proxy(id="px-hold", provider="soax", kind="static", endpoint="h:3",
                       credentials_ref="u:p", max_sessions=5))
        db.commit()
        lease_proxy(db, wid, "soax", kind="rotating")   # the worker's existing warmup-pool lease
        lease_proxy(db, wid, "soax", kind="static")     # a pre-existing hold-pool lease -- untouched
    finally:
        db.close()

    class _FakeBus:
        def __init__(self):
            self.sent = []

        def send_command(self, worker_id, type, **payload):
            self.sent.append((worker_id, type, payload))

    import apps.hub.db as hub_db
    import apps.hub.events as events_mod
    orig = hub_db.SessionLocal
    hub_db.SessionLocal = TestSession
    events_mod.SessionLocal = TestSession
    bus = _FakeBus()
    try:
        hub = events_mod.EventHub.__new__(events_mod.EventHub)
        hub._handle_proxy_request(wid, "warmup", bus, health_check=lambda url: (True, "ok"))
    finally:
        hub_db.SessionLocal = orig
        events_mod.SessionLocal = orig

    assert len(bus.sent) == 1
    worker_id, typ, payload = bus.sent[0]
    assert worker_id == wid and typ == "proxy_assigned"
    assert payload["proxy"].endswith("h:2")   # leased the OTHER warmup-pool proxy, not the same one

    db = TestSession()
    try:
        leases = {l.proxy_id: l for l in db.query(M.ProxyLease).filter(M.ProxyLease.worker_id == wid).all()}
        assert leases["px-warm-old"].released_at is not None    # old warmup lease released
        assert leases["px-warm-new"].released_at is None        # new warmup lease active
        assert leases["px-hold"].released_at is None             # untouched hold-pool lease
    finally:
        db.close()


def test_handle_proxy_request_sends_unavailable_when_pool_exhausted(client):
    c, TestSession = client
    h = _op_headers(c)
    iid = c.post("/identities", json={"full_name": "X", "type": "test"}, headers=h).json()["id"]
    bid = c.post("/bookings", json={"name": "b", "type": "test", "posto_id": "2032"}, headers=h).json()["id"]
    wid = c.post(f"/bookings/{bid}/identities",
                 json={"identity_id": iid, "assigned_role": "apply"}, headers=h).json()["id"]

    class _FakeBus:
        def __init__(self):
            self.sent = []

        def send_command(self, worker_id, type, **payload):
            self.sent.append((worker_id, type, payload))

    import apps.hub.db as hub_db
    import apps.hub.events as events_mod
    orig = hub_db.SessionLocal
    hub_db.SessionLocal = TestSession
    events_mod.SessionLocal = TestSession
    bus = _FakeBus()
    try:
        hub = events_mod.EventHub.__new__(events_mod.EventHub)
        hub._handle_proxy_request(wid, "hold", bus, health_check=lambda url: (True, "ok"))
        # no proxies seeded at all -- pool exhausted regardless of health_check (never even called)
    finally:
        hub_db.SessionLocal = orig
        events_mod.SessionLocal = orig

    assert bus.sent == [(wid, "proxy_unavailable", {"reason": bus.sent[0][2]["reason"]})]
    # Message reworded 2026-08-20 to separate a dead pool from lost claim races; the reason
    # still has to name the provider and say the lease could not be satisfied.
    assert "no webshare proxy leased" in bus.sent[0][2]["reason"]


# ── Phase 5: mid-hold rotation must resolve the SAME effective config as launch, and must enforce
# resource_type='static' whenever the resolved hold configuration requires it -- Phase 4 found both
# guarantees held only at launch, never at a subsequent rotation. ────────────────────────────────────
def _seed_and_call_proxy_request(client, *, booking_settings, worker_settings=None, phase="warmup",
                                 proxies):
    """Shared plumbing for the tests below: create an identity/booking/worker (optionally with a
    WORKER-layer override), seed proxy rows, and invoke _handle_proxy_request exactly as the P3 tests
    above do. Returns (bus.sent, TestSession) for the caller to assert against."""
    from libs.db import models as M

    c, TestSession = client
    h = _op_headers(c)
    iid = c.post("/identities", json={"full_name": "X", "type": "test"}, headers=h).json()["id"]
    bid = c.post("/bookings", json={"name": "b", "type": "test", "posto_id": "2032"}, headers=h).json()["id"]
    attach_body = {"identity_id": iid, "assigned_role": "apply"}
    if worker_settings:
        attach_body["worker_settings"] = worker_settings
    wid = c.post(f"/bookings/{bid}/identities", json=attach_body, headers=h).json()["id"]

    db = TestSession()
    try:
        bk = db.get(M.Booking, bid)
        bk.settings_json = {**bk.settings_json, **booking_settings}
        for kwargs in proxies:
            db.add(M.Proxy(**kwargs))
        db.commit()
    finally:
        db.close()

    class _FakeBus:
        def __init__(self):
            self.sent = []

        def send_command(self, worker_id, type, **payload):
            self.sent.append((worker_id, type, payload))

    import apps.hub.db as hub_db
    import apps.hub.events as events_mod
    orig = hub_db.SessionLocal
    hub_db.SessionLocal = TestSession
    events_mod.SessionLocal = TestSession
    bus = _FakeBus()
    try:
        hub = events_mod.EventHub.__new__(events_mod.EventHub)
        hub._handle_proxy_request(wid, phase, bus, health_check=lambda url: (True, "ok"))
    finally:
        hub_db.SessionLocal = orig
        events_mod.SessionLocal = orig
    return bus.sent, TestSession, wid


def test_handle_proxy_request_uses_worker_layer_override_same_as_launch(client):
    """Regression: booking-level PROXY_PROVIDER='soax' (A), worker-level override 'webshare' (B).
    _do_launch_worker already resolves B (tested in tests/hub/test_live_launch.py); this proves
    mid-hold rotation now resolves the SAME B, not the booking's A -- the exact inconsistency Phase 4
    found (events.py's layers dict never included L.WORKER)."""
    sent, _, wid = _seed_and_call_proxy_request(
        client,
        booking_settings={"PROXY_PROVIDER": "soax"},
        worker_settings={"PROXY_PROVIDER": "webshare"},
        phase="warmup",
        proxies=[
            dict(id="px-soax", provider="soax", kind="rotating", endpoint="soax:1",
                credentials_ref="u:p", max_sessions=5),
            dict(id="px-webshare", provider="webshare", kind="rotating", endpoint="webshare:1",
                credentials_ref="u:p", max_sessions=5),
        ],
    )
    assert len(sent) == 1
    worker_id, typ, payload = sent[0]
    assert typ == "proxy_assigned"
    assert payload["proxy"].endswith("webshare:1"), (
        "rotation resolved the booking-level provider ('soax'), not the worker override -- "
        "launch and rotation are resolving different effective configs")


def test_handle_proxy_request_hold_rejects_rotating_when_static_required(client):
    """Invariant B / F: HOLD_SWAP_AFTER_WARMUP is on, so the resolved hold configuration requires
    resource_type='static' -- and no static row exists, only a rotating one. The rotation must fail
    cleanly (proxy_unavailable), and must NEVER return the rotating candidate as a fallback."""
    sent, TestSession, wid = _seed_and_call_proxy_request(
        client,
        booking_settings={"HOLD_PROXY_PROVIDER": "soax", "HOLD_SWAP_AFTER_WARMUP": True},
        phase="hold",
        proxies=[
            dict(id="px-rotating", provider="soax", kind="rotating", endpoint="soax:1",
                credentials_ref="u:p", max_sessions=5, resource_type="rotating"),
        ],
    )
    assert len(sent) == 1
    worker_id, typ, payload = sent[0]
    assert typ == "proxy_unavailable", "a rotating candidate must never be returned for a static-required hold request"
    assert "static" in payload["reason"].lower()

    db = TestSession()
    try:
        from libs.db import models as M
        assert db.query(M.ProxyLease).filter(M.ProxyLease.worker_id == wid).count() == 0, (
            "the rotating candidate must not have been leased at all")
    finally:
        db.close()


def test_handle_proxy_request_hold_succeeds_and_returns_static_when_available(client):
    """Positive case: same static-required configuration, but a static candidate exists alongside a
    rotating one for the same provider -- the rotation must succeed and return the STATIC row, never
    the rotating one, even though nothing about score/LRU ordering favors either."""
    sent, _, wid = _seed_and_call_proxy_request(
        client,
        booking_settings={"HOLD_PROXY_PROVIDER": "soax", "HOLD_SWAP_AFTER_WARMUP": True},
        phase="hold",
        proxies=[
            dict(id="px-rotating", provider="soax", kind="rotating", endpoint="rot:1",
                credentials_ref="u:p", max_sessions=5, resource_type="rotating"),
            dict(id="px-static", provider="soax", kind="rotating", endpoint="stat:1",
                credentials_ref="u:p", max_sessions=5, resource_type="static"),
        ],
    )
    assert len(sent) == 1
    worker_id, typ, payload = sent[0]
    assert typ == "proxy_assigned"
    assert payload["proxy"].endswith("stat:1")
    assert payload["resource_type"] == "static"


def test_handle_proxy_request_warmup_never_requires_static(client):
    """Sanity boundary: HOLD_SWAP_AFTER_WARMUP being on must not leak a static requirement into the
    UNRELATED warmup-phase (PROXY_PROVIDER) pool -- only "hold" is ever static-constrained."""
    sent, _, wid = _seed_and_call_proxy_request(
        client,
        booking_settings={"PROXY_PROVIDER": "soax", "HOLD_SWAP_AFTER_WARMUP": True},
        phase="warmup",
        proxies=[
            dict(id="px-rotating", provider="soax", kind="rotating", endpoint="rot:1",
                credentials_ref="u:p", max_sessions=5, resource_type="rotating"),
        ],
    )
    assert len(sent) == 1
    worker_id, typ, payload = sent[0]
    assert typ == "proxy_assigned"
    assert payload["proxy"].endswith("rot:1")


def test_persist_booked_pdf_creates_artifact_and_appointment(client, tmp_path):
    c, TestSession = client
    h = _op_headers(c)
    iid = c.post("/identities", json={"full_name": "X", "type": "test"}, headers=h).json()["id"]
    bid = c.post("/bookings", json={"name": "b", "type": "test", "posto_id": "2032"}, headers=h).json()["id"]
    wid = c.post(f"/bookings/{bid}/identities",
                 json={"identity_id": iid, "assigned_role": "apply"}, headers=h).json()["id"]

    pdf_path = tmp_path / "booked.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    from libs.db import models as M
    db = TestSession()
    try:
        w = db.get(M.Worker, wid)
        w.effective_settings_json = {"_sub_account_id": "sa-123"}
        db.commit()
    finally:
        db.close()

    import apps.hub.events as events_mod
    hub = events_mod.EventHub.__new__(events_mod.EventHub)  # bypass __init__ (no redis needed)
    # _persist_booked_pdf only touches SessionLocal, which is overridden via get_db's factory
    # resolution pattern used elsewhere in this app -- but events.py imports SessionLocal directly,
    # so patch it to the test session factory for this call.
    import apps.hub.db as hub_db
    orig = hub_db.SessionLocal
    hub_db.SessionLocal = TestSession
    try:
        events_mod.SessionLocal = TestSession
        hub._persist_booked_pdf(wid, str(pdf_path), {"date": "2027-01-05", "period": 1})
    finally:
        hub_db.SessionLocal = orig
        events_mod.SessionLocal = orig

    appts = c.get(f"/bookings/{bid}/appointments", headers=h).json()
    assert len(appts) == 1
    assert appts[0]["sub_account_id"] == "sa-123"
    artifact_id = appts[0]["pdf_artifact_id"]
    assert artifact_id

    meta = c.get(f"/artifacts/{artifact_id}", headers=h).json()
    assert meta["kind"] == "pdf" and meta["path"] == str(pdf_path)

    dl = c.get(f"/artifacts/{artifact_id}/file", headers=h)
    assert dl.status_code == 200
    assert dl.content == b"%PDF-1.4 fake"


def test_a_booked_slot_stays_available_while_capacity_remains(client, tmp_path):
    """Multi-capacity leasing (operator spec 2026-08-14): one physical (date, period) slot can
    absorb SLOT_CAPACITY_PER_PERIOD concurrent bookings before it's exhausted -- 10 open slots at
    cap=5 is 50 leasable units, not 10. _assign_slots and _reclaim_stale_slot_leases both correctly
    compute remaining capacity from live lease counts -- but _persist_booked_pdf set
    slot.status = TAKEN unconditionally on every successful booking, with no capacity check at all.
    TAKEN is excluded from every reconsideration path in the codebase by design
    (_assign_slots' own candidate query is `status == AVAILABLE`; _reclaim_stale_slot_leases
    explicitly refuses to touch it: "genuinely booked -- never reopen"). So the FIRST booking on a
    5-capacity slot discarded the other 4 units, permanently, until a scout happened to
    independently rediscover that exact (date, period) later. CONFIRMED LIVE 2026-09-0X: 9
    previously-taken slots sat invisible for a whole booking run because nothing ever re-touched
    them. This pins the fix: a slot with resolved capacity > the number of bookings actually landed
    on it must stay reconsiderable, not jump straight to the single-capacity terminal state."""
    c, TestSession = client
    h = _op_headers(c)
    iid = c.post("/identities", json={"full_name": "X", "type": "test"}, headers=h).json()["id"]
    bid = c.post("/bookings", json={"name": "b", "type": "test", "posto_id": "2032",
                                    "settings": {"SLOT_CAPACITY_PER_PERIOD": "3"}}, headers=h).json()["id"]
    wid = c.post(f"/bookings/{bid}/identities",
                 json={"identity_id": iid, "assigned_role": "apply"}, headers=h).json()["id"]

    pdf_path = tmp_path / "booked.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    from datetime import datetime, timezone

    from libs.db import models as M
    from libs.domain.enums import SlotStatus
    db = TestSession()
    try:
        w = db.get(M.Worker, wid)
        w.effective_settings_json = {"_sub_account_id": "sa-123"}
        slot = M.Slot(booking_id=bid, posto_id="2032", date="2027-01-05", period=1,
                      status=SlotStatus.AVAILABLE)
        db.add(slot)
        db.flush()
        # _persist_booked_pdf only marks leases it finds already open -- _assign_slots always
        # creates this row before a worker starts applying, so the test must too, or the "how
        # many bookings does this slot actually carry" count stays 0 no matter what happens.
        db.add(M.SlotLease(slot_id=slot.id, worker_id=wid, leased_at=datetime.now(timezone.utc)))
        db.commit()
    finally:
        db.close()

    import apps.hub.events as events_mod
    hub = events_mod.EventHub.__new__(events_mod.EventHub)
    import apps.hub.db as hub_db
    orig = hub_db.SessionLocal
    hub_db.SessionLocal = TestSession
    try:
        events_mod.SessionLocal = TestSession
        hub._persist_booked_pdf(wid, str(pdf_path), {"date": "2027-01-05", "period": 1})
    finally:
        hub_db.SessionLocal = orig
        events_mod.SessionLocal = orig

    from sqlalchemy import select
    db = TestSession()
    try:
        slot = db.scalar(select(M.Slot).where(M.Slot.booking_id == bid, M.Slot.date == "2027-01-05",
                                              M.Slot.period == 1))
        assert slot.status != SlotStatus.TAKEN, (
            "1/3 capacity used but the slot was marked TAKEN -- the other 2 leasable units were "
            "discarded and are now invisible to _assign_slots/_reclaim_stale_slot_leases until a "
            "scout happens to independently rediscover this exact (date, period) again")
    finally:
        db.close()


def test_a_booked_slot_still_becomes_taken_once_capacity_is_exhausted(client, tmp_path):
    """The other half of the fix above: the common case (SLOT_CAPACITY_PER_PERIOD unset, or a
    genuinely single-capacity test posto) must still transition to TAKEN once the resolved cap is
    actually reached -- the fix narrows WHEN this fires, it must not remove it."""
    c, TestSession = client
    h = _op_headers(c)
    iid = c.post("/identities", json={"full_name": "X", "type": "test"}, headers=h).json()["id"]
    # No SLOT_CAPACITY_PER_PERIOD override -- "auto" with no capacity_estimate resolves to
    # DEFAULT_SLOT_CAPACITY, so pin the case that actually matters here explicitly: cap=1.
    bid = c.post("/bookings", json={"name": "b", "type": "test", "posto_id": "2032",
                                    "settings": {"SLOT_CAPACITY_PER_PERIOD": "1"}}, headers=h).json()["id"]
    wid = c.post(f"/bookings/{bid}/identities",
                 json={"identity_id": iid, "assigned_role": "apply"}, headers=h).json()["id"]

    pdf_path = tmp_path / "booked.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    from datetime import datetime, timezone

    from libs.db import models as M
    from libs.domain.enums import SlotStatus
    db = TestSession()
    try:
        w = db.get(M.Worker, wid)
        w.effective_settings_json = {"_sub_account_id": "sa-123"}
        slot = M.Slot(booking_id=bid, posto_id="2032", date="2027-01-05", period=1,
                      status=SlotStatus.AVAILABLE)
        db.add(slot)
        db.flush()
        db.add(M.SlotLease(slot_id=slot.id, worker_id=wid, leased_at=datetime.now(timezone.utc)))
        db.commit()
    finally:
        db.close()

    import apps.hub.events as events_mod
    hub = events_mod.EventHub.__new__(events_mod.EventHub)
    import apps.hub.db as hub_db
    orig = hub_db.SessionLocal
    hub_db.SessionLocal = TestSession
    try:
        events_mod.SessionLocal = TestSession
        hub._persist_booked_pdf(wid, str(pdf_path), {"date": "2027-01-05", "period": 1})
    finally:
        hub_db.SessionLocal = orig
        events_mod.SessionLocal = orig

    from sqlalchemy import select
    db = TestSession()
    try:
        slot = db.scalar(select(M.Slot).where(M.Slot.booking_id == bid, M.Slot.date == "2027-01-05",
                                              M.Slot.period == 1))
        assert slot.status == SlotStatus.TAKEN, (
            "cap=1 and 1/1 capacity used -- the slot must still transition to TAKEN")
    finally:
        db.close()

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
    # thread instead of using Depends(get_db) -- point it at this test's isolated engine too, or a
    # fleet launch silently no-ops against the real dev DB instead of this StaticPool one.
    import apps.hub.main as hub_main
    monkeypatch.setattr(hub_main, "SessionLocal", TestSession)
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


def test_ops_status_reports_redis_disk_and_workers(client):
    r = client.get("/ops/status")
    assert r.status_code == 200
    body = r.json()
    assert "redis" in body and "disk" in body and "workers" in body
    assert "free_pct" in body["disk"]
    assert "running" in body["workers"]
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


def test_import_identities_and_proxies(client):
    h = _operate_headers(client)
    csv = ("identity_id,first_name,last_name,account_type,username,password\n"
           "ID1,Ana,Silva,test,ana1,pw1\nID1,Ana,Silva,test,ana2,pw2\n")
    r = client.post("/identities/import", json={"csv": csv}, headers=h).json()
    assert r["created"] == 1 and r["sub_accounts"] == 2
    assert len(client.get("/identities", headers=h).json()) == 1
    # Regression: identity_ids/sub_account_ids let the dashboard chain gen-subaccounts + a
    # registration run onto exactly the rows this import just created, without a second lookup.
    assert len(r["identity_ids"]) == 1
    assert len(r["sub_account_ids"]) == 2
    all_ids = {i["id"] for i in client.get("/identities", headers=h).json()}
    assert set(r["identity_ids"]) <= all_ids
    all_sub_ids = {s["id"] for s in client.get("/sub-accounts", headers=h).json()}
    assert set(r["sub_account_ids"]) <= all_sub_ids

    # SOAX-style: same endpoint, DIFFERENT session strings -> distinct proxies; exact dup -> skipped
    r = client.post("/proxies/import", json={"provider": "soax", "kind": "datacenter",
                    "text": "http://sess1:x@proxy.soax.com:5000\n"
                            "http://sess2:x@proxy.soax.com:5000\n"
                            "http://sess1:x@proxy.soax.com:5000"}, headers=h).json()
    assert r["created"] == 2  # two distinct sessions; the third is an exact duplicate
    assert client.get("/proxies", headers=h).json()[0]["provider"] == "soax"


def test_templates_crud_and_seed(client):
    h = _operate_headers(client)
    # seed defaults via startup isn't run in tests; create explicitly
    r = client.post("/templates", json={"name": "Scout A", "role": "scout",
                                        "settings": {"CAPTURE_ONLY": True, "NAV_TIMEOUT_SCALE": 4}}, headers=h)
    assert r.status_code == 201 and r.json()["role"] == "scout"
    # wrong-layer setting rejected
    bad = client.post("/templates", json={"name": "x", "role": "apply", "settings": {"SCOUT_POOL": True}}, headers=h)
    assert bad.status_code == 400
    assert any(t["name"] == "Scout A" for t in client.get("/templates").json())


def test_arm_with_template_and_start_booking(client):
    h = _operate_headers(client)
    tpl = client.post("/templates", json={"name": "Apply A", "role": "apply", "settings": {}}, headers=h).json()
    iid = client.post("/identities", json={"full_name": "R", "type": "real"}, headers=h).json()["id"]
    bid = client.post("/bookings", json={"name": "b", "type": "real", "posto_id": "5084"}, headers=h).json()["id"]
    w = client.post(f"/bookings/{bid}/identities",
                    json={"identity_id": iid, "template_id": tpl["id"]}, headers=h)
    assert w.status_code == 201 and w.json()["role"] == "apply"
    # template role mismatch rejected
    iid2 = client.post("/identities", json={"full_name": "T", "type": "test"}, headers=h).json()["id"]
    # (test identity in real booking -> scout; apply template mismatches)
    bad = client.post(f"/bookings/{bid}/identities",
                      json={"identity_id": iid2, "template_id": tpl["id"]}, headers=h)
    assert bad.status_code == 400
    # start booking launches ready workers (supervisor.spawn will try a subprocess; tolerate)
    r = client.post(f"/bookings/{bid}/start", headers=h)
    assert r.status_code == 200 and r.json()["started"] >= 1

    # Regression guard: _launch_worker_fleet launches each worker on its own background-thread DB
    # session (P4, needed once a live-mode lease can do a real network probe) -- confirm the worker
    # actually reaches RUNNING, not just that /start counted it as "started".
    workers = client.get(f"/bookings/{bid}/workers", headers=h).json()
    assert any(w["status"] == "running" for w in workers)


def test_start_does_not_fire_slot_open(client):
    # /start launches workers and begins warmup; it must NOT decide slot-open on its own -- that's
    # a separate decision (timed /schedule, or manual /fire-slot-open). Corrected after an initial
    # version auto-fired slot-open on every start, which isn't what "Start Instantly" means.
    h = _operate_headers(client)
    bid = client.post("/bookings", json={"name": "b-warm", "type": "real", "posto_id": "5084"}, headers=h).json()["id"]
    r = client.post(f"/bookings/{bid}/start", headers=h)
    assert r.status_code == 200 and r.json()["booking_status"] == "warming"
    got = client.get(f"/bookings/{bid}", headers=h)
    assert got.json()["status"] == "warming"


def test_fire_slot_open_manually(client):
    h = _operate_headers(client)
    bid = client.post("/bookings", json={"name": "b-fso", "type": "real", "posto_id": "5084"}, headers=h).json()["id"]
    # too early: not warming/armed yet
    r = client.post(f"/bookings/{bid}/fire-slot-open", headers=h)
    assert r.status_code == 409

    client.post(f"/bookings/{bid}/start", headers=h)
    r = client.post(f"/bookings/{bid}/fire-slot-open", headers=h)
    assert r.status_code == 200 and r.json()["fired"] is True and r.json()["booking_status"] == "running"

    # idempotent
    r2 = client.post(f"/bookings/{bid}/fire-slot-open", headers=h)
    assert r2.status_code == 200 and r2.json()["already_fired"] is True


def test_fire_slot_found_manually(client):
    """Regression: unlike fire-slot-open, slot-found previously had NO manual override at all --
    it only ever fired automatically once a real AVAILABLE Slot row existed, so an operator testing
    the apply-worker path had no way to force it."""
    h = _operate_headers(client)
    bid = client.post("/bookings", json={"name": "b-fsf", "type": "real", "posto_id": "5084"}, headers=h).json()["id"]

    # too early: slot-open hasn't fired yet
    r = client.post(f"/bookings/{bid}/fire-slot-found", headers=h)
    assert r.status_code == 409

    client.post(f"/bookings/{bid}/start", headers=h)
    client.post(f"/bookings/{bid}/fire-slot-open", headers=h)

    r = client.post(f"/bookings/{bid}/fire-slot-found", headers=h)
    assert r.status_code == 200 and r.json()["fired"] is True

    got = client.get(f"/bookings/{bid}", headers=h)
    assert got.json()["slot_open_fired_at"] is not None
    assert got.json()["slot_found_fired_at"] is not None
    r2 = client.post(f"/bookings/{bid}/fire-slot-found", headers=h)
    assert r2.status_code == 200 and r2.json()["already_fired"] is True


def test_stop_then_restart_booking_status_not_conflated(client):
    # Confirmed bug fix (F13): stop must not report the booking as "completed", and restart must
    # actually move the status forward again, not leave it stuck.
    h = _operate_headers(client)
    bid = client.post("/bookings", json={"name": "b2", "type": "real", "posto_id": "5084"}, headers=h).json()["id"]
    r = client.post(f"/bookings/{bid}/start", headers=h)
    assert r.status_code == 200 and r.json()["booking_status"] == "warming"

    r = client.post(f"/bookings/{bid}/stop", headers=h)
    assert r.status_code == 200
    assert r.json()["booking_status"] == "stopped"   # NOT "completed"

    got = client.get(f"/bookings/{bid}", headers=h)
    assert got.json()["status"] == "stopped"

    r = client.post(f"/bookings/{bid}/start", headers=h)
    assert r.status_code == 200
    assert r.json()["booking_status"] == "warming"   # actually resumed, not stuck -- and slot-open
                                                      # never fired before, so it's a fresh warmup


def test_restart_after_slot_open_is_a_fresh_run_and_re_enables_the_signals(client):
    """Operator decision 2026-08-14 (replaces the old resume-into-RUNNING shortcut): every /start is
    a FRESH RUN. Confirmed bug it fixes -- slot_open_fired_at/slot_found_fired_at persisted across a
    stop/start cycle, so a restarted booking came back at RUNNING with both fire buttons
    permanently disabled and no way to re-fire a signal on what was, to the operator, a new run."""
    h = _operate_headers(client)
    bid = client.post("/bookings", json={"name": "b3", "type": "real", "posto_id": "5084"}, headers=h).json()["id"]
    client.post(f"/bookings/{bid}/start", headers=h)
    assert client.post(f"/bookings/{bid}/fire-slot-open", headers=h).json()["fired"] is True
    assert client.post(f"/bookings/{bid}/fire-slot-found", headers=h).json()["fired"] is True

    r = client.post(f"/bookings/{bid}/stop", headers=h)
    assert r.json()["booking_status"] == "stopped"

    r = client.post(f"/bookings/{bid}/start", headers=h)
    assert r.status_code == 200 and r.json()["booking_status"] == "warming"

    bk = client.get(f"/bookings/{bid}", headers=h).json()
    assert bk["slot_open_fired_at"] is None
    assert bk["slot_found_fired_at"] is None
    # and they're genuinely re-firable, not just blank in the response
    assert client.post(f"/bookings/{bid}/fire-slot-open", headers=h).json()["fired"] is True


def test_configs_seed_and_edit(client):
    from apps.hub.config_store import seed_defaults
    from apps.hub.db import get_db as _  # noqa
    # seed via the app's session (the test overrides get_db); call seed on the same DB
    # by hitting the override session through a request-scoped dependency isn't trivial here,
    # so seed directly using the overridden session factory:
    gen = client.app.dependency_overrides
    # Simplest: PUT a config, then GET it back (covers the editable path end-to-end)
    h = _operate_headers(client)
    r = client.put("/configs/form_defaults", json={"value": {"dynamic": {"f30": "2027/01/05"}}}, headers=h)
    assert r.status_code == 200
    got = client.get("/configs/form_defaults", headers=h).json()
    assert got["value"]["dynamic"]["f30"] == "2027/01/05"
    assert "form_defaults" in client.get("/configs", headers=h).json()


def test_gen_test_identities_and_subaccounts(client):
    h = _operate_headers(client)
    r = client.post("/identities/gen-test", json={"count": 4, "accounts_per_identity": 2}, headers=h).json()
    assert r["created"] == 4 and r["sub_accounts"] == 8
    ids = client.get("/identities", headers=h).json()
    assert len([i for i in ids if i["type"] == "test"]) == 4
    # gen more sub-accounts for an existing identity
    iid = ids[0]["id"]
    r2 = client.post("/identities/gen-subaccounts", json={"identity_ids": [iid], "count": 3}, headers=h).json()
    assert r2["sub_accounts"] == 3


def test_global_settings_roundtrip_and_masking(client):
    h = _operate_headers(client)
    # wrong-layer key rejected
    assert client.put("/settings/global", json={"values": {"SCOUT_POOL": True}}, headers=h).status_code == 400
    # valid global keys stored; secret masked on read
    assert client.put("/settings/global",
                      json={"values": {"CAPSOLVER_KEYS": "abc123", "HOST_SESSION_CAP": 200}},
                      headers=h).status_code == 200
    got = client.get("/settings/global", headers=h).json()
    assert got["CAPSOLVER_KEYS"] == "•••set" and got["HOST_SESSION_CAP"] == "200"


# ── Slots tab "Clear" + every-start-is-a-fresh-run (operator request 2026-08-14) ──────────────
def _booking_with_slots(client, h, *, name="ClearB"):
    from libs.domain.enums import SlotStatus

    import apps.hub.main as main
    from libs.db import models as M

    bid = client.post("/bookings", json={"name": name, "type": "test", "posto_id": "3018",
                                         "nationality": "CPV", "residence": "FRA"},
                      headers=h).json()["id"]
    db = main.SessionLocal()
    db.add_all([
        M.Slot(booking_id=bid, posto_id="3018", date="2026-09-01", period=1,
               status=SlotStatus.AVAILABLE),
        M.Slot(booking_id=bid, posto_id="3018", date="2026-09-02", period=2,
               status=SlotStatus.LEASED),
        M.Slot(booking_id=bid, posto_id="3018", date="2026-09-03", period=3,
               status=SlotStatus.TAKEN),
    ])
    db.commit(); db.close()
    return bid


def test_clear_slots_retires_observations_but_never_a_booked_slot(client):
    """Slots are per-run OBSERVATIONS; a stale pool makes the scheduler fire slot_found on the next
    tick and skip discovery. Clear drops one without restarting the run -- retiring to CLOSED (which
    reopens on rediscovery) and leaving TAKEN alone, because that is a real appointment and never
    something a cleanup click may erase."""
    import apps.hub.main as main
    from sqlalchemy import select

    from libs.db import models as M

    h = _operate_headers(client)
    bid = _booking_with_slots(client, h)

    r = client.request("DELETE", f"/bookings/{bid}/slots", headers=h)
    assert r.status_code == 200 and r.json()["cleared"] == 2

    db = main.SessionLocal()
    got = {(s.date, str(s.status)) for s in db.scalars(
        select(M.Slot).where(M.Slot.booking_id == bid)).all()}
    assert got == {("2026-09-01", "closed"), ("2026-09-02", "closed"), ("2026-09-03", "taken")}
    assert db.get(M.Booking, bid).slot_found_fired_at is None
    db.close()


def test_clear_slots_requires_the_operate_seat(client):
    h = _operate_headers(client)
    bid = _booking_with_slots(client, h, name="ClearB2")
    assert client.request("DELETE", f"/bookings/{bid}/slots").status_code in (401, 403)


def test_start_clears_stale_signal_state_even_when_already_running(client):
    """Gating the fresh-run reset on DRAFT/SCHEDULED/STOPPED meant clicking Start on a booking that
    was already RUNNING silently skipped all of it, so the run inherited the previous one's fired
    signal timestamps and slot pool. Confirmed live: a restarted "Test 3018" still showed
    "slot-open fired @15:25Z" from the run before, latch included, so it could not be re-fired."""
    import apps.hub.main as main
    from sqlalchemy import select

    from libs.db import models as M
    from libs.domain.enums import BookingStatus

    h = _operate_headers(client)
    bid = _booking_with_slots(client, h, name="FreshRun")
    db = main.SessionLocal()
    bk = db.get(M.Booking, bid)
    bk.status = BookingStatus.RUNNING
    bk.slot_open_fired_at = bk.slot_found_fired_at = main._now()
    db.commit(); db.close()

    assert client.post(f"/bookings/{bid}/start", headers=h).status_code == 200

    db = main.SessionLocal()
    bk = db.get(M.Booking, bid)
    assert bk.slot_open_fired_at is None and bk.slot_found_fired_at is None
    # the previous run's observations are retired too -- but never the booked one
    left = {str(s.status) for s in db.scalars(select(M.Slot).where(M.Slot.booking_id == bid)).all()}
    assert left == {"closed", "taken"}
    db.close()

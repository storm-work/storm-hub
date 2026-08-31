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
    h = _operate_headers(client)          # reads require a token since require_viewer landed
    r = client.get("/ops/status", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert "redis" in body and "disk" in body and "workers" in body
    assert "free_pct" in body["disk"]
    assert "running" in body["workers"]
    cat = client.get("/settings/catalog", headers=h).json()
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


def test_import_default_target_posto_applies_to_real_only(client):
    h = _operate_headers(client)
    csv = ("identity_id,first_name,last_name,account_type,username,password\n"
          "R1,Real,One,real,r1,pw1\nT1,Test,One,test,t1,pw2\n")
    r = client.post("/identities/import",
                    json={"csv": csv, "default_target_posto": "5084"}, headers=h).json()
    assert r["created"] == 2
    got = {i["full_name"]: i["target_posto"] for i in client.get("/identities", headers=h).json()}
    assert got["Real One"] == "5084"   # real identity gets the default
    assert got["Test One"] is None     # target_posto is app-enforced real-identities-only


def test_import_without_default_target_posto_leaves_it_unset(client):
    h = _operate_headers(client)
    csv = "identity_id,first_name,last_name,account_type,username,password\nR1,Real,One,real,r1,pw1\n"
    client.post("/identities/import", json={"csv": csv}, headers=h)
    got = client.get("/identities", headers=h).json()
    assert got[0]["target_posto"] is None


def test_templates_crud_and_seed(client):
    h = _operate_headers(client)
    # seed defaults via startup isn't run in tests; create explicitly
    r = client.post("/templates", json={"name": "Scout A", "role": "scout",
                                        "settings": {"CAPTURE_ONLY": True, "NAV_TIMEOUT_SCALE": 4}}, headers=h)
    assert r.status_code == 201 and r.json()["role"] == "scout"
    # wrong-layer setting rejected
    bad = client.post("/templates", json={"name": "x", "role": "apply", "settings": {"SCOUT_POOL": True}}, headers=h)
    assert bad.status_code == 400
    assert any(t["name"] == "Scout A" for t in client.get("/templates", headers=h).json())


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


# ── Proxies page "Reset pool health" (operator request 2026-08-16) ────────────────────────────
def _seed_proxies(healthy=2, condemned=3):
    import apps.hub.main as main
    from libs.db import models as M

    db = main.SessionLocal()
    for i in range(healthy):
        db.add(M.Proxy(provider="webshare", kind="rotating", endpoint=f"ok{i}:80",
                       credentials_ref="u:p", max_sessions=2, is_healthy=True, health=None))
    for i in range(condemned):
        db.add(M.Proxy(provider="webshare", kind="rotating", endpoint=f"bad{i}:80",
                       credentials_ref="u:p", max_sessions=2, is_healthy=False,
                       health="ERR_CONNECTION_CLOSED", score=-3.0))
    db.commit(); db.close()


def test_reset_proxy_health_dry_run_reports_scope_and_writes_nothing(client):
    """lease_proxy filters on is_healthy and health.apply_result sets it False on ANY probe failure,
    so a bad prober condemns good proxies permanently (639 of 2000 on 2026-08-15). This is the only
    way to undo that, which is exactly why it must show the scope before writing."""
    from sqlalchemy import select

    import apps.hub.main as main
    from libs.db import models as M

    h = _operate_headers(client)
    _seed_proxies()

    r = client.post("/proxies/reset-health", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["confirmed"] is False
    assert body["probed"] == 3 and body["total"] == 5 and body["leasable_before"] == 2

    db = main.SessionLocal()
    assert len(db.scalars(select(M.Proxy).where(M.Proxy.is_healthy.is_(False))).all()) == 3
    db.close()


def test_reset_proxy_health_confirm_clears_every_verdict_to_unprobed(client):
    """Unprobed, NOT known-good: the next lease re-probes each one -- now with the same browser
    configuration a worker uses."""
    from sqlalchemy import select

    import apps.hub.main as main
    from libs.db import models as M

    h = _operate_headers(client)
    _seed_proxies()

    r = client.post("/proxies/reset-health?confirm=true", headers=h)
    assert r.status_code == 200 and r.json()["confirmed"] is True

    db = main.SessionLocal()
    rows = db.scalars(select(M.Proxy)).all()
    assert all(p.is_healthy and p.health is None and p.score == 0.0 for p in rows)
    db.close()


def test_reset_proxy_health_refuses_while_workers_are_running(client):
    """A running worker re-probes and would immediately re-condemn what this clears."""
    import apps.hub.main as main
    from libs.db import models as M
    from libs.domain.enums import BookingStatus, WorkerRole, WorkerStatus

    h = _operate_headers(client)
    _seed_proxies()
    db = main.SessionLocal()
    bk = M.Booking(name="RunningB", type="test", posto_id="3018", status=BookingStatus.RUNNING)
    i = M.Identity(full_name="W", type="real")
    db.add_all([bk, i]); db.commit()
    db.add(M.Worker(booking_id=bk.id, identity_id=i.id, role=WorkerRole.APPLY,
                    status=WorkerStatus.RUNNING))
    db.commit(); db.close()

    assert client.post("/proxies/reset-health?confirm=true", headers=h).status_code == 409
    # ...and force is the documented escape hatch, matching the script's own
    assert client.post("/proxies/reset-health?confirm=true&force=true",
                       headers=h).status_code == 200


def test_reset_proxy_health_requires_the_operate_seat(client):
    _seed_proxies()
    assert client.post("/proxies/reset-health").status_code in (401, 403)


def test_proxy_list_exposes_health_so_the_page_can_show_what_is_condemned(client):
    """is_healthy decides whether a proxy is leasable at all -- the most consequential field on the
    row, and it was not being returned by the API."""
    h = _operate_headers(client)
    _seed_proxies()
    rows = client.get("/proxies", headers=h).json()
    assert sum(1 for p in rows if p["is_healthy"] is False) == 3
    assert any(p["health"] == "ERR_CONNECTION_CLOSED" for p in rows)


# ── scheduling a booking for a LATER run (operator report 2026-08-19) ─────────────────────────
def _future(minutes):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def test_a_stopped_booking_can_be_scheduled_for_a_later_run(client):
    """THE reported bug. plan_booking_actions only emits "start" for a SCHEDULED booking, and
    /schedule refused anything that wasn't DRAFT/SCHEDULED -- so after a booking's FIRST run, an
    operator who stopped it, edited the times and pressed Schedule got a 409 and a warmup that never
    fired again. "Stop, retime, schedule for tomorrow" is the ordinary way a booking gets reused."""
    h = _operate_headers(client)
    bid = client.post("/bookings", json={"name": "Sched1", "type": "test", "posto_id": "2032",
                                         "nationality": "CPV", "residence": "IRL"},
                      headers=h).json()["id"]
    client.put(f"/bookings/{bid}", json={"warmup_start_at": _future(5),
                                         "slot_open_at": _future(30)}, headers=h)
    client.post(f"/bookings/{bid}/start", headers=h)
    client.post(f"/bookings/{bid}/stop", headers=h)

    client.put(f"/bookings/{bid}", json={"warmup_start_at": _future(10),
                                         "slot_open_at": _future(40)}, headers=h)
    r = client.post(f"/bookings/{bid}/schedule", headers=h)
    assert r.status_code == 200, r.json()
    assert r.json()["status"] == "scheduled"


def test_scheduling_clears_the_previous_runs_signal_latch(client):
    """A scheduled run is a FRESH run, exactly as /start is. Without this the new run inherits the
    last one's fired-signal timestamps and per-worker notified-sets, and slot-open/slot-found are
    suppressed by a latch rather than firing on the clock."""
    import apps.hub.main as main
    from libs.db import models as M

    h = _operate_headers(client)
    bid = client.post("/bookings", json={"name": "Sched2", "type": "test", "posto_id": "2032"},
                      headers=h).json()["id"]
    client.put(f"/bookings/{bid}", json={"warmup_start_at": _future(5)}, headers=h)
    db = main.SessionLocal()
    bk = db.get(M.Booking, bid)
    bk.slot_open_fired_at = bk.slot_found_fired_at = main._now()
    db.commit(); db.close()

    assert client.post(f"/bookings/{bid}/schedule", headers=h).status_code == 200
    db = main.SessionLocal()
    bk = db.get(M.Booking, bid)
    assert bk.slot_open_fired_at is None and bk.slot_found_fired_at is None
    db.close()


def test_scheduling_refuses_a_warmup_time_already_in_the_past(client):
    """It would fire on the very next tick -- that is Start now wearing a schedule's clothing, and
    it silently skips the whole hold phase."""
    h = _operate_headers(client)
    bid = client.post("/bookings", json={"name": "Sched3", "type": "test", "posto_id": "2032"},
                      headers=h).json()["id"]
    client.put(f"/bookings/{bid}", json={"warmup_start_at": _future(-60)}, headers=h)
    r = client.post(f"/bookings/{bid}/schedule", headers=h)
    assert r.status_code == 400 and "past" in r.json()["detail"]


def test_scheduling_still_refuses_a_live_booking(client):
    h = _operate_headers(client)
    bid = client.post("/bookings", json={"name": "Sched4", "type": "test", "posto_id": "2032"},
                      headers=h).json()["id"]
    client.put(f"/bookings/{bid}", json={"warmup_start_at": _future(5)}, headers=h)
    client.post(f"/bookings/{bid}/start", headers=h)          # -> WARMING
    assert client.post(f"/bookings/{bid}/schedule", headers=h).status_code == 409


def test_imported_proxies_get_their_geo(client):
    """The geo column was backfilled by a ONE-TIME migration and the import endpoint was never
    taught to populate it -- so every proxy added afterwards arrived with geo NULL. A NULL geo is
    deliberately never excluded by a PROXY_GEOS filter, so a whole newly-imported pool would have
    been unfilterable, which defeats the point of importing it to compare geos."""
    from sqlalchemy import select

    import apps.hub.main as main
    from libs.db import models as M

    h = _operate_headers(client)
    text = "\n".join([
        "package-350716-country-de-sessionid-AAAA-sessionlength-3600-bindttl-600-opt-wb:pw@proxy.soax.com:5000",
        "package-350716-country-it-sessionid-BBBB-sessionlength-3600-bindttl-600-opt-wb:pw@proxy.soax.com:5000",
        "p.webshare.io:80:sodade2026-PT-9:pw",
    ])
    r = client.post("/proxies/import",
                    json={"provider": "soax_mobile", "kind": "rotating", "text": text}, headers=h)
    assert r.status_code == 200 and r.json()["created"] == 3

    db = main.SessionLocal()
    geos = sorted(g for (g,) in db.execute(select(M.Proxy.geo)).all() if g)
    assert geos == ["DE", "IT", "PT"]
    db.close()


def test_a_single_created_proxy_gets_its_geo_too(client):
    from sqlalchemy import select

    import apps.hub.main as main
    from libs.db import models as M

    h = _operate_headers(client)
    r = client.post("/proxies", json={"provider": "soax_mobile", "kind": "rotating",
                                      "endpoint": "proxy.soax.com:5000",
                                      "credentials_ref": "package-350716-country-de-sessionid-Z-opt-wb:pw"},
                    headers=h)
    assert r.status_code == 201 and r.json()["geo"] == "DE"


# ── booked identities: one row per PERSON, and admin-only credentials (2026-08-19) ────────────
def _book_identity(client, h, *, name, itype="real", posto="2032", booking="BK1"):
    """Create an identity with TWO sub-accounts and book it on one of them."""
    import apps.hub.main as main
    from libs.db import models as M
    from libs.domain.enums import SlotStatus

    iid = client.post("/identities", json={"full_name": name, "type": itype}, headers=h).json()["id"]
    client.post("/identities/gen-subaccounts", json={"identity_ids": [iid], "count": 2}, headers=h)
    bid = client.post("/bookings", json={"name": booking, "type": "test", "posto_id": posto},
                      headers=h).json()["id"]
    db = main.SessionLocal()
    from sqlalchemy import select as _sel
    subs = db.scalars(_sel(M.SubAccount).where(M.SubAccount.identity_id == iid)).all()
    slot = M.Slot(booking_id=bid, posto_id=posto, date="2026-09-14", period=3,
                  status=SlotStatus.TAKEN)
    db.add(slot); db.commit()
    db.add(M.BookedAppointment(booking_id=bid, identity_id=iid, sub_account_id=subs[1].id,
                               slot_id=slot.id, booked_at=main._now()))
    db.commit(); db.close()
    return iid, subs[1].username


def test_booked_list_has_one_row_per_identity_naming_the_winning_subaccount(client):
    """"If any one of the sub-accounts booked, that identity is mission completed." An identity is a
    PERSON; its sub-accounts are login attempts on their behalf, so this lists the person once and
    names the account that actually got through."""
    h = _operate_headers(client)
    iid, winner = _book_identity(client, h, name="ANA BOOKED")

    rows = client.get("/identities/booked", headers=h).json()
    assert len(rows) == 1
    assert rows[0]["identity_id"] == iid
    assert rows[0]["sub_account_username"] == winner
    assert rows[0]["posto_id"] == "2032" and rows[0]["slot_date"] == "2026-09-14"


def test_a_booked_identity_disappears_from_the_attach_list(client):
    """The attach panel's only source is /identities. Offering a person who already HAS an
    appointment invites a second one for the same person."""
    h = _operate_headers(client)
    iid, _ = _book_identity(client, h, name="BEA BOOKED")
    client.post("/identities", json={"full_name": "CARL FREE", "type": "real"}, headers=h)

    names = [i["full_name"] for i in client.get("/identities", headers=h).json()]
    assert "BEA BOOKED" not in names
    assert "CARL FREE" in names


def test_booked_list_filters_by_type_posto_and_date(client):
    h = _operate_headers(client)
    _book_identity(client, h, name="REAL ONE", itype="real", posto="2032", booking="B-A")
    _book_identity(client, h, name="TEST ONE", itype="test", posto="3018", booking="B-B")

    assert len(client.get("/identities/booked?type=real", headers=h).json()) == 1
    assert len(client.get("/identities/booked?posto_id=3018", headers=h).json()) == 1
    assert len(client.get("/identities/booked?type=real&posto_id=3018", headers=h).json()) == 0
    future = "2099-01-01T00:00:00"
    assert client.get(f"/identities/booked?date_from={future}", headers=h).json() == []


def test_credentials_export_is_admin_only(client):
    """The ONE endpoint that returns stored passwords. An operator seat is not enough."""
    h = _operate_headers(client)                       # bootstrapped admin holds the seat
    iid, _ = _book_identity(client, h, name="DORA BOOKED")

    client.post("/auth/users", json={"username": "op1", "password": "pw", "role": "operator"},
                headers=h)
    r = client.post("/auth/login", json={"username": "op1", "password": "pw", "purpose": "view"})
    oph = {"Authorization": f"Bearer {r.json()['token']}"}

    assert client.post("/identities/booked/credentials", json={"identity_ids": [iid]},
                       headers=oph).status_code == 403
    ok = client.post("/identities/booked/credentials", json={"identity_ids": [iid]}, headers=h)
    assert ok.status_code == 200 and ok.json()["count"] == 1
    assert ok.json()["rows"][0]["password"]


def test_credentials_export_only_returns_identities_that_actually_booked(client):
    """It must not be usable to dump the account pool -- only identities WITH a booking."""
    h = _operate_headers(client)
    unbooked = client.post("/identities", json={"full_name": "EVE FREE", "type": "real"},
                           headers=h).json()["id"]
    client.post("/identities/gen-subaccounts", json={"identity_ids": [unbooked], "count": 2},
                headers=h)

    r = client.post("/identities/booked/credentials", json={"identity_ids": [unbooked]}, headers=h)
    assert r.status_code == 200 and r.json()["count"] == 0


def test_appointments_default_to_the_current_run(client):
    """A booking page showing appointments from PREVIOUS runs makes a run that booked nothing look
    like it booked three. The cut is booked_at >= started_at, done server-side."""
    import apps.hub.main as main
    from datetime import timedelta
    from libs.db import models as M

    h = _operate_headers(client)
    iid, _ = _book_identity(client, h, name="OLD RUN", booking="RunFilter")
    db = main.SessionLocal()
    from sqlalchemy import select as _sel
    bk = db.scalar(_sel(M.Booking).where(M.Booking.name == "RunFilter"))
    appt = db.scalar(_sel(M.BookedAppointment).where(M.BookedAppointment.booking_id == bk.id))
    appt.booked_at = main._now() - timedelta(days=2)          # a previous run's result
    bk.started_at = main._now() - timedelta(minutes=10)       # this run started after it
    db.commit(); bid = bk.id; db.close()

    assert client.get(f"/bookings/{bid}/appointments?since_started=true", headers=h).json() == []
    assert len(client.get(f"/bookings/{bid}/appointments", headers=h).json()) == 1


def test_appointments_carry_the_identity_name_from_the_server(client):
    """The dashboard used to resolve these against /identities, which no longer returns identities
    that have booked -- and every appointment is by definition one of those, so every name silently
    degraded to an id fragment."""
    h = _operate_headers(client)
    iid, winner = _book_identity(client, h, name="NAMED PERSON", booking="NameRes")

    rows = client.get("/bookings/"
                      + client.get("/bookings", headers=h).json()[0]["id"]
                      + "/appointments", headers=h).json()
    assert rows and rows[0]["identity_name"] == "NAMED PERSON"
    assert rows[0]["sub_account_username"] == winner


def test_an_appointment_is_never_hidden_by_a_missing_name(client):
    """The name joins are decoration; the appointment is the record. An inner join would let a
    missing identity or sub-account row hide a real booking -- caught by an existing test when this
    endpoint first grew its joins."""
    import apps.hub.main as main
    from libs.db import models as M

    h = _operate_headers(client)
    bid = client.post("/bookings", json={"name": "Dangling", "type": "test", "posto_id": "2032"},
                      headers=h).json()["id"]
    db = main.SessionLocal()
    i = M.Identity(full_name="GHOST", type="real")
    db.add(i); db.commit()
    db.add(M.BookedAppointment(booking_id=bid, identity_id=i.id,
                               sub_account_id="no-such-sub-account", booked_at=main._now()))
    db.commit(); db.close()

    rows = client.get(f"/bookings/{bid}/appointments", headers=h).json()
    assert len(rows) == 1
    assert rows[0]["identity_name"] == "GHOST"
    assert rows[0]["sub_account_username"] is None


def test_attach_refuses_identity_that_already_holds_an_appointment(client):
    """An identity with a real BookedAppointment can never warm -- the portal answers
    ScheduleController with "already has an application registered with a future schedule", so the
    worker burns a login, a CAPTCHA solve and a proxy lease before failing.

    The guard deliberately keys on the APPOINTMENT ROW, not Identity.status. Confirmed live
    2026-08-26: an identity that booked on 2026-08-18 was still ACTIVE eight days later (the
    booking flow wrote the appointment and never marked the identity), and two others were in the
    same state -- so the derived status field cannot be trusted for this. This test reproduces that
    exact shape: status left ACTIVE, appointment present.
    """
    from apps.hub.db import get_db
    from libs.db import models as M

    h = _operate_headers(client)
    iid = client.post("/identities", json={"full_name": "Already Booked", "type": "real"},
                      headers=h).json()["id"]
    bid = client.post("/bookings", json={"name": "b", "type": "real", "posto_id": "5084"},
                      headers=h).json()["id"]

    # The appointment came from an EARLIER booking, which is the real shape: the identity booked
    # once, that booking ended, and the identity was later offered again.
    prior = client.post("/bookings", json={"name": "prior", "type": "real", "posto_id": "5084"},
                        headers=h).json()["id"]
    db = next(app.dependency_overrides[get_db]())
    sa = M.SubAccount(identity_id=iid, username="u1", password="test-password", email="u1@e.test")
    db.add(sa); db.commit()
    # Status stays ACTIVE on purpose -- that is the stale state observed in production.
    db.add(M.BookedAppointment(identity_id=iid, booking_id=prior, sub_account_id=sa.id))
    db.commit()
    assert db.get(M.Identity, iid).status != "booked"

    r = client.post(f"/bookings/{bid}/identities", json={"identity_id": iid}, headers=h)
    assert r.status_code == 409, r.text
    assert "already holds a booked appointment" in r.json()["detail"]

    # and a clean identity still attaches
    ok = client.post("/identities", json={"full_name": "Clean", "type": "real"},
                     headers=h).json()["id"]
    assert client.post(f"/bookings/{bid}/identities", json={"identity_id": ok},
                       headers=h).status_code == 201


# ── a rotating launch must not run a per-row portal health probe (2026-08-31) ────────────────
def test_static_pool_helper_only_treats_fixed_exits_as_screenable():
    from apps.hub.main import _static_pool
    assert _static_pool("static") is True
    assert _static_pool("isp") is True
    assert _static_pool("ISP") is True
    assert _static_pool("rotating") is False, \
        "a rotating row has no fixed exit to screen -- probing it condemned 4,489 good rows"
    assert _static_pool("mobile") is False
    assert _static_pool(None) is False
    assert _static_pool("") is False

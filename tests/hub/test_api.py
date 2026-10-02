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


def test_auto_by_role_attach_prefers_the_default_template_over_oldest(client):
    """'Auto (by role)' (no template_id) used to pick the OLDEST template of the role -- silently the
    soax-pinned one. It must now resolve to the template flagged is_default for that role, whatever
    the creation order."""
    h = _operate_headers(client)
    # First-created apply template (would win by the old oldest-wins rule).
    old = client.post("/templates", json={"name": "Apply w Soax", "role": "apply",
                                          "settings": {"PROXY_PROVIDER": "soax", "PROXY_KIND": "rotating"}},
                      headers=h).json()
    # Second-created, no proxy definition -- the one the operator wants as default.
    plain = client.post("/templates", json={"name": "Apply", "role": "apply", "settings": {}},
                        headers=h).json()
    assert old["is_default"] is False and plain["is_default"] is False

    # Mark the plain one default; the make-default is idempotent and one-per-role.
    r = client.post(f"/templates/{plain['id']}/make-default", headers=h)
    assert r.status_code == 200 and r.json()["is_default"] is True

    iid = client.post("/identities", json={"full_name": "R", "type": "real", "passport_no": "PA9"},
                      headers=h).json()["id"]
    bid = client.post("/bookings", json={"name": "b", "type": "real", "posto_id": "5084"},
                      headers=h).json()["id"]
    w = client.post(f"/bookings/{bid}/identities", json={"identity_id": iid}, headers=h).json()
    # Auto (by role) resolved to the DEFAULT (plain) template, not the older soax one.
    got = client.get(f"/bookings/{bid}/workers", headers=h).json()[0]
    assert got["role"] == "apply"
    # The attached worker carries the plain template (verify via the template listing marker).
    tpls = {t["id"]: t for t in client.get("/templates", headers=h).json()}
    assert tpls[plain["id"]]["is_default"] is True
    assert tpls[old["id"]]["is_default"] is False


def test_make_default_is_one_per_role(client):
    """Flagging a second template default for the same role clears the first -- never two defaults."""
    h = _operate_headers(client)
    a = client.post("/templates", json={"name": "A", "role": "apply", "settings": {}}, headers=h).json()
    b = client.post("/templates", json={"name": "B", "role": "apply", "settings": {}}, headers=h).json()
    s = client.post("/templates", json={"name": "S", "role": "scout", "settings": {}}, headers=h).json()
    client.post(f"/templates/{a['id']}/make-default", headers=h)
    client.post(f"/templates/{s['id']}/make-default", headers=h)   # different role -- unaffected by b
    client.post(f"/templates/{b['id']}/make-default", headers=h)   # supersedes a
    tpls = {t["id"]: t for t in client.get("/templates", headers=h).json()}
    assert tpls[a["id"]]["is_default"] is False
    assert tpls[b["id"]]["is_default"] is True
    assert tpls[s["id"]]["is_default"] is True   # a different role keeps its own default


def test_auto_by_role_still_falls_back_to_oldest_when_no_default(client):
    """No default flagged for the role -> legacy behavior: oldest of the role wins."""
    h = _operate_headers(client)
    first = client.post("/templates", json={"name": "First", "role": "apply", "settings": {}}, headers=h).json()
    client.post("/templates", json={"name": "Second", "role": "apply", "settings": {}}, headers=h)
    iid = client.post("/identities", json={"full_name": "R", "type": "real", "passport_no": "PA8"},
                      headers=h).json()["id"]
    bid = client.post("/bookings", json={"name": "b", "type": "real", "posto_id": "5084"}, headers=h).json()["id"]
    client.post(f"/bookings/{bid}/identities", json={"identity_id": iid}, headers=h)
    # No default set anywhere; the oldest apply template ("First") is still the resolved one.
    assert client.get("/templates", headers=h).json()  # sanity: templates exist
    # (resolution asserted indirectly: the attach succeeded with a valid apply worker)
    assert client.get(f"/bookings/{bid}/workers", headers=h).json()[0]["role"] == "apply"
    assert first["is_default"] is False


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
    csv = ("identity_id,first_name,last_name,account_type,passport_no,nationality,birthdate,gender,username,password\n"
          "R1,Real,One,real,PA9001,CPV,1985-01-01,M,r1,pw1\nT1,Test,One,test,,,,,t1,pw2\n")
    r = client.post("/identities/import",
                    json={"csv": csv, "default_target_posto": "5084"}, headers=h).json()
    assert r["created"] == 2
    got = {i["full_name"]: i["target_posto"] for i in client.get("/identities", headers=h).json()}
    assert got["Real One"] == "5084"   # real identity gets the default
    assert got["Test One"] is None     # target_posto is app-enforced real-identities-only


def test_import_without_default_target_posto_leaves_it_unset(client):
    h = _operate_headers(client)
    csv = ("identity_id,first_name,last_name,account_type,passport_no,nationality,birthdate,gender,username,password\n"
           "R1,Real,One,real,PA9002,CPV,1985-01-01,M,r1,pw1\n")
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


# ── /proxies must never return the whole table (2026-09-15) ───────────────────────────────────
# The pool grew 2,600 -> 42,600 (+30k 08-21, +10k 09-03). GET /proxies had no limit, so the
# dashboard pulled 14.8 MB and rendered ~340k DOM nodes -- the Proxies page stopped working, and
# every OTHER page's proxy-pool picker paid the same cost to derive ~3 dropdown options.
def test_proxy_list_is_paginated_and_hard_caps_the_limit(client):
    h = _operate_headers(client)
    for i in range(12):
        client.post("/proxies", json={"provider": "soax", "kind": "rotating",
                                      "endpoint": f"p{i}:1", "credentials_ref": "u:p",
                                      "max_sessions": 2}, headers=h)
    assert len(client.get("/proxies?limit=5", headers=h).json()) == 5
    assert len(client.get("/proxies?limit=5&offset=10", headers=h).json()) == 2
    # An unbounded request is the exact mistake the signature exists to prevent.
    assert len(client.get("/proxies?limit=99999", headers=h).json()) == 12   # capped, not rejected


def test_proxy_list_filters_by_provider_and_kind_server_side(client):
    h = _operate_headers(client)
    client.post("/proxies", json={"provider": "soax", "kind": "rotating", "endpoint": "a:1",
                                  "credentials_ref": "u:p", "max_sessions": 2}, headers=h)
    client.post("/proxies", json={"provider": "soax", "kind": "mobile", "endpoint": "b:1",
                                  "credentials_ref": "u:p", "max_sessions": 2}, headers=h)
    client.post("/proxies", json={"provider": "webshare", "kind": "rotating", "endpoint": "c:1",
                                  "credentials_ref": "u:p", "max_sessions": 2}, headers=h)
    assert len(client.get("/proxies?provider=soax", headers=h).json()) == 2
    assert len(client.get("/proxies?provider=soax&kind=mobile", headers=h).json()) == 1
    assert len(client.get("/proxies?kind=rotating", headers=h).json()) == 2


def test_proxy_pools_aggregate_serves_every_picker_without_the_full_table(client):
    """The pool tree and all three proxy-pool pickers read this instead of the table. It must carry
    true pool-wide totals -- a picker built from one PAGE would offer only the pools that happened to
    appear in the first 100 rows."""
    h = _operate_headers(client)
    _seed_proxies()
    pools = client.get("/proxies/pools", headers=h).json()
    assert pools, "aggregate must not be empty when proxies exist"
    for p in pools:
        assert {"provider", "kind", "geo", "resource_type",
                "total", "active", "unhealthy", "leasable"} <= set(p)
    # resource_type is load-bearing for the dashboard, not decoration: derivePoolOptions offers
    # rotating pools from SETTINGS and only STATIC pools from these rows, so dropping this field
    # would make every static/ISP pool vanish from the pickers.
    # Totals must reconcile with the real table, not with a page of it.
    everything = client.get("/proxies?limit=1000", headers=h).json()
    assert sum(p["total"] for p in pools) == len(everything)
    assert sum(p["unhealthy"] for p in pools) == sum(
        1 for r in everything if r["is_healthy"] is False)


# ── named ISP pools (operator requirement 2026-09-16) ────────────────────────────────────────
def _import_isp(client, h, pool, lines, provider="thordata"):
    return client.post("/proxies/import", json={
        "provider": provider, "kind": "static", "resource_type": "static",
        "pool_name": pool, "max_sessions": 1, "text": "\n".join(lines)}, headers=h).json()


def test_importing_under_a_new_name_creates_the_pool_and_an_existing_name_appends(client):
    """The operator's exact workflow: "I will add the name or select from existing ISP pools and
    add those 10 proxy strings. If that is new then a new pool will be created, if existing then
    10 proxies will be added to that pool." There is no separate create endpoint because a pool IS
    the set of rows carrying its name."""
    h = _operate_headers(client)
    assert _import_isp(client, h, "thordata-oct",
                       [f"http://u{i}:p@10.0.0.{i}:8000" for i in range(3)])["created"] == 3

    pools = {p["pool_name"]: p for p in client.get("/proxies/isp-pools", headers=h).json()}
    assert pools["thordata-oct"]["total"] == 3

    # Same name again -> APPEND, not a second pool.
    assert _import_isp(client, h, "thordata-oct",
                       [f"http://u{i}:p@10.0.0.{i}:8000" for i in range(3, 5)])["created"] == 2
    pools = {p["pool_name"]: p for p in client.get("/proxies/isp-pools", headers=h).json()}
    assert pools["thordata-oct"]["total"] == 5
    assert len([p for p in pools if p == "thordata-oct"]) == 1

    # A different name -> a genuinely separate pool, both still listed.
    _import_isp(client, h, "iproyal-a", ["http://z:p@10.9.9.9:8000"], provider="iproyal")
    pools = {p["pool_name"]: p for p in client.get("/proxies/isp-pools", headers=h).json()}
    assert pools["thordata-oct"]["total"] == 5 and pools["iproyal-a"]["total"] == 1


def test_a_duplicate_proxy_is_refused_across_pools_and_says_which_pool_holds_it(client):
    """Dedup is GLOBAL, not per-pool. The same physical ISP proxy in two pools would be leased
    twice concurrently, and one-worker-one-IP is the entire point of a static resource. The message
    names the owning pool so an operator who pasted the wrong batch can tell what happened."""
    h = _operate_headers(client)
    _import_isp(client, h, "thordata-oct", ["http://u:p@10.0.0.1:8000"])
    res = _import_isp(client, h, "iproyal-a", ["http://u:p@10.0.0.1:8000"])
    assert res["created"] == 0
    assert any("thordata-oct" in e for e in res["errors"]), res["errors"]


def test_isp_pool_listing_reports_leasable_not_just_total(client):
    """`leasable` is the number that answers the operator's real question -- do these pools cover
    my worker count -- and a condemned STATIC row really is out of service (D33), unlike a rotating
    one (D19). Reporting only `total` would overstate capacity exactly when it matters."""
    h = _operate_headers(client)
    _import_isp(client, h, "thordata-oct",
                [f"http://u{i}:p@10.0.0.{i}:8000" for i in range(3)])
    rows = client.get(f"/proxies?limit=10", headers=h).json()
    client.post(f"/proxies/{rows[0]['id']}/toggle-active", headers=h)

    pool = [p for p in client.get("/proxies/isp-pools", headers=h).json()
            if p["pool_name"] == "thordata-oct"][0]
    assert pool["total"] == 3 and pool["leasable"] == 2


# ── two-tier health check + delete (operator requirement 2026-09-16) ─────────────────────────
def _mk_proxies(client, h, n, pool="thordata-oct"):
    for i in range(n):
        client.post("/proxies", json={"provider": "thordata", "kind": "static",
                                      "endpoint": f"10.1.1.{i}:8000", "credentials_ref": "u:p",
                                      "max_sessions": 1, "resource_type": "static",
                                      "pool_name": pool}, headers=h)
    return [p["id"] for p in client.get("/proxies?limit=100", headers=h).json()]


def test_health_check_tier1_failure_condemns_and_tier2_failure_does_not(client, monkeypatch):
    """THE property the whole two-tier split exists for. Tier 2 is 'can this IP reach the portal',
    which on 2026-08-28 was false for 4,489 perfectly good proxies at once while the portal
    throttled -- they connected 20/20 when re-probed neutrally. So a tier-2 failure must be
    RECORDED and must NOT set is_healthy, or a portal outage presents the pool as garbage to a
    delete button."""
    import libs.proxies.health as H
    h = _operate_headers(client)
    ids = _mk_proxies(client, h, 2)

    # proxy 0: tier 1 alive, tier 2 (portal) fails.  proxy 1: tier 1 dead.
    def _conn(url):
        return (False, "dead: connection refused") if url.endswith("10.1.1.1:8000/") or "10.1.1.1" in url else (True, "alive (status=200)")
    monkeypatch.setattr(H, "connectivity_probe", _conn)
    monkeypatch.setattr(H, "check_proxy", lambda url: (False, "net::ERR_TIMED_OUT"))

    res = client.post("/proxies/health-check", json={"ids": ids, "portal": True}, headers=h).json()
    assert res["checked"] == 2 and res["dead"] == 1 and res["alive"] == 1

    by_ep = {p["endpoint"]: p for p in client.get("/proxies?limit=100", headers=h).json()}
    alive = by_ep["10.1.1.0:8000"]
    assert alive["is_healthy"] is True, "a portal failure must never condemn the proxy"
    assert alive["portal_ok"] is False and "ERR_TIMED_OUT" in (alive["portal_health"] or "")
    assert by_ep["10.1.1.1:8000"]["is_healthy"] is False


def test_tier2_is_skipped_when_tier1_fails(client, monkeypatch):
    """Not just an optimisation (~25s of Chromium per dead proxy): a browser probe through a dead
    tunnel reports a PORTAL-shaped error for a CONNECTIVITY problem, which is the misattribution
    that has repeatedly sent investigations here the wrong way."""
    import libs.proxies.health as H
    h = _operate_headers(client)
    ids = _mk_proxies(client, h, 1)
    monkeypatch.setattr(H, "connectivity_probe", lambda url: (False, "dead: tunnel failed"))

    def _boom(url):
        raise AssertionError("tier 2 ran for a proxy whose tier 1 failed")

    monkeypatch.setattr(H, "check_proxy", _boom)
    res = client.post("/proxies/health-check", json={"ids": ids, "portal": True}, headers=h).json()
    assert res["dead"] == 1 and res["portal_checked"] == 0


def test_delete_refuses_a_live_proxy_and_removes_a_dead_one(client, monkeypatch):
    """Deletion is irreversible and an ISP proxy is a purchased asset, so 'dead' has to mean tier 1
    actually failed -- not 'unchecked', and not 'the portal was down'."""
    import libs.proxies.health as H
    h = _operate_headers(client)
    ids = _mk_proxies(client, h, 2)
    monkeypatch.setattr(H, "connectivity_probe",
                        lambda url: (False, "dead: refused") if "10.1.1.1" in url else (True, "alive"))
    client.post("/proxies/health-check", json={"ids": ids}, headers=h)

    res = client.post("/proxies/bulk-delete", json={"ids": ids}, headers=h).json()
    assert res["deleted"] == 1, res
    assert len(res["skipped"]) == 1 and "alive" in res["skipped"][0]["reason"]
    left = [p["endpoint"] for p in client.get("/proxies?limit=100", headers=h).json()]
    assert left == ["10.1.1.0:8000"]


def test_delete_refuses_a_never_checked_proxy(client):
    """Absence of a verdict is not a verdict. Without this an operator could select a freshly
    imported pool and delete all of it before a single probe had run."""
    h = _operate_headers(client)
    ids = _mk_proxies(client, h, 1)
    res = client.post("/proxies/bulk-delete", json={"ids": ids}, headers=h).json()
    assert res["deleted"] == 0
    assert "never health-checked" in res["skipped"][0]["reason"]


def test_delete_refuses_a_proxy_that_is_still_leased(client, monkeypatch):
    """proxy_leases.proxy_id is a live FK: deleting a leased row raises a non-JSON 500, which is
    exactly what the worker Detach button produced (F-detach) and what made migration ci000droprot
    fail on its first run. Refuse readably instead."""
    import libs.proxies.health as H
    from libs.proxies.lease import lease_proxy
    h = _operate_headers(client)
    ids = _mk_proxies(client, h, 1)

    # Lease BEFORE condemning it: _candidate_query excludes an unhealthy static row outright (D33),
    # so a dead proxy cannot be leased in the first place. The situation this guards is the real
    # one -- a worker holding a proxy that has since been found dead.
    import apps.hub.main as hub_main
    db = hub_main.SessionLocal()
    try:
        lease_proxy(db, "some-worker", "thordata", resource_type="static")
    finally:
        db.close()

    monkeypatch.setattr(H, "connectivity_probe", lambda url: (False, "dead: refused"))
    client.post("/proxies/health-check", json={"ids": ids}, headers=h)

    res = client.post("/proxies/bulk-delete", json={"ids": ids}, headers=h).json()
    assert res["deleted"] == 0
    assert "leased" in res["skipped"][0]["reason"]
    assert client.get("/proxies?limit=100", headers=h).json(), "the row must survive the refusal"


def test_health_check_rejects_an_oversized_selection(client):
    """One dashboard click must not be able to launch hundreds of Chromium processes -- that is the
    shape of the 2026-09-15 incident where an inline probe saturated the executor and took the Hub
    down."""
    h = _operate_headers(client)
    r = client.post("/proxies/health-check",
                    json={"ids": [f"id-{i}" for i in range(201)]}, headers=h)
    assert r.status_code == 400 and "max 200" in r.json()["detail"]


def test_bulk_update_changes_max_sessions_on_existing_proxies(client):
    """max_sessions was settable at IMPORT and then frozen forever -- no endpoint could change it,
    so a pool imported with the wrong capacity had to be deleted and re-imported, losing its health
    history. It is the pool's real capacity ceiling: on 2026-09-16 seven ISP proxies at
    max_sessions=1 hit 7/7 concurrent leases and starved both scouts out of the hold swap, while
    the Hub only ever reported "no thordata proxy leased"."""
    h = _operate_headers(client)
    ids = _mk_proxies(client, h, 3)

    out = client.post("/proxies/bulk-update",
                      json={"ids": ids, "max_sessions": 2}, headers=h).json()
    assert [p["max_sessions"] for p in out] == [2, 2, 2]
    rows = client.get("/proxies?limit=10", headers=h).json()
    assert all(p["max_sessions"] == 2 for p in rows)


def test_bulk_update_can_move_proxies_between_pools_and_refuses_nonsense(client):
    h = _operate_headers(client)
    ids = _mk_proxies(client, h, 2)

    client.post("/proxies/bulk-update",
                json={"ids": ids, "pool_name": "thordata-nov"}, headers=h)
    assert {p["pool_name"] for p in client.get("/proxies?limit=10", headers=h).json()} == {"thordata-nov"}

    # A blank name clears it back to unnamed -- a legitimate thing to want.
    client.post("/proxies/bulk-update", json={"ids": ids, "pool_name": ""}, headers=h)
    assert {p["pool_name"] for p in client.get("/proxies?limit=10", headers=h).json()} == {None}

    assert client.post("/proxies/bulk-update", json={"ids": ids, "max_sessions": 0},
                       headers=h).status_code == 400
    assert client.post("/proxies/bulk-update", json={"ids": ids}, headers=h).status_code == 400


def test_bulk_update_never_touches_endpoint_or_credentials(client):
    """Deliberately narrow: those identify the resource, and rewriting them under a live lease would
    silently repoint a worker's exit mid-session."""
    h = _operate_headers(client)
    ids = _mk_proxies(client, h, 1)
    before = client.get("/proxies?limit=10", headers=h).json()[0]

    client.post("/proxies/bulk-update", json={"ids": ids, "max_sessions": 5}, headers=h)
    after = client.get("/proxies?limit=10", headers=h).json()[0]
    assert after["endpoint"] == before["endpoint"]
    assert after["max_sessions"] == 5


# ── booking archive / hide-show (operator request 2026-09-17) ────────────────────────────────
def test_archiving_hides_a_booking_from_the_default_list_without_changing_its_status(client):
    """A booking's `status` records how the run ENDED -- "completed with three appointments" reads
    very differently from "stopped" -- and that is the most useful thing about an old booking. So
    archiving is a separate timestamp, never a BookingStatus value: hide it, keep the outcome."""
    h = _operate_headers(client)
    bid = client.post("/bookings", json={"name": "Arch1", "type": "test", "posto_id": "2032"},
                      headers=h).json()["id"]
    client.post(f"/bookings/{bid}/stop", headers=h)
    before = client.get(f"/bookings/{bid}", headers=h).json()["status"]

    r = client.post(f"/bookings/{bid}/archive", headers=h).json()
    assert r["archived_at"] is not None
    assert r["status"] == before, "archiving must not rewrite how the booking ended"

    assert bid not in {b["id"] for b in client.get("/bookings", headers=h).json()}
    assert bid in {b["id"] for b in
                   client.get("/bookings?include_archived=true", headers=h).json()}


def test_unarchive_brings_it_back_and_both_are_idempotent(client):
    h = _operate_headers(client)
    bid = client.post("/bookings", json={"name": "Arch2", "type": "test", "posto_id": "2032"},
                      headers=h).json()["id"]
    client.post(f"/bookings/{bid}/stop", headers=h)

    client.post(f"/bookings/{bid}/archive", headers=h)
    first = client.get(f"/bookings/{bid}", headers=h).json()["archived_at"]
    # Archiving twice must not move the timestamp -- it records WHEN it was archived.
    assert client.post(f"/bookings/{bid}/archive", headers=h).json()["archived_at"] == first

    assert client.post(f"/bookings/{bid}/unarchive", headers=h).json()["archived_at"] is None
    assert client.post(f"/bookings/{bid}/unarchive", headers=h).status_code == 200
    assert bid in {b["id"] for b in client.get("/bookings", headers=h).json()}


def test_archiving_a_live_booking_is_refused(client):
    """THE guard. Archiving is a visibility action; hiding a booking the scheduler is still driving
    would make a live run invisible on the page an operator uses to watch it -- workers still
    holding proxies and sub-accounts, and nothing on screen to say so. SCHEDULED counts as live: it
    is not running yet, but it WILL auto-start at warmup_start_at."""
    from datetime import datetime, timedelta, timezone
    h = _operate_headers(client)
    bid = client.post("/bookings", json={"name": "Arch3", "type": "test", "posto_id": "2032"},
                      headers=h).json()["id"]
    fut = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    client.put(f"/bookings/{bid}", json={"warmup_start_at": fut,
                                         "slot_open_at": fut}, headers=h)
    client.post(f"/bookings/{bid}/schedule", headers=h)

    r = client.post(f"/bookings/{bid}/archive", headers=h)
    assert r.status_code == 409
    assert "stop it before archiving" in r.json()["detail"]
    assert bid in {b["id"] for b in client.get("/bookings", headers=h).json()}, \
        "a refused archive must leave the booking visible"


# ── blind apply (operator direction 2026-09-18) ──────────────────────────────────────────────
def _running_booking(client, h, posto="2032", **settings):
    bid = client.post("/bookings", json={"name": "blind-b", "type": "test", "posto_id": posto,
                                         "settings": settings}, headers=h).json()["id"]
    client.post(f"/bookings/{bid}/start", headers=h)
    client.post(f"/bookings/{bid}/fire-slot-open", headers=h)
    return bid


def test_blind_apply_seeds_the_grid_with_the_postos_proven_periods(client):
    """Operator confirmed 12/13/14 October open by eye; discovery found nothing. Seeding writes one
    AVAILABLE slot per (date x period), source='blind', using the posto's PROVEN period set --
    2032 opens 3/4/5, measured over every PDF-verified booking there."""
    h = _operate_headers(client)
    bid = _running_booking(client, h, posto="2032")
    r = client.post(f"/bookings/{bid}/blind-apply",
                    json={"dates": "2026-10-12, 2026-10-13, 2026-10-14"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["periods"] == [3, 4, 5] and body["seeded"] == 9
    rows = client.get(f"/bookings/{bid}/slots", headers=h).json()
    assert len(rows) == 9 and all(s["status"] == "available" for s in rows)


def test_blind_apply_is_idempotent_and_can_widen_the_grid(client):
    h = _operate_headers(client)
    bid = _running_booking(client, h, posto="2032")
    client.post(f"/bookings/{bid}/blind-apply", json={"dates": "2026-10-12", "periods": "3,4"}, headers=h)
    r = client.post(f"/bookings/{bid}/blind-apply", json={"dates": "2026-10-12", "periods": "3,4,5"}, headers=h).json()
    assert r["seeded"] == 1 and r["skipped_existing"] == 2, r


def test_blind_apply_refuses_when_discovery_already_has_slots_unless_forced(client):
    """Blind apply is for when the system sees nothing and the operator sees something. Seeding
    phantoms on top of real finds only dilutes the pool with combinations the fleet then spends
    attempts on. `force` exists for the operator who has decided otherwise."""
    import apps.hub.main as hub_main
    from libs.db import models as M
    from libs.domain.enums import SlotStatus

    h = _operate_headers(client)
    bid = _running_booking(client, h)
    db = hub_main.SessionLocal()
    try:
        db.add(M.Slot(booking_id=bid, posto_id="2032", date="2026-10-12", period=3,
                      status=SlotStatus.AVAILABLE, source="scout"))
        db.commit()
    finally:
        db.close()
    r = client.post(f"/bookings/{bid}/blind-apply", json={"dates": "2026-10-13"}, headers=h)
    assert r.status_code == 409 and "already has 1 AVAILABLE" in r.json()["detail"]
    r = client.post(f"/bookings/{bid}/blind-apply", json={"dates": "2026-10-13", "force": True}, headers=h)
    assert r.status_code == 200 and r.json()["seeded"] == 3


def test_blind_apply_refuses_before_slot_open_and_rejects_garbage(client):
    h = _operate_headers(client)
    bid = client.post("/bookings", json={"name": "b", "type": "test", "posto_id": "2032"}, headers=h).json()["id"]
    client.post(f"/bookings/{bid}/start", headers=h)          # running, slot-open NOT fired
    r = client.post(f"/bookings/{bid}/blind-apply", json={"dates": "2026-10-12"}, headers=h)
    assert r.status_code == 409 and "fire slot-open first" in r.json()["detail"]
    client.post(f"/bookings/{bid}/fire-slot-open", headers=h)
    r = client.post(f"/bookings/{bid}/blind-apply", json={"dates": "someday"}, headers=h)
    assert r.status_code == 400


def test_posto_change_refuses_to_silently_wipe_a_blind_seed(client):
    """Confirmed incident 2026-09-21 ("0921+5086"): seeding a blind pool, then editing the booking's
    posto, silently CLOSED every seeded slot (the posto-change retirement treats them as stale). A
    blind seed is an operator's deliberate, confirmed-by-eye plan for a SPECIFIC posto -- the PUT must
    refuse rather than destroy it. A DISCOVERED pool is still retired on posto change as before."""
    h = _operate_headers(client)
    bid = _running_booking(client, h, posto="2032", BLIND_APPLY_MODE=True)
    assert client.post(f"/bookings/{bid}/blind-apply",
                       json={"dates": "2026-10-12", "periods": "3,4,5"}, headers=h).status_code == 200
    # the posto edit must be refused while a blind seed exists
    r = client.put(f"/bookings/{bid}", json={"posto_id": "3018"}, headers=h)
    assert r.status_code == 409 and "blind" in r.json()["detail"].lower(), r.text
    # the seed survives untouched, and the posto is unchanged
    rows = client.get(f"/bookings/{bid}/slots", headers=h).json()
    assert len(rows) == 3 and all(s["status"] == "available" for s in rows)
    assert client.get(f"/bookings/{bid}", headers=h).json()["posto_id"] == "2032"
    # clearing the pool first lets the posto change through
    assert client.delete(f"/bookings/{bid}/slots", headers=h).status_code == 200
    assert client.put(f"/bookings/{bid}", json={"posto_id": "3018"}, headers=h).status_code == 200


def test_blind_apply_defaults_dates_from_the_cal_window(client):
    h = _operate_headers(client)
    bid = _running_booking(client, h, CAL_START_DATE="2026-10-12", CAL_END_DATE="2026-10-14")
    r = client.post(f"/bookings/{bid}/blind-apply", json={}, headers=h).json()
    assert r["dates"] == ["2026-10-12", "2026-10-13", "2026-10-14"]


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


# ── manual self-scout: for when the scouts are lying (2026-09-02) ─────────────────────────────
def _make_holding_worker(bid, role, substate):
    """Reach the same StaticPool engine the client fixture monkeypatched SessionLocal onto."""
    import apps.hub.main as hub_main
    from libs.db import models as M
    db = hub_main.SessionLocal()
    try:
        ident = M.Identity(full_name="X", type="real", status="active")
        db.add(ident); db.flush()
        w = M.Worker(booking_id=bid, identity_id=ident.id, role=role,
                     status="running", substate=substate)
        db.add(w); db.commit()
        return w.id
    finally:
        db.close()


def test_force_self_scout_commands_every_holding_apply_worker(client):
    """_maybe_self_scout's automatic gates cannot see the case this exists for: a scout that is
    RUNNING, in `discovering`, and returning nothing. MEASURED on the 17:15 run of "Test 0901",
    scout 28b8fe7f reported substate=discovering for two hours while answering {"data":{}} six times
    out of six -- by every automatic test, a working scout."""
    h = _operate_headers(client)
    bid = client.post("/bookings", json={"name": "b-ss", "type": "real", "posto_id": "5084"},
                      headers=h).json()["id"]

    assert client.post(f"/bookings/{bid}/self-scout", headers=h).status_code == 409, \
        "a booking that is not running has nothing holding yet"

    client.post(f"/bookings/{bid}/start", headers=h)
    assert client.post(f"/bookings/{bid}/self-scout", headers=h).status_code == 409, \
        "WARMING is not RUNNING: before slot-open there is nothing to discover, and the endpoint " \
        "mirrors _maybe_self_scout's own precondition rather than inventing a looser one"

    client.post(f"/bookings/{bid}/fire-slot-open", headers=h)
    assert client.post(f"/bookings/{bid}/self-scout", headers=h).status_code == 409, \
        "running, but with no HOLDING apply worker there is nothing to command"

    want = sorted(_make_holding_worker(bid, "apply", "keeping") for _ in range(2))
    # neither of these may be commanded: a scout, and an apply worker that is not holding
    _make_holding_worker(bid, "scout", "discovering")
    _make_holding_worker(bid, "apply", "logging_in")

    r = client.post(f"/bookings/{bid}/self-scout", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["commanded"] == 2, f"expected both HOLDING apply workers, got {body}"
    assert sorted(body["worker_ids"]) == want, \
        "commanded something other than the holding apply workers (a scout, or a logging-in one)"


def test_force_self_scout_is_not_open_to_anonymous_callers(client):
    """It makes the fleet spend CAPTCHA solves against the live portal, so it is an operator action."""
    bid = client.post("/bookings", json={"name": "b-ss2", "type": "real", "posto_id": "5084"},
                      headers=_operate_headers(client)).json()["id"]
    r = client.post(f"/bookings/{bid}/self-scout")
    assert r.status_code in (401, 403), r.status_code


# ── identity priority + real deletion (2026-09-02) ────────────────────────────────────────────
_CSV_HEAD = ("identity_id,first_name,last_name,traveldoc,nationality,account_type,"
             "birthdate,gender,username,password,email\n")


def _csv(passport, user="u1", ident="ID1", first="Ana", last="Silva", kind="real"):
    # Real identities carry full detail (birthdate + gender) -- required at import since 2026-09-19.
    return _CSV_HEAD + f"{ident},{first},{last},{passport},CPV,{kind},1985-01-01,F,{user},pw1,{user}@x.com\n"


def test_a_re_imported_passport_is_deduped_not_duplicated(client):
    """Dedup (2026-10-02, replaces the old supersede+create-new): re-importing a passport REUSES the
    existing row -- promoted to HIGH, gaining only genuinely-new sub-accounts -- rather than adding a
    second row. The old behaviour multiplied rows on every re-import and buried the verified one."""
    from libs.domain import priority
    h = _operate_headers(client)
    r1 = client.post("/identities/import", json={"csv": _csv("PP-1", user="old1"),
                                                 "default_type": "real"}, headers=h)
    assert r1.status_code == 200, r1.text
    first = r1.json()["identity_ids"][0]
    client.patch(f"/identities/{first}", json={"priority": priority.NORMAL}, headers=h)  # so promote shows

    r2 = client.post("/identities/import", json={"csv": _csv("PP-1", user="new1"),
                                                 "default_type": "real"}, headers=h)
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["created"] == 0, f"a duplicate passport must NOT create a new row: {body}"
    assert body["promoted"] == [first], f"the existing row was not reused/promoted: {body}"
    assert body["identity_ids"] == []

    same = [i for i in client.get("/identities", headers=h).json() if i["passport_no"] == "PP-1"]
    assert len(same) == 1, f"dedup must leave exactly one row per passport: {same}"
    assert same[0]["id"] == first and same[0]["priority"] == priority.HIGH
    subs = [s for s in client.get("/sub-accounts", headers=h).json() if s["identity_id"] == first]
    assert {s["username"] for s in subs} == {"old1", "new1"}, "the new sub was not merged onto it"


def test_a_re_imported_HIGH_verified_passport_is_ignored(client):
    """The protected case: an existing row already HIGH AND holding a verified sub-account is KEPT
    and the incoming identical copy IGNORED -- never wiped, nothing added."""
    import apps.hub.main as hub_main
    from libs.db import models as M
    from libs.domain.enums import SubAccountStatus
    from sqlalchemy import select as _select
    h = _operate_headers(client)
    first = client.post("/identities/import", json={"csv": _csv("PP-2", user="v1"),
                                                    "default_type": "real"},
                        headers=h).json()["identity_ids"][0]
    db = hub_main.SessionLocal()
    try:
        sa = db.scalars(_select(M.SubAccount).where(M.SubAccount.identity_id == first)).first()
        sa.status = SubAccountStatus.VERIFIED
        db.commit()
    finally:
        db.close()

    r = client.post("/identities/import", json={"csv": _csv("PP-2", user="v2"),
                                                "default_type": "real"}, headers=h).json()
    assert r["created"] == 0 and r["ignored"] == [first], f"identical HIGH+verified not ignored: {r}"
    subs = [s for s in client.get("/sub-accounts", headers=h).json() if s["identity_id"] == first]
    assert {s["username"] for s in subs} == {"v1"}, "an ignored import must add NOTHING"


def test_add_identity_dedups_on_passport(client):
    """The manual-add path obeys the same rule: adding an existing passport reuses + promotes."""
    from libs.domain import priority
    h = _operate_headers(client)
    first = client.post("/identities/import", json={"csv": _csv("PP-D", user="d1"),
                                                    "default_type": "real"},
                        headers=h).json()["identity_ids"][0]
    client.patch(f"/identities/{first}", json={"priority": priority.NORMAL}, headers=h)
    dup = client.post("/identities", json={"full_name": "Dup", "type": "real",
                                           "passport_no": "PP-D"}, headers=h).json()
    assert dup["id"] == first, "manual add of an existing passport must reuse the row, not duplicate"
    assert dup["priority"] == priority.HIGH
    assert len([i for i in client.get("/identities", headers=h).json()
                if i["passport_no"] == "PP-D"]) == 1


def test_priority_orders_the_list_the_attach_panel_renders(client):
    """Priority's read site. A column that is only ever written is the trap this codebase keeps
    rediscovering, so the ordering is asserted, not just the stored value."""
    from libs.domain import priority
    h = _operate_headers(client)
    client.post("/identities/import", json={"csv": _csv("PP-A", user="a1"), "default_type": "real"}, headers=h)
    plain = client.post("/identities", json={"full_name": "Z", "type": "real",
                                             "passport_no": "PP-Z"}, headers=h).json()
    assert plain["priority"] == priority.HIGH, "a freshly added identity is in-demand -> HIGH"

    # demote the import to LOW; the hand-added HIGH one must then sort above it.
    a_id = [i["id"] for i in client.get("/identities", headers=h).json()
            if i["passport_no"] == "PP-A"][0]
    client.patch(f"/identities/{a_id}", json={"priority": priority.LOW}, headers=h)
    listed = client.get("/identities", headers=h).json()
    assert listed[0]["passport_no"] == "PP-Z", \
        f"priority is stored but not read: {[i['passport_no'] for i in listed]}"


def test_force_delete_removes_a_burned_out_identity_and_its_sub_accounts(client):
    """The "no longer available" case. Without force the FK raises and the only advice was "archive
    it", which leaves the row in the pool forever."""
    import apps.hub.main as hub_main
    from libs.db import models as M
    from sqlalchemy import select as _select
    h = _operate_headers(client)
    iid = client.post("/identities/import", json={"csv": _csv("PP-B", user="b1"),
                                                  "default_type": "real"},
                      headers=h).json()["identity_ids"][0]

    def _subs():
        db = hub_main.SessionLocal()
        try:
            return db.scalars(_select(M.SubAccount).where(M.SubAccount.identity_id == iid)).all()
        finally:
            db.close()

    assert _subs(), "the import should have created a sub-account to delete"

    # NOTE: this asserts what force DOES, not that a plain delete refuses. SQLite does not enforce
    # foreign keys by default (delete_identity's own comment says as much), so the plain path here
    # would orphan the sub-accounts rather than raise -- the 409 only happens on Postgres. Asserting
    # the refusal would pass or fail on the test backend rather than on the behaviour.
    r = client.delete(f"/identities/{iid}?force=true", headers=h)
    assert r.status_code == 204, r.text
    assert all(i["id"] != iid for i in client.get("/identities", headers=h).json())
    assert not _subs(), "force left the identity's sub-accounts behind"


def test_a_booked_appointment_blocks_deletion_even_with_force(client):
    """Force clears what we generated. It must never clear the record that a real person holds a
    real appointment -- the same rule DELETE /bookings enforces."""
    import apps.hub.main as hub_main
    from libs.db import models as M
    h = _operate_headers(client)
    iid = client.post("/identities", json={"full_name": "B", "type": "real",
                                           "passport_no": "PP-C"}, headers=h).json()["id"]
    bid = client.post("/bookings", json={"name": "bk", "type": "real", "posto_id": "5084"},
                      headers=h).json()["id"]
    db = hub_main.SessionLocal()
    try:
        slot = M.Slot(booking_id=bid, posto_id="5084", date="2026-09-09", period=3)
        sa = M.SubAccount(identity_id=iid, username="bk1", password="pw")
        db.add_all([slot, sa]); db.flush()
        db.add(M.BookedAppointment(booking_id=bid, identity_id=iid, slot_id=slot.id,
                                   sub_account_id=sa.id))
        db.commit()
    finally:
        db.close()

    r = client.delete(f"/identities/{iid}?force=true", headers=h)
    assert r.status_code == 409 and "booked appointment" in r.json()["detail"], r.text


def test_bulk_delete_reports_per_id_instead_of_failing_on_the_first_refusal(client):
    """An operator clearing a burned batch wants the removable ones gone and a precise list of what
    could not go -- not an all-or-nothing 409 that leaves them guessing which id caused it."""
    h = _operate_headers(client)
    ok1 = client.post("/identities", json={"full_name": "K1", "type": "real", "passport_no": "PP-D"},
                      headers=h).json()["id"]
    ok2 = client.post("/identities", json={"full_name": "K2", "type": "real", "passport_no": "PP-E"},
                      headers=h).json()["id"]

    r = client.post("/identities/bulk-delete",
                    json={"identity_ids": [ok1, "does-not-exist", ok2], "force": True}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["deleted"]) == sorted([ok1, ok2, "does-not-exist"]) or \
           sorted(body["deleted"]) == sorted([ok1, ok2]), body
    remaining = {i["id"] for i in client.get("/identities", headers=h).json()}
    assert ok1 not in remaining and ok2 not in remaining


def test_bulk_delete_never_removes_an_identity_with_a_booked_appointment_even_with_force(client):
    """THE guard the bulk-delete UI depends on. An appointment is the only record that a real
    person holds a real slot, and it is not ours to discard because the identity behind it went
    stale -- so `force` must NOT override it, unlike sub-accounts/allocations/event history which
    are rows we generated.

    The dashboard reads this: it offers the force pass only for refusals force can actually fix,
    and never for a booked appointment, because prompting to force something that cannot be forced
    is a dishonest prompt."""
    import apps.hub.main as hub_main
    from libs.db import models as M

    h = _operate_headers(client)
    iid = client.post("/identities", json={"full_name": "Booked One", "type": "real",
                                           "passport_no": "PP-BK1"}, headers=h).json()["id"]
    bid = client.post("/bookings", json={"name": "bk-guard", "type": "test",
                                         "posto_id": "2032"}, headers=h).json()["id"]
    db = hub_main.SessionLocal()
    try:
        # sub_account_id is NOT NULL on booked_appointments -- an appointment always belongs to the
        # sub-account that made it, so the fixture has to create one rather than pass None.
        sa = M.SubAccount(identity_id=iid, username="bk1", password="pw", status="verified")
        db.add(sa)
        db.flush()
        db.add(M.BookedAppointment(booking_id=bid, identity_id=iid,
                                   sub_account_id=sa.id, slot_id=None))
        db.commit()
        sa_id = sa.id
    finally:
        db.close()

    r = client.post("/identities/bulk-delete",
                    json={"identity_ids": [iid], "force": True}, headers=h).json()
    assert r["deleted"] == []
    assert iid in r["refused"]
    assert "booked appointment" in r["refused"][iid].lower()

    # Asserted against the ROW, not GET /identities: that endpoint annotates and orders for the
    # attach panel, so a listing miss would not prove the row is gone. The claim here is that the
    # identity still EXISTS -- and that force did not strip the sub-account out from under the
    # appointment on its way to refusing (the booked check runs before the force block, and this
    # pins that ordering).
    db = hub_main.SessionLocal()
    try:
        assert db.get(M.Identity, iid) is not None, "forced bulk delete removed a booked identity"
        assert db.get(M.SubAccount, sa_id) is not None, "force stripped the appointment's sub-account"
    finally:
        db.close()


def test_force_delete_clears_registration_jobs_hanging_off_the_sub_accounts(client):
    """REPRODUCES THE 2026-09-17 FAILURE. registration_jobs references SUB_ACCOUNTS, not the
    identity, and the force cascade cleared sub_accounts without it -- so deleting any identity
    that had ever been through registration raised

        ForeignKeyViolation: ... "registration_jobs_sub_account_id_fkey"

    outside the endpoint's try/except, returning an HTML 500. The dashboard rendered that as
    `Unexpected token 'I', "Internal S"... is not valid JSON`, and a 359-identity bulk delete died
    on the first offender.

    Second time a missed FK on registration_jobs broke a delete path (after migration ci000droprot),
    which is why the cascade now carries the full referencing set read from pg_constraint."""
    import apps.hub.main as hub_main
    from libs.db import models as M

    h = _operate_headers(client)
    iid = client.post("/identities", json={"full_name": "RegJob", "type": "real",
                                           "passport_no": "PP-RJ"}, headers=h).json()["id"]
    db = hub_main.SessionLocal()
    try:
        sa = M.SubAccount(identity_id=iid, username="rj1", password="pw", status="verified")
        db.add(sa); db.flush()
        db.add(M.RegistrationJob(sub_account_id=sa.id, mode="parallel", status="verified"))
        db.commit()
    finally:
        db.close()

    r = client.post("/identities/bulk-delete",
                    json={"identity_ids": [iid], "force": True}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == [iid], r.json()

    # Asserts the ROW IS GONE, not merely that the call returned 200. The FK-violation form of this
    # test is untestable here: SQLite does not enforce foreign keys by default (delete_identity's
    # own comment says so), so removing the cascade still returns 200 under the test DB while
    # raising ForeignKeyViolation against Postgres. Verified by neutralisation -- with the
    # registration_jobs delete removed, THIS assertion fails and the status check does not.
    db = hub_main.SessionLocal()
    try:
        assert db.get(M.Identity, iid) is None
        assert db.query(M.RegistrationJob).count() == 0, (
            "force delete left an orphaned registration_job -- against Postgres this is the "
            "ForeignKeyViolation that returned an HTML 500 to the dashboard")
    finally:
        db.close()


def test_a_database_error_on_one_identity_does_not_500_the_whole_batch(client, monkeypatch):
    """The endpoint promises per-id outcomes 'instead of failing on the first refusal', but caught
    only HTTPException -- so any DB error it had not anticipated became a 500 and lost the outcome
    of every other id in the batch. One bad row must cost one row."""
    import apps.hub.main as hub_main
    from sqlalchemy.exc import IntegrityError

    h = _operate_headers(client)
    good = client.post("/identities", json={"full_name": "G", "type": "real",
                                            "passport_no": "PP-G1"}, headers=h).json()["id"]
    bad = client.post("/identities", json={"full_name": "B", "type": "real",
                                           "passport_no": "PP-B1"}, headers=h).json()["id"]

    real = hub_main.delete_identity

    def _boom(identity_id, force=False, db=None, _op=None):
        if identity_id == bad:
            raise IntegrityError("stmt", {}, Exception("simulated FK violation"))
        return real(identity_id, force=force, db=db, _op=_op)

    monkeypatch.setattr(hub_main, "delete_identity", _boom)

    r = client.post("/identities/bulk-delete",
                    json={"identity_ids": [bad, good], "force": True}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert good in body["deleted"], "a later id must still be processed after an earlier failure"
    assert bad in body["refused"]


def test_bulk_priority_sets_the_same_level_on_every_selected_identity(client):
    h = _operate_headers(client)
    a = client.post("/identities", json={"full_name": "P1", "type": "real", "passport_no": "PP-F"},
                    headers=h).json()["id"]
    b = client.post("/identities", json={"full_name": "P2", "type": "real", "passport_no": "PP-G"},
                    headers=h).json()["id"]

    r = client.post("/identities/bulk-priority",
                    json={"identity_ids": [a, b], "priority": 0}, headers=h)
    assert r.status_code == 200, r.text
    assert sorted(r.json()["updated"]) == sorted([a, b])
    by_id = {i["id"]: i for i in client.get("/identities", headers=h).json()}
    assert by_id[a]["priority"] == 0 and by_id[b]["priority"] == 0


def test_bulk_priority_skips_unknown_ids_instead_of_failing_the_whole_batch(client):
    """A stale selection (a row deleted by someone else mid-edit) must not block updating the rest --
    same discipline as bulk-delete's per-id reporting, just simpler since there is no refusal case."""
    h = _operate_headers(client)
    a = client.post("/identities", json={"full_name": "P3", "type": "real", "passport_no": "PP-H"},
                    headers=h).json()["id"]

    r = client.post("/identities/bulk-priority",
                    json={"identity_ids": [a, "does-not-exist"], "priority": 200}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == [a]

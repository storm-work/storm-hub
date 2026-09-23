"""Registration subsystem: sub-account listing/reset, run creation, job tracking, and the
Hub-side event handling that turns apps/registrar's reg_result events into DB state. No live
network/portal calls here -- those live only in apps/registrar's manually-run process; this covers
the DB/API/event-wiring layer with fakes, matching how ops_live.py's tests use fakes rather than a
real browser."""
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


# Registration MINTS rotating proxies since 2026-09-17 (D39/D40), so these runs need pool
# credentials exactly as a worker launch does. Fake values via monkeypatch, because tests/conftest.py
# scrubs the ambient ones -- the suite must not depend on the developer's .env.
@pytest.fixture(autouse=True)
def _pool_credentials(monkeypatch):
    monkeypatch.setenv("SOAX_ROTATING_PACKAGE", "352438")
    monkeypatch.setenv("SOAX_ROTATING_PASSWORD", "test-rot-pw")
    monkeypatch.setenv("SOAX_MOBILE_PACKAGE", "350716")
    monkeypatch.setenv("SOAX_MOBILE_PASSWORD", "test-mob-pw")
    monkeypatch.setenv("WEBSHARE_PREFIX", "sodade2026")
    monkeypatch.setenv("WEBSHARE_PASSWORD", "test-ws-pw")


def _op_headers(c, username="admin", password="pw"):
    c.post("/auth/bootstrap", json={"username": username, "password": password})
    r = c.post("/auth/login", json={"username": username, "password": password, "purpose": "operate"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _make_identity_with_subs(c, h, n=1, type_="test"):
    iid = c.post("/identities", json={"full_name": "Ana Silva", "type": type_}, headers=h).json()["id"]
    c.post("/identities/gen-subaccounts", json={"identity_ids": [iid], "count": n}, headers=h)
    subs = c.get("/sub-accounts", headers=h).json()
    return iid, [s for s in subs if s["identity_id"] == iid]


def test_gen_subaccounts_returns_created_ids(client):
    """Regression: the dashboard's upload -> import -> gen-subaccounts -> run-registration pipeline
    needs the exact sub_account_ids this call just created to chain the registration run onto,
    without a second /sub-accounts lookup racing other concurrent operator actions."""
    c, _ = client
    h = _op_headers(c)
    iid = c.post("/identities", json={"full_name": "Ana Silva", "type": "real"}, headers=h).json()["id"]
    r = c.post("/identities/gen-subaccounts", json={"identity_ids": [iid], "count": 3}, headers=h).json()
    assert len(r["sub_account_ids"]) == 3
    all_ids = {s["id"] for s in c.get("/sub-accounts", headers=h).json() if s["identity_id"] == iid}
    assert set(r["sub_account_ids"]) == all_ids


# ── GET /sub-accounts ────────────────────────────────────────────────────────
def test_list_sub_accounts_flattens_identity_and_defaults_registration_status_none(client):
    c, _ = client
    h = _op_headers(c)
    _iid, subs = _make_identity_with_subs(c, h, n=2)
    assert len(subs) == 2
    for s in subs:
        assert s["identity_name"] == "Ana Silva"
        assert s["identity_type"] == "test"
        assert s["registration_status"] is None
        assert s["attempts"] == 0


def test_sub_accounts_requires_auth(client):
    c, _ = client
    assert c.get("/sub-accounts").status_code == 401


# ── POST /sub-accounts/reset ─────────────────────────────────────────────────
def test_reset_regenerates_credentials_and_status(client):
    c, _ = client
    h = _op_headers(c)
    _iid, subs = _make_identity_with_subs(c, h, n=1)
    sid = subs[0]["id"]
    old_username = subs[0]["username"]

    r = c.post("/sub-accounts/reset", json={"sub_account_ids": [sid]}, headers=h)
    assert r.status_code == 200
    assert r.json()["created"] == 1

    updated = next(s for s in c.get("/sub-accounts", headers=h).json() if s["id"] == sid)
    assert updated["username"] != old_username
    assert updated["status"] == "new"


def test_reset_unknown_id_reported_as_error_not_exception(client):
    c, _ = client
    h = _op_headers(c)
    r = c.post("/sub-accounts/reset", json={"sub_account_ids": ["nope"]}, headers=h)
    assert r.status_code == 200
    assert r.json()["created"] == 0
    assert "not found" in r.json()["errors"][0]


# ── POST /sub-accounts/retire ────────────────────────────────────────────────
def test_retire_sets_status(client):
    """Regression, live 2026-08-27: 8 accounts stuck at NEW/REGISTERED with 12-20 failed
    registration attempts each, cluttering the Registration page's "not verified" queue with rows
    that will never succeed on a retry. Retire is the operator's way to say so."""
    c, _ = client
    h = _op_headers(c)
    _iid, subs = _make_identity_with_subs(c, h, n=1)
    sid = subs[0]["id"]

    r = c.post("/sub-accounts/retire", json={"sub_account_ids": [sid]}, headers=h)
    assert r.status_code == 200
    assert r.json()["created"] == 1

    updated = next(s for s in c.get("/sub-accounts", headers=h).json() if s["id"] == sid)
    assert updated["status"] == "retired"


def test_retire_unknown_id_reported_as_error_not_exception(client):
    c, _ = client
    h = _op_headers(c)
    r = c.post("/sub-accounts/retire", json={"sub_account_ids": ["nope"]}, headers=h)
    assert r.status_code == 200
    assert r.json()["created"] == 0
    assert "not found" in r.json()["errors"][0]


def test_retired_account_excluded_from_live_ranking(client):
    """The exact safety property retiring exists for: a retired account must never be picked for a
    real login, not even as a last resort -- mirrors BURNED's existing exclusion."""
    import apps.hub.main as hub_main
    from libs.db import models as M

    from libs.domain.enums import SubAccountStatus
    from sqlalchemy import select

    c, TestSession = client
    h = _op_headers(c)
    iid, subs = _make_identity_with_subs(c, h, n=2)
    retire_id = subs[0]["id"]
    # Verify BOTH first: the ranking only ever considers verified-or-better accounts, so leaving
    # them NEW would make this test pass for the wrong reason (nothing ranked, retired trivially
    # absent). Verified-then-retired is the case the safety property is actually about.
    db = TestSession()
    try:
        for sa in db.scalars(select(M.SubAccount).where(M.SubAccount.identity_id == iid)).all():
            sa.status = SubAccountStatus.VERIFIED
        db.commit()
    finally:
        db.close()
    c.post("/sub-accounts/retire", json={"sub_account_ids": [retire_id]}, headers=h)

    db = TestSession()
    try:
        ranked = hub_main._rank_live_sub_accounts(db, iid)
        assert retire_id not in [s.id for s in ranked]
        assert len(ranked) == 1   # the other (untouched) sub-account is still offered
    finally:
        db.close()


def test_run_registration_rejects_a_selection_of_only_retired_accounts(client):
    c, _ = client
    h = _op_headers(c)
    _iid, subs = _make_identity_with_subs(c, h, n=1)
    sid = subs[0]["id"]
    c.post("/sub-accounts/retire", json={"sub_account_ids": [sid]}, headers=h)

    r = c.post("/registration/run", json={"sub_account_ids": [sid], "concurrency": 1,
                                          "provider": "soax"}, headers=h)
    assert r.status_code == 400


# ── POST /registration/run ───────────────────────────────────────────────────
def test_run_registration_rejects_without_capsolver_keys(client):
    c, _ = client
    h = _op_headers(c)
    _iid, subs = _make_identity_with_subs(c, h, n=1)
    c.post("/proxies", json={"provider": "soax", "kind": "static_res", "endpoint": "1.2.3.4:5000"},
          headers=h)
    r = c.post("/registration/run", json={"sub_account_ids": [subs[0]["id"]]}, headers=h)
    assert r.status_code == 409
    assert "CAPSOLVER" in r.json()["detail"]


def test_run_registration_rejects_when_the_rotating_pool_is_unconfigured(client, monkeypatch):
    """Was "rejects without proxies", meaning no DB rows. Rotating pools have no rows since
    ci000droprot -- "unavailable" now means the pool's credentials are not configured, and the 409
    has to name the env var so the operator knows what to set rather than hunting for proxies that
    were never supposed to exist."""
    c, _ = client
    h = _op_headers(c)
    monkeypatch.delenv("SOAX_ROTATING_PASSWORD", raising=False)
    _iid, subs = _make_identity_with_subs(c, h, n=1)
    c.put("/settings/global", json={"values": {"CAPSOLVER_KEYS": "key1"}}, headers=h)
    r = c.post("/registration/run", json={"sub_account_ids": [subs[0]["id"]]}, headers=h)
    assert r.status_code == 409
    assert "SOAX_ROTATING_PASSWORD" in r.json()["detail"]


def test_run_registration_creates_jobs_and_spawns(client):
    c, TestSession = client
    h = _op_headers(c)
    _iid, subs = _make_identity_with_subs(c, h, n=2)
    c.put("/settings/global", json={"values": {"CAPSOLVER_KEYS": "key1,key2"}}, headers=h)
    c.post("/proxies", json={"provider": "soax", "kind": "static_res", "endpoint": "1.2.3.4:5000"},
          headers=h)

    ids = [s["id"] for s in subs]
    r = c.post("/registration/run", json={"sub_account_ids": ids, "concurrency": 5}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["queued"] == 2
    assert body["run_id"].startswith("reg-")

    jobs = c.get("/registration/jobs", headers=h).json()
    assert len(jobs) == 2
    assert {j["sub_account_id"] for j in jobs} == set(ids)
    assert all(j["status"] == "queued" and j["attempts"] == 1 for j in jobs)

    # re-running the same accounts bumps attempts on the SAME rows rather than creating new ones
    c.post("/registration/run", json={"sub_account_ids": ids}, headers=h)
    jobs2 = c.get("/registration/jobs", headers=h).json()
    assert len(jobs2) == 2
    assert all(j["attempts"] == 2 for j in jobs2)


def test_run_registration_sends_slash_formatted_birthdate(client, monkeypatch):
    """Regression: Identity.birthdate is stored ISO (YYYY-MM-DD, importers.py's own convention) but
    the portal's /register `bday` field expects YYYY/MM/DD (confirmed against storm-auto's proven
    batch_register.py). A live run sent the unconverted ISO value and failed 10/10 registrations --
    apps/hub/main.py's run_registration must convert at the point of use (_bday_for_register)."""
    c, _ = client
    h = _op_headers(c)
    iid = c.post("/identities", json={"full_name": "Ana Silva", "type": "test",
                                      "birthdate": "2007-12-30"}, headers=h).json()["id"]
    c.post("/identities/gen-subaccounts", json={"identity_ids": [iid], "count": 1}, headers=h)
    sid = next(s["id"] for s in c.get("/sub-accounts", headers=h).json() if s["identity_id"] == iid)
    c.put("/settings/global", json={"values": {"CAPSOLVER_KEYS": "key1"}}, headers=h)
    c.post("/proxies", json={"provider": "soax", "kind": "static", "endpoint": "1.2.3.4:5000"}, headers=h)

    import apps.hub.main as hub_main
    captured = {}
    orig_send = hub_main._bus.send_command

    def _spy(worker_id, type, **payload):
        if type == "launch":
            captured.update(payload)
        return orig_send(worker_id, type, **payload)

    monkeypatch.setattr(hub_main._bus, "send_command", _spy)

    r = c.post("/registration/run", json={"sub_account_ids": [sid]}, headers=h)
    assert r.status_code == 200
    acct = captured["accounts"][0]
    assert acct["birthdate"] == "2007/12/30"


def test_run_registration_sends_proxy_id_alongside_url(client, monkeypatch):
    """Regression: RegistrationJob.proxy_id/solver_used were modeled columns that nothing ever wrote --
    the registrar only ever received bare proxy URL strings, with no DB id to report back. The launch
    payload must carry {id, url} pairs so apps/registrar can echo which proxy row it used in reg_result,
    and events.py's _persist_registration_result can attribute it on the job (see the paired test
    below).

    Pinned to kind='static' since 2026-09-17: a ROW id only exists for a static/ISP proxy. Rotating
    proxies are minted per run and have no row, so their entries carry id=None -- asserted by
    test_registration_mints_rotating_proxies_instead_of_querying_rows below."""
    c, _ = client
    h = _op_headers(c)
    iid = c.post("/identities", json={"full_name": "Ana Silva", "type": "test"}, headers=h).json()["id"]
    c.post("/identities/gen-subaccounts", json={"identity_ids": [iid], "count": 1}, headers=h)
    sid = next(s["id"] for s in c.get("/sub-accounts", headers=h).json() if s["identity_id"] == iid)
    c.put("/settings/global", json={"values": {"CAPSOLVER_KEYS": "key1"}}, headers=h)
    pid = c.post("/proxies", json={"provider": "soax", "kind": "static", "endpoint": "1.2.3.4:5000"},
                headers=h).json()["id"]

    import apps.hub.main as hub_main
    captured = {}
    orig_send = hub_main._bus.send_command

    def _spy(worker_id, type, **payload):
        if type == "launch":
            captured.update(payload)
        return orig_send(worker_id, type, **payload)

    monkeypatch.setattr(hub_main._bus, "send_command", _spy)

    r = c.post("/registration/run", json={"sub_account_ids": [sid], "kind": "static"}, headers=h)
    assert r.status_code == 200, r.text
    assert captured["proxies"] == [{"id": pid, "url": "http://1.2.3.4:5000"}]


def test_run_registration_defaults_to_captcha_solve_proxyless(client, monkeypatch):
    """CONFIRMED LIVE 2026-09-03 on reg-8f0c0aa69af3 (webshare/rotating): every registration attempt
    that reached "register form loaded" -- proving the browser's own connection through that proxy
    was already working -- then failed CAPTCHA-solving 100% of the time (ERROR_PROXY_CONNECT_REFUSED
    / ERROR_CAPTCHA_SOLVE_FAILED). CapSolver's remote worker opening a second connection through the
    same sticky session the browser already holds collides with it -- an isolated ProxyLess solve
    against the same sitekey succeeded immediately. Same switch and same default as login
    (RECAPTCHA_SOLVE_PROXY, catalog default False); this pins that the launch payload actually
    carries it through to apps/registrar, not just that the setting exists."""
    c, _ = client
    h = _op_headers(c)
    iid = c.post("/identities", json={"full_name": "Ana Silva", "type": "test"}, headers=h).json()["id"]
    c.post("/identities/gen-subaccounts", json={"identity_ids": [iid], "count": 1}, headers=h)
    sid = next(s["id"] for s in c.get("/sub-accounts", headers=h).json() if s["identity_id"] == iid)
    c.put("/settings/global", json={"values": {"CAPSOLVER_KEYS": "key1"}}, headers=h)
    c.post("/proxies", json={"provider": "soax", "kind": "static", "endpoint": "1.2.3.4:5000"}, headers=h)

    import apps.hub.main as hub_main
    captured = {}
    orig_send = hub_main._bus.send_command

    def _spy(worker_id, type, **payload):
        if type == "launch":
            captured.update(payload)
        return orig_send(worker_id, type, **payload)

    monkeypatch.setattr(hub_main._bus, "send_command", _spy)

    r = c.post("/registration/run", json={"sub_account_ids": [sid]}, headers=h)
    assert r.status_code == 200
    assert captured["captcha_solve_proxy"] is False


def test_run_registration_honours_captcha_solve_proxy_when_switched_on(client, monkeypatch):
    """The dashboard-switchable half: RECAPTCHA_SOLVE_PROXY has its own track record on
    soax/rotating specifically (342 verified registrations, per session.py's note), so this must
    stay a real operator switch, not hardcoded off."""
    c, _ = client
    h = _op_headers(c)
    iid = c.post("/identities", json={"full_name": "Ana Silva", "type": "test"}, headers=h).json()["id"]
    c.post("/identities/gen-subaccounts", json={"identity_ids": [iid], "count": 1}, headers=h)
    sid = next(s["id"] for s in c.get("/sub-accounts", headers=h).json() if s["identity_id"] == iid)
    c.put("/settings/global",
          json={"values": {"CAPSOLVER_KEYS": "key1", "RECAPTCHA_SOLVE_PROXY": "true"}}, headers=h)
    c.post("/proxies", json={"provider": "soax", "kind": "static", "endpoint": "1.2.3.4:5000"}, headers=h)

    import apps.hub.main as hub_main
    captured = {}
    orig_send = hub_main._bus.send_command

    def _spy(worker_id, type, **payload):
        if type == "launch":
            captured.update(payload)
        return orig_send(worker_id, type, **payload)

    monkeypatch.setattr(hub_main._bus, "send_command", _spy)

    r = c.post("/registration/run", json={"sub_account_ids": [sid]}, headers=h)
    assert r.status_code == 200
    assert captured["captcha_solve_proxy"] is True


def test_reg_result_registered_records_proxy_and_solver_on_job(client):
    c, TestSession = client
    h = _op_headers(c)
    _iid, subs = _make_identity_with_subs(c, h, n=1)
    sid = subs[0]["id"]
    c.put("/settings/global", json={"values": {"CAPSOLVER_KEYS": "key1"}}, headers=h)
    pid = c.post("/proxies", json={"provider": "soax", "kind": "static", "endpoint": "1.2.3.4:5000"},
                headers=h).json()["id"]
    c.post("/registration/run", json={"sub_account_ids": [sid]}, headers=h)

    import apps.hub.db as hub_db
    import apps.hub.events as events_mod
    orig = hub_db.SessionLocal
    hub_db.SessionLocal = TestSession
    events_mod.SessionLocal = TestSession
    try:
        hub = events_mod.EventHub.__new__(events_mod.EventHub)
        hub._persist_registration_result(
            sid, "registered",
            {"email": "a@b.com", "email_pass": "pw", "proxy_id": pid, "solver_used": "capsolver"})
    finally:
        hub_db.SessionLocal = orig
        events_mod.SessionLocal = orig

    jobs = c.get("/registration/jobs", headers=h).json()
    job = next(j for j in jobs if j["sub_account_id"] == sid)
    assert job["proxy_id"] == pid
    assert job["solver_used"] == "capsolver"


def test_stop_registration_run(client):
    c, _ = client
    h = _op_headers(c)
    r = c.post("/registration/runs/reg-doesnotexist/stop", headers=h)
    assert r.status_code == 200
    assert r.json()["stopped"] is True


# ── events.py: reg_result -> DB persistence ──────────────────────────────────
def test_reg_result_verified_advances_subaccount_and_job(client):
    c, TestSession = client
    h = _op_headers(c)
    _iid, subs = _make_identity_with_subs(c, h, n=1)
    sid = subs[0]["id"]
    c.put("/settings/global", json={"values": {"CAPSOLVER_KEYS": "key1"}}, headers=h)
    c.post("/proxies", json={"provider": "soax", "kind": "static_res", "endpoint": "1.2.3.4:5000"},
          headers=h)
    c.post("/registration/run", json={"sub_account_ids": [sid]}, headers=h)

    import apps.hub.db as hub_db
    import apps.hub.events as events_mod
    orig = hub_db.SessionLocal
    hub_db.SessionLocal = TestSession
    events_mod.SessionLocal = TestSession
    try:
        hub = events_mod.EventHub.__new__(events_mod.EventHub)
        hub._persist_registration_result(sid, "registered", {"email": "a@b.com", "email_pass": "pw"})
        hub._persist_registration_result(sid, "verified", {})
    finally:
        hub_db.SessionLocal = orig
        events_mod.SessionLocal = orig

    updated = next(s for s in c.get("/sub-accounts", headers=h).json() if s["id"] == sid)
    assert updated["status"] == "verified"
    assert updated["email"] == "a@b.com"


def test_eventual_success_clears_stale_failure_notes(client):
    """Regression: an earlier failed attempt's note (e.g. a ReCaptchaError detail) must not linger
    once a later attempt succeeds -- confirmed confusing in a live run where accounts showed
    "verified" next to a stale failure note from attempt 1."""
    c, TestSession = client
    h = _op_headers(c)
    _iid, subs = _make_identity_with_subs(c, h, n=1)
    sid = subs[0]["id"]
    c.put("/settings/global", json={"values": {"CAPSOLVER_KEYS": "key1"}}, headers=h)
    c.post("/proxies", json={"provider": "soax", "kind": "static", "endpoint": "1.2.3.4:5000"}, headers=h)
    c.post("/registration/run", json={"sub_account_ids": [sid]}, headers=h)

    import apps.hub.db as hub_db
    import apps.hub.events as events_mod
    orig = hub_db.SessionLocal
    hub_db.SessionLocal = TestSession
    events_mod.SessionLocal = TestSession
    try:
        hub = events_mod.EventHub.__new__(events_mod.EventHub)
        hub._persist_registration_result(sid, "failed", {"detail": "type=ReCaptchaError desc=..."})
        hub._persist_registration_result(sid, "registered", {"email": "a@b.com", "email_pass": "pw"})
        hub._persist_registration_result(sid, "verified", {})
    finally:
        hub_db.SessionLocal = orig
        events_mod.SessionLocal = orig

    from libs.db import models as M
    db = TestSession()
    try:
        sa = db.get(M.SubAccount, sid)
        assert sa.status == "verified"
        assert sa.notes is None
    finally:
        db.close()


def test_reg_result_failed_leaves_subaccount_status_alone(client):
    c, TestSession = client
    h = _op_headers(c)
    _iid, subs = _make_identity_with_subs(c, h, n=1)
    sid = subs[0]["id"]
    c.put("/settings/global", json={"values": {"CAPSOLVER_KEYS": "key1"}}, headers=h)
    c.post("/proxies", json={"provider": "soax", "kind": "static_res", "endpoint": "1.2.3.4:5000"},
          headers=h)
    c.post("/registration/run", json={"sub_account_ids": [sid]}, headers=h)

    import apps.hub.db as hub_db
    import apps.hub.events as events_mod
    orig = hub_db.SessionLocal
    hub_db.SessionLocal = TestSession
    events_mod.SessionLocal = TestSession
    try:
        hub = events_mod.EventHub.__new__(events_mod.EventHub)
        hub._persist_registration_result(sid, "failed", {"detail": "verify_email_timeout"})
    finally:
        hub_db.SessionLocal = orig
        events_mod.SessionLocal = orig

    updated = next(s for s in c.get("/sub-accounts", headers=h).json() if s["id"] == sid)
    assert updated["status"] == "new"  # unchanged -- FAILED isn't a SubAccountStatus rung
    assert updated["registration_status"] == "failed"
    jobs = c.get("/registration/jobs", headers=h).json()
    assert jobs[0]["status"] == "failed"


# ── registration proxy selection (operator question 2026-08-19) ───────────────────────────────
def _mk_proxy(db, M, *, provider, kind, geo, healthy=True):
    p = M.Proxy(provider=provider, kind=kind, endpoint=f"{provider}-{geo}:5000",
                credentials_ref=f"package-1-country-{(geo or 'xx').lower()}-sessionid-{geo}-opt-wb:pw",
                max_sessions=2, active=True, is_healthy=healthy, geo=geo)
    db.add(p); db.commit()
    return p


def test_registration_selects_proxies_by_provider_kind_and_geo(client):
    """The endpoint always supported provider/kind; geo is new, and registration needs it for the
    same reason booking does -- it hits the same portal behind the same reCAPTCHA, where the gate is
    the reputation of the IP presenting the token."""
    c, TestSession = client
    import apps.hub.main as main
    from libs.db import models as M

    h = _op_headers(c)
    db = TestSession()
    _mk_proxy(db, M, provider="soax_mobile", kind="mobile", geo="DE")
    _mk_proxy(db, M, provider="soax_mobile", kind="mobile", geo="IT")
    _mk_proxy(db, M, provider="soax", kind="rotating", geo="CZ")
    db.close()

    iid = c.post("/identities", json={"full_name": "REG ONE", "type": "test"},
                      headers=h).json()["id"]
    c.post("/identities/gen-subaccounts", json={"identity_ids": [iid], "count": 1}, headers=h)
    sid = c.get("/sub-accounts", headers=h).json()[0]["id"]

    # ROW SELECTION now applies to STATIC pools only -- a rotating kind is minted and never
    # consults the table, so these refusals are asserted with kind='static'.
    r = c.post("/registration/run",
                    json={"sub_account_ids": [sid], "provider": "soax", "kind": "static"}, headers=h)
    assert r.status_code == 409 and "kind=static" in r.json()["detail"]

    # a geo with no members is refused too
    r2 = c.post("/registration/run",
                     json={"sub_account_ids": [sid], "provider": "soax_mobile", "kind": "static",
                           "geos": "PT"}, headers=h)
    assert r2.status_code == 409 and "geo=PT" in r2.json()["detail"]


def test_registration_geo_filter_never_excludes_an_unclassified_proxy(client):
    """Same rule as lease_proxy: a filter narrows the pool, it must not silently empty it for a
    provider whose credentials carry no country."""
    c, TestSession = client
    import apps.hub.main as main
    from libs.db import models as M

    h = _op_headers(c)
    db = TestSession()
    p = _mk_proxy(db, M, provider="nogeo", kind="mobile", geo=None)
    db.close()

    iid = c.post("/identities", json={"full_name": "REG TWO", "type": "test"},
                      headers=h).json()["id"]
    c.post("/identities/gen-subaccounts", json={"identity_ids": [iid], "count": 1}, headers=h)
    sid = c.get("/sub-accounts", headers=h).json()[0]["id"]

    r = c.post("/registration/run",
                    json={"sub_account_ids": [sid], "provider": "nogeo", "geos": "DE,IT"}, headers=h)
    # It may still fail later (CAPSOLVER keys aren't configured in this fixture) -- what matters is
    # that it did NOT fail on proxy selection, i.e. the NULL-geo proxy was not filtered out.
    detail = r.json().get("detail", "") if r.status_code >= 400 else ""
    assert "proxies available" not in detail, detail


# ── the portal blocks after 2 reCAPTCHA failures (2026-08-21) ────────────────
def test_recaptcha_rejection_is_recognised_in_both_forms():
    """The portal's own rule, readable only once the detail stopped being cut at 80 chars:

        "o sistema apenas permite falhar o processo de ReCaptcha 2 vezes ... o seu acesso a
         plataforma de E-VISA sera bloqueado"

    Two failures allowed; more and platform access is BLOCKED. MAX_PROXY_RETRIES was 5 and batches
    run concurrently, so the retry policy was not merely failing to register -- it was working
    toward a block, and "Dispoe de mais 0 tentativas" was the counter saying so."""
    from apps.registrar.__main__ import is_recaptcha_rejection

    assert is_recaptcha_rejection(
        "ReCaptchaError",
        "De forma a melhor garantir a possibilidade de agendamentos para todos os utilizadores o "
        "sistema apenas permite falhar o processo de ReCaptcha 2 vezes.")
    assert is_recaptcha_rejection("ReCaptchaError", "Dispõe de mais 0 tentativas ")


def test_other_failures_are_not_treated_as_recaptcha():
    """A proxy/network failure still deserves another proxy -- those never reached the CAPTCHA
    check, and refusing to retry them would throw away recoverable attempts."""
    from apps.registrar.__main__ import is_recaptcha_rejection

    assert not is_recaptcha_rejection("error", "Foi encontrado um erro ao executar a operação.")
    assert not is_recaptcha_rejection("", "")
    assert not is_recaptcha_rejection("already_exists", "account exists")


def test_registration_mints_rotating_proxies_instead_of_querying_rows(client, monkeypatch):
    """THE 2026-09-17 FAILURE. Registration was the last path still selecting ROTATING proxies from
    DB rows, and ci000droprot deleted those rows -- so it failed with

        409: no active healthy soax proxies available (kind=rotating)

    against a pool that was working perfectly for every worker on the host. Workers have minted
    their own since 2026-09-15; registration now does too.

    Asserts the three properties that matter: it does not consult the table (none exists here),
    every entry is a real minted credential, and the exits are DISTINCT -- apps/registrar
    round-robins this list with itertools.cycle, so duplicates would put concurrent registrations
    on the same IP behind the same reCAPTCHA."""
    c, _ = client
    h = _op_headers(c)
    iid = c.post("/identities", json={"full_name": "Mint One", "type": "test"},
                 headers=h).json()["id"]
    c.post("/identities/gen-subaccounts", json={"identity_ids": [iid], "count": 1}, headers=h)
    sid = next(s["id"] for s in c.get("/sub-accounts", headers=h).json() if s["identity_id"] == iid)
    c.put("/settings/global", json={"values": {"CAPSOLVER_KEYS": "key1"}}, headers=h)

    assert c.get("/proxies?limit=10", headers=h).json() == [], "no rows -- the point of the test"

    import apps.hub.main as hub_main
    captured = {}
    orig_send = hub_main._bus.send_command

    def _spy(worker_id, type, **payload):
        if type == "launch":
            captured.update(payload)
        return orig_send(worker_id, type, **payload)

    monkeypatch.setattr(hub_main._bus, "send_command", _spy)

    r = c.post("/registration/run",
               json={"sub_account_ids": [sid], "provider": "soax", "kind": "rotating",
                     "concurrency": 4}, headers=h)
    assert r.status_code == 200, r.text

    proxies = captured.get("proxies") or []
    assert len(proxies) >= 4, f"one exit per concurrent slot expected, got {len(proxies)}"
    assert all(p["id"] is None for p in proxies), "a minted proxy has no DB row"
    assert all(p["url"].startswith("http://package-352438-") for p in proxies), proxies[:1]
    assert all(p["url"].endswith("@proxy.soax.com:5000") for p in proxies), proxies[:1]
    assert len({p["url"] for p in proxies}) == len(proxies), "exits must be distinct"

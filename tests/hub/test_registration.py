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


def test_run_registration_rejects_without_proxies(client):
    c, _ = client
    h = _op_headers(c)
    _iid, subs = _make_identity_with_subs(c, h, n=1)
    c.put("/settings/global", json={"values": {"CAPSOLVER_KEYS": "key1"}}, headers=h)
    r = c.post("/registration/run", json={"sub_account_ids": [subs[0]["id"]]}, headers=h)
    assert r.status_code == 409
    assert "proxies" in r.json()["detail"]


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
    below)."""
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

    r = c.post("/registration/run", json={"sub_account_ids": [sid]}, headers=h)
    assert r.status_code == 200
    assert captured["proxies"] == [{"id": pid, "url": "http://1.2.3.4:5000"}]


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

    # a provider with no matching kind is refused, and says so
    r = c.post("/registration/run",
                    json={"sub_account_ids": [sid], "provider": "soax", "kind": "mobile"}, headers=h)
    assert r.status_code == 409 and "kind=mobile" in r.json()["detail"]

    # a geo with no members is refused too
    r2 = c.post("/registration/run",
                     json={"sub_account_ids": [sid], "provider": "soax_mobile", "kind": "mobile",
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

"""Named rotating pools (operator direction 2026-10-08): DB-stored rotating-proxy credential
entries, managed on the Proxies page, selected by a booking via PROXY_PROVIDER='pool:<name>'.

Covers the two things that matter most: the password is NEVER returned by the read API, and a
'pool:<name>' provider resolves to the pool's real provider/kind + creds (which then feed the
Hub-only mint), while a missing/inactive pool fails loud. The worker never sees creds -- it gets a
minted URL -- so there is no worker-side test here by design."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.hub.db import get_db
from apps.hub.main import app, _resolve_rotating_pool
from libs.db import metadata
import libs.db.models as M


@pytest.fixture()
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
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
    yield TestClient(app), TestSession
    app.dependency_overrides.clear()


def _op_headers(c, username="admin", password="pw"):
    c.post("/auth/bootstrap", json={"username": username, "password": password})
    r = c.post("/auth/login", json={"username": username, "password": password, "purpose": "operate"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_create_soax_pool_redacts_password_but_echoes_package(client):
    c, _ = client
    h = _op_headers(c)
    r = c.post("/rotating-pools", json={
        "name": "soax-acct2", "provider": "soax", "kind": "rotating",
        "credentials": {"PACKAGE": "352438", "PASSWORD": "s3cret"}}, headers=h)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["credentials"]["PACKAGE"] == "352438"      # non-secret identifier echoed
    assert body["credentials"]["PASSWORD"] == "********"    # secret redacted
    # and the LIST view never leaks it either
    got = c.get("/rotating-pools", headers=h).json()
    assert got[0]["credentials"]["PASSWORD"] == "********"
    assert "s3cret" not in r.text and "s3cret" not in str(got)


def test_create_rejects_missing_and_unexpected_fields(client):
    c, _ = client
    h = _op_headers(c)
    miss = c.post("/rotating-pools", json={
        "name": "p1", "provider": "soax", "credentials": {"PACKAGE": "1"}}, headers=h)
    assert miss.status_code == 422 and "PASSWORD" in miss.text
    extra = c.post("/rotating-pools", json={
        "name": "p2", "provider": "webshare",
        "credentials": {"PREFIX": "x", "PASSWORD": "y", "PACKAGE": "nope"}}, headers=h)
    assert extra.status_code == 422 and "PACKAGE" in extra.text


def test_duplicate_name_rejected(client):
    c, _ = client
    h = _op_headers(c)
    body = {"name": "dup", "provider": "webshare", "credentials": {"PREFIX": "a", "PASSWORD": "b"}}
    assert c.post("/rotating-pools", json=body, headers=h).status_code == 201
    assert c.post("/rotating-pools", json=body, headers=h).status_code == 409


def test_update_rotates_password_and_keeps_it_when_omitted(client):
    c, TestSession = client
    h = _op_headers(c)
    pid = c.post("/rotating-pools", json={
        "name": "w", "provider": "webshare",
        "credentials": {"PREFIX": "acct", "PASSWORD": "old"}}, headers=h).json()["id"]
    # omit PASSWORD -> kept; change PREFIX only
    c.patch(f"/rotating-pools/{pid}", json={"credentials": {"PREFIX": "acct-new"}}, headers=h)
    with TestSession() as db:
        row = db.get(M.RotatingPool, pid)
        assert row.credentials["PREFIX"] == "acct-new"
        assert row.credentials["PASSWORD"] == "old"        # untouched
    # now rotate the password
    c.patch(f"/rotating-pools/{pid}", json={"credentials": {"PASSWORD": "new"}}, headers=h)
    with TestSession() as db:
        assert db.get(M.RotatingPool, pid).credentials["PASSWORD"] == "new"


def test_delete(client):
    c, _ = client
    h = _op_headers(c)
    pid = c.post("/rotating-pools", json={
        "name": "gone", "provider": "soax",
        "credentials": {"PACKAGE": "1", "PASSWORD": "2"}}, headers=h).json()["id"]
    assert c.delete(f"/rotating-pools/{pid}", headers=h).status_code == 200
    assert c.get("/rotating-pools", headers=h).json() == []


def test_create_requires_operator(client):
    c, _ = client
    # no auth header -> not the operator
    r = c.post("/rotating-pools", json={
        "name": "x", "provider": "soax", "credentials": {"PACKAGE": "1", "PASSWORD": "2"}})
    assert r.status_code in (401, 403)


# ── name resolution feeds the Hub-only mint ─────────────────────────────────────
def test_resolve_pool_name_returns_provider_kind_and_creds(client):
    c, TestSession = client
    h = _op_headers(c)
    c.post("/rotating-pools", json={
        "name": "soax-b", "provider": "soax", "kind": "mobile",
        "credentials": {"PACKAGE": "350716", "PASSWORD": "mpw"}}, headers=h)
    with TestSession() as db:
        prov, kind, creds = _resolve_rotating_pool(db, "pool:soax-b", None)
        assert prov == "soax" and kind == "mobile"
        assert creds == {"PACKAGE": "350716", "PASSWORD": "mpw"}


def test_resolve_passes_through_non_pool_provider(client):
    _, TestSession = client
    with TestSession() as db:
        assert _resolve_rotating_pool(db, "soax", "rotating") == ("soax", "rotating", None)


def test_resolve_missing_or_inactive_pool_is_409(client):
    from fastapi import HTTPException
    c, TestSession = client
    h = _op_headers(c)
    with TestSession() as db:
        with pytest.raises(HTTPException) as ei:
            _resolve_rotating_pool(db, "pool:nope", None)
        assert ei.value.status_code == 409
    pid = c.post("/rotating-pools", json={
        "name": "off", "provider": "soax",
        "credentials": {"PACKAGE": "1", "PASSWORD": "2"}}, headers=h).json()["id"]
    c.patch(f"/rotating-pools/{pid}", json={"active": False}, headers=h)
    with TestSession() as db:
        with pytest.raises(HTTPException) as ei2:
            _resolve_rotating_pool(db, "pool:off", None)
        assert ei2.value.status_code == 409 and "inactive" in str(ei2.value.detail)

"""Readiness of a sub-account is decided ONCE, and everything else reads that decision.

The bug this file exists to prevent (confirmed live 2026-09-04): the attach panel decided which
identities were usable from `Identity.status == "active"`. Per IdentityStatus's own definition that
only means ">=1 sub-account ROW exists", and gen_subaccounts_bulk sets it the instant sub-accounts
are generated -- so identities whose sub-accounts were all still NEW/REGISTERED were offered as
attachable, and nothing downstream stopped them: _rank_live_sub_accounts ranked NEW/REGISTERED as
4th/3rd-choice candidates, so a live launch happily picked one, leased a proxy, solved a CAPTCHA and
attempted a login the portal rejects before it ever looks at the credentials.

The first patch for it re-derived the tier client-side, in JSX, from /sub-accounts. That is the same
defect with a different spelling: a second copy of a domain rule, free to drift from the first.

So the invariant here is not "the panel filters correctly", it is:

    what GET /identities REPORTS about an identity's readiness is exactly what the launch gate
    ENFORCES -- for every sub-account status, with no third opinion anywhere.

Parametrized over the whole SubAccountStatus enum, so adding a status forces a decision here rather
than silently inheriting one.
"""
from __future__ import annotations

import pathlib
import re

import fakeredis
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.hub.db import get_db
from apps.hub.main import app
from libs.db import models as M
from libs.db.models import metadata


# Rotating-proxy credentials are ENVIRONMENT values (DECISIONS.md D40) and a launch 409s without
# them. This suite launches for real to compare the gate against the list, so it needs a configured
# Hub -- fake values, set explicitly, because apps/hub/config.py loads .env at import and these
# tests would otherwise mint with the real production credentials.
@pytest.fixture(autouse=True)
def _pool_credentials(monkeypatch):
    monkeypatch.setenv("SOAX_ROTATING_PACKAGE", "352438")
    monkeypatch.setenv("SOAX_ROTATING_PASSWORD", "test-rot-pw")
    monkeypatch.setenv("SOAX_MOBILE_PACKAGE", "350716")
    monkeypatch.setenv("SOAX_MOBILE_PASSWORD", "test-mob-pw")
    monkeypatch.setenv("WEBSHARE_PREFIX", "sodade2026")
    monkeypatch.setenv("WEBSHARE_PASSWORD", "test-ws-pw")
from libs.domain.enums import READY_SUB_ACCOUNT_STATUSES, SubAccountStatus


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
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
    import apps.hub.main as hub_main
    from libs.bus.bus import Bus
    monkeypatch.setattr(hub_main, "_bus", Bus(fakeredis.FakeStrictRedis(decode_responses=True)))
    monkeypatch.setattr(hub_main, "SessionLocal", TestSession)
    # A live launch probes the proxy for real; keep these hermetic.
    monkeypatch.setattr(hub_main, "check_proxy", lambda conn_url, timeout=20.0: (True, "ok"))
    monkeypatch.setattr(hub_main, "provider_reachable", lambda *a, **k: (True, "ok"))

    from fastapi.testclient import TestClient
    c = TestClient(app)
    c._Session = TestSession
    yield c
    app.dependency_overrides.clear()


def _headers(client):
    client.post("/auth/bootstrap", json={"username": "admin", "password": "pw"})
    r = client.post("/auth/login",
                    json={"username": "admin", "password": "pw", "purpose": "operate"})
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    # Rotating pools are minted from global settings since 2026-09-15, not leased from rows -- an
    # unconfigured pool 409s the launch by design, which would make every launch here fail for a
    # reason that has nothing to do with sub-account readiness (what this file actually tests).
    client.put("/settings/global", json={"values": {
        "SOAX_ROTATING_PACKAGE": "352438", "SOAX_ROTATING_PASSWORD": "test-rot-pw",
        "WEBSHARE_PREFIX": "sodade2026", "WEBSHARE_PASSWORD": "test-ws-pw",
    }}, headers=h)
    return h


def _live_booking(client, h, name="live-b"):
    client.post("/proxies", json={"provider": "soax", "kind": "rotating", "endpoint": "soax:1",
                                  "credentials_ref": "u:p", "max_sessions": 5}, headers=h)
    return client.post("/bookings",
                       json={"name": name, "type": "real", "posto_id": "5084",
                             "settings": {"ENGINE_MODE": "live"}},
                       headers=h).json()["id"]


def _identity_with_subs(client, h, statuses, name="R Person"):
    iid = client.post("/identities", json={"full_name": name, "type": "real"},
                      headers=h).json()["id"]
    db = client._Session()
    try:
        for n, st in enumerate(statuses):
            db.add(M.SubAccount(identity_id=iid, username=f"{iid[:6]}u{n}", password="pw",
                                status=st))
        db.commit()
    finally:
        db.close()
    return iid


def _reported_ready(client, h, identity_id):
    rows = client.get("/identities", headers=h).json()
    row = next(r for r in rows if r["id"] == identity_id)
    return row["ready_sub_accounts"]


@pytest.mark.parametrize("status", sorted(SubAccountStatus, key=lambda s: s.value))
def test_what_the_list_reports_is_what_the_launch_gate_enforces(client, status):
    """The parity invariant itself, for every status the enum can hold."""
    h = _headers(client)
    bid = _live_booking(client, h)
    iid = _identity_with_subs(client, h, [status])
    w = client.post(f"/bookings/{bid}/identities", json={"identity_id": iid}, headers=h).json()

    reported_ready = _reported_ready(client, h, iid) > 0
    launched = client.post(f"/workers/{w['id']}/launch", headers=h).status_code == 200

    assert reported_ready == launched, (
        f"{status}: GET /identities says ready={reported_ready} but a live launch "
        f"{'succeeded' if launched else 'was refused'} -- the list and the gate disagree")
    assert reported_ready == (status in READY_SUB_ACCOUNT_STATUSES), (
        f"{status}: readiness disagrees with READY_SUB_ACCOUNT_STATUSES, the one definition")


def test_the_count_is_a_count_not_a_flag(client):
    """It reports rotation DEPTH -- how many login attempts do_login() has before it runs out."""
    h = _headers(client)
    iid = _identity_with_subs(client, h, [SubAccountStatus.VERIFIED, SubAccountStatus.LOGIN_OK,
                                          SubAccountStatus.NEW, SubAccountStatus.BURNED])
    assert _reported_ready(client, h, iid) == 2


def test_an_identity_with_only_unverified_sub_accounts_is_refused_with_a_reason(client):
    """"no usable sub-account" on an identity that visibly HAS sub-accounts reads as a Hub bug.
    The refusal has to say the accounts exist but are not verified yet."""
    h = _headers(client)
    bid = _live_booking(client, h)
    iid = _identity_with_subs(client, h, [SubAccountStatus.NEW, SubAccountStatus.REGISTERED])
    w = client.post(f"/bookings/{bid}/identities", json={"identity_id": iid}, headers=h).json()

    r = client.post(f"/workers/{w['id']}/launch", headers=h)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "none is verified yet" in detail
    assert "2 not past registration/verification" in detail


def test_launch_never_rotates_into_an_unverified_sub_account(client, monkeypatch):
    """The candidate list handed to the worker is do_login()'s whole rotation budget. An unverified
    account in it is not a fallback -- it is a proxy lease and a CAPTCHA solve spent on a login the
    portal rejects outright."""
    h = _headers(client)
    bid = _live_booking(client, h)
    iid = _identity_with_subs(client, h, [SubAccountStatus.VERIFIED, SubAccountStatus.NEW,
                                          SubAccountStatus.REGISTERED, SubAccountStatus.RETIRED,
                                          SubAccountStatus.BURNED])
    w = client.post(f"/bookings/{bid}/identities", json={"identity_id": iid}, headers=h).json()

    import apps.hub.main as hub_main
    captured: dict = {}
    orig = hub_main._bus.send_command

    def _spy(worker_id, type, **payload):
        if type == "launch":
            captured.update(payload)
        return orig(worker_id, type, **payload)

    monkeypatch.setattr(hub_main._bus, "send_command", _spy)
    assert client.post(f"/workers/{w['id']}/launch", headers=h).status_code == 200

    db = client._Session()
    try:
        ready_ids = {s.id for s in db.scalars(
            select(M.SubAccount).where(M.SubAccount.identity_id == iid,
                                       M.SubAccount.status == SubAccountStatus.VERIFIED)).all()}
    finally:
        db.close()
    sent_ids = {a["sub_account_id"] for a in captured["accounts"]}
    assert sent_ids == ready_ids, "an unusable sub-account rode along in the rotation candidates"


# ── drift guard: the dashboard must not grow a second copy of the rule ───────────────────────
def _jsx_function_body(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    rest = src[start + 1:]
    end = rest.find("\nfunction ")
    return rest[:end if end != -1 else len(rest)]


def _code_only(body: str) -> str:
    return "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("//"))


def test_the_attach_panel_reads_readiness_and_does_not_recompute_it():
    """Same spirit as test_dashboard_action_table_matches_the_backend: the dashboard is allowed to
    RENDER a server decision, not to re-derive it. A sub-account status literal inside the attach
    panel means the readiness tier was spelled out a second time in JSX -- exactly the patch this
    change replaced: correct on the day it was written, free to drift the day the enum changes.

    Only the READY statuses are banned, and only in this one function. A blanket ban would fire on
    unrelated vocabularies that share words (a worker's outcome is also "booked"/"failed"), and a
    guard with false positives gets deleted rather than obeyed."""
    src = pathlib.Path("apps/dashboard/src/App.jsx").read_text()
    code = _code_only(_jsx_function_body(src, "AttachPanel"))
    for status in sorted(READY_SUB_ACCOUNT_STATUSES, key=lambda s: s.value):
        assert f'"{status.value}"' not in code, (
            f"AttachPanel spells out sub-account status {status.value!r} -- filter on the Hub's "
            f"ready_sub_accounts instead of re-deriving the tier client-side")
    assert "ready_sub_accounts" in code, (
        "the attach panel no longer reads ready_sub_accounts -- if the gate moved, move this test")


@pytest.mark.parametrize("fn", ["AttachPanel", "BookingWorkers"])
def test_the_attach_flow_does_not_join_sub_accounts_client_side(fn):
    """The first fix for this bug fetched /sub-accounts into the booking page and grouped it by
    identity to rebuild the ready set. That join belongs in the query that already knows the rule."""
    src = pathlib.Path("apps/dashboard/src/App.jsx").read_text()
    assert '"/sub-accounts"' not in _code_only(_jsx_function_body(src, fn)), (
        f"{fn} fetches /sub-accounts -- readiness is answered by GET /identities "
        f"(ready_sub_accounts), computed from READY_SUB_ACCOUNT_STATUSES")

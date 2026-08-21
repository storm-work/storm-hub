"""Every action in WORKER_ACTION_STATUSES must actually be ENFORCED by its endpoint.

state_machines.py states the invariant in its own comment: "The Hub endpoints enforce these and the
dashboard mirrors them, so a disabled button always means 'not applicable', never 'will 409'." That
was true for launch/stop/restart and silently false for detach, which never called
_require_worker_action at all -- so the table and the dashboard both forbade detaching a RUNNING
worker while the API happily did it, deleting its ProxyLease/SlotLease rows out from under a live
process.

The parity test below is the part worth keeping: it fails if a FIFTH action is ever added to the
table without an endpoint guard, rather than only pinning the one case that was broken.
"""
from __future__ import annotations

import fakeredis
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.hub.db import get_db
from apps.hub.main import app
from libs.db import models as M
from libs.db.models import metadata
from libs.domain.enums import WorkerStatus
from libs.domain.state_machines import WORKER_ACTION_STATUSES, can_worker_action


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
    import apps.hub.main as hub_main
    from libs.bus.bus import Bus
    monkeypatch.setattr(hub_main, "_bus", Bus(fakeredis.FakeStrictRedis(decode_responses=True)))
    monkeypatch.setattr(hub_main.supervisor, "spawn", lambda wid: 0)
    monkeypatch.setattr(hub_main.supervisor, "stop", lambda wid: True)
    monkeypatch.setattr(hub_main, "SessionLocal", TestSession)

    from fastapi.testclient import TestClient
    c = TestClient(app)
    c._Session = TestSession
    yield c
    app.dependency_overrides.clear()


def _headers(client):
    client.post("/auth/bootstrap", json={"username": "admin", "password": "pw"})
    r = client.post("/auth/login", json={"username": "admin", "password": "pw", "purpose": "operate"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _bk_with_worker(client, h):
    bid = client.post("/bookings", json={"name": "b", "type": "real", "posto_id": "5084"},
                      headers=h).json()["id"]
    iid = client.post("/identities", json={"full_name": "X", "type": "real"}, headers=h).json()["id"]
    w = client.post(f"/bookings/{bid}/identities", json={"identity_id": iid}, headers=h).json()
    return bid, iid, w["id"]


def _force_status(client, worker_id, status):
    db = client._Session()
    try:
        db.get(M.Worker, worker_id).status = status
        db.commit()
    finally:
        db.close()


def test_detach_is_refused_while_the_worker_is_running(client):
    """The exact gap: table says no, dashboard disables the button, endpoint used to allow it."""
    h = _headers(client)
    bid, iid, wid = _bk_with_worker(client, h)
    _force_status(client, wid, WorkerStatus.RUNNING)

    r = client.delete(f"/bookings/{bid}/identities/{iid}", headers=h)
    assert r.status_code == 409, r.text
    assert "detach" in r.text

    db = client._Session()
    try:
        assert db.get(M.Worker, wid) is not None, "a refused detach must not delete the worker"
    finally:
        db.close()


@pytest.mark.parametrize("status", sorted(WORKER_ACTION_STATUSES["detach"], key=lambda s: s.value))
def test_detach_succeeds_from_every_status_the_table_allows(client, status):
    h = _headers(client)
    bid, iid, wid = _bk_with_worker(client, h)
    _force_status(client, wid, status)
    r = client.delete(f"/bookings/{bid}/identities/{iid}", headers=h)
    assert r.status_code == 200, f"{status}: {r.text}"
    assert r.json() == {"detached": True}


def test_every_declared_action_has_an_endpoint_guard():
    """Parity, so a new action cannot be added to the table with no enforcement behind it.

    Reads main.py rather than exercising each endpoint: the point is that the guard call EXISTS for
    every action name, which is precisely what was missing for detach.
    """
    import pathlib
    src = pathlib.Path("apps/hub/main.py").read_text()
    for action in WORKER_ACTION_STATUSES:
        assert f'_require_worker_action(w, "{action}")' in src \
            or f'_require_worker_action(worker, "{action}")' in src, \
            f"action {action!r} is in WORKER_ACTION_STATUSES but no endpoint enforces it"


def test_dashboard_action_table_matches_the_backend():
    """The dashboard hardcodes its own copy of the table; drift means a button that lies."""
    import json
    import pathlib
    import re

    src = pathlib.Path("apps/dashboard/src/App.jsx").read_text()
    m = re.search(r"const WORKER_ACTIONS = \{(.*?)\n\};", src, re.S)
    assert m, "WORKER_ACTIONS not found in App.jsx"
    ui = {}
    for line in m.group(1).splitlines():
        lm = re.match(r"\s*(\w+):\s*\[(.*?)\],", line)
        if lm:
            ui[lm.group(1)] = {v.strip().strip('"') for v in lm.group(2).split(",") if v.strip()}

    backend = {a: {s.value for s in st} for a, st in WORKER_ACTION_STATUSES.items()}
    assert ui == backend, f"dashboard/backend action tables disagree:\n  ui={ui}\n  backend={backend}"


# ── HOST_SESSION_CAP (audit finding 2026-08-20) ──────────────────────────────
def test_host_session_cap_blocks_a_launch_past_the_limit(client):
    """The setting sat in the catalog with a documented meaning and no reader -- the recurring trap
    here. One live worker is one Chromium tree (~50-60 processes measured), on a host also running
    Postgres and Redis, and two concurrent bookings each size themselves without knowing about the
    other. Counted across ALL bookings for that reason."""
    h = _headers(client)
    bid, iid, wid = _bk_with_worker(client, h)

    client.put("/settings/global", json={"values": {"HOST_SESSION_CAP": 1}}, headers=h)
    _force_status(client, wid, WorkerStatus.RUNNING)      # one running == the cap

    bid2 = client.post("/bookings", json={"name": "b2", "type": "real", "posto_id": "5084"},
                       headers=h).json()["id"]
    iid2 = client.post("/identities", json={"full_name": "Y", "type": "real"}, headers=h).json()["id"]
    w2 = client.post(f"/bookings/{bid2}/identities", json={"identity_id": iid2}, headers=h).json()

    r = client.post(f"/workers/{w2['id']}/launch", headers=h)
    assert r.status_code == 409, r.text
    assert "cap" in r.text.lower()


def test_host_session_cap_of_zero_means_unlimited(client):
    h = _headers(client)
    bid, iid, wid = _bk_with_worker(client, h)
    client.put("/settings/global", json={"values": {"HOST_SESSION_CAP": 0}}, headers=h)
    _force_status(client, wid, WorkerStatus.RUNNING)

    bid2 = client.post("/bookings", json={"name": "b3", "type": "real", "posto_id": "5084"},
                       headers=h).json()["id"]
    iid2 = client.post("/identities", json={"full_name": "Z", "type": "real"}, headers=h).json()["id"]
    w2 = client.post(f"/bookings/{bid2}/identities", json={"identity_id": iid2}, headers=h).json()

    r = client.post(f"/workers/{w2['id']}/launch", headers=h)
    assert r.status_code != 409, "0 must disable the cap, not block everything"


# ── clear-checkpoint button (operator request 2026-08-21) ────────────────────
def test_clear_checkpoint_removes_the_identitys_saved_session(client, tmp_path, monkeypatch):
    """Checkpoints are keyed by IDENTITY, not worker, and a bad one is self-sustaining: the worker
    restores it, fails, and can restore the same dead cookies next launch. Clearing it by hand meant
    an ad-hoc rm against var/checkpoints/ on the server -- the habit that cost this project four days."""
    from libs.engine import checkpoint as ckpt

    monkeypatch.setattr(ckpt, "_CHECKPOINTS_DIR", tmp_path)
    h = _headers(client)
    bid, iid, wid = _bk_with_worker(client, h)
    ckpt.save_checkpoint(iid, cookies={"Vistos_sid": "x"}, sub_account_id="s1")
    assert ckpt.load_checkpoint(iid) is not None

    r = client.delete(f"/workers/{wid}/checkpoint", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["cleared"] is True
    assert ckpt.load_checkpoint(iid) is None


def test_clear_checkpoint_reports_when_there_was_none(client, tmp_path, monkeypatch):
    """"cleared: false" is a real answer, not an error -- the operator needs to know the difference
    between "removed it" and "there was nothing there"."""
    from libs.engine import checkpoint as ckpt

    monkeypatch.setattr(ckpt, "_CHECKPOINTS_DIR", tmp_path)
    h = _headers(client)
    bid, iid, wid = _bk_with_worker(client, h)

    r = client.delete(f"/workers/{wid}/checkpoint", headers=h)
    assert r.status_code == 200
    assert r.json()["cleared"] is False


def test_clear_checkpoint_works_from_any_worker_status(client, tmp_path, monkeypatch):
    """Deliberately NOT status-gated: the file is read at launch and written on healthy transitions,
    so deleting it never touches a live session -- it only changes what the next launch starts from."""
    from libs.engine import checkpoint as ckpt

    monkeypatch.setattr(ckpt, "_CHECKPOINTS_DIR", tmp_path)
    h = _headers(client)
    bid, iid, wid = _bk_with_worker(client, h)
    _force_status(client, wid, WorkerStatus.RUNNING)
    ckpt.save_checkpoint(iid, cookies={}, sub_account_id="s1")

    assert client.delete(f"/workers/{wid}/checkpoint", headers=h).status_code == 200
    assert ckpt.load_checkpoint(iid) is None


def test_clear_checkpoint_404s_on_an_unknown_worker(client):
    h = _headers(client)
    assert client.delete("/workers/does-not-exist/checkpoint", headers=h).status_code == 404

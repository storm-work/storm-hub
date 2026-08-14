"""Worker status/substate semantics and the action-legality guards (review batch, 2026-08-14).

Four confirmed bugs, all from the same root cause -- four overlapping vocabularies (Worker.status,
Worker.substate, the WS live substate, and the WS terminal outcome) with no single owner:

1. `substate` was written ONLY by apps/hub/events.py's map_event_to_update, so every endpoint and
   reaper that moved a worker out of RUNNING left the in-flight substate behind. Rows read
   `status=ready, substate=holding` and the dashboard faithfully rendered a stopped worker as
   "holding". Fixed by _set_worker_status / Scheduler._fail_worker, which clear it.

2. `held` (libs/engine/phases.py's two PhaseResult("held", ...) exits -- "no slot assigned" and
   "apply bounced") had no entry in _TERMINAL_STATUS and hit the FAILED default. See
   tests/hub/test_control.py for that half plus its drift guard.

3. launch/restart had NO status guard, so both were live on a COMPLETED worker that had already
   booked a real appointment -- and launching an already-RUNNING one ran _do_launch_worker's
   unconditional lease_proxy (which has no per-worker dedupe), leaking a permanently unreleased
   ProxyLease row per click.

4. The dashboard's row and bulk buttons each invented their own enablement rule. They now share
   WORKER_ACTION_STATUSES with the endpoints; this file pins the backend half.
"""
import time

import fakeredis
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.hub.db import get_db
from apps.hub.main import app
from libs.db import metadata
from libs.db import models as M
from libs.domain.enums import WorkerStatus, WorkerSubstate
from libs.domain.state_machines import can_worker_action


# Captured at import time, BEFORE conftest's autouse _launch_fleet_synchronously patches it out.
# (Deliberately not importlib.reload(hub_main) to get it back: reloading re-executes module-level
# code and would build fresh supervisor/_bus/scheduler singletons mid-session, which every other
# test is still holding references to.)
from apps.hub.main import _begin_fleet_launch as _REAL_BEGIN_FLEET_LAUNCH  # noqa: E402


@pytest.fixture()
def _real_background_launch(monkeypatch):
    """Undo conftest's synchronous-launch fixture for the two tests that exercise the real
    background dispatch. Safe here because both stub _launch_worker_fleet itself, so no thread ever
    touches the SQLite connection."""
    import apps.hub.main as hub_main
    monkeypatch.setattr(hub_main, "_begin_fleet_launch", _REAL_BEGIN_FLEET_LAUNCH)


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


def _operate_headers(client, username="admin", password="pw"):
    client.post("/auth/bootstrap", json={"username": username, "password": password})
    r = client.post("/auth/login", json={"username": username, "password": password, "purpose": "operate"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _bk_with_worker(client, h, name="b"):
    bid = client.post("/bookings", json={"name": name, "type": "real", "posto_id": "5084"},
                      headers=h).json()["id"]
    iid = client.post("/identities", json={"full_name": "X", "type": "real"}, headers=h).json()["id"]
    w = client.post(f"/bookings/{bid}/identities", json={"identity_id": iid}, headers=h).json()
    return bid, w["id"]


def _force(client, worker_id, **fields):
    """Put a worker directly into a status/substate the happy path can't reach in a sim test."""
    db = client._Session()
    try:
        w = db.get(M.Worker, worker_id)
        for k, v in fields.items():
            setattr(w, k, v)
        db.commit()
    finally:
        db.close()


def _row(client, worker_id):
    db = client._Session()
    try:
        w = db.get(M.Worker, worker_id)
        db.refresh(w)
        return {"status": w.status, "substate": w.substate, "detail": w.substate_detail}
    finally:
        db.close()


def _await_status(client, worker_id, status, timeout=5.0):
    """The fleet launch is backgrounded now (_begin_fleet_launch), so /start returns before the
    worker is actually RUNNING -- poll instead of asserting on a race."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _row(client, worker_id)["status"] == status:
            return
        time.sleep(0.05)
    raise AssertionError(f"worker {worker_id} never reached {status} "
                         f"(still {_row(client, worker_id)['status']})")


# ── the capability table itself (pure) ────────────────────────────────────────
def test_completed_worker_can_never_be_launched_or_restarted():
    assert can_worker_action("launch", WorkerStatus.COMPLETED) is False
    assert can_worker_action("restart", WorkerStatus.COMPLETED) is False
    assert can_worker_action("detach", WorkerStatus.COMPLETED) is True   # cleanup stays allowed


def test_running_worker_cannot_be_launched_again():
    assert can_worker_action("launch", WorkerStatus.RUNNING) is False
    assert can_worker_action("stop", WorkerStatus.RUNNING) is True


def test_held_worker_is_relaunchable_like_stopped_and_failed():
    for action in ("launch", "restart", "detach"):
        assert can_worker_action(action, WorkerStatus.HELD) is True
    assert can_worker_action("stop", WorkerStatus.HELD) is False


def test_dashboard_action_table_matches_the_backend_table():
    """The dashboard mirrors WORKER_ACTION_STATUSES so a disabled button means "not applicable"
    rather than "will 409". Nothing enforces that at runtime -- they're in different languages -- so
    this parses the JS table out and pins them together. Drifting them apart is exactly how the
    buttons got confusing in the first place."""
    import json
    import re
    from pathlib import Path

    from libs.domain.state_machines import WORKER_ACTION_STATUSES

    src = Path("apps/dashboard/src/App.jsx").read_text()
    block = re.search(r"const WORKER_ACTIONS = \{(.*?)\n\};", src, re.S)
    assert block, "could not find WORKER_ACTIONS in App.jsx -- did it get renamed?"
    js = {}
    for action, values in re.findall(r"(\w+):\s*\[([^\]]*)\]", block.group(1)):
        js[action] = set(json.loads(f"[{values}]"))

    py = {action: {str(s) for s in statuses} for action, statuses in WORKER_ACTION_STATUSES.items()}
    assert js == py


def test_every_worker_status_is_covered_by_the_action_table():
    """A new WorkerStatus that nobody adds to the table would silently disable every action on it."""
    from libs.domain.state_machines import WORKER_ACTION_STATUSES

    covered = set().union(*WORKER_ACTION_STATUSES.values())
    assert set(WorkerStatus) - covered == set()


# ── endpoint guards ───────────────────────────────────────────────────────────
def test_launch_on_running_worker_is_rejected_and_leases_nothing(client):
    """The proxy-lease leak: Supervisor.spawn is idempotent so no second process appeared, but
    _do_launch_worker's lease_proxy ran unconditionally and had no per-worker dedupe."""
    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)
    client.post(f"/bookings/{bid}/start", headers=h)
    _await_status(client, wid, WorkerStatus.RUNNING)

    r = client.post(f"/workers/{wid}/launch", headers=h)
    assert r.status_code == 409 and "cannot launch" in r.json()["detail"]

    db = client._Session()
    try:
        leases = db.scalars(select(M.ProxyLease).where(M.ProxyLease.worker_id == wid)).all()
    finally:
        db.close()
    assert len(leases) == 0   # sim mode leases none; the point is the second call added nothing


def test_launch_and_restart_on_completed_worker_are_rejected(client):
    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)
    _force(client, wid, status=WorkerStatus.COMPLETED, substate=WorkerSubstate.BOOKED)

    assert client.post(f"/workers/{wid}/launch", headers=h).status_code == 409
    assert client.post(f"/workers/{wid}/restart", headers=h).status_code == 409
    assert _row(client, wid)["status"] == WorkerStatus.COMPLETED   # untouched


def test_stop_on_a_non_running_worker_is_rejected(client):
    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)   # starts READY
    assert client.post(f"/workers/{wid}/stop", headers=h).status_code == 409


# ── substate never outlives the run it belongs to ─────────────────────────────
def test_stop_worker_clears_the_in_flight_substate(client):
    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)
    client.post(f"/bookings/{bid}/start", headers=h)
    _await_status(client, wid, WorkerStatus.RUNNING)
    _force(client, wid, substate=WorkerSubstate.HOLDING)

    client.post(f"/workers/{wid}/stop", headers=h)

    row = _row(client, wid)
    assert row["status"] == WorkerStatus.STOPPED
    assert row["substate"] is None   # NOT still "holding"


def test_stop_booking_clears_the_in_flight_substate(client):
    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)
    client.post(f"/bookings/{bid}/start", headers=h)
    _await_status(client, wid, WorkerStatus.RUNNING)
    _force(client, wid, substate=WorkerSubstate.DISCOVERING)

    client.post(f"/bookings/{bid}/stop", headers=h)

    assert _row(client, wid)["substate"] is None


def test_start_booking_requeue_clears_a_stale_substate(client):
    """The exact shape the operator saw: status=ready next to substate=holding."""
    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)
    client.post(f"/bookings/{bid}/start", headers=h)
    _await_status(client, wid, WorkerStatus.RUNNING)
    client.post(f"/workers/{wid}/stop", headers=h)
    _force(client, wid, status=WorkerStatus.FAILED, substate=WorkerSubstate.HOLDING)

    client.post(f"/bookings/{bid}/start", headers=h)
    _await_status(client, wid, WorkerStatus.RUNNING)   # requeued READY, then launched

    assert _row(client, wid)["substate"] is None                    # the worker's own `started` event repopulates it


def test_start_booking_also_requeues_held_workers(client):
    """HELD is relaunchable -- it used to be swept up only because it was mis-labelled FAILED."""
    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)
    client.post(f"/bookings/{bid}/start", headers=h)
    _await_status(client, wid, WorkerStatus.RUNNING)
    _force(client, wid, status=WorkerStatus.HELD, substate=None)

    r = client.post(f"/bookings/{bid}/start", headers=h)

    assert r.json()["started"] == 1
    _await_status(client, wid, WorkerStatus.RUNNING)


# ── /start must not block on the fleet launch (confirmed live 2026-08-14) ──────
@pytest.mark.usefixtures("_real_background_launch")
def test_start_returns_immediately_and_is_idempotent_while_launching(client, monkeypatch):
    """"Start button doesn't work": /bookings/{id}/start ran _launch_worker_fleet INLINE, and a live
    launch is minutes of real per-proxy browser probes -- so the request never came back. The
    operator clicked eight more times and each click started ANOTHER concurrent fleet launch (234
    chrome processes observed for a 6-worker booking). Must return at once, and a second click
    while one is in flight must be a no-op."""
    import threading

    import apps.hub.main as hub_main

    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)

    release = threading.Event()
    calls: list[list[str]] = []

    def _slow_fleet(worker_ids, *, jitter_secs=0.0):
        calls.append(list(worker_ids))
        release.wait(timeout=5)      # stand in for the minutes a real launch takes
        return []

    monkeypatch.setattr(hub_main, "_launch_worker_fleet", _slow_fleet)

    try:
        r1 = client.post(f"/bookings/{bid}/start", headers=h)   # returns despite the slow launch
        assert r1.status_code == 200
        assert r1.json()["launching"] is True

        r2 = client.post(f"/bookings/{bid}/start", headers=h)   # second click, launch still running
        assert r2.status_code == 200
        assert r2.json()["already_launching"] is True
    finally:
        release.set()

    assert len(calls) == 1   # exactly ONE fleet launch, not one per click


@pytest.mark.usefixtures("_real_background_launch")
def test_start_clears_the_in_flight_flag_even_if_the_launch_raises(client, monkeypatch):
    """A launch that blows up must not wedge the booking into permanently 'already launching'."""
    import apps.hub.main as hub_main

    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)

    def _boom(worker_ids, *, jitter_secs=0.0):
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(hub_main, "_launch_worker_fleet", _boom)
    client.post(f"/bookings/{bid}/start", headers=h)

    for _ in range(50):
        if bid not in hub_main._launching:
            break
        time.sleep(0.05)
    assert bid not in hub_main._launching

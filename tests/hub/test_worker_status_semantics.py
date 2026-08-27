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
from libs.domain.enums import SlotStatus as M_SlotStatus
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


# ── worker log replay (confirmed live 2026-08-14) ─────────────────────────────
def test_worker_logs_replays_history_from_the_event_stream(client):
    """Worker logs were WS-only: a worker that did its work before the tab connected showed "no log
    lines received over the WS yet" while its whole history sat in Redis. Oldest-first, and terminal
    events are included so a failure reason is visible in the same view."""
    import apps.hub.main as hub_main

    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)

    hub_main._bus.emit_event(wid, "log_line", text="-> logging_in")
    hub_main._bus.emit_event(wid, "log_line", text="-> warmed")
    hub_main._bus.emit_event(wid, "terminal", outcome="held", detail="no slot assigned")
    hub_main._bus.emit_event("someone-else", "log_line", text="not mine")

    r = client.get(f"/workers/{wid}/logs", headers=h)
    assert r.status_code == 200
    texts = [l["text"] for l in r.json()["lines"]]
    assert texts == ["-> logging_in", "-> warmed", "[terminal] held: no slot assigned"]


def test_worker_logs_404s_for_an_unknown_worker(client):
    h = _operate_headers(client)
    assert client.get("/workers/nope/logs", headers=h).status_code == 404


# ── identity allocation exposure (attach panel, 2026-08-14) ───────────────────
def test_identities_expose_the_active_booking_holding_them(client):
    """C2 exclusivity was enforced only at attach time (409), never exposed -- so the attach panel
    listed identities that were guaranteed to fail and the operator found out by picking one."""
    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)          # attaches one identity to an active booking
    held = client.get(f"/bookings/{bid}/workers", headers=h).json()[0]["identity_id"]
    free = client.post("/identities", json={"full_name": "Free", "type": "real"},
                       headers=h).json()["id"]

    rows = {i["id"]: i for i in client.get("/identities", headers=h).json()}
    assert rows[held]["allocated_booking_id"] == bid
    assert rows[held]["allocated_booking_name"] == "b"
    assert rows[free]["allocated_booking_id"] is None


def test_an_unreleased_allocation_blocks_reattach_even_from_a_terminal_booking(client):
    """The annotation must report what ACTUALLY blocks. attach_identity's own 409 uses
    BOOKING_ACTIVE_STATUSES (which excludes DRAFT/COMPLETED/ABORTED), but the DB index is
    UNIQUE(identity_id) WHERE released_at IS NULL and ignores booking status -- and nothing releases
    an allocation when a booking merely goes terminal. Reporting the narrower rule would make the
    attach panel offer identities that then fail."""
    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)
    held = client.get(f"/bookings/{bid}/workers", headers=h).json()[0]["identity_id"]

    db = client._Session()
    try:
        bk = db.get(M.Booking, bid)
        bk.status = "aborted"          # terminal, but the allocation is still unreleased
        db.commit()
    finally:
        db.close()

    rows = {i["id"]: i for i in client.get("/identities", headers=h).json()}
    assert rows[held]["allocated_booking_id"] == bid   # still reported as held -- correctly

    bid2 = client.post("/bookings", json={"name": "b2", "type": "real", "posto_id": "5084"},
                       headers=h).json()["id"]
    r = client.post(f"/bookings/{bid2}/identities", json={"identity_id": held}, headers=h)
    assert r.status_code == 409                        # and it genuinely does still block
    assert "unreleased allocation" in r.json()["detail"]


def test_detaching_frees_the_identity_for_another_booking(client):
    """The escape hatch the 409 message points at must actually work."""
    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)
    held = client.get(f"/bookings/{bid}/workers", headers=h).json()[0]["identity_id"]

    assert client.delete(f"/bookings/{bid}/identities/{held}", headers=h).status_code == 200

    rows = {i["id"]: i for i in client.get("/identities", headers=h).json()}
    assert rows[held]["allocated_booking_id"] is None

    bid2 = client.post("/bookings", json={"name": "b2", "type": "real", "posto_id": "5084"},
                       headers=h).json()["id"]
    assert client.post(f"/bookings/{bid2}/identities",
                       json={"identity_id": held}, headers=h).status_code == 201


# ── launch failures must be recorded (confirmed live 2026-08-14) ──────────────
def test_a_failed_launch_records_the_reason_on_the_worker(client, monkeypatch):
    """Two workers on a 7-worker booking sat at READY forever while five ran. Their launches had
    raised (proxy lease exhausting its health-probe budget -- which raises BEFORE spawn, correctly
    leaving the worker READY and relaunchable) but the exception was swallowed and the `failed` list
    discarded: no log, no event, no field on the row. The operator saw "ready" and nothing else."""
    import apps.hub.main as hub_main

    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)

    def _boom(db, w):
        raise HTTPException(409, f"worker {w.id}: no free soax proxy available (pool exhausted)")

    from fastapi import HTTPException
    monkeypatch.setattr(hub_main, "_do_launch_worker", _boom)

    failed = hub_main._launch_worker_fleet([wid])

    assert failed == [wid]
    row = _row(client, wid)
    assert row["status"] == WorkerStatus.READY            # still relaunchable, as before
    assert "launch failed" in row["detail"]
    assert "pool exhausted" in row["detail"]              # and it says WHY


def test_hub_applies_vendor_env_for_its_own_proxy_probes():
    """check_proxy runs in the HUB, so the Hub must apply the same vendor settings as the workers.
    It didn't: proxies were screened at NAV_TIMEOUT_SCALE=1.0 with anti-detect off, harsher than the
    workers that would use them -- and each false negative set is_healthy=False, which
    _candidate_query filters on, permanently dropping a good proxy from the pool."""
    import os

    from libs.engine import vendor_env

    for k in ("NAV_TIMEOUT_SCALE", "STEALTH_HARDEN"):
        os.environ.pop(k, None)
    exported = vendor_env.apply({"NAV_TIMEOUT_SCALE": 4.0, "STEALTH_HARDEN": True, "IGNORED": "x"})
    assert exported == {"NAV_TIMEOUT_SCALE": "4", "STEALTH_HARDEN": "1"}
    assert os.environ["NAV_TIMEOUT_SCALE"] == "4"


# ── partial settings updates must not clobber (confirmed live 2026-08-14) ─────
def test_patch_settings_merges_instead_of_replacing(client):
    """Two panels both PUT the WHOLE settings object from their own page-load snapshot, so the
    overview's proxy/swap shortcut silently wiped the Settings tab's calendar window. Confirmed on
    booking Test2: CAL_START_DATE/CAL_END_DATE were configured and came back absent."""
    h = _operate_headers(client)
    bid = client.post("/bookings", json={
        "name": "cal", "type": "real", "posto_id": "5084",
        "settings": {"CAL_START_DATE": "2026-09-01", "CAL_END_DATE": "2026-09-20"},
    }, headers=h).json()["id"]

    r = client.patch(f"/bookings/{bid}/settings",
                     json={"PROXY_PROVIDER": "soax", "HOLD_SWAP_AFTER_WARMUP": True}, headers=h)
    assert r.status_code == 200
    got = r.json()["settings_json"]
    assert got["CAL_START_DATE"] == "2026-09-01"     # untouched by an unrelated panel
    assert got["CAL_END_DATE"] == "2026-09-20"
    assert got["PROXY_PROVIDER"] == "soax" and got["HOLD_SWAP_AFTER_WARMUP"] is True


def test_put_settings_still_replaces_wholesale(client):
    """The full Settings panel relies on replace semantics -- an absent key means "clear this
    override" -- so PUT must NOT quietly become a merge."""
    h = _operate_headers(client)
    bid = client.post("/bookings", json={
        "name": "cal2", "type": "real", "posto_id": "5084",
        "settings": {"CAL_START_DATE": "2026-09-01", "PROXY_PROVIDER": "soax"},
    }, headers=h).json()["id"]

    client.put(f"/bookings/{bid}", json={"settings": {"PROXY_PROVIDER": "webshare"}}, headers=h)

    got = client.get(f"/bookings/{bid}", headers=h).json()["settings_json"]
    assert got == {"PROXY_PROVIDER": "webshare"}     # CAL_START_DATE deliberately cleared


# ── a fresh run must not inherit the previous run's slots ─────────────────────
def test_start_closes_the_previous_runs_slots(client):
    """Confirmed live 2026-08-14: booking Test2 restarted at 08:44:22 and had slot_open AND
    slot_found stamped at 08:44:25 -- the same instant -- purely because 24 slots from the prior run
    were still AVAILABLE, so _fire_slot_found_if_new fired on the next tick. The run skipped the
    entire slot-open discovery phase on stale observations."""
    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)
    db = client._Session()
    try:
        db.add(M.Slot(booking_id=bid, posto_id="5084", date="2026-09-01", period=1,
                      status=M_SlotStatus.AVAILABLE))
        db.add(M.Slot(booking_id=bid, posto_id="5084", date="2026-09-02", period=2,
                      status=M_SlotStatus.TAKEN))       # a real booking -- must survive
        db.commit()
    finally:
        db.close()

    client.post(f"/bookings/{bid}/start", headers=h)

    db = client._Session()
    try:
        statuses = sorted(str(s.status) for s in
                          db.scalars(select(M.Slot).where(M.Slot.booking_id == bid)).all())
    finally:
        db.close()
    assert statuses == ["closed", "taken"]     # observation retired, real booking untouched


def test_start_releases_leases_on_the_slots_it_closes(client):
    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)
    db = client._Session()
    try:
        slot = M.Slot(booking_id=bid, posto_id="5084", date="2026-09-01", period=1,
                      status=M_SlotStatus.LEASED)
        db.add(slot); db.commit()
        db.add(M.SlotLease(slot_id=slot.id, worker_id=wid)); db.commit()
    finally:
        db.close()

    client.post(f"/bookings/{bid}/start", headers=h)

    db = client._Session()
    try:
        assert not db.scalars(select(M.SlotLease).where(
            M.SlotLease.released_at.is_(None))).all()
    finally:
        db.close()


def test_worker_logs_are_scoped_to_the_current_run(client):
    """A Worker ROW outlives the run that created it -- every /start relaunches the same row -- and
    the event stream is keyed only by worker_id. So the log replayed yesterday's attempts
    interleaved with today's, with nothing to tell them apart, which is exactly the view an operator
    reads to understand what THIS run did. Booking.started_at is rewritten on every /start, so it is
    the run boundary."""
    import time

    import apps.hub.main as hub_main
    from libs.db import models as M

    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)

    hub_main._bus.emit_event(wid, "log_line", text="from the PREVIOUS run")
    time.sleep(0.02)

    # Start the run: this stamps started_at, which becomes the boundary.
    db = hub_main.SessionLocal()
    try:
        from datetime import datetime, timezone
        db.get(M.Booking, bid).started_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()
    time.sleep(0.02)
    hub_main._bus.emit_event(wid, "log_line", text="from THIS run")

    r = client.get(f"/workers/{wid}/logs", headers=h).json()
    assert [l["text"] for l in r["lines"]] == ["from THIS run"]
    assert r["hidden_from_earlier_runs"] == 1

    # ...and the operator can still get everything back deliberately.
    r_all = client.get(f"/workers/{wid}/logs?all_runs=true", headers=h).json()
    assert [l["text"] for l in r_all["lines"]] == ["from the PREVIOUS run", "from THIS run"]
    assert r_all["hidden_from_earlier_runs"] == 0


def test_a_booking_that_never_started_shows_everything(client):
    """No started_at means no run boundary to apply -- hiding everything would be worse than
    showing a little extra."""
    import apps.hub.main as hub_main

    h = _operate_headers(client)
    bid, wid = _bk_with_worker(client, h)
    hub_main._bus.emit_event(wid, "log_line", text="before any run")

    r = client.get(f"/workers/{wid}/logs", headers=h).json()
    assert [l["text"] for l in r["lines"]] == ["before any run"]
    assert r["run_started_at"] is None


def test_a_launch_failure_reason_survives_the_requeue_to_ready():
    """READY + `launch failed:` is the dead-end state this marker exists for, so it must not be
    wiped by the very relaunch that is about to re-establish it.

    Every relaunch path calls _set_worker_status(w, READY) on the way in, and that helper nulls
    substate_detail by design. Confirmed live 2026-08-27 on "Test 2032": worker 645b41e9 sat READY
    with substate_detail NULL while the Hub log held the real cause ("no webshare proxy leased
    after 5 probe(s): 5 failed health"). The operator had no way to see why a worker never ran.
    """
    from apps.hub.main import _set_worker_status
    from apps.hub.scheduler import LAUNCH_FAILED_PREFIX
    from libs.db import models as M
    from libs.domain.enums import WorkerStatus

    w = M.Worker(booking_id="b", identity_id="i", role="apply", status=WorkerStatus.READY)
    w.substate_detail = f"{LAUNCH_FAILED_PREFIX} no webshare proxy leased after 5 probe(s)"
    _set_worker_status(w, WorkerStatus.READY)
    assert (w.substate_detail or "").startswith(LAUNCH_FAILED_PREFIX), \
        "the launch-failure reason was wiped by the requeue"
    assert w.substate is None


def test_an_explicit_new_detail_still_replaces_a_launch_failure_reason():
    from apps.hub.main import _set_worker_status
    from apps.hub.scheduler import LAUNCH_FAILED_PREFIX
    from libs.db import models as M
    from libs.domain.enums import WorkerStatus
    w = M.Worker(booking_id="b", identity_id="i", role="apply", status=WorkerStatus.READY)
    w.substate_detail = f"{LAUNCH_FAILED_PREFIX} old reason"
    _set_worker_status(w, WorkerStatus.READY, detail="operator stop")
    assert w.substate_detail == "operator stop"


def test_other_stale_details_are_still_cleared():
    """The narrow exception must not resurrect the 2026-08-14 behaviour it is carved out of: a
    relaunched worker must not keep reading 'no live process (dead-worker reap)'."""
    from apps.hub.main import _set_worker_status
    from libs.db import models as M
    from libs.domain.enums import WorkerStatus
    w = M.Worker(booking_id="b", identity_id="i", role="apply", status=WorkerStatus.FAILED)
    w.substate_detail = "no live process (dead-worker reap)"
    _set_worker_status(w, WorkerStatus.READY)
    assert w.substate_detail is None


def test_a_launch_failure_reason_is_not_carried_into_running():
    """Only READY keeps it. A worker that actually started must never display a stale failure."""
    from apps.hub.main import _set_worker_status
    from apps.hub.scheduler import LAUNCH_FAILED_PREFIX
    from libs.db import models as M
    from libs.domain.enums import WorkerStatus
    w = M.Worker(booking_id="b", identity_id="i", role="apply", status=WorkerStatus.READY)
    w.substate_detail = f"{LAUNCH_FAILED_PREFIX} lease exhausted"
    _set_worker_status(w, WorkerStatus.RUNNING)
    assert w.substate_detail is None

"""_do_launch_worker's live-engine binding (Phase C6): extends the launch payload with everything
LiveEngineOps needs, and only marks a worker RUNNING after spawn+command-group are confirmed."""
import fakeredis
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.hub.db import get_db
from apps.hub.main import app
from libs.db import metadata


@pytest.fixture()
def db_session():
    """A plain session for unit-testing _do_launch_worker directly (the `client` fixture below
    builds a whole TestClient, which these launch-abort tests don't need)."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


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

    # Fake the bus so a live-mode launch test never needs a real Redis, and stub the supervisor so
    # no real subprocess is spawned for the live-mode assertions.
    import apps.hub.main as hub_main
    fake_redis = fakeredis.FakeStrictRedis(decode_responses=True)
    from libs.bus.bus import Bus
    monkeypatch.setattr(hub_main, "_bus", Bus(fake_redis))
    monkeypatch.setattr(hub_main.supervisor, "spawn", lambda wid: None)
    monkeypatch.setattr(hub_main.supervisor, "stop", lambda wid: None)
    # A live-mode launch now runs a real-portal health probe before leasing a proxy (P4) -- fake it
    # healthy so these tests stay fast/hermetic, no real browser/network involved.
    monkeypatch.setattr(hub_main, "check_proxy", lambda conn_url, timeout=20.0: (True, "ok"))
    # _launch_worker_fleet (POST /bookings/{id}/start) opens its own SessionLocal() per background
    # thread instead of using Depends(get_db) -- point it at this test's isolated engine too.
    monkeypatch.setattr(hub_main, "SessionLocal", TestSession)

    from fastapi.testclient import TestClient
    yield TestClient(app)
    app.dependency_overrides.clear()


def _operate_headers(client, username="admin", password="pw"):
    client.post("/auth/bootstrap", json={"username": username, "password": password})
    r = client.post("/auth/login", json={"username": username, "password": password, "purpose": "operate"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_live_launch_missing_sub_account_returns_409_and_does_not_spawn(client):
    h = _operate_headers(client)
    tpl = client.post("/templates", json={"name": "Apply Live", "role": "apply", "settings": {}}, headers=h).json()
    iid = client.post("/identities", json={"full_name": "R Person", "type": "real"}, headers=h).json()["id"]
    bid = client.post(
        "/bookings",
        json={"name": "live-b", "type": "real", "posto_id": "5084",
              "settings": {"ENGINE_MODE": "live"}},
        headers=h,
    ).json()["id"]
    w = client.post(f"/bookings/{bid}/identities",
                    json={"identity_id": iid, "template_id": tpl["id"]}, headers=h).json()

    r = client.post(f"/workers/{w['id']}/launch", headers=h)
    assert r.status_code == 409
    assert "no usable sub-account" in r.json()["detail"]

    got = client.get(f"/bookings/{bid}/workers", headers=h).json()
    assert got[0]["status"] != "running"


def test_start_booking_applies_warmup_start_jitter_per_worker(client, monkeypatch):
    """WARMUP_START_JITTER, resolved from the booking's own settings, must reach
    _launch_worker_fleet's jitter_secs -- confirms the /bookings/{id}/start endpoint wires it
    through, not just the fleet-launch mechanism in isolation."""
    h = _operate_headers(client)
    tpl = client.post("/templates", json={"name": "Jitter Sim", "role": "apply", "settings": {}},
                      headers=h).json()
    iid = client.post("/identities", json={"full_name": "Jitter Test", "type": "real"},
                      headers=h).json()["id"]
    bid = client.post(
        "/bookings",
        json={"name": "jitter-b", "type": "real", "posto_id": "5084",
              "settings": {"WARMUP_START_JITTER": 42.0}},   # ENGINE_MODE defaults to sim
        headers=h,
    ).json()["id"]
    client.post(f"/bookings/{bid}/identities", json={"identity_id": iid, "template_id": tpl["id"]},
               headers=h)

    import apps.hub.main as hub_main
    captured = {}
    orig = hub_main._launch_worker_fleet

    def _spy(worker_ids, **kw):
        captured["jitter_secs"] = kw.get("jitter_secs")
        return orig(worker_ids, **kw)

    monkeypatch.setattr(hub_main, "_launch_worker_fleet", _spy)

    r = client.post(f"/bookings/{bid}/start", headers=h)
    assert r.status_code == 200
    assert captured["jitter_secs"] == 42.0


def test_live_launch_sends_full_payload_and_marks_running(client, monkeypatch):
    h = _operate_headers(client)
    tpl = client.post("/templates", json={"name": "Apply Live2", "role": "apply", "settings": {}}, headers=h).json()
    iid = client.post(
        "/identities",
        json={"full_name": "Jane Doe", "type": "real", "nationality": "CPV", "gender": "F",
              "passport_no": "P123", "birthdate": "1990-01-01"},
        headers=h,
    ).json()["id"]
    client.post("/identities/gen-subaccounts", json={"identity_ids": [iid], "count": 1}, headers=h)
    client.post("/proxies", json={"provider": "soax", "kind": "static", "endpoint": "1.2.3.4:8080",
                                  "credentials_ref": "user:pass", "max_sessions": 1}, headers=h)
    bid = client.post(
        "/bookings",
        json={"name": "live-b2", "type": "real", "posto_id": "5084", "nationality": "CPV",
              "residence": "IRL", "settings": {"ENGINE_MODE": "live"}},
        headers=h,
    ).json()["id"]
    w = client.post(f"/bookings/{bid}/identities",
                    json={"identity_id": iid, "template_id": tpl["id"]}, headers=h).json()

    captured = {}
    import apps.hub.main as hub_main
    orig_send = hub_main._bus.send_command

    def _spy_send_command(worker_id, type, **payload):
        if type == "launch":
            captured.update(payload)
        return orig_send(worker_id, type, **payload)

    monkeypatch.setattr(hub_main._bus, "send_command", _spy_send_command)

    r = client.post(f"/workers/{w['id']}/launch", headers=h)
    assert r.status_code == 200
    assert r.json()["status"] == "running"

    assert captured["_engine"] == "live"
    assert captured["account"]["first_name"] == "Jane"
    assert captured["account"]["last_name"] == "Doe"
    assert captured["account"]["gender_expanded"] == "FEMALE"
    assert captured["posto_id"] == "5084"
    assert captured["nationality"] == "CPV"
    assert captured["residence"] == "IRL"
    assert captured["warm_proxy"].startswith("http://user:pass@1.2.3.4:8080")
    assert captured["identity_id"] == iid
    assert captured["booking_id"] == bid

    # Sub-account rotation (do_login()'s sticky-then-rotate loop): the FULL ranked candidate list
    # rides along, each carrying its own sub_account_id, and the DB's Worker.sub_account_id is set
    # to match the one actually picked -- previously never written at all.
    assert captured["accounts"]
    assert all("sub_account_id" in a for a in captured["accounts"])
    assert captured["accounts"][0]["sub_account_id"] == captured["sub_account_id"]
    got = client.get(f"/bookings/{bid}/workers", headers=h).json()
    assert got[0]["sub_account_id"] == captured["sub_account_id"]


def test_live_launch_leases_hold_proxy_only_when_swap_enabled(client, monkeypatch):
    h = _operate_headers(client)
    tpl = client.post(
        "/templates",
        json={"name": "Apply Swap", "role": "apply", "settings": {"HOLD_SWAP_AFTER_FOUND": True}},
        headers=h,
    ).json()
    iid = client.post("/identities", json={"full_name": "Swap Test", "type": "real"}, headers=h).json()["id"]
    client.post("/identities/gen-subaccounts", json={"identity_ids": [iid], "count": 1}, headers=h)
    client.post("/proxies", json={"provider": "soax", "kind": "static", "endpoint": "1.1.1.1:1111",
                                  "credentials_ref": "u1:p1", "max_sessions": 1}, headers=h)
    client.post("/proxies", json={"provider": "soax", "kind": "residential", "endpoint": "2.2.2.2:2222",
                                  "credentials_ref": "u2:p2", "max_sessions": 1}, headers=h)
    bid = client.post(
        "/bookings",
        json={"name": "live-swap", "type": "real", "posto_id": "5084",
              "settings": {"ENGINE_MODE": "live", "HOLD_PROXY_PROVIDER": "soax"}},
        headers=h,
    ).json()["id"]
    w = client.post(f"/bookings/{bid}/identities",
                    json={"identity_id": iid, "template_id": tpl["id"]}, headers=h).json()

    captured = {}
    import apps.hub.main as hub_main
    orig_send = hub_main._bus.send_command

    def _spy(worker_id, type, **payload):
        if type == "launch":
            captured.update(payload)
        return orig_send(worker_id, type, **payload)

    monkeypatch.setattr(hub_main._bus, "send_command", _spy)

    r = client.post(f"/workers/{w['id']}/launch", headers=h)
    assert r.status_code == 200

    assert captured["warm_proxy"].endswith("1.1.1.1:1111")
    assert captured["hold_proxy"].endswith("2.2.2.2:2222")
    assert captured["warm_proxy"] != captured["hold_proxy"]


def test_live_launch_swap_disabled_leases_no_hold_proxy(client, monkeypatch):
    h = _operate_headers(client)
    tpl = client.post("/templates", json={"name": "Apply NoSwap", "role": "apply", "settings": {}},
                      headers=h).json()   # HOLD_SWAP_AFTER_FOUND defaults False
    iid = client.post("/identities", json={"full_name": "No Swap", "type": "real"}, headers=h).json()["id"]
    client.post("/identities/gen-subaccounts", json={"identity_ids": [iid], "count": 1}, headers=h)
    client.post("/proxies", json={"provider": "soax", "kind": "static", "endpoint": "3.3.3.3:3333",
                                  "credentials_ref": "u3:p3", "max_sessions": 1}, headers=h)
    bid = client.post(
        "/bookings", json={"name": "live-noswap", "type": "real", "posto_id": "5084",
                           "settings": {"ENGINE_MODE": "live"}},
        headers=h,
    ).json()["id"]
    w = client.post(f"/bookings/{bid}/identities",
                    json={"identity_id": iid, "template_id": tpl["id"]}, headers=h).json()

    captured = {}
    import apps.hub.main as hub_main
    orig_send = hub_main._bus.send_command

    def _spy(worker_id, type, **payload):
        if type == "launch":
            captured.update(payload)
        return orig_send(worker_id, type, **payload)

    monkeypatch.setattr(hub_main._bus, "send_command", _spy)

    r = client.post(f"/workers/{w['id']}/launch", headers=h)
    assert r.status_code == 200
    assert captured["hold_proxy"] is None


def test_launch_does_not_clobber_substate_already_advanced_by_a_racing_worker(client, monkeypatch):
    """Regression, confirmed against a LIVE booking on 2026-08-13: _do_launch_worker's final write
    used to force substate=IDLE unconditionally after confirming spawn+launch succeeded. Under a
    fast serial launch this always won the race against the freshly spawned worker process's OWN
    "started" event (which also sets substate=IDLE, then LOGGING_IN/... as the phase machine
    advances) -- but once launches started running concurrently (P4's _launch_worker_fleet, needed
    for the live-proxy-lease health probe), the worker process could race ahead, get several real
    status_change events applied by EventHub (observed live: all the way to HOLDING), and then this
    write would land late and stomp substate back to IDLE -- PERMANENTLY, since a worker sitting in
    a real hold has nothing further to report. That worker then became invisible to the scheduler's
    HOLDING-only assignment query despite genuinely holding (confirmed live: two apply workers stuck
    at "idle" in the DB while their real Redis event history showed them holding, one already
    swapped -- assign never fired, no slots ever got attempted despite plenty being AVAILABLE), and
    the dashboard showed a permanently stale substate.

    Simulates the exact race: the fake send_command, standing in for "the launch command reached
    the worker process", synchronously applies the worker's real event sequence (the way EventHub
    would) BEFORE _do_launch_worker's own final write executes."""
    h = _operate_headers(client)
    tpl = client.post("/templates", json={"name": "Race", "role": "apply", "settings": {}},
                      headers=h).json()
    iid = client.post("/identities", json={"full_name": "Race Worker", "type": "real"},
                      headers=h).json()["id"]
    bid = client.post("/bookings", json={"name": "race-b", "type": "real", "posto_id": "5084"},
                      headers=h).json()["id"]
    w = client.post(f"/bookings/{bid}/identities",
                    json={"identity_id": iid, "template_id": tpl["id"]}, headers=h).json()

    import apps.hub.events as events_mod
    import apps.hub.main as hub_main

    # hub_main.SessionLocal was already pointed at this test's isolated engine by the client
    # fixture (needed for P4's _launch_worker_fleet) -- reuse it for events.py's own separate
    # SessionLocal import so the simulated EventHub write lands in the same test DB.
    monkeypatch.setattr(events_mod, "SessionLocal", hub_main.SessionLocal)
    orig_send = hub_main._bus.send_command

    def _racing_send_command(worker_id, type, **payload):
        result = orig_send(worker_id, type, **payload)
        if type == "launch":
            hub = events_mod.EventHub.__new__(events_mod.EventHub)
            for ev_type, ev_payload in [
                ("started", {}),
                ("status_change", {"substate": "logging_in"}),
                ("status_change", {"substate": "logged_in"}),
                ("status_change", {"substate": "warming_up"}),
                ("status_change", {"substate": "warmed"}),
                ("status_change", {"substate": "holding"}),
            ]:
                hub._apply_db(worker_id, events_mod.map_event_to_update(ev_type, ev_payload))
        return result

    monkeypatch.setattr(hub_main._bus, "send_command", _racing_send_command)

    r = client.post(f"/workers/{w['id']}/launch", headers=h)
    assert r.status_code == 200
    assert r.json()["status"] == "running"
    assert r.json()["substate"] == "holding"   # NOT reset to "idle" by the launch-confirmation write


def test_launch_worker_fleet_runs_concurrently_and_isolates_one_workers_failure(monkeypatch):
    """P4: /bookings/{id}/start and the scheduler's warmup-start tick now launch all READY workers
    via _launch_worker_fleet (a bounded thread pool, each worker on its own DB session) instead of a
    serial loop -- required once a live-mode lease can involve a real network probe per worker.
    Tests _launch_worker_fleet directly, with SessionLocal and _do_launch_worker both faked, rather
    than through the full HTTP+SQLite-StaticPool+thread-pool stack: two real threads concurrently
    reading/writing the SAME shared SQLite connection (StaticPool hands out one physical connection
    to every session) is inherently racy and would make this test flaky for reasons that have
    nothing to do with the behavior under test -- real concurrency safety over a shared DB row is
    Postgres's FOR UPDATE SKIP LOCKED, covered by tests/proxies/test_lease.py instead.

    Proves two things deterministically: (1) both workers' launches genuinely run concurrently, via
    a Barrier that only releases once both threads have entered -- if _launch_worker_fleet secretly
    serialized them, this hangs/times out rather than silently passing; (2) one worker's launch
    raising does not prevent the other's from completing or propagate out of _launch_worker_fleet,
    unlike the old serial `for w in workers: _do_launch_worker(db, w)` loop, where an exception
    mid-loop aborted every worker after it in iteration order."""
    import threading
    import types

    import apps.hub.main as hub_main

    barrier = threading.Barrier(2, timeout=5)
    calls: list[str] = []
    lock = threading.Lock()

    def fake_launch(db, w) -> None:
        with lock:
            calls.append(w.id)
        barrier.wait()   # both threads must arrive here -- proves genuine concurrency, not serial
        if w.id == "bad":
            raise RuntimeError("boom")

    rows = {"good": types.SimpleNamespace(id="good"), "bad": types.SimpleNamespace(id="bad")}

    class _FakeDB:
        def get(self, _model, worker_id):
            return rows.get(worker_id)

        def commit(self) -> None:   # the failure path records a reason on the row
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(hub_main, "SessionLocal", lambda: _FakeDB())
    monkeypatch.setattr(hub_main, "_do_launch_worker", fake_launch)

    failed = hub_main._launch_worker_fleet(["good", "bad"])

    assert failed == ["bad"]
    assert set(calls) == {"good", "bad"}   # both were attempted, regardless of "bad"'s failure
    # and the failure is RECORDED, not merely counted -- a worker left behind by a failed launch
    # must be able to say why (2026-08-14)
    assert "boom" in rows["bad"].substate_detail
    assert not hasattr(rows["good"], "substate_detail")


def test_launch_worker_fleet_jitter_delays_each_worker_before_launch(monkeypatch):
    """WARMUP_START_JITTER (operator spec 2026-08-14): each worker's spawn is staggered by a random
    delay in [0, jitter_secs) BEFORE _do_launch_worker runs -- anti-herd for a fleet launch."""
    import apps.hub.main as hub_main

    launched: list[str] = []
    sleeps: list[float] = []

    def fake_launch(db, w) -> None:
        launched.append(w.id)

    rows = {"w1": __import__("types").SimpleNamespace(id="w1"),
           "w2": __import__("types").SimpleNamespace(id="w2")}

    class _FakeDB:
        def get(self, _model, worker_id):
            return rows.get(worker_id)

        def close(self) -> None:
            pass

    monkeypatch.setattr(hub_main, "SessionLocal", lambda: _FakeDB())
    monkeypatch.setattr(hub_main, "_do_launch_worker", fake_launch)
    monkeypatch.setattr(hub_main.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(hub_main.random, "uniform", lambda a, b: 3.5)

    failed = hub_main._launch_worker_fleet(["w1", "w2"], jitter_secs=10.0)

    assert failed == []
    assert set(launched) == {"w1", "w2"}
    assert sleeps == [3.5, 3.5]   # one jittered sleep per worker


def test_launch_worker_fleet_zero_jitter_never_sleeps(monkeypatch):
    import apps.hub.main as hub_main

    sleeps: list[float] = []
    monkeypatch.setattr(hub_main, "SessionLocal",
                        lambda: __import__("types").SimpleNamespace(get=lambda *a: None, close=lambda: None))
    monkeypatch.setattr(hub_main.time, "sleep", lambda s: sleeps.append(s))

    hub_main._launch_worker_fleet(["w1"], jitter_secs=0.0)

    assert sleeps == []


# ── a launch decided before a stop must not spawn after it (confirmed live 2026-08-14) ───────
def test_launch_aborts_when_the_booking_is_no_longer_active(db_session, monkeypatch):
    """The operator stopped "Test 3018" at 15:01:24 and a scout process came up at 15:01:47 -- 23s
    AFTER the stop. A launch decided beforehand (hold-respawn, or a fleet launch still inside its
    WARMUP_START_JITTER sleep) spawns long after it, and a stop can only kill processes that already
    exist when it runs. The decision must be re-validated at the moment of spawning."""
    import apps.hub.main as main
    from libs.db import models as M
    from libs.domain.enums import BookingStatus, WorkerRole, WorkerStatus

    spawned = []
    monkeypatch.setattr(main.supervisor, "spawn", lambda *a, **k: spawned.append(a), raising=False)

    db = db_session
    bk = M.Booking(name="b", type="real", posto_id="3018", status=BookingStatus.STOPPED)
    i = M.Identity(full_name="GILSON TAVARES", type="real")
    db.add_all([bk, i]); db.commit()
    w = M.Worker(booking_id=bk.id, identity_id=i.id, role=WorkerRole.SCOUT,
                 status=WorkerStatus.READY)
    db.add(w); db.commit()

    main._do_launch_worker(db, w)

    assert spawned == [], "spawned a worker for a stopped booking"
    assert w.status != WorkerStatus.RUNNING


def test_launch_aborts_when_the_worker_itself_was_stopped_mid_flight(db_session, monkeypatch):
    import apps.hub.main as main
    from libs.db import models as M
    from libs.domain.enums import BookingStatus, WorkerRole, WorkerStatus

    spawned = []
    monkeypatch.setattr(main.supervisor, "spawn", lambda *a, **k: spawned.append(a), raising=False)

    db = db_session
    bk = M.Booking(name="b2", type="real", posto_id="3018", status=BookingStatus.RUNNING)
    i = M.Identity(full_name="X Y", type="real")
    db.add_all([bk, i]); db.commit()
    w = M.Worker(booking_id=bk.id, identity_id=i.id, role=WorkerRole.SCOUT,
                 status=WorkerStatus.STOPPED)
    db.add(w); db.commit()

    main._do_launch_worker(db, w)
    assert spawned == []


def test_start_launches_only_after_the_booking_transition_is_committed(client, monkeypatch):
    """CONFIRMED LIVE 2026-08-14. start_booking called _begin_fleet_launch BEFORE the commit that
    moved the booking out of STOPPED. The fleet launch runs on its own thread with its own DB
    session, so that session read the booking as still STOPPED and _do_launch_worker's stop-guard
    correctly refused all six workers -- while the endpoint reported a successful start and the
    dashboard sat on "Launching 6 worker(s)" with nothing running for ten minutes.

    Pins the ordering: by the time a launch is requested, the status the launch thread will read
    must already be persisted."""
    import apps.hub.main as main
    from libs.db import models as M
    from libs.domain.enums import BookingStatus

    h = _operate_headers(client)
    iid = client.post("/identities", json={"full_name": "Order Test", "type": "real"},
                      headers=h).json()["id"]
    bid = client.post("/bookings", json={"name": "OrderB", "type": "test", "posto_id": "3018",
                                         "nationality": "CPV", "residence": "FRA"},
                      headers=h).json()["id"]
    client.post(f"/bookings/{bid}/attach-identities", json={"identity_ids": [iid]}, headers=h)

    db = main.SessionLocal()
    bk = db.get(M.Booking, bid)
    bk.status = BookingStatus.STOPPED
    db.commit(); db.close()

    seen = {}

    def _spy(booking_id, worker_ids, *, jitter_secs=0.0):
        # Read through a FRESH session, exactly as the real launch thread does.
        db2 = main.SessionLocal()
        try:
            seen["status"] = db2.get(M.Booking, booking_id).status
        finally:
            db2.close()
        return True

    monkeypatch.setattr(main, "_begin_fleet_launch", _spy)
    client.post(f"/bookings/{bid}/start", headers=h)

    assert "status" in seen, "no launch was requested at all"
    assert seen["status"] != BookingStatus.STOPPED, (
        "the launch thread would read STOPPED -- _do_launch_worker's stop-guard refuses that, "
        "which is exactly the silent no-op seen live")

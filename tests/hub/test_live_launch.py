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
    # Static/ISP leases now use check_proxy_lease_verdict (never condemns a live exit on a portal
    # throttle) -- fake it healthy too, same reason.
    monkeypatch.setattr(hub_main, "check_proxy_lease_verdict", lambda conn_url, timeout=20.0: (True, "ok"))
    # _launch_worker_fleet (POST /bookings/{id}/start) opens its own SessionLocal() per background
    # thread instead of using Depends(get_db) -- point it at this test's isolated engine too.
    monkeypatch.setattr(hub_main, "SessionLocal", TestSession)

    from fastapi.testclient import TestClient
    c = TestClient(app)
    c._Session = TestSession                     # for _gen_verified_subs below
    yield c
    app.dependency_overrides.clear()


def _gen_verified_subs(client, h, identity_id, count=1):
    """Generate sub-accounts AND mark them verified.

    gen-subaccounts creates rows in NEW -- generated, never registered at the portal. A live launch
    refuses those (READY_SUB_ACCOUNT_STATUSES; see
    tests/hub/test_sub_account_readiness_parity.py), which is the point of that gate: an unverified
    account cannot log in, so launching one spends a proxy lease and a CAPTCHA solve for nothing. In
    production the registrar moves them to VERIFIED; these tests are about the launch PAYLOAD, so
    they fast-forward that step rather than exercise it."""
    from libs.db import models as M
    from libs.domain.enums import SubAccountStatus
    from sqlalchemy import select

    client.post("/identities/gen-subaccounts",
                json={"identity_ids": [identity_id], "count": count}, headers=h)
    db = client._Session()
    try:
        for sa in db.scalars(select(M.SubAccount).where(
                M.SubAccount.identity_id == identity_id)).all():
            sa.status = SubAccountStatus.VERIFIED
        db.commit()
    finally:
        db.close()


def _operate_headers(client, username="admin", password="pw"):
    client.post("/auth/bootstrap", json={"username": username, "password": password})
    r = client.post("/auth/login", json={"username": username, "password": password, "purpose": "operate"})
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    return h


# Rotating pools are MINTED per launch (libs/proxies/rotating.py), not leased from rows, and their
# credentials come from the ENVIRONMENT -- never from settings (DECISIONS.md D40). A Hub with no
# pool credentials cannot launch a rotating worker at all: 409, by design, because a half-built
# credential authenticates as nobody and the failure then reads as a portal problem.
#
# autouse so every launch test in this module gets a configured Hub, which is the equivalent of
# bootstrapping the admin user. The values are FAKE and set via monkeypatch: apps/hub/config.py
# loads .env at import, so without an explicit override these tests would mint with the real
# production credentials.
@pytest.fixture(autouse=True)
def _pool_credentials(monkeypatch):
    monkeypatch.setenv("SOAX_ROTATING_PACKAGE", "352438")
    monkeypatch.setenv("SOAX_ROTATING_PASSWORD", "test-rot-pw")
    monkeypatch.setenv("SOAX_MOBILE_PACKAGE", "350716")
    monkeypatch.setenv("SOAX_MOBILE_PASSWORD", "test-mob-pw")
    monkeypatch.setenv("WEBSHARE_PREFIX", "sodade2026")
    monkeypatch.setenv("WEBSHARE_PASSWORD", "test-ws-pw")


def _is_minted(url: str, provider: str) -> bool:
    """A rotating warm_proxy is MINTED per launch (libs/proxies/rotating.py), so it can no longer
    be compared against a seeded row's endpoint. What still matters -- and what these tests were
    really pinning -- is that the credential belongs to the RESOLVED PROVIDER, which is what the
    settings-layer override tests are about."""
    if provider == "soax":
        return url.startswith("http://package-") and url.endswith("@proxy.soax.com:5000")
    if provider == "webshare":
        return url.startswith("http://sodade2026-") and url.endswith("@p.webshare.io:80")
    raise AssertionError(f"unknown provider {provider}")


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
    _gen_verified_subs(client, h, iid)
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
    assert _is_minted(captured["warm_proxy"], "soax")
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


def _spy_launch(monkeypatch):
    import apps.hub.main as hub_main
    captured = {}
    orig = hub_main._bus.send_command

    def _spy(worker_id, type, **payload):
        if type == "launch":
            captured.update(payload)
        return orig(worker_id, type, **payload)

    monkeypatch.setattr(hub_main._bus, "send_command", _spy)
    return captured


def test_unpinned_worker_mints_from_the_set_and_is_NOT_frozen_to_one_geo(client, monkeypatch):
    """An UNPINNED worker is no longer bound to a single country (2026-10-01): it mints with a geo
    from the booking's PROXY_GEOS set and carries the WHOLE set in its launch payload so it can vary
    its exit country on every roll (ops_live._roll_exit). So w.proxy_geo stays NULL (not frozen), and
    launch ships proxy_geos. The old per-worker freeze was the login-throttle worst case."""
    h = _operate_headers(client)
    tpl = client.post("/templates", json={"name": "Geo", "role": "apply", "settings": {}},
                      headers=h).json()
    iid = client.post("/identities", json={"full_name": "Geo Test", "type": "real"},
                      headers=h).json()["id"]
    _gen_verified_subs(client, h, iid)
    bid = client.post("/bookings", json={
        "name": "geo", "type": "real", "posto_id": "5084",
        # single-code set -> the mint pick is deterministic, so the mint can be asserted exactly.
        "settings": {"ENGINE_MODE": "live", "PROXY_PROVIDER": "soax", "PROXY_GEOS": "PT"}},
        headers=h).json()["id"]
    w = client.post(f"/bookings/{bid}/identities",
                    json={"identity_id": iid, "template_id": tpl["id"]}, headers=h).json()

    captured = _spy_launch(monkeypatch)
    assert client.post(f"/workers/{w['id']}/launch", headers=h).status_code == 200
    assert "-country-pt-" in captured["warm_proxy"], captured["warm_proxy"]
    assert captured["proxy_geos"] == ["PT"], captured.get("proxy_geos")   # whole set shipped
    # NOT frozen on the row -> the worker varies, it is not bound to one country.
    assert client.get(f"/bookings/{bid}/workers", headers=h).json()[0]["proxy_geo"] is None


def test_operator_geo_override_persists_and_binds_the_mint(client, monkeypatch):
    """PATCH /workers/{id}/geo sets the binding; the next launch mints with it, overriding the
    booking default -- this is also the reuse path (launch reads an already-bound proxy_geo).
    Clearing it (geo="") unbinds so the next launch re-draws from the set."""
    h = _operate_headers(client)
    tpl = client.post("/templates", json={"name": "GeoOv", "role": "apply", "settings": {}},
                      headers=h).json()
    iid = client.post("/identities", json={"full_name": "Geo Ov", "type": "real"},
                      headers=h).json()["id"]
    _gen_verified_subs(client, h, iid)
    bid = client.post("/bookings", json={
        "name": "geo-ov", "type": "real", "posto_id": "5084",
        "settings": {"ENGINE_MODE": "live", "PROXY_PROVIDER": "soax", "PROXY_GEOS": "PT"}},
        headers=h).json()["id"]
    w = client.post(f"/bookings/{bid}/identities",
                    json={"identity_id": iid, "template_id": tpl["id"]}, headers=h).json()

    # Options come from the resolved provider (soax). List is empty until populated, so validation is
    # permissive -- an override still takes and persists.
    opts = client.get(f"/workers/{w['id']}/geo", headers=h).json()
    assert opts["provider"] == "soax" and opts["current"] is None

    r = client.patch(f"/workers/{w['id']}/geo", json={"geo": "es"}, headers=h)
    assert r.status_code == 200 and r.json()["proxy_geo"] == "ES"   # normalized upper

    captured = _spy_launch(monkeypatch)
    assert client.post(f"/workers/{w['id']}/launch", headers=h).status_code == 200
    assert "-country-es-" in captured["warm_proxy"], "the operator override must win over PROXY_GEOS=PT"

    # Clearing unbinds (persisted): the next launch re-draws from PT.
    assert client.patch(f"/workers/{w['id']}/geo", json={"geo": ""}, headers=h).json()["proxy_geo"] is None


def test_stopping_a_worker_releases_its_static_proxy_lease(client, monkeypatch):
    """A STOPPED worker must not keep holding a finite ISP lease. Confirmed leak 2026-09-25
    (booking 0924-3018): 13 STOPPED workers held 13/13 of the host's active leases forever, because
    stop set status=STOPPED but never released the lease."""
    from libs.db import models as M
    h = _operate_headers(client)
    tpl = client.post("/templates", json={"name": "StopRel", "role": "apply", "settings": {}},
                      headers=h).json()
    iid = client.post("/identities", json={"full_name": "Stop Rel", "type": "real"},
                      headers=h).json()["id"]
    _gen_verified_subs(client, h, iid)
    client.post("/proxies", json={"provider": "capsolver", "kind": "static", "endpoint": "cap:1",
                                  "credentials_ref": "u:p", "max_sessions": 1,
                                  "resource_type": "static"}, headers=h)
    bid = client.post("/bookings", json={
        "name": "stop-rel", "type": "real", "posto_id": "5084",
        "settings": {"ENGINE_MODE": "live", "PROXY_PROVIDER": "capsolver", "PROXY_KIND": "static"}},
        headers=h).json()["id"]
    w = client.post(f"/bookings/{bid}/identities",
                    json={"identity_id": iid, "template_id": tpl["id"]}, headers=h).json()
    assert client.post(f"/workers/{w['id']}/launch", headers=h).status_code == 200

    db = client._Session()
    active = lambda: db.query(M.ProxyLease).filter(M.ProxyLease.worker_id == w["id"],
                                                   M.ProxyLease.released_at.is_(None)).count()
    assert active() == 1, "a launched static worker must hold exactly one active lease"

    assert client.post(f"/workers/{w['id']}/stop", headers=h).status_code == 200
    db.expire_all()
    assert active() == 0, "stopping the worker must release its lease"
    db.close()


def test_all_isp_provider_leases_across_providers_without_a_kind(client, monkeypatch):
    """PROXY_PROVIDER='all-isp' leases a STATIC row from ANY ISP provider -- no PROXY_KIND needed
    (the sentinel forces the lease path), and the union spans providers. Two static rows of two
    different providers; the worker leases one of them (endpoint carried, so LEASED not minted)."""
    h = _operate_headers(client)
    tpl = client.post("/templates", json={"name": "All ISP", "role": "apply", "settings": {}},
                      headers=h).json()
    iid = client.post("/identities", json={"full_name": "All ISP Test", "type": "real"},
                      headers=h).json()["id"]
    _gen_verified_subs(client, h, iid)
    client.post("/proxies", json={"provider": "capsolver", "kind": "static", "endpoint": "cap:1",
                                  "credentials_ref": "u:p", "max_sessions": 5,
                                  "resource_type": "static"}, headers=h)
    client.post("/proxies", json={"provider": "thordata", "kind": "static", "endpoint": "thor:1",
                                  "credentials_ref": "u:p", "max_sessions": 5,
                                  "resource_type": "static"}, headers=h)
    bid = client.post(
        "/bookings",
        json={"name": "all-isp", "type": "real", "posto_id": "5084",
              # No PROXY_KIND on purpose -- 'all-isp' alone must route to the lease path.
              "settings": {"ENGINE_MODE": "live", "PROXY_PROVIDER": "all-isp"}},
        headers=h,
    ).json()["id"]
    w = client.post(f"/bookings/{bid}/identities",
                    json={"identity_id": iid, "template_id": tpl["id"]}, headers=h).json()

    import apps.hub.main as hub_main
    captured = {}
    orig_send = hub_main._bus.send_command

    def _spy(worker_id, type, **payload):
        if type == "launch":
            captured.update(payload)
        return orig_send(worker_id, type, **payload)

    monkeypatch.setattr(hub_main._bus, "send_command", _spy)
    r = client.post(f"/workers/{w['id']}/launch", headers=h)
    assert r.status_code == 200, r.text
    # A leased static row carries its own endpoint -- and it is one of the two ISP providers, never
    # a minted rotating credential.
    assert captured["warm_proxy"].endswith(("cap:1", "thor:1")), captured["warm_proxy"]
    assert "package-" not in captured["warm_proxy"] and "sodade2026-" not in captured["warm_proxy"], (
        "all-isp must LEASE an ISP row, not mint a rotating credential")


def test_launch_and_mid_hold_rotation_resolve_the_same_worker_override(client, monkeypatch):
    """Phase 5, end-to-end: booking-level PROXY_PROVIDER='soax' (A), worker-level override
    'webshare' (B). Proves BOTH halves of the invariant in one test -- launch resolves B (already
    covered individually by test_live_launch_sends_full_payload_and_marks_running's provider-neutral
    assertions, but pinned again here for the direct A/B contrast) AND a subsequent mid-hold rotation
    request resolves the SAME B, not booking A. Before Phase 5, apps/hub/events.py's
    _handle_proxy_request never included the WORKER settings layer at all, so this second half would
    have resolved 'soax' -- silently reverting the worker's own override the instant it rotated."""
    h = _operate_headers(client)
    tpl = client.post("/templates", json={"name": "Worker Override", "role": "apply", "settings": {}},
                      headers=h).json()
    iid = client.post("/identities", json={"full_name": "Override Test", "type": "real"},
                      headers=h).json()["id"]
    _gen_verified_subs(client, h, iid)
    # Both pools are STATIC as of 2026-09-15: a rotating mid-flight rotation never reaches
    # _handle_proxy_request any more (the worker mints its own), so the A/B override contrast is
    # only observable on the static path -- which is exactly where it still matters, because a
    # static exit IS assigned by the Hub and reverting the worker's override there picks a wrong,
    # finite, individually-owned IP.
    client.post("/proxies", json={"provider": "soax", "kind": "static", "endpoint": "soax:1",
                                  "credentials_ref": "u:p", "max_sessions": 5,
                                  "resource_type": "static"}, headers=h)
    client.post("/proxies", json={"provider": "webshare", "kind": "static", "endpoint": "webshare:1",
                                  "credentials_ref": "u:p", "max_sessions": 5,
                                  "resource_type": "static"}, headers=h)
    bid = client.post(
        "/bookings",
        json={"name": "worker-override", "type": "real", "posto_id": "5084",
              "settings": {"ENGINE_MODE": "live", "PROXY_PROVIDER": "soax",
                           "PROXY_KIND": "static"}},
        headers=h,
    ).json()["id"]
    w = client.post(
        f"/bookings/{bid}/identities",
        json={"identity_id": iid, "template_id": tpl["id"],
             "worker_settings": {"PROXY_PROVIDER": "webshare"}},
        headers=h,
    ).json()

    import apps.hub.events as events_mod
    import apps.hub.main as hub_main

    captured = {}
    orig_send = hub_main._bus.send_command

    def _spy_send_command(worker_id, type, **payload):
        if type == "launch":
            captured.update(payload)
        return orig_send(worker_id, type, **payload)

    monkeypatch.setattr(hub_main._bus, "send_command", _spy_send_command)

    r = client.post(f"/workers/{w['id']}/launch", headers=h)
    assert r.status_code == 200
    # A static pool is LEASED, not minted -- so the launch payload carries the row's own endpoint.
    assert captured["warm_proxy"].endswith("webshare:1"), (
        "launch resolved a different effective PROXY_PROVIDER than the worker override")

    class _FakeBus:
        def __init__(self):
            self.sent = []

        def send_command(self, worker_id, type, **payload):
            self.sent.append((worker_id, type, payload))

    monkeypatch.setattr(events_mod, "SessionLocal", hub_main.SessionLocal)
    bus = _FakeBus()
    hub = events_mod.EventHub.__new__(events_mod.EventHub)
    hub._handle_proxy_request(w["id"], "warmup", bus, health_check=lambda url: (True, "ok"))

    assert len(bus.sent) == 1
    worker_id, typ, payload = bus.sent[0]
    assert typ == "proxy_assigned"
    assert payload["proxy"].endswith("webshare:1"), (
        "mid-hold rotation resolved a different effective PROXY_PROVIDER than launch did")


def test_mid_flight_rotation_refuses_a_rotating_pool_before_probing_anything(client, monkeypatch):
    """CONFIRMED LIVE 2026-09-11 (booking 0911, mid-run): _handle_proxy_request ran the real
    browser health probe on EVERY rotation, including for rotating pools where a per-row health
    flag has no subject (the exit changes per session). _do_launch_worker has always gated this
    (`health_check=(check_proxy if _static_pool(_kind) else None)`); this sibling call site never
    got the same gate. Measured cost: 46-124s per rotation, ~12.7 requests/min against a 4-thread
    executor -- a self-amplifying queue, since a slow rotation makes a worker roll more exits.

    2026-09-15 made the cure structural rather than a gate: a rotating rotation is REFUSED here, so
    the worker rolls locally and the Hub is never in the path at all. The original assertion is
    kept verbatim -- `check_proxy` still must not run -- because it is the expensive half, and it
    now proves the refusal happens BEFORE any probe rather than after a wasted one."""
    import apps.hub.events as events_mod
    import apps.hub.main as hub_main
    import libs.proxies.health as health_mod

    h = _operate_headers(client)
    tpl = client.post("/templates", json={"name": "Rot", "role": "apply", "settings": {}},
                      headers=h).json()
    iid = client.post("/identities", json={"full_name": "Rot Test", "type": "real"},
                      headers=h).json()["id"]
    _gen_verified_subs(client, h, iid)
    client.post("/proxies", json={"provider": "soax", "kind": "rotating", "endpoint": "soax:9",
                                  "credentials_ref": "u:p", "max_sessions": 5}, headers=h)
    bid = client.post("/bookings",
                      json={"name": "rot-b", "type": "real", "posto_id": "5084",
                            "settings": {"ENGINE_MODE": "live", "PROXY_PROVIDER": "soax",
                                         "PROXY_KIND": "rotating"}}, headers=h).json()["id"]
    w = client.post(f"/bookings/{bid}/identities",
                    json={"identity_id": iid, "template_id": tpl["id"]}, headers=h).json()

    def _explode(*a, **kw):
        raise AssertionError("browser health probe ran for a ROTATING pool -- that probe is ~25s "
                             "per candidate and tells us nothing about a per-session exit")

    monkeypatch.setattr(health_mod, "check_proxy", _explode)
    monkeypatch.setattr(events_mod, "check_proxy", _explode, raising=False)

    class _FakeBus:
        def __init__(self): self.sent = []
        def send_command(self, worker_id, type, **payload): self.sent.append((worker_id, type, payload))

    monkeypatch.setattr(events_mod, "SessionLocal", hub_main.SessionLocal)
    bus = _FakeBus()
    hub = events_mod.EventHub.__new__(events_mod.EventHub)
    hub._handle_proxy_request(w["id"], "warmup", bus)   # NO injected health_check -- the real gate

    assert len(bus.sent) == 1
    _wid, typ, payload = bus.sent[0]
    assert typ == "proxy_unavailable", f"the Hub assigned a rotating exit instead of refusing: {payload}"
    assert "mint" in payload["reason"], payload["reason"]


def test_launch_resolves_template_level_proxy_override_over_the_booking_default(client, monkeypatch):
    """2026-09-09: PROXY_PROVIDER etc. are now template-overridable, so several templates in one
    booking can draw from different pools. Booking-level PROXY_PROVIDER='soax' (A), template-level
    override 'webshare' (B) -- launch must resolve B, proving TEMPLATE actually outranks BOOKING
    for these keys now (the resolver's own WORKER > TEMPLATE > BOOKING > GLOBAL precedence was
    already correct; only the catalog's settable-layers list was blocking this specific override)."""
    h = _operate_headers(client)
    tpl = client.post("/templates",
                      json={"name": "Webshare Warmup", "role": "apply",
                            "settings": {"PROXY_PROVIDER": "webshare"}},
                      headers=h).json()
    iid = client.post("/identities", json={"full_name": "Template Override Test", "type": "real"},
                      headers=h).json()["id"]
    _gen_verified_subs(client, h, iid)
    client.post("/proxies", json={"provider": "soax", "kind": "rotating", "endpoint": "soax:1",
                                  "credentials_ref": "u:p", "max_sessions": 5}, headers=h)
    client.post("/proxies", json={"provider": "webshare", "kind": "rotating", "endpoint": "webshare:1",
                                  "credentials_ref": "u:p", "max_sessions": 5}, headers=h)
    bid = client.post(
        "/bookings",
        json={"name": "template-override", "type": "real", "posto_id": "5084",
              "settings": {"ENGINE_MODE": "live", "PROXY_PROVIDER": "soax"}},
        headers=h,
    ).json()["id"]
    w = client.post(f"/bookings/{bid}/identities",
                    json={"identity_id": iid, "template_id": tpl["id"]}, headers=h).json()

    import apps.hub.main as hub_main
    captured = {}
    orig_send = hub_main._bus.send_command

    def _spy(worker_id, type, **payload):
        if type == "launch":
            captured.update(payload)
        return orig_send(worker_id, type, **payload)

    monkeypatch.setattr(hub_main._bus, "send_command", _spy)

    r = client.post(f"/workers/{w['id']}/launch", headers=h)
    assert r.status_code == 200
    assert _is_minted(captured["warm_proxy"], "webshare"), (
        "launch resolved the booking's soax default instead of the template's webshare override")


def test_live_launch_leases_hold_proxy_only_when_swap_enabled(client, monkeypatch):
    h = _operate_headers(client)
    tpl = client.post(
        "/templates",
        json={"name": "Apply Swap", "role": "apply", "settings": {"HOLD_SWAP_AFTER_LOGIN": True}},
        headers=h,
    ).json()
    iid = client.post("/identities", json={"full_name": "Swap Test", "type": "real"}, headers=h).json()["id"]
    _gen_verified_subs(client, h, iid)
    client.post("/proxies", json={"provider": "soax", "kind": "static", "endpoint": "1.1.1.1:1111",
                                  "credentials_ref": "u1:p1", "max_sessions": 1}, headers=h)
    # Phase 3 (D32): the post-swap (hold) pool must resolve to a resource_type='static' proxy or
    # _do_launch_worker rejects the launch outright (no rotating fallback) -- this is the fixture
    # that makes an already-otherwise-valid HOLD_SWAP_AFTER_LOGIN configuration actually launchable.
    client.post("/proxies", json={"provider": "soax", "kind": "residential", "resource_type": "static",
                                  "endpoint": "2.2.2.2:2222", "credentials_ref": "u2:p2",
                                  "max_sessions": 1}, headers=h)
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

    assert _is_minted(captured["warm_proxy"], "soax")   # phase-1 rotating: minted, not leased
    assert captured["hold_proxy"].endswith("2.2.2.2:2222")   # phase-2 static: still a real lease
    assert captured["warm_proxy"] != captured["hold_proxy"]


def test_live_launch_swap_enabled_without_static_pool_rejects_and_releases_phase1_lease(client):
    """Phase 3 (D32): HOLD_SWAP_AFTER_LOGIN=True but the resolved post-swap pool has no active
    resource_type='static' proxy -- the launch must be rejected outright, with NO rotating
    fallback, and the phase-1 (warm) proxy lease that already succeeded must be released rather
    than left stranded (mirrors the existing ProxyLeaseError-on-hold-lease path just above, which
    already proves the release-on-failure behavior for a different failure cause)."""
    h = _operate_headers(client)
    tpl = client.post(
        "/templates",
        json={"name": "Apply Swap No Static", "role": "apply", "settings": {"HOLD_SWAP_AFTER_LOGIN": True}},
        headers=h,
    ).json()
    iid = client.post("/identities", json={"full_name": "No Static Test", "type": "real"}, headers=h).json()["id"]
    _gen_verified_subs(client, h, iid)
    # Only a rotating (default resource_type) proxy exists for provider=soax -- no static resource
    # anywhere, so the post-swap pool cannot be satisfied under the new validation.
    client.post("/proxies", json={"provider": "soax", "kind": "static", "endpoint": "4.4.4.4:4444",
                                  "credentials_ref": "u4:p4", "max_sessions": 1}, headers=h)
    bid = client.post(
        "/bookings",
        json={"name": "live-swap-no-static", "type": "real", "posto_id": "5084",
              "settings": {"ENGINE_MODE": "live", "HOLD_PROXY_PROVIDER": "soax"}},
        headers=h,
    ).json()["id"]
    w = client.post(f"/bookings/{bid}/identities",
                    json={"identity_id": iid, "template_id": tpl["id"]}, headers=h).json()

    r = client.post(f"/workers/{w['id']}/launch", headers=h)
    assert r.status_code == 409
    assert "no active static" in r.json()["detail"]
    assert "HOLD_SWAP_AFTER_LOGIN" in r.json()["detail"]

    import apps.hub.main as hub_main
    from libs.db import models as M
    db = hub_main.SessionLocal()
    try:
        # Phase-1 is ROTATING here (the booking sets no PROXY_KIND), and rotating is MINTED since
        # 2026-09-15 -- so there is no phase-1 lease to strand in the first place. The guarantee the
        # original test protected still holds, now trivially: a rejected launch must leave NOTHING
        # checked out. Any lease that did exist would still have to be released.
        leases = db.query(M.ProxyLease).filter(M.ProxyLease.worker_id == w["id"]).all()
        assert all(lease.released_at is not None for lease in leases), (
            "a lease was left stranded instead of being released on the swap rejection")
    finally:
        db.close()

    got = client.get(f"/bookings/{bid}/workers", headers=h).json()
    assert got[0]["status"] != "running"


def test_live_launch_swap_disabled_leases_no_hold_proxy(client, monkeypatch):
    h = _operate_headers(client)
    tpl = client.post("/templates", json={"name": "Apply NoSwap", "role": "apply", "settings": {}},
                      headers=h).json()   # HOLD_SWAP_AFTER_LOGIN defaults False
    iid = client.post("/identities", json={"full_name": "No Swap", "type": "real"}, headers=h).json()["id"]
    _gen_verified_subs(client, h, iid)
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
                ("status_change", {"substate": "keeping"}),
            ]:
                hub._apply_db(worker_id, events_mod.map_event_to_update(ev_type, ev_payload))
        return result

    monkeypatch.setattr(hub_main._bus, "send_command", _racing_send_command)

    r = client.post(f"/workers/{w['id']}/launch", headers=h)
    assert r.status_code == 200
    assert r.json()["status"] == "running"
    assert r.json()["substate"] == "keeping"   # NOT reset to "idle" by the launch-confirmation write


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


def test_launch_aborts_and_releases_the_lease_when_stop_lands_mid_lease(client, monkeypatch):
    """F30, CONFIRMED LIVE 2026-08-28/2026-08-31: the entry guard (previous two tests) only fires
    if the booking was ALREADY stopped before _do_launch_worker started. lease_proxy can spend up
    to ~100s in real proxy health probes -- plenty of time for /stop to land in between. Two scout
    relaunches passed the entry guard while RUNNING, then spawned real processes 25-44s after their
    booking was STOPPED mid-lease, and held forever with no way to recover (see FAILURE_MODES.md
    F30). Simulates the race by flipping the booking to STOPPED from inside lease_proxy itself."""
    import apps.hub.main as hub_main
    from libs.db import models as M
    from libs.domain.enums import BookingStatus

    h = _operate_headers(client)
    tpl = client.post("/templates", json={"name": "Race Apply", "role": "apply", "settings": {}},
                      headers=h).json()
    iid = client.post("/identities", json={"full_name": "Race Test", "type": "real"},
                      headers=h).json()["id"]
    _gen_verified_subs(client, h, iid)
    client.post("/proxies", json={"provider": "soax", "kind": "rotating", "endpoint": "soax:1",
                                  "credentials_ref": "u:p", "max_sessions": 5}, headers=h)
    bid = client.post(
        "/bookings",
        json={"name": "race-b", "type": "real", "posto_id": "5084",
              "settings": {"ENGINE_MODE": "live"}},
        headers=h,
    ).json()["id"]
    w = client.post(f"/bookings/{bid}/identities",
                    json={"identity_id": iid, "template_id": tpl["id"]}, headers=h).json()

    orig_mint = hub_main.mint_rotating_url

    def _mint_then_concurrent_stop(*args, **kwargs):
        # Hooks the MINT rather than the lease: phase-1 is rotating here, and rotating stopped
        # going through lease_proxy on 2026-09-15. The guard under test is unchanged -- after
        # acquiring a proxy by either route, _do_launch_worker must re-read the booking and abort
        # if it is no longer RUNNING.
        #
        # The real-world window for THIS path is now much smaller (minting is a string operation,
        # not ~100s of health probes), but the guard still has to hold: the static hold-lease below
        # it is still slow, and a /stop can still land during spawn.
        result = orig_mint(*args, **kwargs)
        db = hub_main.SessionLocal()
        try:
            bk = db.get(M.Booking, bid)
            bk.status = BookingStatus.STOPPED
            db.commit()
        finally:
            db.close()
        return result

    monkeypatch.setattr(hub_main, "mint_rotating_url", _mint_then_concurrent_stop)
    spawned = []
    monkeypatch.setattr(hub_main.supervisor, "spawn", lambda wid: spawned.append(wid))

    r = client.post(f"/workers/{w['id']}/launch", headers=h)
    assert r.status_code == 200               # aborts quietly, same shape as the entry guard
    assert spawned == [], "spawned a worker whose booking was stopped mid-lease"
    assert r.json()["status"] != "running"

    db = hub_main.SessionLocal()
    try:
        leases = db.query(M.ProxyLease).filter_by(worker_id=w["id"], released_at=None).all()
        assert leases == [], "proxy lease taken during the race was never released"
    finally:
        db.close()


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


# ── rotating is MINTED, not leased (operator direction 2026-09-15) ────────────────────────────
def test_a_rotating_launch_leases_nothing_and_still_ships_a_usable_proxy(client, monkeypatch):
    """The whole point of the change: a rotating pool is not a resource to be checked out. The
    launch must produce a working credential while creating ZERO proxy/lease rows -- that is what
    removes the 46-124s Hub rotation latency, the lease table's unbounded growth, and the
    42,600-row pool the dashboard could not render."""
    import apps.hub.main as hub_main
    from libs.db import models as M
    from libs.proxies.lease import roll_sticky_session, session_id

    h = _operate_headers(client)
    tpl = client.post("/templates", json={"name": "Mint", "role": "apply", "settings": {}},
                      headers=h).json()
    iid = client.post("/identities", json={"full_name": "Mint Test", "type": "real"},
                      headers=h).json()["id"]
    _gen_verified_subs(client, h, iid)
    bid = client.post("/bookings",
                      json={"name": "mint-b", "type": "real", "posto_id": "5084",
                            "settings": {"ENGINE_MODE": "live", "PROXY_PROVIDER": "soax"}},
                      headers=h).json()["id"]
    w = client.post(f"/bookings/{bid}/identities",
                    json={"identity_id": iid, "template_id": tpl["id"]}, headers=h).json()

    captured = {}
    orig = hub_main._bus.send_command
    monkeypatch.setattr(hub_main._bus, "send_command",
                        lambda wid, type, **p: (captured.update(p) if type == "launch" else None)
                        or orig(wid, type, **p))

    # NO proxy rows exist at all -- the old path would 409 with "pool exhausted".
    assert client.get("/proxies?limit=10", headers=h).json() == []
    assert client.post(f"/workers/{w['id']}/launch", headers=h).status_code == 200

    warm = captured["warm_proxy"]
    assert _is_minted(warm, "soax")
    # It must be a credential the worker can actually operate on: identifiable and rollable,
    # because self-rotation is now its ONLY way off a dead exit.
    assert session_id(warm), "minted a credential its own identifier cannot read"
    assert session_id(roll_sticky_session(warm)) != session_id(warm)

    db = hub_main.SessionLocal()
    try:
        assert db.query(M.Proxy).count() == 0, "minting must not create proxy rows"
        assert db.query(M.ProxyLease).count() == 0, "minting must not create lease rows"
    finally:
        db.close()


def test_an_unconfigured_rotating_pool_refuses_the_launch_loudly(client, monkeypatch):
    """A half-built credential authenticates as nobody and reads as a portal failure -- exactly the
    kind of misattribution that cost days on 0911. Fail at launch, naming the ENV VAR to set.

    Unsetting the environment variable is the whole point: credentials are env-only since D40, so
    this test also pins the negative -- putting SOAX_ROTATING_PASSWORD into /settings/global below
    must NOT rescue the launch. A dashboard field that could supply a proxy password is precisely
    what that decision removed."""
    import apps.hub.main as hub_main

    h = _operate_headers(client)
    monkeypatch.delenv("SOAX_ROTATING_PASSWORD", raising=False)
    client.put("/settings/global", json={"values": {"CAPSOLVER_KEYS": "k"}}, headers=h)
    tpl = client.post("/templates", json={"name": "Unconf", "role": "apply", "settings": {}},
                      headers=h).json()
    iid = client.post("/identities", json={"full_name": "Unconf Test", "type": "real"},
                      headers=h).json()["id"]
    _gen_verified_subs(client, h, iid)
    bid = client.post("/bookings",
                      json={"name": "unconf-b", "type": "real", "posto_id": "5084",
                            "settings": {"ENGINE_MODE": "live", "PROXY_PROVIDER": "soax"}},
                      headers=h).json()["id"]
    w = client.post(f"/bookings/{bid}/identities",
                    json={"identity_id": iid, "template_id": tpl["id"]}, headers=h).json()

    r = client.post(f"/workers/{w['id']}/launch", headers=h)
    assert r.status_code == 409
    assert "SOAX_ROTATING_PASSWORD" in r.json()["detail"]
    assert ".env" in r.json()["detail"], "the 409 must say WHERE to set it, not just what is missing"


# ── named ISP pools reach the LEASE, not just the settings table (2026-09-16) ────────────────
def test_the_bookings_named_pools_actually_constrain_the_launch_lease(client, monkeypatch):
    """The delivery-gap class, which is this project's dominant bug: a setting that parses, stores
    and resolves correctly but that no code on the path actually READS. PROXY_POOLS is worthless
    unless it reaches lease_proxy, so this asserts the argument arrives rather than asserting the
    setting round-trips.

    Also the negative that makes the feature safe: a pool the booking did NOT name must not be
    leased. An ISP proxy is a finite, individually-owned IP, so handing one out from another
    booking's purchase is a real conflict, not untidy accounting."""
    h = _operate_headers(client)
    tpl = client.post("/templates", json={"name": "Pooled", "role": "apply", "settings": {}},
                      headers=h).json()
    iid = client.post("/identities", json={"full_name": "Pool Test", "type": "real"},
                      headers=h).json()["id"]
    _gen_verified_subs(client, h, iid)

    # Two named pools; only one is assigned to the booking.
    for name, ep in (("thordata-oct", "10.0.0.1:8000"), ("someone-elses", "10.0.0.2:8000")):
        client.post("/proxies", json={"provider": "thordata", "kind": "static",
                                      "endpoint": ep, "credentials_ref": "u:p",
                                      "max_sessions": 1, "resource_type": "static",
                                      "pool_name": name}, headers=h)

    bid = client.post("/bookings",
                      json={"name": "pooled-b", "type": "real", "posto_id": "5084",
                            "settings": {"ENGINE_MODE": "live", "PROXY_PROVIDER": "thordata",
                                         "PROXY_KIND": "static",
                                         "PROXY_POOLS": "thordata-oct"}}, headers=h).json()["id"]
    w = client.post(f"/bookings/{bid}/identities",
                    json={"identity_id": iid, "template_id": tpl["id"]}, headers=h).json()

    import apps.hub.main as hub_main
    monkeypatch.setattr(hub_main, "check_proxy", lambda url: (True, "ok"))

    seen = {}
    real_lease = hub_main.lease_proxy

    def _spy(db, worker_id, provider, **kw):
        seen.update(kw)
        return real_lease(db, worker_id, provider, **kw)

    monkeypatch.setattr(hub_main, "lease_proxy", _spy)

    r = client.post(f"/workers/{w['id']}/launch", headers=h)
    assert r.status_code == 200, r.text
    assert seen.get("pool_names") == {"thordata-oct"}, (
        f"PROXY_POOLS never reached lease_proxy: got {seen.get('pool_names')!r}")

    leased = client.get(f"/proxies?limit=10", headers=h).json()
    held = [p for p in leased if p["endpoint"] == "10.0.0.2:8000"]
    assert held and held[0].get("pool_name") == "someone-elses"


# ── ACCOUNT_GEO_PIN: a sub-account runs from the country its registration succeeded from ────────────
def _set_registered_geo(client, identity_id, geo, fp="abc123"):
    from libs.db import models as M
    from sqlalchemy import select
    db = client._Session()
    try:
        for sa in db.scalars(select(M.SubAccount).where(M.SubAccount.identity_id == identity_id)).all():
            sa.registered_geo, sa.fingerprint_hash = geo, fp
        db.commit()
    finally:
        db.close()


def _geo_booking(client, h, name, settings=None):
    tpl = client.post("/templates", json={"name": name, "role": "apply", "settings": {}}, headers=h).json()
    iid = client.post("/identities", json={"full_name": name, "type": "real"}, headers=h).json()["id"]
    _gen_verified_subs(client, h, iid)
    bid = client.post("/bookings", json={
        "name": name, "type": "real", "posto_id": "5084",
        "settings": {"ENGINE_MODE": "live", "PROXY_PROVIDER": "soax", "PROXY_GEOS": "PT,IT", **(settings or {})}},
        headers=h).json()["id"]
    w = client.post(f"/bookings/{bid}/identities", json={"identity_id": iid, "template_id": tpl["id"]},
                    headers=h).json()
    return iid, bid, w


def test_a_registered_account_launches_from_its_registration_country(client, monkeypatch):
    h = _operate_headers(client)
    iid, _bid, w = _geo_booking(client, h, "AcctGeo")
    _set_registered_geo(client, iid, "DE")
    captured = _spy_launch(monkeypatch)
    assert client.post(f"/workers/{w['id']}/launch", headers=h).status_code == 200
    assert "-country-de-" in captured["warm_proxy"], captured["warm_proxy"]
    assert captured["proxy_geos"] == ["DE"], "the set is the account's country, not the booking's PT,IT"
    assert captured["proxy_geo_operator_pinned"] is False
    acct = captured["account"]
    assert acct["registered_geo"] == "DE" and acct["fingerprint_hash"] == "abc123"
    assert all(a["registered_geo"] == "DE" for a in captured["accounts"])


def test_an_account_with_no_recorded_country_keeps_the_booking_geos(client, monkeypatch):
    h = _operate_headers(client)
    _iid, _bid, w = _geo_booking(client, h, "NoGeo")
    captured = _spy_launch(monkeypatch)
    assert client.post(f"/workers/{w['id']}/launch", headers=h).status_code == 200
    assert captured["proxy_geos"] == ["IT", "PT"]
    assert captured["account"]["registered_geo"] is None and captured["account"]["fingerprint_hash"] is None


def test_the_operators_worker_pin_outranks_the_account_country(client, monkeypatch):
    h = _operate_headers(client)
    iid, _bid, w = _geo_booking(client, h, "OpPin")
    _set_registered_geo(client, iid, "DE")
    assert client.patch(f"/workers/{w['id']}/geo", json={"geo": "es"}, headers=h).status_code == 200
    captured = _spy_launch(monkeypatch)
    assert client.post(f"/workers/{w['id']}/launch", headers=h).status_code == 200
    assert "-country-es-" in captured["warm_proxy"]
    assert captured["proxy_geo_operator_pinned"] is True


def test_account_geo_pin_can_be_switched_off_per_booking(client, monkeypatch):
    h = _operate_headers(client)
    iid, _bid, w = _geo_booking(client, h, "PinOff", {"ACCOUNT_GEO_PIN": False})
    _set_registered_geo(client, iid, "DE")
    captured = _spy_launch(monkeypatch)
    assert client.post(f"/workers/{w['id']}/launch", headers=h).status_code == 200
    assert captured["proxy_geos"] == ["IT", "PT"]


def test_account_geo_pin_helper_ignores_junk():
    import apps.hub.main as hub_main
    from types import SimpleNamespace as NS
    assert hub_main._account_geo_pin({}, NS(registered_geo="de")) == "DE"
    assert hub_main._account_geo_pin({"ACCOUNT_GEO_PIN": False}, NS(registered_geo="DE")) is None
    assert hub_main._account_geo_pin({}, NS(registered_geo=None)) is None
    assert hub_main._account_geo_pin({}, NS(registered_geo="D3")) is None
    assert hub_main._account_geo_pin({}, None) is None


# ── solver keys: the worker gets EVERY key of the chosen solver (rotated), not one frozen at launch ──
def test_a_worker_launch_carries_every_key_of_the_chosen_solver_rotated_by_worker(client, monkeypatch):
    from libs.domain.solver_select import rr_index
    h = _operate_headers(client)
    assert client.put("/settings/global", json={"values": {
        "CAPSOLVER_KEYS": "c1,c2", "ANTICAPTCHA_KEYS": "a1,a2,a3", "RECAPTCHA_SOLVER": "anticaptcha"}},
        headers=h).status_code == 200
    _iid, _bid, w = _geo_booking(client, h, "Keys")
    captured = _spy_launch(monkeypatch)
    assert client.post(f"/workers/{w['id']}/launch", headers=h).status_code == 200
    start = rr_index(w["id"]) % 3
    expect = ["a1", "a2", "a3"][start:] + ["a1", "a2", "a3"][:start]
    assert captured["anticaptcha_keys"] == expect, "all three keys, this worker's own pick first"
    assert captured["capsolver_keys"] == [], "the other solver is not handed over when it is not selected"
    assert captured["schedule_anticaptcha_keys"] == expect

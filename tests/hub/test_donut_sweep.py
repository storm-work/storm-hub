"""Orphaned Donut profiles are stopped at Hub startup -- and only the orphaned ones.

A Donut profile outlives the worker that launched it. After booking "Test 2032 Donut" the three apply
workers exited cleanly and their three Wayfern profiles ran on for hours. A sweep that kills a LIVE
worker's profile is worse than the leak, so most of these tests pin what must NOT be stopped."""
import subprocess
import sys
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import apps.hub.donut_sweep as sweep
import libs.db.models as M
from apps.hub.supervisor import live_keys
from libs.db import metadata
from libs.domain.enums import (BookingStatus, RegistrationStatus, WorkerRole, WorkerStatus)

NOW = 1_000_000.0
OLD = NOW - 3600
FRESH = NOW - 60


def _p(name, *, running=True, launched=OLD, pid="id"):
    return {"id": f"{pid}-{name}", "name": name, "is_running": running, "last_launch": launched}


# ── the pure plan ────────────────────────────────────────────────────────────────────────────────
def test_an_old_running_unowned_storm_profile_is_stopped():
    out = sweep.plan_sweep([_p("storm-aaa")], set(), NOW)
    assert [p["name"] for p in out] == ["storm-aaa"]


def test_an_owned_profile_is_never_stopped():
    assert sweep.plan_sweep([_p("storm-aaa")], {"storm-aaa"}, NOW) == []


def test_a_recent_launch_is_left_alone():
    assert sweep.plan_sweep([_p("storm-aaa", launched=FRESH)], set(), NOW) == []


def test_profiles_the_bot_does_not_own_or_that_are_not_running_are_left_alone():
    profiles = [_p("my-personal-profile"), _p("storm-stopped", running=False)]
    assert sweep.plan_sweep(profiles, set(), NOW) == []


# ── ownership from the DB + /proc ────────────────────────────────────────────────────────────────
@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield s
    s.close()


def _name(u):
    return "storm-" + u


def _world(db):
    bk = M.Booking(name="b", type="test", posto_id="2032", status=BookingStatus.RUNNING)
    live_i, dead_i, reg_i = (M.Identity(full_name=n, type="test") for n in ("LIVE", "DEAD", "REG"))
    db.add_all([bk, live_i, dead_i, reg_i]); db.commit()
    subs = {}
    for u, ident in (("live1", live_i), ("live2", live_i), ("dead1", dead_i), ("reg1", reg_i)):
        subs[u] = M.SubAccount(identity_id=ident.id, username=u, password="x")
    db.add_all(subs.values()); db.commit()
    live_w = M.Worker(booking_id=bk.id, identity_id=live_i.id, role=WorkerRole.APPLY,
                      status=WorkerStatus.RUNNING, sub_account_id=subs["live1"].id)
    dead_w = M.Worker(booking_id=bk.id, identity_id=dead_i.id, role=WorkerRole.APPLY,
                      status=WorkerStatus.COMPLETED, sub_account_id=subs["dead1"].id)
    db.add_all([live_w, dead_w]); db.commit()
    db.add(M.RegistrationJob(sub_account_id=subs["reg1"].id, status=RegistrationStatus.RUNNING))
    db.commit()
    return live_w, dead_w


def test_a_live_workers_whole_identity_is_protected_not_just_its_current_account(db, monkeypatch):
    live_w, _ = _world(db)
    monkeypatch.setattr(sweep, "live_keys", lambda module: {live_w.id} if module == "apps.worker" else set())
    names = sweep.protected_profile_names(db, _name)
    assert names == {"storm-live1", "storm-live2"}, (
        "a worker rotates through its identity's sub-accounts; protecting only the current one "
        "would kill the profile it moves to next")


def test_a_finished_workers_profile_is_not_protected(db, monkeypatch):
    live_w, dead_w = _world(db)
    monkeypatch.setattr(sweep, "live_keys", lambda module: {live_w.id} if module == "apps.worker" else set())
    assert "storm-dead1" not in sweep.protected_profile_names(db, _name)


def test_registration_accounts_are_protected_only_while_a_registrar_is_alive(db, monkeypatch):
    _world(db)
    monkeypatch.setattr(sweep, "live_keys", lambda module: {"run1"} if module == "apps.registrar" else set())
    assert "storm-reg1" in sweep.protected_profile_names(db, _name)
    monkeypatch.setattr(sweep, "live_keys", lambda module: set())
    assert "storm-reg1" not in sweep.protected_profile_names(db, _name)


# ── the whole sweep against a fake Donut API ─────────────────────────────────────────────────────
class _FakeApi:
    calls: list = []
    profiles: list = []

    def __init__(self, url=None, token=None):
        assert token == "tok", "the sweep must use the token from the Global settings"

    def call(self, method, path, body=None, timeout=30.0):
        _FakeApi.calls.append((method, path))
        return {"profiles": _FakeApi.profiles} if method == "GET" else None


def _set_global(db, **kv):
    for k, v in kv.items():
        db.add(M.SettingRow(layer="GLOBAL", scope_key="", key=k, value=v))
    db.commit()


@pytest.fixture()
def fake_donut(monkeypatch):
    import libs.engine.vendor  # noqa: F401
    import donut
    _FakeApi.calls = []
    monkeypatch.setattr(donut, "_Api", _FakeApi)
    return _FakeApi


def test_the_sweep_stops_orphans_and_spares_a_live_workers_profile(db, fake_donut, monkeypatch):
    live_w, _ = _world(db)
    _set_global(db, BROWSER_BACKEND="donut", DONUT_API_TOKEN="tok")
    monkeypatch.setattr(sweep, "live_keys", lambda module: {live_w.id} if module == "apps.worker" else set())
    import donut
    fake_donut.profiles = [
        {"id": "a", "name": donut.profile_name("live1"), "is_running": True, "last_launch": OLD},
        {"id": "b", "name": donut.profile_name("dead1"), "is_running": True, "last_launch": OLD},
    ]
    stopped = sweep.sweep_donut_profiles(db, now=NOW)
    assert stopped == [donut.profile_name("dead1")]
    assert ("POST", "/v1/profiles/b/kill") in fake_donut.calls
    assert ("POST", "/v1/profiles/a/kill") not in fake_donut.calls


def test_a_dry_run_stops_nothing(db, fake_donut, monkeypatch):
    _world(db)
    _set_global(db, BROWSER_BACKEND="donut", DONUT_API_TOKEN="tok")
    monkeypatch.setattr(sweep, "live_keys", lambda module: set())
    fake_donut.profiles = [{"id": "b", "name": "storm-zzz", "is_running": True, "last_launch": OLD}]
    assert sweep.sweep_donut_profiles(db, dry_run=True, now=NOW) == ["storm-zzz"]
    assert [c for c in fake_donut.calls if c[0] == "POST"] == []


def test_the_sweep_does_nothing_unless_the_donut_backend_is_selected(db, fake_donut):
    _set_global(db, DONUT_API_TOKEN="tok")          # BROWSER_BACKEND unset -> chromium
    assert sweep.sweep_donut_profiles(db, now=NOW) == []
    assert fake_donut.calls == []


def test_an_unreachable_donut_never_breaks_boot(db, monkeypatch):
    _set_global(db, BROWSER_BACKEND="donut", DONUT_API_TOKEN="tok")
    import libs.engine.vendor  # noqa: F401
    import donut

    class _Down:
        def __init__(self, *a, **k):
            pass

        def call(self, *a, **k):
            raise donut.DonutError("Donut API unreachable")

    monkeypatch.setattr(donut, "_Api", _Down)
    assert sweep.sweep_donut_profiles(db, now=NOW) == []


def test_live_keys_finds_a_real_foreign_process():
    proc = subprocess.Popen([sys.executable, "-m", "http.server", "18291"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        time.sleep(0.3)
        assert "18291" in live_keys("http.server")
        assert "18291" not in live_keys("apps.worker")
    finally:
        proc.terminate()
        proc.wait(timeout=5)

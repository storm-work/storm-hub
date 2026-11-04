"""Donut profile operator tools (2026-11-03): create profiles for verified sub-accounts, and the
status reconciliation (matched / junk / missing). In-memory SQLite + fakeredis + a fake Donut API so
nothing touches a real Donut app. The create path must honour the hourly budget and backfill the row;
the status path must classify every storm-* profile the same way the sweep does."""
import fakeredis
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import apps.hub.donut_profiles as DP
from libs.db import metadata
from libs.db import models as M
from libs.domain.enums import SubAccountStatus
from libs.engine.vendor import donut

G = {"BROWSER_BACKEND": "donut", "DONUT_API_URL": "http://x", "DONUT_API_TOKEN": "t",
     "DONUT_PROFILES_PER_HOUR": "100"}


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield s
    s.close()


class FakeApi:
    """A stand-in for donut._Api backed by an in-memory profile list. One instance is shared across
    every donut._Api(...) call in a test via the monkeypatched factory, so creates are visible to the
    listing that follows."""
    def __init__(self, profiles=None, rate_limited=False, racer=False, fail=False):
        self.profiles = list(profiles or [])
        self.rate_limited = rate_limited
        self.racer = racer          # a concurrent creator (registrar/worker) makes the same name too
        self.fail = fail
        self.posts = 0
        self._n = 0

    def _add(self, name):
        self._n += 1
        pid = f"id{self._n}"
        self.profiles.append({"id": pid, "name": name, "is_running": False, "last_launch": 0})
        return pid

    def call(self, method, path, body=None, timeout=30.0):
        if method == "GET" and path == "/v1/profiles":
            return {"profiles": list(self.profiles)}
        if method == "POST" and path == "/v1/profiles":
            if self.rate_limited:
                raise donut.DonutRateLimited("429", 300.0)
            if self.fail:
                raise donut.DonutError("boom")
            self.posts += 1
            if self.racer:
                self._add(body["name"])
            return {"profile": {"id": self._add(body["name"])}}
        if method == "DELETE" and path.startswith("/v1/profiles/"):
            pid = path.rsplit("/", 1)[1]
            self.profiles = [p for p in self.profiles if p["id"] != pid]
            return None
        return {}


@pytest.fixture(autouse=True)
def _reset_cache():
    DP._cache = None          # existing_profile_names caches 60s; each test starts clean
    yield
    DP._cache = None


def _patch(monkeypatch, api):
    monkeypatch.setattr(donut, "_Api", lambda *a, **k: api)


def _ident(db, status=SubAccountStatus.VERIFIED, **kw):
    i = M.Identity(full_name="P P", type="real")
    db.add(i)
    db.commit()
    s = M.SubAccount(identity_id=i.id, username=kw.get("username", f"u{i.id[:6]}"),
                     password="x", status=status)
    db.add(s)
    db.commit()
    return i, s


# ── create_profiles ─────────────────────────────────────────────────────────────
def test_create_makes_missing_and_backfills_row(db, monkeypatch):
    api = FakeApi()
    _patch(monkeypatch, api)
    i, s = _ident(db)
    r = DP.create_profiles(db, G, [s.username], redis=fakeredis.FakeStrictRedis(decode_responses=True))
    assert r["created"] == [s.username] and r["existed"] == [] and r["errors"] == []
    db.refresh(s)
    assert s.donut_profile == donut.profile_name(s.username)
    assert (s.browser_backend or "").lower() == "donut"
    assert any(p["name"] == donut.profile_name(s.username) for p in api.profiles)


def test_create_skips_existing_and_backfills(db, monkeypatch):
    i, s = _ident(db)
    api = FakeApi(profiles=[{"id": "p", "name": donut.profile_name(s.username), "is_running": False}])
    _patch(monkeypatch, api)
    r = DP.create_profiles(db, G, [s.username], redis=fakeredis.FakeStrictRedis(decode_responses=True))
    assert r["existed"] == [s.username] and r["created"] == []
    db.refresh(s)
    assert s.donut_profile == donut.profile_name(s.username)   # backfilled even though not created


def test_create_respects_hourly_budget(db, monkeypatch):
    api = FakeApi()
    _patch(monkeypatch, api)
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    idents = [_ident(db, username=f"user{n}") for n in range(3)]
    usernames = [s.username for _, s in idents]
    g = {**G, "DONUT_PROFILES_PER_HOUR": "2"}
    out = DP.create_profiles(db, g, usernames, redis=r)
    assert len(out["created"]) == 2 and len(out["deferred"]) == 1
    assert r.zcard(DP._BUDGET_KEY) == 2                        # only the created ones took a slot


def test_create_rejects_non_donut_backend(db, monkeypatch):
    i, s = _ident(db)
    out = DP.create_profiles(db, {**G, "BROWSER_BACKEND": "chromium"}, [s.username])
    assert out["created"] == [] and out["errors"]


def test_create_rate_limited_defers_rest(db, monkeypatch):
    api = FakeApi(rate_limited=True)
    _patch(monkeypatch, api)
    idents = [_ident(db, username=f"rl{n}") for n in range(2)]
    out = DP.create_profiles(db, G, [s.username for _, s in idents],
                             redis=fakeredis.FakeStrictRedis(decode_responses=True))
    assert out["created"] == [] and len(out["deferred"]) == 2


def test_create_twice_never_double_creates(db, monkeypatch):
    """Regression: the create decision used the 60s existing_profile_names cache, so a second click
    within a minute re-POSTed every profile -- duplicates plus double-spent hourly quota."""
    api = FakeApi()
    _patch(monkeypatch, api)
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    _, s = _ident(db)
    DP.existing_profile_names(G)                       # warm the shared cache with the EMPTY list
    first = DP.create_profiles(db, G, [s.username], redis=r)
    second = DP.create_profiles(db, G, [s.username], redis=r)
    assert first["created"] == [s.username]
    assert second["created"] == [] and second["existed"] == [s.username]
    assert len(api.profiles) == 1 and r.zcard(DP._BUDGET_KEY) == 1


def test_create_invalidates_shared_cache(db, monkeypatch):
    """Regression: after creating, keep_with_profile must see the new profile at once, not filter the
    just-enabled account out for up to 60s from a stale cached list."""
    api = FakeApi()
    _patch(monkeypatch, api)
    _, s = _ident(db)
    assert donut.profile_name(s.username) not in DP.existing_profile_names(G)   # cached: absent
    DP.create_profiles(db, G, [s.username], redis=fakeredis.FakeStrictRedis(decode_responses=True))
    assert donut.profile_name(s.username) in DP.existing_profile_names(G)


# ── dedup guard ─────────────────────────────────────────────────────────────────
def test_claim_held_by_another_request_skips_as_busy(db, monkeypatch):
    """Two overlapping creates: the second must not POST a second profile with the same name."""
    api = FakeApi()
    _patch(monkeypatch, api)
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    _, s = _ident(db)
    r.set(DP._CLAIM_PREFIX + donut.profile_name(s.username), "1")      # the other request's claim
    out = DP.create_profiles(db, G, [s.username], redis=r)
    assert out["busy"] == [s.username] and out["created"] == [] and api.posts == 0


def test_successful_create_keeps_its_claim_failed_one_releases_it(db, monkeypatch):
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    _, ok = _ident(db, username="okuser")
    _patch(monkeypatch, FakeApi())
    DP.create_profiles(db, G, ["okuser"], redis=r)
    assert r.exists(DP._CLAIM_PREFIX + donut.profile_name("okuser"))       # "just created" marker
    _, bad = _ident(db, username="baduser")
    _patch(monkeypatch, FakeApi(fail=True))
    DP._cache = None
    out = DP.create_profiles(db, G, ["baduser"], redis=r)
    assert out["errors"] and not r.exists(DP._CLAIM_PREFIX + donut.profile_name("baduser"))


def test_post_create_check_removes_a_racers_copy(db, monkeypatch):
    """A creator that does not take the claim (registrar/worker first launch) made the same name in
    the gap: the batch's post-create check leaves exactly one copy."""
    api = FakeApi(racer=True)
    _patch(monkeypatch, api)
    _, s = _ident(db)
    out = DP.create_profiles(db, G, [s.username], redis=fakeredis.FakeStrictRedis(decode_responses=True))
    name = donut.profile_name(s.username)
    assert out["created"] == [s.username] and out["duplicates_removed"] == [name]
    assert [p["name"] for p in api.profiles] == [name]


def test_status_counts_duplicates_including_running(db, monkeypatch):
    _patch(monkeypatch, FakeApi(profiles=[
        {"id": "1", "name": "storm-aaa", "is_running": True},
        {"id": "2", "name": "storm-aaa", "is_running": True},
        {"id": "3", "name": "storm-aaa", "is_running": False},
        {"id": "4", "name": "storm-bbb", "is_running": False},
    ]))
    _ident(db)
    st = DP.profile_status(db, G)
    assert st["duplicates"] == 2 and st["duplicate_names"] == ["storm-aaa"]


# ── profile_status ──────────────────────────────────────────────────────────────
def test_status_classifies_matched_junk_and_missing(db, monkeypatch):
    i_ok, s_ok = _ident(db, status=SubAccountStatus.VERIFIED, username="matched1")
    i_dead, s_dead = _ident(db, status=SubAccountStatus.BURNED, username="finished1")
    i_pending, s_pending = _ident(db, status=SubAccountStatus.NEW, username="abandoned1")
    _, s_missing = _ident(db, status=SubAccountStatus.VERIFIED, username="missing1")  # verified, no profile

    api = FakeApi(profiles=[
        {"id": "1", "name": donut.profile_name("matched1"), "is_running": True},
        {"id": "2", "name": donut.profile_name("finished1"), "is_running": False},
        {"id": "3", "name": donut.profile_name("abandoned1"), "is_running": False},
        {"id": "4", "name": "storm-orphanXYZ", "is_running": False},   # no sub-account
        {"id": "5", "name": "operator-manual", "is_running": False},   # not ours -> ignored
    ])
    _patch(monkeypatch, api)

    st = DP.profile_status(db, G)
    assert st["reachable"] is True
    assert st["profiles_total"] == 4            # the non-storm one is ignored
    assert st["running"] == 1
    assert st["matched"] == 1
    assert st["junk_total"] == 3
    assert st["junk"] == {"orphan": 1, "finished": 1, "abandoned": 1}
    assert st["missing_total"] == 1 and st["missing_sample"] == [donut.profile_name("missing1")]


def test_status_not_donut_backend(db):
    st = DP.profile_status(db, {**G, "BROWSER_BACKEND": "chromium"})
    assert st["reachable"] is False and "not donut" in st["note"]

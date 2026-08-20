from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from libs.db import metadata
from libs.db import models as M
from libs.proxies import ProxyLeaseError, conn_url, lease_proxy, release_proxy, release_worker_resources


@pytest.fixture()
def db():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(e)
    s = sessionmaker(bind=e)()
    yield s
    s.close()


def _proxy(db, *, endpoint, provider="soax", kind="datacenter", max_sessions=1):
    """One pool proxy. Used by the cursor tests below, which care about ORDER rather than identity."""
    p = M.Proxy(provider=provider, kind=kind, endpoint=endpoint,
                credentials_ref="u:p", max_sessions=max_sessions)
    db.add(p); db.commit()
    return p


def _worker(db, wid="w1"):
    bk = M.Booking(id="bk1", name="b", type="test", posto_id="2032")
    idn = M.Identity(id="id1", full_name="X", type="test")
    db.add_all([bk, idn]); db.flush()
    w = M.Worker(id=wid, booking_id="bk1", identity_id="id1", role="apply")
    db.add(w); db.commit()
    return w


def test_conn_url_forms():
    assert conn_url(M.Proxy(provider="isp", kind="static_res", endpoint="1.2.3.4:5000",
                            credentials_ref="u:p")) == "http://u:p@1.2.3.4:5000"
    assert conn_url(M.Proxy(provider="soax", kind="dc", endpoint="h:1",
                            credentials_ref=None)) == "http://h:1"


def test_lease_picks_provider_and_respects_capacity(db):
    _worker(db)
    db.add(M.Proxy(id="p_soax", provider="soax", kind="datacenter", endpoint="proxy.soax.com:5000",
                   credentials_ref="sess1:x", max_sessions=1, score=100))
    db.add(M.Proxy(id="p_isp", provider="isp", kind="static_res", endpoint="8.8.8.8:5000",
                   credentials_ref="u:p", max_sessions=2, score=50))
    db.commit()

    # warm phase -> SOAX
    px, url = lease_proxy(db, "w1", "soax")
    assert px.provider == "soax" and url == "http://sess1:x@proxy.soax.com:5000"
    # SOAX max_sessions=1 -> now exhausted
    with pytest.raises(ProxyLeaseError):
        lease_proxy(db, "w1", "soax")

    # hold phase -> ISP (2 per IP)
    px2, _ = lease_proxy(db, "w1", "isp")
    assert px2.provider == "isp"
    lease_proxy(db, "w1", "isp")                 # second lease on the same ISP (cap 2) ok
    with pytest.raises(ProxyLeaseError):
        lease_proxy(db, "w1", "isp")             # third exceeds cap


def test_release_frees_capacity(db):
    _worker(db)
    db.add(M.Proxy(id="p", provider="soax", kind="dc", endpoint="h:1", credentials_ref="s:x",
                   max_sessions=1, score=1))
    db.commit()
    lease_proxy(db, "w1", "soax")
    with pytest.raises(ProxyLeaseError):
        lease_proxy(db, "w1", "soax")
    assert release_proxy(db, "w1") == 1
    px, _ = lease_proxy(db, "w1", "soax")        # freed -> leasable again
    assert px.provider == "soax"


def test_release_worker_resources_frees_proxy_and_slot_lease(db):
    """Regression (P1): before apps/hub/events.py's terminal handler called this, a worker that
    ended NORMALLY (booked/discovered/dry_run/warmed/failed/stopped) leaked its proxy lease and slot
    lease forever -- only the scheduler's dead/stall/runaway reap paths ever released them."""
    w = _worker(db)
    db.add(M.Proxy(id="p", provider="soax", kind="dc", endpoint="h:1", credentials_ref="s:x",
                   max_sessions=1, score=1))
    slot = M.Slot(id="s1", booking_id=w.booking_id, posto_id="2032", date="2026-08-20", period=4,
                  status="leased")
    db.add(slot)
    db.commit()
    lease_proxy(db, "w1", "soax")
    db.add(M.SlotLease(id="sl1", slot_id="s1", worker_id="w1"))
    db.commit()

    release_worker_resources(db, "w1")

    assert db.get(M.ProxyLease, db.query(M.ProxyLease).first().id).released_at is not None
    assert db.get(M.SlotLease, "sl1").released_at is not None
    assert db.get(M.Slot, "s1").status == "available"
    # freed -> leasable again, proving the release actually took effect
    px, _ = lease_proxy(db, "w2", "soax")
    assert px.id == "p"


def test_inactive_is_skipped_but_unhealthy_is_not(db):
    """CHANGED 2026-08-19: this asserted that an unhealthy proxy is skipped too. On a ROTATING pool
    that is wrong -- a row is a session template, not an IP, so a probe failure describes a
    transient exit and excluding the row throws away a good template. `active` is an OPERATOR
    decision ("stop using this") and still excludes; is_healthy is a MEASUREMENT and no longer
    does."""
    _worker(db)
    db.add(M.Proxy(id="dead", provider="isp", kind="s", endpoint="h:1", credentials_ref="u:p",
                   max_sessions=2, is_healthy=False, score=100))
    db.add(M.Proxy(id="off", provider="isp", kind="s", endpoint="h:2", credentials_ref="u:p",
                   max_sessions=2, active=False, score=100))
    db.commit()

    got, _ = lease_proxy(db, "w1", "isp")
    assert got.id == "dead", "an unhealthy row must still be leasable on a rotating pool"

    # ...and with only the deactivated one left, the pool really is exhausted.
    db.query(M.Proxy).filter(M.Proxy.id == "dead").delete()
    db.commit()
    with pytest.raises(ProxyLeaseError):
        lease_proxy(db, "w2", "isp")


# ── P4: live pre-lease screening -- an operator-specified design (2026-08-13): "select a proxy node
# ... check healthy against the real portal ... if unhealthy give it up and select another, until
# healthy" ───────────────────────────────────────────────────────────────────────────────────────
def test_health_check_none_preserves_old_behavior(db):
    """Default (health_check=None) must be byte-for-byte the pre-P4 DB-only path -- every other test
    in this file relies on that."""
    _worker(db)
    db.add(M.Proxy(id="p", provider="soax", kind="dc", endpoint="h:1", credentials_ref="s:x",
                   max_sessions=1, score=1))
    db.commit()
    px, url = lease_proxy(db, "w1", "soax")
    assert px.id == "p" and url == "http://s:x@h:1"


def test_health_check_skips_unhealthy_candidate_and_leases_next(db):
    _worker(db)
    db.add(M.Proxy(id="bad", provider="isp", kind="s", endpoint="h:1", credentials_ref="u:p",
                   max_sessions=2, score=100))   # sorted first by score
    db.add(M.Proxy(id="good", provider="isp", kind="s", endpoint="h:2", credentials_ref="u:p",
                   max_sessions=2, score=50))
    db.commit()

    calls = []

    def fake_check(url):
        calls.append(url)
        return (False, "TimeoutError: nav timed out") if url.endswith("h:1") else (True, "ok")

    px, url = lease_proxy(db, "w1", "isp", health_check=fake_check)
    assert px.id == "good" and url.endswith("h:2")
    assert calls == ["http://u:p@h:1", "http://u:p@h:2"]   # tried the higher-score dead one first

    # the failed candidate's health bookkeeping was updated even though it wasn't leased
    bad = db.get(M.Proxy, "bad")
    assert bad.is_healthy is False
    assert bad.health == "TimeoutError: nav timed out"   # full detail now, see cc000healthdetail
    assert bad.score == 100 - 25.0   # SCORE_STEP_DOWN

    good = db.get(M.Proxy, "good")
    assert good.is_healthy is True
    assert good.score == 50 + 10.0   # SCORE_STEP_UP


def test_health_check_all_candidates_dead_raises_and_still_persists_results(db):
    _worker(db)
    db.add(M.Proxy(id="p1", provider="isp", kind="s", endpoint="h:1", credentials_ref="u:p",
                   max_sessions=2, score=10))
    db.add(M.Proxy(id="p2", provider="isp", kind="s", endpoint="h:2", credentials_ref="u:p",
                   max_sessions=2, score=5))
    db.commit()

    # The message now separates a dead pool from losing claim races to a concurrent leaser --
    # "no healthy proxy" for a contention failure sent a live investigation looking at health
    # while the DB showed every named candidate healthy.
    with pytest.raises(ProxyLeaseError, match=r"no isp proxy leased after \d+ probe"):
        lease_proxy(db, "w1", "isp", health_check=lambda url: (False, "dead"))

    assert db.get(M.Proxy, "p1").is_healthy is False
    assert db.get(M.Proxy, "p2").is_healthy is False


def test_health_check_bounded_by_max_probes(db):
    _worker(db)
    for i in range(5):
        db.add(M.Proxy(id=f"p{i}", provider="isp", kind="s", endpoint=f"h:{i}",
                       credentials_ref="u:p", max_sessions=2, score=float(5 - i)))
    db.commit()

    calls = []
    with pytest.raises(ProxyLeaseError):
        lease_proxy(db, "w1", "isp", health_check=lambda url: calls.append(url) or (False, "dead"),
                   max_probes=2)
    assert len(calls) == 2   # never probed p2/p3/p4


def test_health_check_does_not_affect_a_different_kind_pool(db):
    """A pre-existing lease/health state in one provider+kind pool must not leak into another --
    same isolation the exclude_ids/old-lease logic in events.py's rotation handler relies on."""
    _worker(db)
    db.add(M.Proxy(id="rot", provider="soax", kind="rotating", endpoint="h:1",
                   credentials_ref="u:p", max_sessions=2, score=10))
    db.add(M.Proxy(id="stat", provider="soax", kind="static", endpoint="h:2",
                   credentials_ref="u:p", max_sessions=2, score=10))
    db.commit()

    px, _ = lease_proxy(db, "w1", "soax", kind="static", health_check=lambda url: (True, "ok"))
    assert px.id == "stat"
    assert db.get(M.Proxy, "rot").health is None   # never probed -- different kind


# ── the pool cursor (operator request 2026-08-16) ─────────────────────────────────────────────
def test_leases_walk_the_whole_pool_before_repeating_any_proxy(db):
    """_candidate_query was (score DESC, created_at ASC): fully deterministic, so every lease
    started from the SAME point and the first few proxies were used forever while the rest of the
    pool sat idle. LRU ordering IS the cursor -- each lease resumes where the last one left off."""
    from libs.proxies.lease import lease_proxy, release_proxy

    made = [_proxy(db, endpoint=f"p{i}:80", max_sessions=1) for i in range(5)]
    seen = []
    for i in range(5):
        p, _ = lease_proxy(db, f"w{i}", "soax")
        seen.append(p.endpoint)
        release_proxy(db, f"w{i}")

    assert sorted(seen) == sorted(p.endpoint for p in made), seen
    assert len(set(seen)) == 5, f"repeated a proxy before exhausting the pool: {seen}"


def test_the_cursor_survives_release_so_the_next_lease_resumes_not_restarts(db):
    """Releasing a lease must not send the cursor back to the start -- that is exactly the
    'begins from the first point every time' behaviour being fixed."""
    from libs.proxies.lease import lease_proxy, release_proxy

    for i in range(3):
        _proxy(db, endpoint=f"q{i}:80", max_sessions=1)

    first, _ = lease_proxy(db, "w1", "soax")
    release_proxy(db, "w1")
    second, _ = lease_proxy(db, "w2", "soax")

    assert second.endpoint != first.endpoint, "restarted from the head of the pool after a release"


def test_a_failed_probe_rotates_that_proxy_to_the_back(db):
    """A candidate that fails its probe must not stay at the head of the queue. It used to, which
    is why 'no healthy proxy after probing 5 candidate(s)' kept naming the identical five UUIDs."""
    from libs.proxies.lease import ProxyLeaseError, lease_proxy

    for i in range(6):
        _proxy(db, endpoint=f"r{i}:80", max_sessions=1)

    tried_first, tried_second = [], []
    for bucket in (tried_first, tried_second):
        try:
            lease_proxy(db, "w", "soax", max_probes=3,
                        health_check=lambda url, b=bucket: (b.append(url), (False, "dead"))[1])
        except ProxyLeaseError:
            pass

    assert tried_first and tried_second
    assert not (set(tried_first) & set(tried_second)), (
        f"probed the same dead proxies twice: {tried_first} then {tried_second}")


def test_a_never_used_proxy_outranks_one_with_a_better_score(db):
    """NULLS FIRST: a fresh proxy is preferred over a proven one until the pool has been walked
    once, so a newly imported batch actually gets used instead of being stuck behind favourites."""
    from libs.proxies.lease import lease_proxy

    veteran = _proxy(db, endpoint="veteran:80", max_sessions=5)
    veteran.score = 99.0
    veteran.last_used_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    fresh = _proxy(db, endpoint="fresh:80", max_sessions=5)      # last_used_at is NULL
    db.commit()

    got, _ = lease_proxy(db, "w1", "soax")
    assert got.endpoint == fresh.endpoint


# ── geo filtering (measured 2026-08-18: DE 23.7% / IT 23.4% ... CZ 2.9% login success) ────────
def test_geo_filter_restricts_the_pool_to_the_named_countries(db):
    """An 8x spread in login success by exit geo, and the pool was being spent almost backwards --
    CZ took the MOST attempts at the WORST rate. This is the selector that fixes that."""
    from libs.proxies.lease import lease_proxy, release_proxy

    for geo in ("CZ", "DE", "IT", "LU"):
        for i in range(2):
            p = _proxy(db, endpoint=f"{geo}{i}:80", max_sessions=1)
            p.geo = geo
    db.commit()

    got = []
    for i in range(4):
        p, _ = lease_proxy(db, f"w{i}", "soax", geos={"DE", "IT"})
        got.append(p.geo)
        release_proxy(db, f"w{i}")
    assert set(got) == {"DE", "IT"}, got


def test_a_geo_filter_never_excludes_a_proxy_whose_geo_is_unknown(db):
    """soax credentials carry no country, so geo is NULL there. A filter narrows the pool; it must
    not silently empty it for a provider that cannot be classified."""
    from libs.proxies.lease import lease_proxy

    p = _proxy(db, endpoint="nogeo:80", max_sessions=1)   # geo stays NULL
    db.commit()
    got, _ = lease_proxy(db, "w1", "soax", geos={"DE"})
    assert got.endpoint == "nogeo:80"


def test_no_geo_filter_leaves_selection_untouched(db):
    from libs.proxies.lease import lease_proxy

    p = _proxy(db, endpoint="cz0:80", max_sessions=1); p.geo = "CZ"
    db.commit()
    got, _ = lease_proxy(db, "w1", "soax")
    assert got.endpoint == "cz0:80"


def test_terminal_release_records_an_outcome_on_the_lease(db):
    """release_worker_resources set released_at and left outcome NULL, so a worker that ended while
    holding a slot lease produced a row indistinguishable from one still in flight -- invisible to
    every outcome-based query. Found by auditing a real run: 2 leases had no outcome at all."""
    from libs.db import models as M
    from libs.domain.enums import SlotLeaseOutcome, SlotStatus
    from libs.proxies.lease import release_worker_resources

    w = _worker(db)
    slot = M.Slot(booking_id="bk1", posto_id="2032", date="2026-09-10", period=3,
                  status=SlotStatus.LEASED)
    db.add(slot); db.commit()
    db.add(M.SlotLease(slot_id=slot.id, worker_id=w.id)); db.commit()

    release_worker_resources(db, w.id)

    lease = db.scalars(select(M.SlotLease)).one()
    assert lease.released_at is not None
    assert lease.outcome == SlotLeaseOutcome.EXPIRED
    assert db.get(M.Slot, slot.id).status == SlotStatus.AVAILABLE   # and the slot is freed


# ── rotating pools: a probe failure must not condemn the row (operator, 2026-08-19) ───────────
def test_an_unhealthy_row_is_still_leasable(db):
    """Every pool here is ROTATING -- a row is a session template, not an IP. soax rows carry a
    sessionid whose exit changes once sessionlength lapses or the bound IP drops (bindttl), and
    webshare's gateway picks an exit per connection. So a probe failure says "that exit was bad a
    moment ago", never "this row is bad", and excluding the row throws away a good session template
    over a transient IP. The only recovery that works on a rotating pool is ROTATION."""
    from libs.proxies.lease import lease_proxy

    p = _proxy(db, endpoint="condemned:80", max_sessions=1)
    p.is_healthy, p.health, p.score = False, "ERR_CONNECTION_CLOSED", -5.0
    db.commit()

    got, _ = lease_proxy(db, "w1", "soax")
    assert got.endpoint == "condemned:80"


def test_score_still_orders_ties_without_excluding_anyone(db):
    """health/score stay recorded and still break ties among equally-idle rows, so a consistently
    bad row sinks -- it just never gets cut."""
    from libs.proxies.lease import lease_proxy, release_proxy

    good = _proxy(db, endpoint="good:80", max_sessions=1); good.score = 5.0
    bad = _proxy(db, endpoint="bad:80", max_sessions=1)
    bad.score, bad.is_healthy = -5.0, False
    db.commit()

    first, _ = lease_proxy(db, "w1", "soax"); release_proxy(db, "w1")
    second, _ = lease_proxy(db, "w2", "soax")
    assert first.endpoint == "good:80"      # better score wins the tie...
    assert second.endpoint == "bad:80"      # ...but the other is still reachable

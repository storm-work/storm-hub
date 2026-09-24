from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from libs.db import metadata
from libs.db import models as M
from libs.proxies import (
    ProxyLeaseError, conn_url, find_orphaned_worker_ids, find_reactivation_candidates, lease_proxy,
    release_proxy, release_worker_resources,
)


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


# ── NOTE (2026-09-15): the pool these rotating tests describe no longer exists in PRODUCTION data.
# Rotating proxies stopped being DB rows (migration ci000droprot); workers mint their own session
# ids locally. The tests below are KEPT DELIBERATELY, against the transition plan's suggestion to
# delete them, because `lease_proxy` and `_candidate_query` are byte-unchanged and still serve the
# STATIC/ISP path with exactly this machinery -- pool walking, the LRU cursor, tier ordering and the
# health-as-non-filter branch are all still live code. Deleting their only coverage because the
# rotating CALLER went away would leave that code unguarded. What did change is the justification on
# two of them; see those docstrings.

# ── the pool cursor (operator request 2026-08-16) ─────────────────────────────────────────────
def test_leases_walk_the_whole_pool_before_repeating_any_proxy(db):
    """_candidate_query was (score DESC, created_at ASC): fully deterministic, so every lease
    started from the SAME point and the first few proxies were used forever while the rest of the
    pool sat idle. LRU ordering IS the cursor -- each lease resumes where the last one left off."""
    from libs.proxies.lease import lease_proxy, release_proxy

    # All ESTABLISHED and equally idle, so the LRU cursor is what is under test. (These tests lease
    # with no health_check, so nothing writes a verdict; in production every lease probes and lands
    # in the proven or known-bad tier, and the "established, verdict wiped" tier exists only for the
    # post-reset recovery case.)
    made = [_proxy(db, endpoint=f"p{i}:80", max_sessions=1) for i in range(5)]
    for i, mp in enumerate(made):
        mp.last_used_at = datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(minutes=i)
    db.commit()
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

    # All three ESTABLISHED (same tier), so this measures the cursor rather than the tiering --
    # which is what the test is actually about. Leaving them never-used would promote whichever one
    # is leased into a higher tier and re-select it, testing tiering by accident.
    for i in range(3):
        p = _proxy(db, endpoint=f"q{i}:80", max_sessions=1)
        p.last_used_at = datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(minutes=i)
    db.commit()

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


def test_an_established_proxy_now_outranks_a_never_used_one(db):
    """REVERSES a previously deliberate invariant, on measurement.

    This test used to assert the opposite -- "NULLS FIRST: a fresh proxy is preferred over a proven
    one until the pool has been walked once, so a newly imported batch actually gets used instead of
    being stuck behind favourites". That premise was that a fresh row is an untried but PLAUSIBLE
    proxy. For rotating providers it is not: a row is a sticky SESSION, and one the provider has
    never established is measurably near-useless (2026-08-24: 600 never-used sessionids -> 0.5%
    healthy; 12 already-used -> 92%).

    THAT MEASUREMENT IS RETRACTED (CONFIRMED_FACTS.md §16): the sweep ran the tier-1 raw probe
    first, which poisons the exit it measures, so the 0.5%/92% split measured our own probe, not the
    sessions. The assertion below is kept anyway because it is independently right for the pool that
    still reaches this code -- STATIC/ISP rows, where last_used_at ordering is plain LRU across a
    finite set of individually-owned IPs and spreading load over them is the point. Do not cite the
    0.5%/92% figures as support for anything.

    So walking the fresh pool end-to-end first is not exploration, it is failing ~199 times in 200 --
    which is precisely what left a booking at zero running workers. Exploration is preserved rather
    than dropped: never-used rows are the tier BELOW established ones, reached whenever the
    established set is exhausted or at capacity."""
    from libs.proxies.lease import lease_proxy

    veteran = _proxy(db, endpoint="veteran:80", max_sessions=5)
    veteran.score = 99.0
    veteran.last_used_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    fresh = _proxy(db, endpoint="fresh:80", max_sessions=5)      # last_used_at is NULL
    db.commit()

    got, _ = lease_proxy(db, "w1", "soax")
    assert got.endpoint == veteran.endpoint


def test_a_never_used_proxy_is_still_reached_when_established_ones_are_full(db):
    """The other half of the reversal: exploration must survive, or the established set can never
    grow."""
    from libs.proxies.lease import lease_proxy

    veteran = _proxy(db, endpoint="veteran2:80", max_sessions=1)
    veteran.last_used_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    fresh = _proxy(db, endpoint="fresh2:80", max_sessions=1)
    db.commit()

    first, _ = lease_proxy(db, "w1", "soax")          # takes the veteran's only slot
    second, _ = lease_proxy(db, "w2", "soax")
    assert first.endpoint == veteran.endpoint
    assert second.endpoint == fresh.endpoint


# ── geo filtering (measured 2026-08-18: DE 23.7% / IT 23.4% ... CZ 2.9% login success) ────────
def test_geo_filter_restricts_the_pool_to_the_named_countries(db):
    """An 8x spread in login success by exit geo, and the pool was being spent almost backwards --
    CZ took the MOST attempts at the WORST rate. This is the selector that fixes that."""
    from libs.proxies.lease import lease_proxy, release_proxy

    # Same tier for all, so geo is the only selector under test.
    n = 0
    for geo in ("CZ", "DE", "IT", "LU"):
        for i in range(2):
            p = _proxy(db, endpoint=f"{geo}{i}:80", max_sessions=1)
            p.geo = geo
            p.last_used_at = datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(minutes=n)
            n += 1
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

    # Both ESTABLISHED and equally idle, so score is the only thing separating them.
    good = _proxy(db, endpoint="good:80", max_sessions=1); good.score = 5.0
    good.last_used_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    bad = _proxy(db, endpoint="bad:80", max_sessions=1)
    bad.score, bad.is_healthy = -5.0, False
    bad.last_used_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    db.commit()

    first, _ = lease_proxy(db, "w1", "soax"); release_proxy(db, "w1")
    second, _ = lease_proxy(db, "w2", "soax")
    assert first.endpoint == "good:80"      # better score wins the tie...
    assert second.endpoint == "bad:80"      # ...but the other is still reachable


# ── lease-time geo injection (2026-08-23) ───────────────────────────────────
def test_geo_is_injected_per_provider_format():
    """The pools carry NO country since the 2026-08-22 regeneration, so the country has to be added
    at lease time. Formats differ, so this cannot be generic."""
    from libs.proxies.lease import _inject_geo

    soax = "package-335959-sessionid-AbC-sessionlength-3600-bindttl-600-idlettl-900-opt-wb:PW"
    assert _inject_geo(soax, "soax", "de").startswith("package-335959-country-de-sessionid-")
    assert _inject_geo("sodade2026-17:PW", "webshare", "de") == "sodade2026-DE-17:PW"


def test_a_credential_that_already_has_a_country_is_left_alone():
    from libs.proxies.lease import _inject_geo

    already = "package-335959-country-pt-sessionid-AbC:PW"
    assert _inject_geo(already, "soax", "de") == already


def test_no_geo_leaves_the_credential_byte_identical():
    """Every non-geo caller must get exactly what is stored."""
    from libs.proxies.lease import _inject_geo

    cred = "package-335959-sessionid-AbC:PW"
    assert _inject_geo(cred, "soax", "") == cred
    assert _inject_geo(cred, "soax", None) == cred


def test_an_unknown_provider_is_not_mangled():
    from libs.proxies.lease import _inject_geo

    assert _inject_geo("whatever:PW", "someotherprovider", "de") == "whatever:PW"


# ── two-tier screening (2026-08-24) ─────────────────────────────────────────
def test_a_dead_exit_is_rejected_without_launching_a_browser():
    """storm-auto screens cheaply first; we paid ~20-25s of Chromium to discover a socket would not
    open. ~76% of our transient login failures were connectivity-level, so that is where the probe
    budget was going."""
    from libs.proxies.health import quick_screen

    alive, why = quick_screen("http://u:p@127.0.0.1:9", timeout=3)
    assert alive is False
    assert "dead exit" in why


def test_an_unrecognised_raw_error_fails_OPEN():
    """Failing closed on an unfamiliar error would quietly shrink the pool -- exactly how the
    639-row false-condemnation incident happened. One wasted browser probe is the cheaper mistake."""
    import libs.proxies.health as H

    class _Boom:
        def __init__(self, **kw): pass
        def get(self, url): raise RuntimeError("something nobody has seen before")

    import sys, types
    real = sys.modules.get("primp")
    sys.modules["primp"] = types.SimpleNamespace(Client=_Boom)
    try:
        alive, why = H.quick_screen("http://u:p@x:1")
        assert alive is True and "deferring to browser" in why
    finally:
        if real is not None:
            sys.modules["primp"] = real
        else:
            sys.modules.pop("primp", None)


def test_tier1_rejection_skips_tier2_entirely(db):
    """The point of the cheap tier: a candidate it rejects must never reach the browser."""
    browser_calls = {"n": 0}

    def _never(url):
        browser_calls["n"] += 1
        return True, "ok"

    for i in range(3):
        _proxy(db, endpoint=f"proxy.soax.com:500{i}")
    db.commit()
    try:
        lease_proxy(db, "w1", provider="soax", health_check=_never,
                    quick_screen=lambda url: (False, "dead exit -- test"))
    except ProxyLeaseError:
        pass
    assert browser_calls["n"] == 0, "a rejected candidate must not cost a browser launch"


# ── proven-good proxies come first (2026-08-24) ─────────────────────────────
def test_a_proven_good_proxy_is_preferred_over_an_unprobed_one(db):
    """Pure last_used_at NULLS FIRST is right while exploring a small pool and became harmful at
    10x scale: 20,000 active soax rows, 18,149 never probed, 751 proven ok. Every launch drew five
    UNKNOWN candidates at a ~10% hit rate while the proven set sat unused, and bookings sat at zero
    running workers with "5 failed health"."""
    good = _proxy(db, endpoint="good:1")
    good.health = "ok"
    good.score = 10.0
    good.last_used_at = None
    unprobed = _proxy(db, endpoint="unprobed:1")
    unprobed.health = None
    unprobed.last_used_at = None
    db.commit()

    proxy, _ = lease_proxy(db, "w1", provider="soax")
    assert proxy.id == good.id


def test_a_known_bad_proxy_sorts_last(db):
    bad = _proxy(db, endpoint="bad:1")
    bad.health = "TimeoutError: ..."
    bad.last_used_at = None
    unprobed = _proxy(db, endpoint="unprobed:2")
    unprobed.health = None
    unprobed.last_used_at = None
    db.commit()

    proxy, _ = lease_proxy(db, "w1", provider="soax")
    assert proxy.id == unprobed.id, "an unprobed row still beats one that is known bad"


def test_exploration_survives_when_the_proven_set_is_busy(db):
    """Unprobed rows are the SECOND tier, not abandoned -- otherwise a pool could never grow its
    proven set."""
    good = _proxy(db, endpoint="good:2", max_sessions=1)
    good.health = "ok"
    db.commit()
    lease_proxy(db, "w1", provider="soax")          # consumes the only proven row's capacity

    unprobed = _proxy(db, endpoint="unprobed:3")
    unprobed.health = None
    db.commit()
    proxy, _ = lease_proxy(db, "w2", provider="soax")
    assert proxy.id == unprobed.id


# ── selection must survive a health wipe (2026-08-24) ───────────────────────
def test_an_established_session_beats_a_never_used_one_even_with_no_verdict(db):
    """POST /proxies/reset-health clears every verdict, and on 2026-08-24 one click emptied tier 1
    across all 21,600 rows. Everything then fell into a single tier ordered last_used_at NULLS
    FIRST, which puts NEVER-USED sessions FIRST -- the exact opposite of the measurement (a
    never-used sessionid is ~0.5% healthy, an already-used one ~92%). A cleared verdict loses our
    RECORD of a session, not the session itself.

    The 0.5%/92% figures are RETRACTED (CONFIRMED_FACTS.md §16, and see
    test_an_established_proxy_now_outranks_a_never_used_one). The surviving justification is the
    last sentence -- a wiped verdict must not reorder selection -- which holds for the static pool
    that still reaches this code."""
    from datetime import datetime, timezone

    established = _proxy(db, endpoint="established:1")
    established.health = None                      # verdict wiped
    established.last_used_at = datetime(2026, 8, 20, tzinfo=timezone.utc)
    fresh = _proxy(db, endpoint="fresh:1")
    fresh.health = None
    fresh.last_used_at = None                      # never established
    db.commit()

    proxy, _ = lease_proxy(db, "w1", provider="soax")
    assert proxy.id == established.id


def test_proven_ok_still_outranks_an_established_but_unverified_row(db):
    from datetime import datetime, timezone

    proven = _proxy(db, endpoint="proven:1")
    proven.health = "ok"
    proven.last_used_at = datetime(2026, 8, 24, tzinfo=timezone.utc)   # used MORE recently
    established = _proxy(db, endpoint="established:2")
    established.health = None
    established.last_used_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    db.commit()

    proxy, _ = lease_proxy(db, "w1", provider="soax")
    assert proxy.id == proven.id, "a proven verdict outranks mere LRU recency"


# ── resource_type gating (Phase 3, D32): static/ISP resources are a finite thing whose health IS
#    the row's own state, not an exit that will rotate away on its own (D19 stays true, but only for
#    resource_type='rotating' now) ────────────────────────────────────────────────────────────────
def test_default_resource_type_is_rotating_and_preserves_existing_unhealthy_behavior(db):
    """Every row created before this column existed -- and every row _proxy() makes without saying
    otherwise -- must default to 'rotating' and keep D19's exact pre-Phase-3 behavior: an unhealthy
    row stays leasable. Byte-for-byte the same assertion as test_an_unhealthy_row_is_still_leasable,
    just pinning that the new column's default is what makes it still true."""
    p = _proxy(db, endpoint="legacy:80")
    assert p.resource_type == "rotating"
    p.is_healthy, p.health, p.score = False, "ERR_CONNECTION_CLOSED", -5.0
    db.commit()

    got, _ = lease_proxy(db, "w1", "soax")
    assert got.endpoint == "legacy:80"


def test_static_resource_marked_unhealthy_is_excluded(db):
    """The new half of the rule: a resource_type='static' row IS a finite, individually-owned
    resource, so an unhealthy verdict means this specific resource is bad, not that an exit moved
    underneath it -- excluding it is correct, unlike the rotating case above."""
    bad = M.Proxy(provider="isp", kind="static_res", resource_type="static", endpoint="badstatic:80",
                  credentials_ref="u:p", max_sessions=1, is_healthy=False)
    db.add(bad); db.commit()

    with pytest.raises(ProxyLeaseError):
        lease_proxy(db, "w1", "isp")


def test_static_resource_marked_healthy_is_still_selectable(db):
    """The exclusion is on is_healthy, not on resource_type itself -- a healthy static row must
    still lease normally."""
    good = M.Proxy(provider="isp", kind="static_res", resource_type="static", endpoint="goodstatic:80",
                   credentials_ref="u:p", max_sessions=1, is_healthy=True)
    db.add(good); db.commit()

    got, _ = lease_proxy(db, "w1", "isp")
    assert got.endpoint == "goodstatic:80"


def test_static_and_rotating_pools_coexist_only_the_unhealthy_static_row_is_cut(db):
    """A mixed pool (same provider) proves the filter is scoped to resource_type, not applied
    globally the moment any static row exists."""
    rotating_bad = M.Proxy(provider="isp", kind="s", resource_type="rotating", endpoint="rot:80",
                           credentials_ref="u:p", max_sessions=1, is_healthy=False)
    static_bad = M.Proxy(provider="isp", kind="s", resource_type="static", endpoint="stat:80",
                         credentials_ref="u:p", max_sessions=1, is_healthy=False)
    db.add_all([rotating_bad, static_bad]); db.commit()

    got, _ = lease_proxy(db, "w1", "isp")
    assert got.endpoint == "rot:80", "the unhealthy static row must be excluded, leaving only the rotating one"


# ── all-ISP leasing (2026-09-24): provider=None leases across EVERY ISP provider ────────────────
def test_provider_none_leases_across_every_isp_provider(db):
    """The all-ISP option (PROXY_PROVIDER=all-isp -> provider=None): drawing capacity is the UNION
    of every ISP pool, not one provider. Two different-provider static rows, one lease each, both
    reachable without ever naming a provider."""
    db.add_all([
        M.Proxy(provider="capsolver", kind="static", resource_type="static", endpoint="cap:80",
                credentials_ref="u:p", max_sessions=1, is_healthy=True),
        M.Proxy(provider="thordata", kind="static", resource_type="static", endpoint="thor:80",
                credentials_ref="u:p", max_sessions=1, is_healthy=True),
    ])
    db.commit()

    seen = set()
    got1, _ = lease_proxy(db, "w1", None, resource_type="static")
    seen.add(got1.endpoint)
    got2, _ = lease_proxy(db, "w2", None, resource_type="static")
    seen.add(got2.endpoint)
    assert seen == {"cap:80", "thor:80"}, "both ISP providers must be leasable without a provider filter"

    # Union is exhausted now -- the third lease fails, and the message names ISP rather than a provider.
    with pytest.raises(ProxyLeaseError, match="ISP"):
        lease_proxy(db, "w3", None, resource_type="static")


def test_provider_none_with_health_check_spans_providers(db):
    """The probe path (health_check wired, as production does) must also span providers when
    provider is None -- a dead capsolver row is skipped and a healthy thordata row leased."""
    db.add_all([
        M.Proxy(provider="capsolver", kind="static", resource_type="static", endpoint="capdead:80",
                credentials_ref="u:p", max_sessions=1, is_healthy=True),
        M.Proxy(provider="thordata", kind="static", resource_type="static", endpoint="thorok:80",
                credentials_ref="u:p", max_sessions=1, is_healthy=True),
    ])
    db.commit()
    probe = lambda url: (("thorok" in url), "ok" if "thorok" in url else "dead")
    got, _ = lease_proxy(db, "w1", None, resource_type="static", health_check=probe)
    assert got.endpoint == "thorok:80"


# ── Phase 8 (D-NEW-1): static reactivation candidate query ─────────────────────────────────────
def test_reactivation_candidates_selects_only_static_unhealthy_active(db):
    """The exact five-way matrix Phase 7/8 specified: only static+unhealthy+active is a candidate."""
    db.add_all([
        M.Proxy(id="a", provider="isp", kind="s", endpoint="a:1", resource_type="static",
               active=True, is_healthy=False),               # SELECTED
        M.Proxy(id="b", provider="isp", kind="s", endpoint="b:1", resource_type="static",
               active=True, is_healthy=True),                 # healthy -- not excluded, nothing to reactivate
        M.Proxy(id="c", provider="isp", kind="s", endpoint="c:1", resource_type="static",
               active=False, is_healthy=False),                # inactive -- operator turned it off
        M.Proxy(id="d", provider="soax", kind="s", endpoint="d:1", resource_type="rotating",
               active=True, is_healthy=False),                 # rotating -- D19, never excluded to begin with
        M.Proxy(id="e", provider="soax", kind="s", endpoint="e:1", resource_type="rotating",
               active=True, is_healthy=True),
    ])
    db.commit()

    got = {p.id for p in find_reactivation_candidates(db)}
    assert got == {"a"}


def test_reactivation_candidates_ordered_oldest_failed_first_nulls_first(db):
    p_recent = M.Proxy(id="recent", provider="isp", kind="s", endpoint="r:1", resource_type="static",
                       active=True, is_healthy=False,
                       last_used_at=datetime(2026, 8, 20, tzinfo=timezone.utc))
    p_old = M.Proxy(id="old", provider="isp", kind="s", endpoint="o:1", resource_type="static",
                    active=True, is_healthy=False,
                    last_used_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
    p_never = M.Proxy(id="never", provider="isp", kind="s", endpoint="n:1", resource_type="static",
                      active=True, is_healthy=False, last_used_at=None)
    db.add_all([p_recent, p_old, p_never])
    db.commit()

    got = [p.id for p in find_reactivation_candidates(db)]
    assert got == ["never", "old", "recent"], got   # NULL first, then oldest-to-newest


def test_reactivation_candidates_empty_when_pool_is_all_rotating(db):
    """Regression guard for D19: a pool with only rotating rows must never produce a reactivation
    candidate, healthy or not."""
    db.add_all([
        M.Proxy(id="r1", provider="soax", kind="s", endpoint="1:1", resource_type="rotating",
               active=True, is_healthy=False),
        M.Proxy(id="r2", provider="soax", kind="s", endpoint="2:1", resource_type="rotating",
               active=True, is_healthy=True),
    ])
    db.commit()
    assert find_reactivation_candidates(db) == []


# ── Phase 8 (D-NEW-2): F5 orphaned-lease predicate ──────────────────────────────────────────────
def _worker_with_status(db, wid, status):
    from libs.domain.enums import WorkerRole
    bk = db.get(M.Booking, "bk1") or M.Booking(id="bk1", name="b", type="test", posto_id="2032")
    idn = db.get(M.Identity, "id1") or M.Identity(id="id1", full_name="X", type="test")
    if not db.get(M.Booking, "bk1"):
        db.add(bk)
    if not db.get(M.Identity, "id1"):
        db.add(idn)
    db.flush()
    w = M.Worker(id=wid, booking_id="bk1", identity_id="id1", role=WorkerRole.APPLY, status=status)
    db.add(w); db.commit()
    return w


_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("status", ["ready", "stopped", "held", "completed", "failed"])
def test_orphaned_worker_ids_finds_non_running_aged_leases(db, status):
    from libs.domain.enums import WorkerStatus

    w = _worker_with_status(db, "w1", WorkerStatus(status))
    px = _proxy(db, endpoint="h:1")
    db.add(M.ProxyLease(proxy_id=px.id, worker_id=w.id, leased_at=_NOW - timedelta(seconds=400)))
    db.commit()

    assert find_orphaned_worker_ids(db, grace_seconds=300, now=_NOW) == [w.id]


def test_orphaned_worker_ids_never_includes_running_workers(db):
    from libs.domain.enums import WorkerStatus

    w = _worker_with_status(db, "w1", WorkerStatus.RUNNING)
    px = _proxy(db, endpoint="h:1")
    db.add(M.ProxyLease(proxy_id=px.id, worker_id=w.id, leased_at=_NOW - timedelta(hours=10)))
    db.commit()

    assert find_orphaned_worker_ids(db, grace_seconds=300, now=_NOW) == []


def test_orphaned_worker_ids_excludes_already_released_leases(db):
    """Idempotency at the query level: a lease already released must never be reported as orphaned,
    regardless of worker status or age."""
    from libs.domain.enums import WorkerStatus

    w = _worker_with_status(db, "w1", WorkerStatus.FAILED)
    px = _proxy(db, endpoint="h:1")
    db.add(M.ProxyLease(proxy_id=px.id, worker_id=w.id, leased_at=_NOW - timedelta(hours=10),
                        released_at=_NOW))
    db.commit()

    assert find_orphaned_worker_ids(db, grace_seconds=300, now=_NOW) == []


def test_orphaned_worker_ids_grace_boundary(db):
    from libs.domain.enums import WorkerStatus

    w_under = _worker_with_status(db, "w-under", WorkerStatus.FAILED)
    w_over = _worker_with_status(db, "w-over", WorkerStatus.FAILED)
    px1 = _proxy(db, endpoint="h:1", max_sessions=5)
    px2 = _proxy(db, endpoint="h:2", max_sessions=5)
    # Just under the grace period -- must NOT be reconciled yet (still legitimately in-flight).
    db.add(M.ProxyLease(proxy_id=px1.id, worker_id=w_under.id,
                        leased_at=_NOW - timedelta(seconds=299)))
    # Just over the grace period -- must be reconciled.
    db.add(M.ProxyLease(proxy_id=px2.id, worker_id=w_over.id,
                        leased_at=_NOW - timedelta(seconds=301)))
    db.commit()

    got = set(find_orphaned_worker_ids(db, grace_seconds=300, now=_NOW))
    assert got == {"w-over"}, got


def test_orphaned_worker_ids_exactly_at_grace_boundary_is_not_yet_orphaned(db):
    """`leased_at < cutoff` is a strict inequality -- a lease aged EXACTLY grace_seconds is not yet
    past it. Pinned explicitly so the boundary can't silently flip to <= in a future edit."""
    from libs.domain.enums import WorkerStatus

    w = _worker_with_status(db, "w1", WorkerStatus.FAILED)
    px = _proxy(db, endpoint="h:1")
    db.add(M.ProxyLease(proxy_id=px.id, worker_id=w.id, leased_at=_NOW - timedelta(seconds=300)))
    db.commit()

    assert find_orphaned_worker_ids(db, grace_seconds=300, now=_NOW) == []


def test_tier1_is_disabled_because_it_poisons_the_exit():
    """A raw primp probe to the portal flags the IP, and the browser request that follows on the
    same sticky session gets ERR_CONNECTION_CLOSED. Measured 2026-08-24, same rows, sequential:

        browser only                 5/6 ok  (83%)
        quick_screen THEN browser    0/6 ok  ( 0%)

    storm-auto can screen this way because primp is its PRIMARY transport, so the fingerprint stays
    consistent. We probe with primp and then switch to a browser on the same exit, which is the
    combination that gets flagged. This test exists so re-enabling it is a deliberate act."""
    import apps.hub.main as hub_main

    assert hub_main._screen_tier1() is None

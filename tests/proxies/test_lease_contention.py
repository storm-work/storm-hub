"""A lost claim race is CONTENTION, not a dead pool -- and must not be reported or budgeted as one.

Confirmed live 2026-08-20 on booking "Test 3018": 4 of 9 workers failed to launch with

    no healthy soax proxy available after probing 5 candidate(s) (e7efdf07..., 677ec235..., ...)

naming an identical five candidates across all four workers -- every one of which the DB recorded as
is_healthy=True / health='ok', stamped during that very launch window. The probes had SUCCEEDED. The
workers lost the claim race five times each, `probed` counted those losses against max_probes, and
the failure was then reported as pool health. Two bugs in one message: the budget and the diagnosis.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from libs.db import models as M
from libs.db.models import metadata
from libs.proxies.lease import ProxyLeaseError, lease_proxy


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield s
    s.close()


def _pool(db, n, *, max_sessions=1):
    rows = [M.Proxy(provider="soax", kind="mobile", endpoint=f"h{i}:1", credentials_ref=f"u{i}:p",
                    max_sessions=max_sessions, active=True) for i in range(n)]
    db.add_all(rows)
    db.commit()
    return rows


def _steal(db, conn_url):
    """Simulate a concurrent leaser winning this proxy during our (unlocked) probe window."""
    endpoint = conn_url.split("@")[-1]
    p = next(x for x in db.query(M.Proxy).all() if x.endpoint == endpoint)
    db.add(M.ProxyLease(proxy_id=p.id, worker_id=f"rival-{p.id}",
                        leased_at=datetime.now(timezone.utc)))


def test_lost_races_do_not_exhaust_the_probe_budget(db):
    """Every candidate probes HEALTHY but gets stolen mid-probe. Must keep going past max_probes.

    Before the fix this raised after exactly 5 candidates, reporting "no healthy proxy available"
    about a pool in which every single probe had returned ok.
    """
    _pool(db, 60)
    probes: list[str] = []

    def health(url):
        probes.append(url)
        if len(probes) <= 8:      # first 8 are sniped by a "concurrent leaser"
            _steal(db, url)
        return True, "ok"

    proxy, url = lease_proxy(db, "w1", "soax", kind="mobile", health_check=health, max_probes=5)

    assert proxy is not None
    assert len(probes) > 5, f"gave up after {len(probes)} probes -- lost races still cost budget"
    assert len(probes) == 9, f"expected to win on the 9th candidate, took {len(probes)}"


def test_a_genuinely_dead_pool_still_stops_at_max_probes(db):
    """The budget must still bound a dead pool -- that is what max_probes is FOR."""
    _pool(db, 100)
    calls: list[str] = []

    def health(url):
        calls.append(url)
        return False, "net::ERR_TUNNEL_CONNECTION_FAILED"

    with pytest.raises(ProxyLeaseError):
        lease_proxy(db, "w1", "soax", kind="mobile", health_check=health, max_probes=5)
    assert len(calls) == 5, f"probed {len(calls)} times for max_probes=5"


def test_total_cost_is_still_bounded_under_pure_contention(db):
    """Lost races get a larger allowance, not an unlimited one -- each still costs a browser probe."""
    _pool(db, 500)
    probes: list[str] = []

    def health(url):
        probes.append(url)
        _steal(db, url)          # never winnable
        return True, "ok"

    with pytest.raises(ProxyLeaseError):
        lease_proxy(db, "w1", "soax", kind="mobile", health_check=health, max_probes=5)
    assert len(probes) == 15, f"expected max_probes*3 ceiling, got {len(probes)}"


def test_error_message_distinguishes_contention_from_bad_health(db):
    """'no healthy proxy' sent the live investigation looking at health for a contention bug."""
    _pool(db, 100)
    with pytest.raises(ProxyLeaseError) as ei:
        lease_proxy(db, "w1", "soax", kind="mobile",
                    health_check=lambda u: (False, "net::ERR_TIMED_OUT"), max_probes=3)
    msg = str(ei.value)
    assert "3 failed health" in msg, msg
    assert "0 lost to a concurrent leaser" in msg, msg

    probes: list[str] = []

    def stolen(url):
        probes.append(url)
        _steal(db, url)
        return True, "ok"

    with pytest.raises(ProxyLeaseError) as ei2:
        lease_proxy(db, "w2", "soax", kind="mobile", health_check=stolen, max_probes=2)
    msg2 = str(ei2.value)
    assert "0 failed health" in msg2, msg2
    assert "lost to a concurrent leaser" in msg2 and "0 lost" not in msg2, msg2


def test_concurrent_leasers_do_not_walk_an_identical_candidate_order(db):
    """The mechanism behind the incident: the LRU cursor only advances when a probe COMMITS, so a
    fleet launching at once all reads the same head and collides on the same rows.

    Asserted as "the first candidate varies across leases that see an unchanged pool" -- which was
    deterministically false before, and is false now only with probability ~(1/window)^9.
    """
    _pool(db, 300)
    firsts: set[str] = set()
    for i in range(9):
        seen: list[str] = []

        def health(url, sink=seen):
            sink.append(url)
            return False, "x"     # never claim, and never advance anyone's cursor meaningfully

        with pytest.raises(ProxyLeaseError):
            lease_proxy(db, f"w{i}", "soax", kind="mobile", health_check=health, max_probes=1)
        firsts.add(seen[0])

    assert len(firsts) > 1, (
        "every lease probed the same first candidate -- concurrent leasers will collide exactly as "
        "they did live on 2026-08-20"
    )

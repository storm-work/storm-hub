import pytest
from sqlalchemy import create_engine
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


def test_unhealthy_and_inactive_skipped(db):
    _worker(db)
    db.add(M.Proxy(id="dead", provider="isp", kind="s", endpoint="h:1", credentials_ref="u:p",
                   max_sessions=2, is_healthy=False, score=100))
    db.add(M.Proxy(id="off", provider="isp", kind="s", endpoint="h:2", credentials_ref="u:p",
                   max_sessions=2, active=False, score=100))
    db.commit()
    with pytest.raises(ProxyLeaseError):
        lease_proxy(db, "w1", "isp")


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
    assert bad.health == "TimeoutError: nav timed out"[:24]
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

    with pytest.raises(ProxyLeaseError, match="no healthy isp proxy"):
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

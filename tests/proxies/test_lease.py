import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from libs.db import metadata
from libs.db import models as M
from libs.proxies import ProxyLeaseError, conn_url, lease_proxy, release_proxy


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


def test_unhealthy_and_inactive_skipped(db):
    _worker(db)
    db.add(M.Proxy(id="dead", provider="isp", kind="s", endpoint="h:1", credentials_ref="u:p",
                   max_sessions=2, is_healthy=False, score=100))
    db.add(M.Proxy(id="off", provider="isp", kind="s", endpoint="h:2", credentials_ref="u:p",
                   max_sessions=2, active=False, score=100))
    db.commit()
    with pytest.raises(ProxyLeaseError):
        lease_proxy(db, "w1", "isp")

"""Named ISP pools, and assigning SEVERAL of them to one booking (2026-09-16).

The operator requirement in two halves:
  * "when I purchased 10 ISP proxies from Thordata I need to add those to the DB ... if that is a
    new [name] a new pool will be created, or if an existing one, 10 proxies will be added to it";
  * "if each [pool] has [fewer] proxies than required workers count, we will need to assign
    multiple pools to a booking so the proxy manager can fetch from multiple pools."

An ISP proxy is a finite, individually-owned IP, so both halves are about CAPACITY being real --
which is what separates these from the old rotating pools, where a "pool" was a bag of strings.
"""
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
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _isp(db, endpoint, pool_name=None, provider="thordata", **kw):
    p = M.Proxy(provider=provider, kind="static", resource_type="static", endpoint=endpoint,
                credentials_ref="u:p", max_sessions=1, active=True, pool_name=pool_name, **kw)
    db.add(p)
    db.commit()
    return p


def _worker(db, wid):
    return wid


def test_a_pool_filter_draws_only_from_the_named_pools(db):
    _isp(db, "a:1", pool_name="thordata-oct")
    _isp(db, "b:1", pool_name="iproyal-a")
    _isp(db, "c:1", pool_name="someone-elses")

    got = set()
    for i in range(2):
        proxy, _ = lease_proxy(db, f"w{i}", "thordata", resource_type="static",
                               pool_names={"thordata-oct", "iproyal-a"})
        got.add(proxy.endpoint)
    assert got == {"a:1", "b:1"}, "leased outside the named pools"


def test_several_pools_combine_their_capacity(db):
    """THE requirement. Two purchases of 2 must support 4 workers -- a booking's usable capacity is
    the UNION of the pools it names, which is the entire reason multi-assignment exists."""
    for i in range(2):
        _isp(db, f"t{i}:1", pool_name="thordata-oct")
        _isp(db, f"i{i}:1", pool_name="iproyal-a")

    leased = []
    for i in range(4):
        proxy, _ = lease_proxy(db, f"w{i}", "thordata", resource_type="static",
                               pool_names={"thordata-oct", "iproyal-a"})
        leased.append(proxy.endpoint)

    assert len(set(leased)) == 4, f"an ISP proxy was leased twice: {leased}"
    with pytest.raises(ProxyLeaseError):
        lease_proxy(db, "w-extra", "thordata", resource_type="static",
                    pool_names={"thordata-oct", "iproyal-a"})


def test_exhaustion_names_the_pools_that_were_searched(db):
    """With finite pools this is the EXPECTED error once workers outnumber proxies, and the
    operator's next move is to assign another pool or buy more. 'pool exhausted' alone says
    neither, and sent a previous investigation looking at proxy health for a capacity problem."""
    _isp(db, "a:1", pool_name="thordata-oct")
    lease_proxy(db, "w0", "thordata", resource_type="static", pool_names={"thordata-oct"})
    with pytest.raises(ProxyLeaseError) as e:
        lease_proxy(db, "w1", "thordata", resource_type="static", pool_names={"thordata-oct"})
    assert "thordata-oct" in str(e.value)


def test_an_unnamed_proxy_is_never_handed_to_a_named_request(db):
    """A NULL pool_name means 'belongs to no named pool'. Admitting it the way the GEO filter
    admits NULL would let a booking that carefully named its pools be handed an unrelated ad-hoc
    import -- and with finite, individually-owned IPs that is a real conflict with whoever is
    relying on that row, not just untidy accounting."""
    _isp(db, "unnamed:1", pool_name=None)
    with pytest.raises(ProxyLeaseError):
        lease_proxy(db, "w0", "thordata", resource_type="static", pool_names={"thordata-oct"})


def test_no_pool_filter_still_sees_every_row_including_unnamed(db):
    """Backwards compatibility: every booking predating named pools passes no filter and must keep
    selecting exactly as before, unnamed rows included."""
    _isp(db, "unnamed:1", pool_name=None)
    proxy, _ = lease_proxy(db, "w0", "thordata", resource_type="static")
    assert proxy.endpoint == "unnamed:1"


def test_pool_filter_composes_with_the_health_exclusion(db):
    """Named pools do not weaken D33: an unhealthy STATIC row is still excluded, because a static
    probe failure describes a real resource (unlike a rotating one, D19). A pool filter narrows the
    pool; it must not resurrect a condemned IP inside it."""
    _isp(db, "good:1", pool_name="thordata-oct")
    bad = _isp(db, "bad:1", pool_name="thordata-oct")
    bad.is_healthy = False
    db.commit()

    proxy, _ = lease_proxy(db, "w0", "thordata", resource_type="static",
                           pool_names={"thordata-oct"})
    assert proxy.endpoint == "good:1"
    with pytest.raises(ProxyLeaseError):
        lease_proxy(db, "w1", "thordata", resource_type="static", pool_names={"thordata-oct"})


def test_pool_filter_composes_with_geo(db):
    """Both filters narrow; neither overrides the other."""
    _isp(db, "de:1", pool_name="thordata-oct", geo="DE")
    _isp(db, "cz:1", pool_name="thordata-oct", geo="CZ")
    proxy, _ = lease_proxy(db, "w0", "thordata", resource_type="static",
                           pool_names={"thordata-oct"}, geos={"DE"})
    assert proxy.endpoint == "de:1"

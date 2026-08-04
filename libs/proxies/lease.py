"""Proxy leasing against the DB pools.

`lease_proxy` picks a healthy, active proxy of the requested provider that still has capacity
(active leases < max_sessions), records a `proxy_leases` row, and returns (proxy, connection_url).
Under Postgres it locks the chosen row `FOR UPDATE SKIP LOCKED` so concurrent leasers never grab the
same slot; on SQLite (tests) the lock is skipped. `release_proxy` frees the lease.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from libs.db import models as M


class ProxyLeaseError(RuntimeError):
    pass


def conn_url(proxy: M.Proxy, scheme: str = "http") -> str:
    """Build a connection URL: http://user:pass@host:port (or http://host:port when no creds)."""
    if proxy.credentials_ref:
        return f"{scheme}://{proxy.credentials_ref}@{proxy.endpoint}"
    return f"{scheme}://{proxy.endpoint}"


def _active_lease_counts(db: Session) -> dict[str, int]:
    rows = db.execute(
        select(M.ProxyLease.proxy_id, func.count())
        .where(M.ProxyLease.released_at.is_(None))
        .group_by(M.ProxyLease.proxy_id)
    ).all()
    return {pid: n for pid, n in rows}


def lease_proxy(db: Session, worker_id: str, provider: str, *, kind: str | None = None) -> tuple[M.Proxy, str]:
    """Lease a healthy proxy of `provider` (e.g. 'soax' or 'isp') with free capacity. Returns
    (proxy, conn_url). Raises ProxyLeaseError if the pool is exhausted."""
    is_pg = db.get_bind().dialect.name == "postgresql"
    counts = _active_lease_counts(db)

    q = (
        select(M.Proxy)
        .where(M.Proxy.provider == provider, M.Proxy.active.is_(True), M.Proxy.is_healthy.is_(True))
        .order_by(M.Proxy.score.desc(), M.Proxy.created_at.asc())
    )
    if kind:
        q = q.where(M.Proxy.kind == kind)
    if is_pg:
        q = q.with_for_update(skip_locked=True)

    for proxy in db.scalars(q):
        if counts.get(proxy.id, 0) < proxy.max_sessions:
            lease = M.ProxyLease(proxy_id=proxy.id, worker_id=worker_id,
                                 leased_at=datetime.now(timezone.utc))
            db.add(lease)
            db.commit()
            return proxy, conn_url(proxy)
    raise ProxyLeaseError(f"no free {provider} proxy available (pool exhausted)")


def release_proxy(db: Session, worker_id: str, proxy_id: str | None = None) -> int:
    """Release this worker's active lease(s). Returns how many were released."""
    q = select(M.ProxyLease).where(
        M.ProxyLease.worker_id == worker_id, M.ProxyLease.released_at.is_(None)
    )
    if proxy_id:
        q = q.where(M.ProxyLease.proxy_id == proxy_id)
    n = 0
    for lease in db.scalars(q):
        lease.released_at = datetime.now(timezone.utc)
        n += 1
    if n:
        db.commit()
    return n

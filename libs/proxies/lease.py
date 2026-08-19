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


def _candidate_query(provider: str, kind: str | None, exclude_ids: set[str] | None,
                     geos: set[str] | None = None):
    """Least-recently-used first -- this ordering IS the pool cursor.

    It used to be (score DESC, created_at ASC): fully deterministic, so every lease walked the pool
    from the SAME starting point. The first few proxies were leased and re-probed endlessly while
    the rest of a 1000-strong pool sat untouched, and a failing candidate stayed at the head of the
    queue -- which is why "no healthy proxy after probing 5 candidate(s)" kept naming the identical
    five UUIDs. After a health reset it degenerates completely: every score is 0.0, leaving pure
    creation order.

    `last_used_at` is stamped on every lease AND every probe attempt, so a proxy that just failed
    rotates to the BACK rather than being retried first next time. NULLS FIRST means a never-used
    proxy outranks any used one, so a fresh pool is walked end to end before anything repeats.
    Score still breaks ties among equally-idle proxies, so reliability is preferred without ever
    pinning selection to one favourite."""
    q = (
        select(M.Proxy)
        .where(M.Proxy.provider == provider, M.Proxy.active.is_(True), M.Proxy.is_healthy.is_(True))
        .order_by(M.Proxy.last_used_at.asc().nullsfirst(),
                  M.Proxy.score.desc(), M.Proxy.created_at.asc())
    )
    if kind:
        q = q.where(M.Proxy.kind == kind)
    if geos:
        # NULL geo is never excluded: a filter narrows the pool, it must not silently empty it for
        # a provider whose credentials carry no country (soax).
        q = q.where(M.Proxy.geo.in_(geos) | M.Proxy.geo.is_(None))
    if exclude_ids:
        q = q.where(M.Proxy.id.notin_(exclude_ids))
    return q


def _claim_locked(db: Session, proxy_id: str, worker_id: str, is_pg: bool) -> tuple[M.Proxy, str] | None:
    """Atomically re-check capacity and create the lease for ONE already-probed-healthy proxy,
    guarding against a concurrent caller having grabbed it during the (deliberately unlocked, see
    lease_proxy's docstring) probe window. Returns None if it lost that race -- caller tries the
    next candidate."""
    q = select(M.Proxy).where(M.Proxy.id == proxy_id)
    if is_pg:
        q = q.with_for_update(skip_locked=True)
    proxy = db.scalar(q)
    if proxy is None:
        return None   # SKIP LOCKED found it locked by someone else right now, or it's gone
    if _active_lease_counts(db).get(proxy.id, 0) >= proxy.max_sessions:
        return None
    now = datetime.now(timezone.utc)
    proxy.last_used_at = now          # advance the pool cursor -- see _candidate_query
    db.add(M.ProxyLease(proxy_id=proxy.id, worker_id=worker_id, leased_at=now))
    db.commit()
    return proxy, conn_url(proxy)


def lease_proxy(db: Session, worker_id: str, provider: str, *, kind: str | None = None,
                exclude_ids: set[str] | None = None, health_check=None,
                max_probes: int = 5, geos: set[str] | None = None) -> tuple[M.Proxy, str]:
    """Lease a healthy proxy of `provider` (e.g. 'soax' or 'webshare') with free capacity. Returns
    (proxy, conn_url). Raises ProxyLeaseError if the pool is exhausted. `exclude_ids` (P3, proxy
    rotation): a worker asking the Hub to ROTATE off a proxy must not just get handed the same one
    back -- without this, a still-has-capacity proxy sorted first by score/created_at is exactly what
    a rotation request would otherwise re-lease, defeating the point of asking for a fresh one.

    `geos` restricts selection to those exit countries (PROXY_GEOS). Login success against this
    portal varies ~8x by geo -- DE 23.7% / IT 23.4% / PT 10.8% / NL 10.6% / ES 8.1% / LU 4.9% /
    CZ 2.9%, measured over 1392 real attempts on 2026-08-18 -- and the pool was being spent almost
    backwards, with CZ taking the most attempts at the worst rate. A proxy whose geo is unknown
    (NULL) is never filtered out.

    `health_check` (P4, operator-specified 2026-08-13): an optional `callable(conn_url) ->
    (ok: bool, detail: str)` probed against each candidate BEFORE it's claimed -- a live
    check-then-lease retry loop ("select a proxy node ... check healthy against the real portal ...
    if unhealthy give it up and select another, until healthy"). Deliberately does NOT hold a DB row
    lock/transaction open across the network probe: probes can take several seconds each and this
    function is called concurrently across a whole fleet launch, so holding `FOR UPDATE` across that
    would tie up a DB connection from the pool for far too long and serialize concurrent leasers
    against each other for no reason. Candidates are read WITHOUT a lock, probed outside any open
    transaction, and only the WINNING candidate is re-locked (`_claim_locked`, single row) to guard
    against a concurrent caller grabbing it in the meantime. Each probed candidate's
    is_healthy/health/score is committed immediately -- even on failure, even if this call
    ultimately raises -- so the result is visible to every other concurrent lease_proxy call right
    away, not thrown away. `max_probes` bounds how many dead candidates one call will try before
    giving up, so a mostly-dead pool doesn't turn a single lease request into a probe of the whole
    pool. When `health_check` is None (the default), behavior is unchanged from before P4: a single
    DB-only, row-locked claim of the first eligible candidate."""
    is_pg = db.get_bind().dialect.name == "postgresql"

    if health_check is None:
        counts = _active_lease_counts(db)
        q = _candidate_query(provider, kind, exclude_ids, geos)
        if is_pg:
            q = q.with_for_update(skip_locked=True)
        for proxy in db.scalars(q):
            if counts.get(proxy.id, 0) < proxy.max_sessions:
                now = datetime.now(timezone.utc)
                proxy.last_used_at = now      # advance the pool cursor -- see _candidate_query
                db.add(M.ProxyLease(proxy_id=proxy.id, worker_id=worker_id, leased_at=now))
                db.commit()
                return proxy, conn_url(proxy)
        raise ProxyLeaseError(f"no free {provider} proxy available (pool exhausted)")

    from .health import apply_result

    counts = _active_lease_counts(db)
    q = _candidate_query(provider, kind, exclude_ids, geos)   # no lock -- see docstring
    probed = 0
    tried_ids: list[str] = []
    for proxy in db.scalars(q):
        if counts.get(proxy.id, 0) >= proxy.max_sessions:
            continue
        if probed >= max_probes:
            break
        probed += 1
        tried_ids.append(proxy.id)
        ok, detail = health_check(conn_url(proxy))
        apply_result(proxy, ok, detail)
        # Stamped on the ATTEMPT, not just on success: a candidate that just failed its probe must
        # rotate to the BACK of the queue. Otherwise it stays at the head and the next lease probes
        # the very same dead proxies again -- the reason "no healthy proxy after probing 5
        # candidate(s)" kept naming an identical five UUIDs.
        proxy.last_used_at = datetime.now(timezone.utc)
        db.commit()
        if not ok:
            continue
        claimed = _claim_locked(db, proxy.id, worker_id, is_pg)
        if claimed is not None:
            return claimed
        # lost the claim race to a concurrent leaser -- try the next candidate
    raise ProxyLeaseError(
        f"no healthy {provider} proxy available after probing {probed} candidate(s) "
        f"({', '.join(tried_ids) or 'none eligible'})"
    )


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


def release_worker_resources(db: Session, worker_id: str, bus=None) -> None:
    """A worker reaching ANY end-of-life state -- reaped, stopped, or a plain normal terminal event
    (booked/discovered/dry_run/warmed/failed/stopped) -- must never silently keep holding a proxy
    lease or a slot lease forever. Before this existed, only the scheduler's dead/stall/runaway reap
    paths called it (as `Scheduler._release_worker_resources`, now a thin wrapper around this); a
    worker that ended normally through apps/hub/events.py's `terminal` handler leaked its lease
    permanently, with `Proxy.max_sessions` defaulting to 2 meaning a handful of ordinary
    registrations/bookings could exhaust the pool for everyone. Shared here (libs/, not apps/hub/
    scheduler.py) so both the scheduler's reap paths and the event consumer's normal-termination
    path call the exact same cleanup, not two copies that can drift."""
    from libs.domain.enums import SlotLeaseOutcome, SlotStatus

    release_proxy(db, worker_id)
    for lease in db.scalars(select(M.SlotLease).where(
            M.SlotLease.worker_id == worker_id, M.SlotLease.released_at.is_(None))).all():
        lease.released_at = datetime.now(timezone.utc)
        # Record WHY. This set released_at and left outcome NULL, so a worker that ended while
        # holding a lease produced a row indistinguishable from one still in flight -- invisible to
        # every outcome-based query and report. Found by auditing a real run: 2 of its leases had no
        # outcome at all. EXPIRED is the honest label: the worker went away without using it, which
        # is the same meaning /start's retirement already gives the column.
        lease.outcome = SlotLeaseOutcome.EXPIRED
        slot = db.get(M.Slot, lease.slot_id)
        if slot and slot.status == SlotStatus.LEASED:
            slot.status = SlotStatus.AVAILABLE
    db.commit()
    if bus is not None:
        bus.delete_worker_stream(worker_id)

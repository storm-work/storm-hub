"""Proxy leasing against the DB pools.

`lease_proxy` picks a healthy, active proxy of the requested provider that still has capacity
(active leases < max_sessions), records a `proxy_leases` row, and returns (proxy, connection_url).
Under Postgres it locks the chosen row `FOR UPDATE SKIP LOCKED` so concurrent leasers never grab the
same slot; on SQLite (tests) the lock is skipped. `release_proxy` frees the lease.
"""
from __future__ import annotations

import random
import re
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from libs.db import models as M

# How many extra candidates one lease may try when it keeps LOSING claim races. A lost race is not
# evidence the pool is bad -- it means a concurrent leaser got there first -- so it must not be
# charged against `max_probes`, which exists to bound a mostly-DEAD pool. It still costs a browser
# probe, though, so the total is capped at max_probes * this.
_LOST_RACE_ALLOWANCE = 3

# Candidates are shuffled within a window of this size (see _spread_candidates). Big enough that
# concurrent leasers rarely pick the same row, small enough that selection stays essentially
# least-recently-used.
_CANDIDATE_SPREAD = 24


class ProxyLeaseError(RuntimeError):
    pass


def _spread_window(n_candidates: int, max_probes: int) -> int:
    """How wide the shuffle window may be for a pool of this size.

    Bounded by a FRACTION of the pool, because shuffling must never span the whole thing. The LRU
    ordering carries a second guarantee besides fairness: a proxy that just failed its probe was
    stamped to the back, so the next lease does not retry it. That only holds while the window is
    small relative to the pool -- shuffle everything and the just-failed proxy is as likely to come
    first as anything else, which is precisely the behaviour the cursor exists to prevent.

    So: a 6-row pool gets window 1 (strict LRU, no shuffling, rotation guarantee intact), and a
    600-row pool gets the full spread. Both invariants hold at both ends.
    """
    return max(1, min(_CANDIDATE_SPREAD, n_candidates // 4, max(1, max_probes * 4)))


def _spread_candidates(rows, window: int):
    """LRU order, shuffled within a sliding window -- decorrelates CONCURRENT leasers.

    `_candidate_query`'s last_used_at ordering is a cursor, and a cursor only helps leases that
    happen one after another: it advances when a probe commits. A whole fleet launching at once
    reads the identical head of the queue before any of them has stamped anything, so all N workers
    walk the SAME candidates in the SAME order and pile onto the same rows -- each one probing
    (a real headless browser nav, seconds long) only to lose the claim to whoever committed first.

    Confirmed live 2026-08-20: 4 of 9 workers failed to launch with "no healthy proxy after probing
    5 candidate(s)" naming an identical five UUIDs, every one of which was is_healthy=True with
    health='ok' recorded during that very launch window. They were healthy; the workers just kept
    losing the race for them.

    Shuffling within a window keeps selection least-recently-used in aggregate (the window advances
    through the LRU-ordered stream) while making it very unlikely that two simultaneous leasers try
    the same row first. Deliberately a generator over the lazy stream, not a LIMIT: a pool whose
    head is entirely at capacity must still fall through to the rest of the pool, as before.

    `window` comes from _spread_window -- see there for why it must stay a fraction of the pool.
    """
    buf: list = []
    for row in rows:
        buf.append(row)
        if len(buf) >= window:
            random.shuffle(buf)
            yield from buf
            buf = []
    if buf:
        random.shuffle(buf)
        yield from buf


def _inject_geo(credentials_ref: str, provider: str, geo: str) -> str:
    """Put a country into a credential, in that provider's own format.

    The pools carry NO country as of the 2026-08-22 regeneration -- a geo baked into 30,000 rows can
    only be changed by regenerating them, so the country belongs at LEASE time instead. But wiping
    it shipped without this half, and the consequence was silent: _candidate_query never excludes a
    NULL geo (deliberately -- a filter must not empty the pool), and after the regeneration EVERY
    row is NULL, so `PROXY_GEOS=DE` matched all 30,000 proxies. Geo targeting looked configured and
    did nothing.

    That matters more than it sounds: exit geo is the single biggest measured lever on login
    success -- DE/IT ~23% against CZ ~2.9%.

    Formats differ, so this cannot be generic:
      soax      the country is a parameter:  package-335959-country-de-sessionid-...
      webshare  the country is in the USERNAME: sodade2026-DE-17  (verified against the live
                account 2026-08-22, alongside the no-country sodade2026-17 form)
    Returns the credential unchanged if it already carries a country, or if the provider is unknown.
    """
    cred = credentials_ref or ""
    if not geo or not cred:
        return cred
    cc = geo.strip().lower()
    if provider == "soax":
        if "-country-" in cred:
            return cred
        return re.sub(r"^(package-\d+)", r"\1-country-" + cc, cred, count=1)
    if provider == "webshare":
        user, sep, pw = cred.partition(":")
        if not sep:
            return cred
        m = re.match(r"^(.*?)-(\d+)$", user)          # sodade2026-17 -> sodade2026-DE-17
        if not m:
            return cred
        return f"{m.group(1)}-{cc.upper()}-{m.group(2)}{sep}{pw}"
    return cred


def conn_url(proxy: M.Proxy, scheme: str = "http", geo: str | None = None) -> str:
    """Build a connection URL: http://user:pass@host:port (or http://host:port when no creds).

    `geo` injects a country into the credential per provider (see _inject_geo). Passing None keeps
    the credential exactly as stored, which is what every non-geo caller wants.
    """
    if proxy.credentials_ref:
        cred = _inject_geo(proxy.credentials_ref, str(proxy.provider), geo) if geo \
            else proxy.credentials_ref
        return f"{scheme}://{cred}@{proxy.endpoint}"
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
    # NOTE: is_healthy is NOT a filter. Every pool here is ROTATING -- a row is a session template,
    # not an IP. soax rows carry a sessionid whose exit address changes once sessionlength lapses or
    # the bound IP drops (bindttl), and webshare's gateway picks an exit per connection. So a probe
    # failure says "that exit was bad a moment ago", never "this row is bad": permanently excluding
    # the row throws away a good session template over a transient IP, and the only recovery that
    # actually works for a rotating pool is ROTATION -- which the LRU ordering below already gives.
    #
    # It was a filter, and it cost 639 rows at one point (a Hub-side prober running with
    # anti-detection off condemned a third of the pool). That specific bug is fixed, but the
    # mechanism was wrong for this pool shape regardless of how well-calibrated the probe is.
    # health/score are still recorded, and still order ties below, so a consistently-bad row sinks
    # without being cut.
    q = (
        select(M.Proxy)
        .where(M.Proxy.provider == provider, M.Proxy.active.is_(True))
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


def _claim_locked(db: Session, proxy_id: str, worker_id: str, is_pg: bool,
                  geo: str | None = None) -> tuple[M.Proxy, str] | None:
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
    return proxy, conn_url(proxy, geo=geo)


def lease_proxy(db: Session, worker_id: str, provider: str, *, kind: str | None = None,
                exclude_ids: set[str] | None = None, health_check=None,
                max_probes: int = 5, geos: set[str] | None = None,
                quick_screen=None) -> tuple[M.Proxy, str]:
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
    # Explicit, not auto-defaulted: a caller that injects a fake health_check is describing a fake
    # environment, and silently adding a REAL network probe underneath it would make every such test
    # reach the portal. Production wires both tiers at the call site (apps/hub).
    _quick_screen = quick_screen
    # Pick ONE country for this lease and use it for BOTH the health probe and the URL handed back,
    # so we screen the exit we will actually use. Random rather than round-robin: leases are issued
    # concurrently across a fleet, and a shared cursor would need locking to mean anything.
    geo = random.choice(sorted(geos)) if geos else None

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
                return proxy, conn_url(proxy, geo=geo)
        raise ProxyLeaseError(f"no free {provider} proxy available (pool exhausted)")

    from .health import apply_result

    counts = _active_lease_counts(db)
    q = _candidate_query(provider, kind, exclude_ids, geos)   # no lock -- see docstring
    unhealthy = 0     # candidates that FAILED their probe -- the pool-badness signal max_probes bounds
    lost_races = 0    # healthy candidates a concurrent leaser claimed first -- NOT pool badness
    probes = 0        # browser probes actually run -- the real cost ceiling
    tried_ids: list[str] = []
    max_attempts = max_probes * _LOST_RACE_ALLOWANCE
    # Counted, not guessed: the shuffle window has to stay a fraction of the pool or it defeats the
    # LRU rotation guarantee (see _spread_window). Cheap -- a COUNT over the same indexed filters.
    n_candidates = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    for proxy in _spread_candidates(db.scalars(q), _spread_window(n_candidates, max_probes)):
        if counts.get(proxy.id, 0) >= proxy.max_sessions:
            continue
        # Only a FAILED PROBE counts against max_probes. Losing a claim race used to consume the
        # same budget, so under a concurrent fleet launch a worker could burn all 5 of its probes on
        # candidates that were perfectly healthy and then report "no healthy proxy available" --
        # blaming the pool for what was really contention. Confirmed live 2026-08-20 (4 of 9 workers
        # stuck at READY, every named candidate is_healthy=True). Lost races get their own, larger
        # allowance because retrying one is likely to succeed; `probes` still caps the total cost.
        if unhealthy >= max_probes or probes >= max_attempts:
            break
        probes += 1
        tried_ids.append(proxy.id)
        url = conn_url(proxy, geo=geo)
        # TIER 1 -- cheap raw reachability, no browser. storm-auto's pattern: screen the obvious
        # dead exits for ~4s each instead of paying ~20-25s of Chromium to discover a socket will
        # not open. ~76% of our transient login failures are connectivity-level, so this is where
        # most of the probe budget was being burned. A raw check can only ever REJECT here; anything
        # that answers falls through to the authoritative browser probe below.
        if _quick_screen is not None:
            alive, why = _quick_screen(url)
            if not alive:
                apply_result(proxy, False, why)
                proxy.last_used_at = datetime.now(timezone.utc)
                db.commit()
                unhealthy += 1
                continue
        # TIER 2 -- the real verdict.
        ok, detail = health_check(url)
        apply_result(proxy, ok, detail)
        # Stamped on the ATTEMPT, not just on success: a candidate that just failed its probe must
        # rotate to the BACK of the queue. Otherwise it stays at the head and the next lease probes
        # the very same dead proxies again -- the reason "no healthy proxy after probing 5
        # candidate(s)" kept naming an identical five UUIDs.
        proxy.last_used_at = datetime.now(timezone.utc)
        db.commit()
        if not ok:
            unhealthy += 1
            continue
        claimed = _claim_locked(db, proxy.id, worker_id, is_pg, geo=geo)
        if claimed is not None:
            return claimed
        lost_races += 1   # healthy, but someone else committed first -- try the next candidate
    # Say WHICH of the two it was. "no healthy proxy" sent the last investigation looking at proxy
    # health for a contention bug, with the DB showing every named candidate as healthy.
    raise ProxyLeaseError(
        f"no {provider} proxy leased after {probes} probe(s): "
        f"{unhealthy} failed health, {lost_races} lost to a concurrent leaser "
        f"({', '.join(tried_ids[:5]) or 'none eligible'})"
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

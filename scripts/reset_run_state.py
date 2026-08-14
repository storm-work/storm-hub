#!/usr/bin/env python3
"""reset_run_state.py -- scoped recovery for state corrupted by a known-bad run.

Dry-run by default. Nothing is written without --confirm. Same house rule as
scripts/cleanup_checkpoints.py: recovery from a bad run is a reviewed, scoped, repeatable script,
never an ad-hoc SQL statement typed against production under time pressure (see the reconstruction
plan's F8 -- that reflex is what destroyed this project once already).

Two independent resets, both addressing measurements/state produced by code that has since been
fixed:

  --proxy-health   Clear is_healthy/health/score back to unprobed.
                   Every health verdict in the DB before 2026-08-14 was produced by a prober running
                   with the vendor's own env defaults (NAV_TIMEOUT_SCALE=1.0, anti-detect off)
                   because the Hub never applied its settings -- strictly harsher conditions than the
                   workers that would use those proxies. lease_proxy filters on is_healthy, so each
                   false negative permanently removed a good proxy from the pool.

  --slot-leases    Release unreleased SlotLeases and reopen the slots they were holding.
                   Recovers a pool sterilised by the lease runaway: _assign_slots never enforced
                   one-active-lease-per-worker, and BusSignals discarded `assign` commands while
                   leaving their leases behind, so a single worker could accumulate the entire
                   pool (observed: 120 leases across all 24 slots of one booking).

Run AFTER restarting the Hub onto fixed code -- otherwise the old code simply recreates the mess.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--proxy-health", action="store_true", help="reset all proxy health to unprobed")
    p.add_argument("--slot-leases", metavar="BOOKING",
                   help="release stranded slot leases for this booking NAME (or 'all')")
    p.add_argument("--confirm", action="store_true", help="actually write (default: dry run)")
    p.add_argument("--force", action="store_true",
                   help="proceed even while workers are RUNNING (they will fight the reset)")
    args = p.parse_args()
    if not (args.proxy_health or args.slot_leases):
        p.error("nothing to do -- pass --proxy-health and/or --slot-leases")

    from sqlalchemy import func, select

    from apps.hub.db import SessionLocal
    from libs.db import models as M
    from libs.domain import enums as E

    db = SessionLocal()
    try:
        running = db.scalars(select(M.Worker).where(M.Worker.status == E.WorkerStatus.RUNNING)).all()
        if running and not args.force:
            print(f"REFUSING: {len(running)} worker(s) are RUNNING and would immediately re-dirty "
                  f"this state.\nStop the booking first, or pass --force.", file=sys.stderr)
            return 2

        wrote = False
        if args.proxy_health:
            n = db.scalar(select(func.count()).select_from(M.Proxy).where(
                M.Proxy.health.isnot(None))) or 0
            leasable = db.scalar(select(func.count()).select_from(M.Proxy).where(
                M.Proxy.active.is_(True), M.Proxy.is_healthy.is_(True))) or 0
            total = db.scalar(select(func.count()).select_from(M.Proxy)) or 0
            print(f"proxy-health : {n} probed row(s) -> unprobed; leasable {leasable} -> {total}")
            if args.confirm:
                for pr in db.scalars(select(M.Proxy).where(M.Proxy.health.isnot(None))).all():
                    pr.is_healthy, pr.health, pr.score = True, None, 0.0
                wrote = True

        if args.slot_leases:
            q = select(M.SlotLease).join(M.Slot, M.Slot.id == M.SlotLease.slot_id).where(
                M.SlotLease.released_at.is_(None))
            if args.slot_leases != "all":
                bk = db.scalar(select(M.Booking).where(M.Booking.name == args.slot_leases))
                if not bk:
                    print(f"no booking named {args.slot_leases!r}", file=sys.stderr)
                    return 2
                q = q.where(M.Slot.booking_id == bk.id)
            leases = db.scalars(q).all()
            slot_ids = {le.slot_id for le in leases}
            by_worker: dict[str, int] = {}
            for le in leases:
                by_worker[le.worker_id] = by_worker.get(le.worker_id, 0) + 1
            print(f"slot-leases  : releasing {len(leases)} lease(s) across {len(slot_ids)} slot(s)")
            for wid, n in sorted(by_worker.items(), key=lambda kv: -kv[1])[:5]:
                print(f"               {wid[:8]} held {n}")
            if args.confirm:
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc)
                for le in leases:
                    le.released_at, le.outcome = now, E.SlotLeaseOutcome.EXPIRED
                db.flush()   # so the reopen check below sees the releases (autoflush is off)
                for slot in db.scalars(select(M.Slot).where(M.Slot.id.in_(slot_ids or [""]))).all():
                    if slot.status != E.SlotStatus.TAKEN:
                        slot.status = E.SlotStatus.AVAILABLE
                wrote = True

        if wrote:
            db.commit()
            print("\nWROTE.")
        else:
            print("\nDRY RUN -- nothing written. Re-run with --confirm to apply.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

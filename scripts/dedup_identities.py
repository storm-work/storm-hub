#!/usr/bin/env python
"""One-off cleanup: collapse duplicate identities (same passport) into one canonical row.

Policy (operator rulings 2026-10-02):
  * KEY            = normalized passport_no (UPPER + strip). Every identity in this DB has one.
  * VERIFIED       = READY_SUB_ACCOUNT_STATUSES {VERIFIED, LOGIN_OK, HELD} -- never wiped.
  * LIVE ROWS      = identities allocated to an ACTIVE booking (IdentityAllocation.released_at IS
                     NULL) are UNTOUCHABLE: not deleted, not merged, not re-prioritised. Left as a
                     separate row to be collapsed in a later pass once their booking stops.
  * MERGE          = for each passport group, pick ONE canonical among the NON-allocated ("free")
                     rows -- the one with the most READY sub-accounts, tie-broken by priority then
                     age (oldest first) -- then REPARENT every other free row's children onto it
                     (sub_accounts, identity_allocations, identity_events, booking_identities,
                     workers, booked_appointments -- the six FKs into identities.id) and DELETE the
                     emptied free duplicates. Reparent-then-delete is the only FK-safe order, and it
                     also means an "erase" (a free row with 0 ready subs) is just a merge that
                     carried nothing worth keeping.
  * PRIORITY       = canonical -> HIGH when it ends up with >=1 READY sub-account (a real,
                     in-demand person), else NORMAL (never elevate an empty survivor above a
                     verified/allocated sibling). Allocated rows keep their own priority.

Nothing is created. The only deletions are duplicate Identity ROWS whose children have first been
reparented onto the survivor -- no sub-account, booking, worker or appointment is ever dropped.

USAGE (run on the Hub host, in the repo, with the venv):
    .venv/bin/python scripts/dedup_identities.py            # DRY RUN: prints the full plan, writes nothing
    .venv/bin/python scripts/dedup_identities.py --apply    # APPLY inside one transaction

Review the dry-run output first. --apply commits once at the end; any error rolls the whole thing back.
"""
from __future__ import annotations

import sys
from collections import defaultdict

from sqlalchemy import update

from apps.hub.db import SessionLocal
import libs.db.models as M
from libs.domain.enums import READY_SUB_ACCOUNT_STATUSES
from libs.domain import priority as P

# The six tables that carry a ForeignKey("identities.id") -> every child that must move with a merge.
_CHILD_FKS = [
    M.SubAccount, M.IdentityAllocation, M.IdentityEvent,
    M.BookingIdentity, M.Worker, M.BookedAppointment,
]


def _norm(p: str | None) -> str:
    return (p or "").strip().upper()


def main(apply: bool) -> int:
    db = SessionLocal()
    try:
        idents = db.query(M.Identity).all()
        active_alloc = {
            a.identity_id for a in db.query(M.IdentityAllocation)
            .filter(M.IdentityAllocation.released_at.is_(None)).all()
        }
        ready_by_ident: dict[str, int] = defaultdict(int)
        for sa in db.query(M.SubAccount).all():
            if sa.status in READY_SUB_ACCOUNT_STATUSES:
                ready_by_ident[sa.identity_id] += 1

        by_pp: dict[str, list[M.Identity]] = defaultdict(list)
        for i in idents:
            key = _norm(i.passport_no)
            if key:
                by_pp[key].append(i)

        dup_groups = {k: v for k, v in by_pp.items() if len(v) > 1}
        print(f"identities={len(idents)} passports={len(by_pp)} "
              f"duplicate_groups={len(dup_groups)}  mode={'APPLY' if apply else 'DRY-RUN'}")
        print("-" * 78)

        planned_deletes = 0
        planned_merges = 0
        skipped_all_allocated = 0

        for key, rows in sorted(dup_groups.items()):
            allocated = [r for r in rows if r.id in active_alloc]
            free = [r for r in rows if r.id not in active_alloc]
            if not free:
                skipped_all_allocated += 1
                print(f"[{key}] {len(rows)} rows -- ALL allocated to an active booking; skipped")
                continue

            # canonical = the richest free row: most ready subs, then highest priority, then oldest.
            free.sort(key=lambda r: (ready_by_ident.get(r.id, 0), r.priority,
                                     -(r.created_at.timestamp() if r.created_at else 0.0)),
                      reverse=True)
            canonical = free[0]
            victims = free[1:]
            group_max_prio = max(r.priority for r in rows)
            canon_ready = ready_by_ident.get(canonical.id, 0)
            new_prio = P.HIGH if canon_ready > 0 else P.NORMAL

            print(f"[{key}] {len(rows)} rows ({len(allocated)} allocated, left as-is) "
                  f"name={canonical.full_name!r}")
            print(f"   KEEP canonical id={canonical.id[:8]} ready_subs={canon_ready} "
                  f"prio {canonical.priority}->{new_prio}")
            for a in allocated:
                print(f"   LEAVE allocated id={a.id[:8]} ready_subs={ready_by_ident.get(a.id,0)} "
                      f"prio={a.priority} (untouched)")

            for v in victims:
                child_counts = {}
                for Model in _CHILD_FKS:
                    n = db.query(Model).filter(Model.identity_id == v.id).count()
                    if n:
                        child_counts[Model.__tablename__] = n
                print(f"   MERGE victim id={v.id[:8]} ready_subs={ready_by_ident.get(v.id,0)} "
                      f"prio={v.priority} -> reparent {child_counts or 'nothing'} then DELETE")
                if apply:
                    for Model in _CHILD_FKS:
                        db.execute(update(Model).where(Model.identity_id == v.id)
                                   .values(identity_id=canonical.id))
                    db.delete(v)
                planned_merges += 1
                planned_deletes += 1

            if apply and canonical.priority != new_prio:
                canonical.priority = new_prio

        print("-" * 78)
        print(f"groups needing work: {len(dup_groups) - skipped_all_allocated} | "
              f"all-allocated groups skipped: {skipped_all_allocated}")
        print(f"duplicate rows to delete: {planned_deletes} (each after reparenting its children)")

        if apply:
            db.commit()
            print("APPLIED and committed.")
        else:
            db.rollback()
            print("DRY-RUN only -- nothing written. Re-run with --apply to commit.")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv[1:]))

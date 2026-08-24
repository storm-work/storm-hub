#!/usr/bin/env python3
"""Retire identities the portal has already told us hold a future appointment.

THE PROBLEM. When a warm-up's ScheduleController POST is rejected with

    "This applicant already has an application registered with a future schedule.
     You do not need to request a new application"

that identity can never warm up again for as long as that appointment stands. But nothing recorded
it: the identity stayed status=active with no BookedAppointment row, so it was re-attached run after
run, each time consuming a worker slot, a proxy lease and a LOGIN -- the scarcest resource in the
system -- to fail in exactly the same way. Confirmed 2026-08-23 on Test 2032 for CLAUDIA BORGES and
SOFIA RODRIGUES ANDRADE.

From now on workers report this live (ops_live emits `already_booked`, events.py retires the
identity). This script is the BACKFILL for identities that already hit it before that existed: it
reads the evidence we already have -- the Redis event stream -- and never touches the portal, so it
costs no logins.

It deliberately does NOT create a BookedAppointment. We do not know when the appointment is, and a
fabricated booking record is worse than a missing one; the identity is simply retired out of the
attachable pool, with an IdentityEvent carrying the portal's own wording.

Same house rule as the other cleanup scripts (F8): dry-run by default, scoped to one thing.

    python scripts/mark_already_booked.py                # dry run: who would be retired
    python scripts/mark_already_booked.py --confirm
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_MARKERS = (
    "already has an application registered",
    "already has an application",
    "já tem um pedido registado",
    "ja tem um pedido registado",
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--confirm", action="store_true", help="actually retire (default: dry run)")
    args = ap.parse_args()

    from datetime import datetime, timezone

    from sqlalchemy import select

    from apps.hub.db import SessionLocal
    from apps.hub.main import _bus
    from libs.db import models as M
    from libs.domain.enums import IdentityEvent, IdentityStatus
    from libs.domain.state_machines import IllegalTransition, identity_transition

    # Scan the event stream for the portal's own rejection text, and map it back to the identity
    # via the worker that logged it.
    hits: dict[str, str] = {}          # worker_id -> the portal's wording
    for _id, f in _bus.r.xrange("evt", count=50000):
        data = f.get("data") or "{}"
        try:
            p = json.loads(data)
        except Exception:
            continue
        text = (p.get("text") or "").lower()
        if p.get("worker_id") and any(m in text for m in _MARKERS):
            hits.setdefault(p["worker_id"], (p.get("text") or "")[:300])

    db = SessionLocal()
    try:
        by_identity: dict[str, tuple] = {}
        for wid, detail in hits.items():
            w = db.get(M.Worker, wid)
            if w is None:
                continue
            ident = db.get(M.Identity, w.identity_id)
            if ident is not None:
                by_identity[ident.id] = (ident, w, detail)

        if not by_identity:
            print("no identities found with a portal 'already has an appointment' rejection")
            print("(the event stream is capped -- older evidence may have rolled away)")
            return 0

        print(f"{len(by_identity)} identity/identities the portal says already hold an appointment:\n")
        todo = []
        for ident, w, detail in by_identity.values():
            appt = db.scalar(select(M.BookedAppointment).where(
                M.BookedAppointment.identity_id == ident.id))
            skip = ident.status != IdentityStatus.ACTIVE
            print(f"  {ident.full_name[:28]:30s} status={str(ident.status):9s} "
                  f"our_booking_record={'yes' if appt else 'NO':3s}"
                  f"{'   (already non-active -- skipping)' if skip else ''}")
            print(f"      portal said: {detail[:110]}")
            if not skip:
                todo.append((ident, w, detail))

        if not todo:
            print("\nnothing to change")
            return 0
        if not args.confirm:
            print(f"\n[DRY RUN -- {len(todo)} would be retired to BOOKED. Pass --confirm.]")
            return 0

        for ident, w, detail in todo:
            db.add(M.IdentityEvent(
                identity_id=ident.id, sub_account_id=w.sub_account_id, booking_id=w.booking_id,
                event=IdentityEvent.BOOKED, step="already_booked_at_portal",
                detail=detail[:500], ts=datetime.now(timezone.utc),
            ))
            try:
                ident.status = identity_transition(ident.status, IdentityStatus.BOOKED)
            except IllegalTransition:
                pass
        db.commit()
        print(f"\nretired {len(todo)} identity/identities to BOOKED "
              f"(no appointment rows invented).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

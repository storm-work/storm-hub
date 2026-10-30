#!/usr/bin/env python3
"""Shuffle the real identities into the experiment arms by LABELLING them (group_name), so the dashboard's
attach page can select a whole arm with its group filter. Changes nothing but group_name.

    .venv/bin/python -m scripts.window_split_identities --from-group + --seed 1030           # dry run
    .venv/bin/python -m scripts.window_split_identities --from-group + --seed 1030 --apply   # write
    ... --undo-to +     # put every labelled identity back to the old label

Labels written: ARM-main, ARM-sched, ARM-short, ARM-long, ARM-blind (sizes 50/7/7/7/7; extra stay unlabelled).
Only ACTIVE REAL identities with a VERIFIED sub-account, no booked appointment and no live allocation.
"""
import argparse
import random

from sqlalchemy import select, text

from apps.hub.db import SessionLocal
from libs.db import models as M

SIZES = (("ARM-main", 50), ("ARM-sched", 7), ("ARM-short", 7), ("ARM-long", 7), ("ARM-blind", 7))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-group", default="+")
    ap.add_argument("--seed", type=int, default=1030)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--undo-to")
    a = ap.parse_args()
    db = SessionLocal()
    try:
        if a.undo_to is not None:
            rows = db.scalars(select(M.Identity).where(M.Identity.group_name.like("ARM-%"))).all()
            for i in rows:
                i.group_name = a.undo_to
            db.commit(); print(f"restored {len(rows)} identities to group {a.undo_to!r}"); return 0
        ids = [r[0] for r in db.execute(text("""
            select i.id from identities i where i.type='REAL' and i.status='ACTIVE' and i.group_name = :g
              and exists (select 1 from sub_accounts s where s.identity_id=i.id and s.status='VERIFIED')
              and not exists (select 1 from booked_appointments ba where ba.identity_id=i.id)
              and not exists (select 1 from identity_allocations al join bookings b on b.id=al.booking_id
                  where al.identity_id=i.id and al.released_at is null
                    and b.status in ('SCHEDULED','WARMING','ARMED','RUNNING'))
            order by i.id"""), {"g": a.from_group}).all()]
        random.Random(a.seed).shuffle(ids)
        need = sum(n for _, n in SIZES)
        print(f"{len(ids)} eligible in group {a.from_group!r}; arms need {need}")
        pos = 0
        for label, n in SIZES:
            chunk = ids[pos:pos + n]; pos += n
            print(f"  {label:10} {len(chunk)}")
            if a.apply:
                for iid in chunk:
                    db.get(M.Identity, iid).group_name = label
        print(f"  left unlabelled: {max(0, len(ids) - need)}")
        if a.apply:
            db.commit(); print("APPLIED")
        else:
            print("(dry run -- add --apply)")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

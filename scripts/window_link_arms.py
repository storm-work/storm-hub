#!/usr/bin/env python3
"""Full-mesh slot feed between the four non-blind experiment bookings: each one lists the other three as
feeders, so a slot found by ANY booking's scouts reaches all the others (W-blind stays out: its seeded grid
is never copied out and it takes nothing in).

    .venv/bin/python -m scripts.window_link_arms                # link
    .venv/bin/python -m scripts.window_link_arms --check        # print what is linked, change nothing

The dashboard has no field for discovery_feeder_booking_ids, so this sets it (names -> ids) directly.
See Scheduler._mirror_feeder_slots (one-directional per link; the mesh is 12 links).
"""
import argparse

from sqlalchemy import select

from apps.hub.db import SessionLocal
from libs.db import models as M

MESH = ("W-main", "W-sched", "W-short", "W-long")
OUT = ("W-blind",)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    db = SessionLocal()
    try:
        by_name = {b.name: b for b in db.scalars(select(M.Booking).where(
            M.Booking.name.in_((*MESH, *OUT)), M.Booking.archived_at.is_(None)))}
        missing = [n for n in MESH if n not in by_name]
        if missing:
            print(f"ERROR: no booking named {missing}"); return 1
        for name in (*MESH, *OUT):
            b = by_name.get(name)
            if b is None:
                print(f"  {name:8} missing"); continue
            want = sorted(by_name[o].id for o in MESH if o != name) if name in MESH else []
            have = sorted(b.discovery_feeder_booking_ids or [])
            if not a.check and have != want:
                b.discovery_feeder_booking_ids = want
            names = [n for n, x in by_name.items() if x.id in want]
            print(f"  {name:8} posto={b.posto_id} status={b.status} feeders <- {sorted(names)}"
                  f"{'' if have == want else ('  (would change)' if a.check else '  (set)')}")
        if not a.check:
            db.commit()
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

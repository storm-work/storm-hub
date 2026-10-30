#!/usr/bin/env python3
"""Link the experiment arms to the control: every arm booking gets `W-main` as its slot feeder.

The dashboard has no field for discovery_feeder_booking_ids, so this sets it (names -> ids) directly:

    .venv/bin/python -m scripts.window_link_arms                # W-sched, W-short, W-long <- W-main
    .venv/bin/python -m scripts.window_link_arms --check        # print what is linked, change nothing

Arms never feed W-main (one direction; a slot found by W-main's scouts is mirrored into each arm's pool
every scheduler tick -- see Scheduler._mirror_feeder_slots). W-blind is NOT linked: it runs off the
seeded pool and must stay independent.
"""
import argparse

from sqlalchemy import select

from apps.hub.db import SessionLocal
from libs.db import models as M

CONTROL = "W-main"
ARMS = ("W-sched", "W-short", "W-long")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    db = SessionLocal()
    try:
        by_name = {b.name: b for b in db.scalars(select(M.Booking).where(
            M.Booking.name.in_((CONTROL, *ARMS, "W-blind")), M.Booking.archived_at.is_(None)))}
        ctl = by_name.get(CONTROL)
        if ctl is None:
            print(f"ERROR: no booking named {CONTROL!r}"); return 1
        for name in (*ARMS, "W-blind"):
            b = by_name.get(name)
            if b is None:
                print(f"  {name:8} missing"); continue
            want = [ctl.id] if name in ARMS else []
            have = list(b.discovery_feeder_booking_ids or [])
            if not a.check and have != want:
                b.discovery_feeder_booking_ids = want
            print(f"  {name:8} posto={b.posto_id} status={b.status} feeders: {have} -> {want}"
                  f"{'' if have == want else ('  (would change)' if a.check else '  (set)')}")
        if not a.check:
            db.commit()
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

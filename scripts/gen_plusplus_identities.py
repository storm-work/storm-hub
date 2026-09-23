#!/usr/bin/env python3
"""Create a '++' twin of every BOOKED identity: identical fields, passport + '++', comment '++'.

Operator request 2026-09-20. A person who has already booked cannot book again on the same
traveldoc, so a twin identity with a distinct passport ('PA550330' -> 'PA550330++') lets the same
person be re-registered and re-booked. Everything else is copied verbatim (name, birthdate, gender,
nationality, type, target_posto, priority); only passport_no and comment change.

Identity rows ONLY -- no sub-accounts. Those are generated + registered separately (a copied
username would collide). Idempotent: a twin whose '++' passport already exists is skipped.

    python scripts/gen_plusplus_identities.py            # dry run
    python scripts/gen_plusplus_identities.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv                       # noqa: E402
from sqlalchemy import create_engine, select          # noqa: E402
from sqlalchemy.orm import Session                    # noqa: E402

from libs.db import models as M                        # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--suffix", default="++")
    ap.add_argument("--comment", default="++")
    ap.add_argument("--env", default="/srv/projects/storm-hub/.env")
    args = ap.parse_args()
    load_dotenv(args.env)

    engine = create_engine(os.environ["DATABASE_URL"])
    with Session(engine) as db:
        # distinct booked identities, each with a passport to suffix
        booked_ids = [r[0] for r in db.execute(
            select(M.BookedAppointment.identity_id).distinct()).all()]
        sources = db.scalars(select(M.Identity).where(
            M.Identity.id.in_(booked_ids))).all()
        existing_pass = {p for (p,) in db.execute(
            select(M.Identity.passport_no).where(M.Identity.passport_no.is_not(None))).all()}

        planned, skipped_no_pass, skipped_exists = [], [], []
        for src in sorted(sources, key=lambda s: s.full_name or ""):
            if not src.passport_no:
                skipped_no_pass.append(src.full_name)
                continue
            new_pass = src.passport_no + args.suffix
            if new_pass in existing_pass:
                skipped_exists.append(new_pass)
                continue
            planned.append((src, new_pass))
            existing_pass.add(new_pass)   # guard against a duplicate base within this run

        print(f"booked identities: {len(sources)}")
        print(f"twins to create:   {len(planned)}")
        if skipped_exists:
            print(f"skipped (twin already exists): {len(skipped_exists)}")
        if skipped_no_pass:
            print(f"skipped (source has no passport): {skipped_no_pass}")
        print()
        for src, new_pass in planned[:8]:
            print(f"   {src.full_name[:26]:28} {src.passport_no} -> {new_pass}  "
                  f"({src.type}, {src.nationality}, comment={args.comment!r})")
        if len(planned) > 8:
            print(f"   ... and {len(planned) - 8} more")

        if not args.commit:
            print("\nDRY RUN -- re-run with --commit to create.")
            return 0

        for src, new_pass in planned:
            db.add(M.Identity(
                full_name=src.full_name, type=src.type, birthdate=src.birthdate,
                passport_no=new_pass, gender=src.gender, nationality=src.nationality,
                target_posto=src.target_posto, priority=src.priority,
                status="empty",             # no sub-accounts yet
                comment=args.comment,
            ))
        db.commit()
        print(f"\nCreated {len(planned)} '++' identit{'y' if len(planned) == 1 else 'ies'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

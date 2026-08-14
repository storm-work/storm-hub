#!/usr/bin/env python3
"""cleanup_checkpoints.py -- the ONLY sanctioned way to clear checkpoint files.

Why this exists (Phase F8): the project's original catastrophic data loss was a manual, unscoped
`rm -f`/`rm -rf` run under pressure during a live incident, against a directory that turned out not
to be as narrowly scoped as intended. This script exists to make that mistake structurally impossible
to repeat:

  - dry-run by default -- prints exactly what it would delete, deletes nothing, until you pass --confirm.
  - scoped ONLY to libs/engine/checkpoint.py's var/checkpoints/ directory -- it does not accept an
    arbitrary path argument, on purpose. If you need to clean something else, that's a different,
    separately-reviewed script, not a flag bolted onto this one.
  - every run is logged (what was found, what was [or would be] deleted).

Usage:
  python scripts/cleanup_checkpoints.py                       # dry run, age-based (default 7 days)
  python scripts/cleanup_checkpoints.py --max-age-days 14     # dry run, custom age
  python scripts/cleanup_checkpoints.py --confirm             # actually delete (age-based)
  python scripts/cleanup_checkpoints.py --active-only --confirm
      # delete any checkpoint whose identity_id is not currently allocated to an active booking
      # (queries the DB directly instead of a fixed age window)

Never run a manual `rm` against var/checkpoints/ instead of this script -- that reflex is exactly
what caused this project's original data loss. If this script can't do what you need, extend it
(with review), don't work around it.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from libs.engine.checkpoint import _CHECKPOINTS_DIR  # noqa: E402


def _active_identity_ids() -> set[str]:
    from sqlalchemy import select

    from apps.hub.db import SessionLocal
    from libs.db import models as M
    from libs.domain.enums import BOOKING_ACTIVE_STATUSES

    db = SessionLocal()
    try:
        return set(db.scalars(
            select(M.IdentityAllocation.identity_id)
            .join(M.Booking, M.Booking.id == M.IdentityAllocation.booking_id)
            .where(M.IdentityAllocation.released_at.is_(None),
                  M.Booking.status.in_(BOOKING_ACTIVE_STATUSES))
        ).all())
    finally:
        db.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--max-age-days", type=float, default=7.0,
                   help="delete checkpoints older than this many days (default 7). Ignored with --active-only.")
    p.add_argument("--active-only", action="store_true",
                   help="instead of age, delete any checkpoint whose identity is not currently "
                        "allocated to an active booking (queries the DB)")
    p.add_argument("--confirm", action="store_true",
                   help="actually delete. Without this flag, nothing is deleted -- dry run only.")
    args = p.parse_args()

    if not _CHECKPOINTS_DIR.exists():
        print(f"{_CHECKPOINTS_DIR} does not exist -- nothing to do.")
        return 0

    if args.active_only:
        active = _active_identity_ids()
        candidates = [f for f in _CHECKPOINTS_DIR.glob("*.json") if f.stem not in active]
        reason = f"identity not in {len(active)} active-booking allocation(s)"
    else:
        cutoff = time.time() - args.max_age_days * 86400
        candidates = [f for f in _CHECKPOINTS_DIR.glob("*.json") if f.stat().st_mtime < cutoff]
        reason = f"older than {args.max_age_days} day(s)"

    print(f"Scanning {_CHECKPOINTS_DIR} -- criterion: {reason}")
    if not candidates:
        print("Nothing matches. No action taken.")
        return 0

    print(f"{len(candidates)} checkpoint(s) match:")
    for f in candidates:
        age_days = (time.time() - f.stat().st_mtime) / 86400
        print(f"  {f.name}  (age: {age_days:.1f}d)")

    if not args.confirm:
        print(f"\nDRY RUN -- nothing deleted. Re-run with --confirm to actually delete these "
              f"{len(candidates)} file(s).")
        return 0

    removed = 0
    for f in candidates:
        try:
            f.unlink()
            removed += 1
        except OSError as e:
            print(f"  FAILED to delete {f.name}: {e}")
    print(f"\nDeleted {removed}/{len(candidates)} checkpoint(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

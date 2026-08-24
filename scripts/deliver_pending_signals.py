#!/usr/bin/env python3
"""Deliver slot_open/slot_found to RUNNING workers the scheduler never reached.

WHY THIS EXISTS (2026-08-21). The manual fire-slot-open endpoint does three things synchronously:
sets slot_open_fired_at, publishes the pub/sub broadcast, and flips the booking to RUNNING. It does
NOT deliver the per-worker command -- that is Scheduler._latch_signals' job, once per tick.

On 2026-08-21 the tick was starved: _hold_respawn calls _do_launch_worker INLINE, which calls
lease_proxy(health_check=check_proxy), and check_proxy launches a throwaway headless Chromium per
candidate (up to max_probes=5, ~20s each). check_proxy's own docstring warns that callers on a
shared thread "must dispatch this to a background thread/pool rather than call it inline, or every
other booking's event/tick handling stalls behind it" -- the Scheduler tick is named explicitly.
Result: three real bookings sat at RUNNING with slot_open_fired_at set, and not one scout was ever
told, so discovery never started.

This is the manual equivalent of one _latch_signals pass. It mirrors that method exactly, including
the ordering that matters: a worker is added to the notified set ONLY after its send returns, so a
failed send is retried rather than silently marked done (the confirmed 2026-08-07 bug).

Same house rule as the other scripts (F8): dry-run by default, scoped to one thing, no arbitrary
SQL. It sends ONLY to workers whose booking has already fired the signal -- it can never invent a
slot-open that the operator did not fire.

    python scripts/deliver_pending_signals.py                # dry run: who would be told
    python scripts/deliver_pending_signals.py --confirm      # actually send
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--confirm", action="store_true", help="actually send (default is a dry run)")
    ap.add_argument("--booking", default=None, help="limit to one booking id or name substring")
    args = ap.parse_args()

    from sqlalchemy import select

    from apps.hub.db import SessionLocal
    from apps.hub.main import _bus
    from libs.bus.names import notified_key
    from libs.db import models as M

    r = _bus.r
    db = SessionLocal()
    sent = total = 0
    try:
        bookings = db.scalars(select(M.Booking).where(M.Booking.status == "running")).all()
        if args.booking:
            bookings = [b for b in bookings
                        if args.booking == b.id or args.booking.lower() in b.name.lower()]
        if not bookings:
            print("no RUNNING bookings matched")
            return 1

        for b in bookings:
            print(f"\n=== {b.name}  [{b.status}] ===")
            for signal_name, fired_attr in (("slot_open", "slot_open_fired_at"),
                                            ("slot_found", "slot_found_fired_at")):
                if getattr(b, fired_attr) is None:
                    print(f"  {signal_name}: never fired for this booking -- skipping")
                    continue
                key = notified_key(b.id, signal_name)
                already = {a.decode() if isinstance(a, bytes) else a
                           for a in (r.smembers(key) or set())}
                workers = db.scalars(select(M.Worker).where(
                    M.Worker.booking_id == b.id, M.Worker.status == "running")).all()
                todo = [w for w in workers if w.id not in already]
                print(f"  {signal_name}: {len(workers)} running, {len(already)} already notified, "
                      f"{len(todo)} to deliver")
                for w in todo:
                    total += 1
                    if not args.confirm:
                        print(f"      would send -> {w.id[:8]} ({w.role}, substate={w.substate})")
                        continue
                    try:
                        _bus.send_command(w.id, "signal", name=signal_name)
                    except Exception as e:
                        print(f"      SEND FAILED {w.id[:8]}: {e}  (not marked; will retry)")
                        continue
                    # Mark ONLY after a confirmed send -- same ordering as _latch_signals.
                    r.sadd(key, w.id)
                    r.expire(key, 86400)
                    sent += 1
                    print(f"      sent -> {w.id[:8]} ({w.role})")
    finally:
        db.close()

    if args.confirm:
        print(f"\ndelivered {sent}/{total}")
    else:
        print(f"\nDRY RUN -- {total} deliveries pending. Re-run with --confirm to send.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Manually revert a fired slot-open and drop the scouts back to KEEPING -- runnable on the CURRENT
deployed Hub (before the /revert-slot-open endpoint ships).

It sends every RUNNING worker an `inactivate` signal (scouts leave discovery, back to holding),
clears the fired timestamps + the signal-latch 'already notified' sets + the manual-open flag, and
puts the booking back to WARMING. It CANNOT reset the running Hub's in-memory scout-pool object (that
lives inside the Hub process), but that is harmless: with slot-open cleared, the scheduler dispatches
no discovery, and the next /start or /fire-slot-open rebuilds the pool cleanly.

  cd /srv/projects/storm-hub && .venv/bin/python scripts/revert_slot_open.py "Booking 0925"
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from apps.hub.db import SessionLocal  # noqa: E402
from apps.hub.config import settings as _cfg  # noqa: E402
from libs.bus.bus import Bus, connect  # noqa: E402
from libs.bus.names import manual_open_key, notified_key  # noqa: E402


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "Booking 0925"
    d = SessionLocal()
    try:
        row = d.execute(text("select id, status, slot_open_fired_at from bookings "
                             "where name = :n order by created_at desc limit 1"), {"n": name}).first()
        if not row:
            print(f"no booking named {name!r}")
            return
        bid, status, fired = row
        print(f"booking {bid[:8]} {name!r}  status={status}  slot_open_fired_at={fired}")
        if fired is None:
            print("slot-open is not fired -- nothing to revert.")
            return

        wids = [r[0] for r in d.execute(text(
            "select id from workers where booking_id=:b and status='RUNNING'"), {"b": bid})]
        bus = Bus(connect(_cfg.redis_url))
        for wid in wids:
            try:
                bus.send_command(wid, "signal", name="inactivate")
            except Exception as e:
                print(f"  warn: inactivate {wid[:8]} failed: {e}")
        try:
            bus.r.delete(notified_key(bid, "slot_open"), notified_key(bid, "slot_found"),
                         manual_open_key(bid))
        except Exception as e:
            print(f"  warn: redis key clear failed: {e}")

        # Also clear slot_open_at (the SCHEDULED trigger). Otherwise the scheduler re-fires slot-open
        # on the next tick (status WARMING + slot_open_at <= now), undoing the revert -- confirmed
        # live on Booking 0925. status is the enum NAME stored UPPERCASE ('WARMING', not 'warming'):
        # a lowercase value is not a valid enum member and makes every later read raise LookupError.
        d.execute(text("update bookings set slot_open_fired_at=null, slot_found_fired_at=null, "
                       "slot_open_at=null, status='WARMING' where id=:b"), {"b": bid})
        d.commit()
        print(f"REVERTED: {len(wids)} worker(s) sent inactivate; slot-open cleared; status -> WARMING.")
        print("Re-fire with the dashboard / fire-slot-open when ready. If AVAILABLE slots linger they "
              "can re-trigger slot-found -- DELETE the booking's slots first for a clean hold.")
    finally:
        d.close()


if __name__ == "__main__":
    main()

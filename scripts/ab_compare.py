"""A/B comparison for a headful-vs-new (or any two) booking run.

Usage:
    python -m scripts.ab_compare "<booking A name-or-id>" "<booking B name-or-id>"
    python -m scripts.ab_compare           # auto-pick the two most recently started RUNNING bookings

Prints login-success rate, WAF "try again later" volume, browser-mode confirmation, getTables
confirmations, slots found and bookings -- the signals that actually distinguish the two browser
modes. Read-only: touches the DB and the Redis event log, changes nothing.
"""
from __future__ import annotations

import os
import sys
import json

os.environ.setdefault("PYTHONWARNINGS", "ignore")

from datetime import datetime, timezone
from collections import Counter

import apps.hub.config  # noqa: F401  (loads .env)
from apps.hub.config import settings
from apps.hub.db import SessionLocal
from libs.db import models as M
from sqlalchemy import select
import redis

# Substates that prove a worker got PAST the login wall at least once.
_PAST_LOGIN = {"keeping", "warmed", "warming_up", "discovering", "polling", "applying", "booked", "swapped"}


def _resolve_booking(db, ident):
    b = db.get(M.Booking, ident)
    if b:
        return b
    cands = [x for x in db.execute(select(M.Booking)).scalars().all() if x.name == ident]
    if not cands:
        cands = [x for x in db.execute(select(M.Booking)).scalars().all() if ident in (x.name or "")]
    # newest started first
    cands.sort(key=lambda x: x.started_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return cands[0] if cands else None


def _collect(db, r, booking):
    ws = db.execute(select(M.Worker).where(M.Worker.booking_id == booking.id)).scalars().all()
    wids = {w.id for w in ws}
    sub = Counter((getattr(w.substate, "value", w.substate) or "none") for w in ws)
    past_login = sum(1 for w in ws
                     if (getattr(w.substate, "value", w.substate) in _PAST_LOGIN)
                     or (getattr(w.status, "value", w.status) == "completed"))
    stuck = sub.get("logging_in", 0)
    slots = db.execute(select(M.Slot).where(M.Slot.booking_id == booking.id)).scalars().all()
    avail = sum(1 for s in slots if getattr(s.status, "value", s.status) in ("available", "leased"))
    appts = db.execute(select(M.BookedAppointment).where(M.BookedAppointment.booking_id == booking.id)).scalars().all()
    pdfs = sum(1 for a in appts if getattr(a, "pdf_artifact_id", None))

    # breaker
    bh = {kk.decode(): vv.decode() for kk, vv in r.hgetall(f"breaker:{booking.id}").items()}
    now = datetime.now(timezone.utc).timestamp()
    tripped = float(bh.get("tripped_until", 0)) > now

    # event-log-derived signals for THIS booking's workers
    ev = Counter()
    for eid, fields in r.xrevrange("evt", count=20000):
        f = {kk.decode(): vv.decode() for kk, vv in fields.items()}
        d = f.get("data", "")
        try:
            wid = json.loads(d).get("worker_id", "")
            txt = json.loads(d).get("text", "")
        except Exception:
            continue
        if wid not in wids:
            continue
        tl = txt.lower()
        if "try again later" in tl:
            ev["waf_try_again_later"] += 1
        if "browser mode = headful" in tl:
            ev["mode_headful"] += 1
        if "getTables handshake confirmed" in tl:
            ev["gettables_ok"] += 1
        if "getTables not seen" in tl:
            ev["gettables_absent"] += 1
        if "not bookable" in tl or "dead-trip" in tl:
            ev["keepalive_condemn"] += 1
        if "-- 70 slots" in tl or ("slots" in tl and "found" in tl):
            ev["slots_signals"] += 1

    total = len(ws) or 1
    return {
        "name": booking.name, "id": booking.id[:8], "posto": booking.posto_id,
        "started": (booking.started_at or "").strftime("%H:%M:%S") if booking.started_at else "-",
        "workers": len(ws),
        "roles": dict(Counter(getattr(w.role, "value", w.role) for w in ws)),
        "past_login": past_login, "login_pct": round(100 * past_login / total, 1),
        "stuck_logging_in": stuck,
        "substates": dict(sub),
        "breaker_fail": bh.get("consecutive_fail", "0"), "breaker_tripped": tripped,
        "waf_try_again_later": ev["waf_try_again_later"],
        "mode_headful_confirms": ev["mode_headful"],
        "gettables_ok": ev["gettables_ok"], "gettables_absent": ev["gettables_absent"],
        "keepalive_condemn": ev["keepalive_condemn"],
        "slots_available": avail, "bookings_pdf": pdfs,
    }


def main():
    db = SessionLocal()
    r = redis.from_url(settings.redis_url)
    args = sys.argv[1:]
    if len(args) >= 2:
        a, b = _resolve_booking(db, args[0]), _resolve_booking(db, args[1])
    else:
        running = [x for x in db.execute(select(M.Booking)).scalars().all() if x.status.value == "running"]
        running.sort(key=lambda x: x.started_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        a, b = (running + [None, None])[0], (running + [None, None])[1]
    if not a or not b:
        print("Need two bookings. Pass two names/ids, or have two RUNNING bookings.")
        return
    ca, cb = _collect(db, r, a), _collect(db, r, b)

    rows = [
        ("booking", "name"), ("posto", "posto"), ("started", "started"),
        ("workers", "workers"), ("roles", "roles"),
        ("LOGIN success %", "login_pct"), ("past login", "past_login"), ("stuck logging_in", "stuck_logging_in"),
        ("WAF 'try again later'", "waf_try_again_later"),
        ("breaker consec-fail", "breaker_fail"), ("breaker tripped", "breaker_tripped"),
        ("headful confirms", "mode_headful_confirms"),
        ("getTables ok / absent", None),
        ("keepalive condemns", "keepalive_condemn"),
        ("slots available", "slots_available"), ("bookings (pdf)", "bookings_pdf"),
    ]
    w = 34
    print("\n" + "=" * 80)
    print(f"{'METRIC':<24}{'A: '+str(ca['name'])[:26]:<{w}}{'B: '+str(cb['name'])[:26]}")
    print("-" * 80)
    for label, key in rows:
        if key is None:
            va = f"{ca['gettables_ok']} / {ca['gettables_absent']}"
            vb = f"{cb['gettables_ok']} / {cb['gettables_absent']}"
        else:
            va, vb = ca[key], cb[key]
        print(f"{label:<24}{str(va):<{w}}{str(vb)}")
    print("=" * 80)
    print("Higher LOGIN success %, fewer 'try again later', lower breaker fail = the WAF-friendlier mode.")
    print("Confirm the headful side actually ran headful via 'headful confirms' > 0.")
    db.close()


if __name__ == "__main__":
    main()

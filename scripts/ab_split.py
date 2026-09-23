"""Per-template A/B split WITHIN one booking.

Usage:
    python -m scripts.ab_split "<booking name-or-id>"
    python -m scripts.ab_split            # the single most-recently-started RUNNING booking

ab_compare.py compares two SEPARATE bookings; this splits ONE booking's workers by their
template so a mixed new-vs-headful booking (shared posto / slot-open / WAF state) can be read per
arm. Same signals as ab_compare, grouped by template_id. Read-only: DB + Redis event log, no writes.
"""
from __future__ import annotations

import os, sys, json
os.environ.setdefault("PYTHONWARNINGS", "ignore")

from datetime import datetime, timezone
from collections import Counter, defaultdict

import apps.hub.config  # noqa: F401  (loads .env)
from apps.hub.config import settings
from apps.hub.db import SessionLocal
from libs.db import models as M
from sqlalchemy import select
import redis
from pathlib import Path

_WLOG = Path(__file__).resolve().parents[1] / "var" / "worker-logs"


def _log_headful(worker_id: str):
    """Return (ok, fail) counts of headful signals from THIS worker's log tail.

    The '[session] browser mode = headful on DISPLAY=' confirm and the 'no XServer' / 'no display'
    failure are worker-STDOUT prints (var/worker-logs/worker-<id>.log), never Redis events -- so the
    evt-stream metric is blind to them. Read the log tail (last ~2000 lines) instead.
    """
    f = _WLOG / f"worker-{worker_id}.log"
    ok = fail = 0
    try:
        lines = f.read_text(errors="ignore").splitlines()[-2000:]
    except Exception:
        return 0, 0
    for ln in lines:
        l = ln.lower()
        if "browser mode = headful on display=" in l:
            ok += 1
        elif "no xserver" in l or "missing x server" in l or "headful but no display" in l:
            fail += 1
    return ok, fail

# Substates that prove a worker got PAST the login wall at least once (mirrors ab_compare).
_PAST_LOGIN = {"keeping", "warmed", "warming_up", "discovering", "polling", "applying", "booked", "swapped"}


def _val(x):
    return getattr(x, "value", x)


def _resolve_booking(db, ident):
    if ident:
        b = db.get(M.Booking, ident)
        if b:
            return b
        cands = [x for x in db.execute(select(M.Booking)).scalars().all() if x.name == ident] \
             or [x for x in db.execute(select(M.Booking)).scalars().all() if ident in (x.name or "")]
    else:
        cands = [x for x in db.execute(select(M.Booking)).scalars().all() if _val(x.status) == "running"]
    cands.sort(key=lambda x: x.started_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return cands[0] if cands else None


def main():
    db = SessionLocal()
    r = redis.from_url(settings.redis_url)
    b = _resolve_booking(db, sys.argv[1] if len(sys.argv) > 1 else None)
    if not b:
        print("No booking. Pass a name/id, or have one RUNNING booking.")
        return

    ws = db.execute(select(M.Worker).where(M.Worker.booking_id == b.id)).scalars().all()
    tpls = {t.id: t for t in db.execute(select(M.WorkerTemplate)).scalars().all()}

    # group workers by template
    by_tpl = defaultdict(list)
    for w in ws:
        by_tpl[w.template_id].append(w)

    # per-worker event signals, tallied to the worker's template
    wid_tpl = {w.id: w.template_id for w in ws}
    log_sig = {w.id: _log_headful(w.id) for w in ws}  # per-worker (ok, fail) from logs
    ev = defaultdict(Counter)
    for _eid, fields in r.xrevrange("evt", count=40000):
        f = {kk.decode(): vv.decode() for kk, vv in fields.items()}
        try:
            d = json.loads(f.get("data", ""))
            wid, txt = d.get("worker_id", ""), (d.get("text", "") or "")
        except Exception:
            continue
        tid = wid_tpl.get(wid)
        if not tid:
            continue
        tl = txt.lower()
        if "try again later" in tl:                 ev[tid]["waf"] += 1
        if "gettables handshake confirmed" in tl:   ev[tid]["gt_ok"] += 1
        if "not bookable" in tl or "dead-trip" in tl: ev[tid]["condemn"] += 1

    def row(tid, wl):
        s = Counter(_val(w.substate) or "none" for w in wl)
        past = sum(1 for w in wl if _val(w.substate) in _PAST_LOGIN or _val(w.status) == "completed")
        total = len(wl) or 1
        e = ev[tid]
        hok = sum(log_sig[w.id][0] for w in wl)
        hfail = sum(log_sig[w.id][1] for w in wl)
        t = tpls.get(tid)
        mode = ((t.settings_json or {}).get("HEADLESS_MODE", "new(default)") if t else "?")
        name = t.name if t else (tid or "none")
        return {
            "name": name, "mode": mode, "workers": len(wl),
            "login_pct": round(100 * past / total, 1), "past": past,
            "stuck": s.get("logging_in", 0),
            "waf": e["waf"], "headful_ok": hok, "headful_fail": hfail,
            "gt_ok": e["gt_ok"], "condemn": e["condemn"],
            "substates": dict(s),
        }

    rows = [row(tid, wl) for tid, wl in sorted(by_tpl.items(),
            key=lambda kv: (tpls.get(kv[0]).name if kv[0] in tpls else ""))]

    # booking-level outcomes (shared: discovery + slots are pooled, so not per-arm)
    slots = db.execute(select(M.Slot).where(M.Slot.booking_id == b.id)).scalars().all()
    avail = sum(1 for s in slots if _val(s.status) in ("available", "leased"))
    appts = db.execute(select(M.BookedAppointment).where(M.BookedAppointment.booking_id == b.id)).scalars().all()
    pdfs = sum(1 for a in appts if getattr(a, "pdf_artifact_id", None))

    print("\n" + "=" * 78)
    print(f"BOOKING: {b.name}  [{b.id[:8]}]  posto={b.posto_id}  status={_val(b.status)}")
    print(f"SHARED (pooled across arms): slots_available={avail}  bookings_pdf={pdfs}")
    print("=" * 78)
    W = 22
    hdr = f"{'TEMPLATE / arm':<26}" + "".join(f"{(r['name'][:18]):<{W}}" for r in rows)
    print(hdr)
    print(f"{'  mode':<26}" + "".join(f"{r['mode']:<{W}}" for r in rows))
    print("-" * 78)
    metrics = [
        ("workers", "workers"), ("LOGIN success %", "login_pct"), ("past login", "past"),
        ("stuck logging_in", "stuck"), ("WAF try-again-later", "waf"),
        ("headful OK / FAIL", None), ("getTables ok", "gt_ok"), ("keepalive condemns", "condemn"),
    ]
    for label, key in metrics:
        if key is None:
            vals = [f"{r['headful_ok']} / {r['headful_fail']}" for r in rows]
        else:
            vals = [str(r[key]) for r in rows]
        print(f"{label:<26}" + "".join(f"{v:<{W}}" for v in vals))
    print("=" * 78)
    print("Compare the NEW column(s) vs the HEADFUL column(s): higher LOGIN %, fewer WAF,")
    print("fewer condemns = better. Headful arm MUST show headful OK > 0 and FAIL = 0, else void.")
    print("Slots/bookings are pooled (shared discovery), so they are booking-level, not per-arm.")
    db.close()


if __name__ == "__main__":
    main()

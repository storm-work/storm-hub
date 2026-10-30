"""Hot-window recorder: one JSON line every INTERVAL seconds so the window can be analysed afterwards.

Run it in its own terminal BEFORE the window and leave it running until the window is over:

    python -m scripts.window_recorder                  # every 10 s -> var/window/<date>.jsonl
    python -m scripts.window_recorder --interval 5

Each line: portal pulse state + outcome counts, fleet by booking/role/substate, appointments booked per
booking, host load, number of browser processes, and (every 60 s) the Donut profiles that are running.
Read-only: touches the DB, Redis and the Donut API's GET endpoints, and changes nothing. A failing source
records null for that field and the loop carries on -- the recorder must outlive whatever it records.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("PYTHONWARNINGS", "ignore")

import apps.hub.config  # noqa: F401  (loads .env)
import redis
from sqlalchemy import func, select

from apps.hub.config import settings
from apps.hub.db import SessionLocal
from apps.hub.settings_store import get_global
from libs.bus.portal_pulse import PortalPulse
from libs.db import models as M

OUT_DIR = Path(__file__).resolve().parent.parent / "var" / "window"


def _enum(v) -> str:
    return str(getattr(v, "value", v))


def _fleet(db) -> dict:
    rows = db.execute(
        select(M.Booking.name, M.Worker.role, M.Worker.status, M.Worker.substate, func.count())
        .join(M.Booking, M.Booking.id == M.Worker.booking_id)
        .where(M.Worker.status == "RUNNING")
        .group_by(M.Booking.name, M.Worker.role, M.Worker.status, M.Worker.substate)).all()
    out: dict = {}
    for name, role, _st, sub, n in rows:
        out.setdefault(name, {}).setdefault(_enum(role), {})[_enum(sub) if sub else "none"] = n
    return out


def _booked(db) -> dict:
    rows = db.execute(
        select(M.Booking.name, func.count()).select_from(M.BookedAppointment)
        .join(M.Booking, M.Booking.id == M.BookedAppointment.booking_id)
        .group_by(M.Booking.name)).all()
    return {n: c for n, c in rows}


def _procs() -> int | None:
    try:
        out = subprocess.run(["pgrep", "-fc", "chrome"], capture_output=True, text=True, timeout=5).stdout
        return int(out.strip() or 0)
    except Exception:
        return None


def _donut_running(g: dict) -> int | None:
    try:
        import libs.engine.vendor  # noqa: F401
        import donut
        api = donut._Api(url=g.get("DONUT_API_URL") or "", token=g.get("DONUT_API_TOKEN") or "")
        return sum(1 for p in donut._items(api.call("GET", "/v1/profiles", timeout=10), "profiles")
                   if p.get("is_running"))
    except Exception:
        return None


def main() -> int:
    interval = 10.0
    if "--interval" in sys.argv:
        interval = max(2.0, float(sys.argv[sys.argv.index("--interval") + 1]))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{datetime.now(timezone.utc):%Y%m%d}.jsonl"
    r = redis.from_url(settings.redis_url)
    pulse = PortalPulse(r)
    g = {}
    donut_n, donut_at = None, 0.0
    print(f"recording to {path} every {interval:.0f}s -- Ctrl-C to stop", flush=True)
    while True:
        t0 = time.time()
        rec: dict = {"t": round(t0, 1), "iso": datetime.fromtimestamp(t0, timezone.utc).strftime("%H:%M:%S")}
        try:
            rec["pulse"] = pulse.state()
        except Exception as e:
            rec["pulse"] = None
            rec["pulse_err"] = str(e)[:80]
        try:
            with SessionLocal() as db:
                if t0 - donut_at > 60:
                    g = get_global(db)
                    donut_n, donut_at = _donut_running(g), t0
                rec["fleet"] = _fleet(db)
                rec["booked"] = _booked(db)
        except Exception as e:
            rec["db_err"] = str(e)[:80]
        rec["donut_running"] = donut_n
        rec["browser_procs"] = _procs()
        try:
            rec["load1"] = os.getloadavg()[0]
        except Exception:
            pass
        with path.open("a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        time.sleep(max(0.5, interval - (time.time() - t0)))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("stopped")

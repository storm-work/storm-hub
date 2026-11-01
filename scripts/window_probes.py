#!/usr/bin/env python3
"""Hot-window side probes as ONE scheduled auto-flow (docs/engineering/EXPERIMENT_PLAN_2026-10-30.md §3).

Start it once, in its own terminal / tmux, any time before T0-155 min, and leave it:

    .venv/bin/python -m scripts.window_probes --t0 10:00 --booking W-main            # live
    .venv/bin/python -m scripts.window_probes --t0 10:00 --dry-run                   # print the plan

It sleeps until each probe's time, launches it as a subprocess with `--group EXP --identity-id <spare>`
(so a probe can NEVER touch a real applier), and writes every probe's output to var/window/probes/.
The probes never submit, never book.

Spare allocation: the first spare is RESERVED for the long soak. The rest are handed out
least-recently-used, and a spare is skipped if it already took 2 login shots in the last 60 min
(the portal's per-account throttle is 3/h; login-only probes make exactly one shot -- tries=1, exit-tries=1).
A probe with no eligible spare is skipped and logged, never forced.

Guards: `touch var/window/ABORT` stops all further launches; a probe is skipped when load1 > --max-load.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "var" / "window" / "probes"
PY = str(ROOT / ".venv" / "bin" / "python")

# (offset minutes from T0, kind). Sorted by time when built.
WALL = (-100, -80, -60, -40, -20, 0, 15)          # X6 login-wall map: one shot each
PRIMP = (-70, -55, -40, -25, 10)                  # X5 primp A/B: strict flips ~T0-76; none in the T0-10 freeze
FORMSTATE = (-120, -60)                            # X7 cursor life (Q29)
SOAK_START, SOAK_MIN = -150, 210                   # X1/X2 uninterrupted hold, T0-150 .. T0+60


def _t0(arg: str) -> datetime:
    if "T" in arg or "-" in arg:
        d = datetime.fromisoformat(arg)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    h, m = (int(x) for x in arg.split(":"))
    now = datetime.now(timezone.utc)
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


def _spares(group: str) -> list[tuple[str, str]]:
    """(identity_id, name) of EXP identities that can log in: ACTIVE, a VERIFIED sub-account, not allocated."""
    from sqlalchemy import select
    from apps.hub.db import SessionLocal
    from libs.db import models as M
    db = SessionLocal()
    try:
        rows = db.execute(
            select(M.Identity.id, M.Identity.full_name).where(
                M.Identity.group_name == group, M.Identity.status == "ACTIVE",
                M.Identity.id.in_(select(M.SubAccount.identity_id).where(M.SubAccount.status == "VERIFIED")),
            ).order_by(M.Identity.id)).all()
        return [(r[0], r[1]) for r in rows]
    finally:
        db.close()


def build_plan(t0: datetime, headed_soak: bool = False) -> list[tuple[datetime, str]]:
    at = lambda off: t0 + timedelta(minutes=off)  # noqa: E731
    plan = [(at(SOAK_START), "soak")]
    if headed_soak:
        plan.append((at(SOAK_START), "soak-headed"))
    plan += [(at(o), "wall") for o in WALL]
    plan += [(at(o), "primp") for o in PRIMP]
    plan += [(at(o), "formstate") for o in FORMSTATE]
    return sorted(plan)


def command(kind: str, when: datetime, ident: str, a) -> tuple[list[str], str]:
    hhmm = when.strftime("%H%M")
    scope = ["--group", a.group, "--identity-id", ident]
    common = ["--pool", a.pool, "--geo", a.geo]
    if kind in ("soak", "soak-headed"):
        tag = "window-soak" + ("-headed" if kind == "soak-headed" else "")
        return [PY, "probes/quest_hold_probe.py", "--booking", a.booking, *common, "--min-s", "60",
                "--max-s", "300", "--minutes", str(SOAK_MIN), "--tag", tag, *scope,
                *(["--headed"] if kind == "soak-headed" else [])], tag
    if kind == "wall":
        tag = f"wall-{hhmm}"
        return [PY, "probes/quest_hold_probe.py", "--booking", a.booking, *common, "--login-only",
                "--tries", "1", "--exit-tries", "1", "--tag", tag, *scope], tag
    if kind == "primp":
        tag = f"primp-{hhmm}"
        return [PY, "probes/primp_impersonate_probe.py", a.posto, "--pool", a.pool, "--geo", a.geo,
                "--accounts", "1", "--exit-tries", "1", *scope], tag
    tag = f"formstate-{hhmm}"
    return [PY, "probes/formstate_ttl_probe.py", "--booking", a.booking, "--minutes", "20",
            *common, "--interval", "120", "--silent-at", "480", "--out", f"var/window/probes/{tag}.jsonl", *scope], tag


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--t0", required=True, help="slot-open, UTC: HH:MM today or an ISO timestamp")
    ap.add_argument("--booking", default="W-main", help="booking to take posto/nationality/residence from")
    ap.add_argument("--posto", default="5084")
    ap.add_argument("--group", default="EXP")
    ap.add_argument("--pool", default="soax")
    ap.add_argument("--geo", default="DE", help="best measured login rate (DE/IT ~23%%)")
    ap.add_argument("--max-load", type=float, default=56.0, help="skip a probe above this load1 (host has 56 cores)")
    ap.add_argument("--headed-soak", action="store_true",
                    help="also run a second soak with a REAL window (DONUT_HEADLESS=0) on its own spare "
                         "(needs >= 7 spares; the headless soak keeps the first)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    t0 = _t0(a.t0)
    plan = build_plan(t0, a.headed_soak)
    try:
        spares = _spares(a.group)
    except Exception as e:                                           # noqa: BLE001
        if not a.dry_run:
            raise
        print(f"(dry-run: spares not read: {e})")
        spares = []
    print(f"T0 = {t0:%Y-%m-%d %H:%M} UTC | spares in group {a.group!r}: {len(spares)}")
    for i, (iid, name) in enumerate(spares):
        print(f"  {'SOAK ' if i == 0 else 'spare'} {iid[:8]} {name[:30]}")
    if len(spares) < 6:
        print(f"WARNING: {len(spares)} usable spares (< 6). Probes will be skipped where none is eligible.")
    print(f"{len(plan)} probes:")
    for when, kind in plan:
        print(f"  {when:%H:%M}  T0{int((when - t0).total_seconds() // 60):+d}  {kind}")
    if a.dry_run:
        return 0
    if not spares:
        print("no spare identities -- nothing to run"); return 1

    OUT.mkdir(parents=True, exist_ok=True)
    abort = ROOT / "var" / "window" / "ABORT"
    if a.headed_soak and len(spares) < 7:
        print("--headed-soak needs >= 7 spares; dropping it"); a.headed_soak = False
        plan = [p for p in plan if p[1] != "soak-headed"]
    reserved = 2 if a.headed_soak else 1
    soak_id, pool = spares[0][0], [s[0] for s in spares[reserved:]] or [spares[0][0]]
    headed_id = spares[1][0] if a.headed_soak else None
    shots: dict[str, list[float]] = {}
    last_used: dict[str, float] = {}
    procs: list[tuple[str, subprocess.Popen]] = []
    log = (OUT / f"scheduler_{t0:%Y%m%d}.log").open("a")

    def say(msg: str) -> None:
        line = f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}"
        print(line, flush=True); log.write(line + "\n"); log.flush()

    for when, kind in plan:
        while (wait := (when - datetime.now(timezone.utc)).total_seconds()) > 0:
            if abort.exists():
                say("ABORT file present -- stopping the scheduler"); return 0
            time.sleep(min(wait, 15))
        if abort.exists():
            say("ABORT file present -- stopping the scheduler"); return 0
        if (when - datetime.now(timezone.utc)).total_seconds() < -300:
            say(f"{kind} @ {when:%H:%M} missed by > 5 min (scheduler started late) -- skipped"); continue
        if os.getloadavg()[0] > a.max_load:
            say(f"{kind} @ {when:%H:%M} skipped: load1 {os.getloadavg()[0]:.1f} > {a.max_load}"); continue
        now = time.time()
        if kind == "soak":
            ident = soak_id
        elif kind == "soak-headed":
            ident = headed_id
        else:
            ok = [i for i in pool if len([t for t in shots.get(i, []) if now - t < 3600]) < 2]
            if not ok:
                say(f"{kind} @ {when:%H:%M} skipped: every spare already took 2 login shots this hour"); continue
            ident = min(ok, key=lambda i: last_used.get(i, 0))
        shots.setdefault(ident, []).append(now); last_used[ident] = now
        cmd, tag = command(kind, when, ident, a)
        f = (OUT / f"{tag}.log").open("w")
        p = subprocess.Popen(cmd, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT)
        procs.append((tag, p))
        say(f"launched {tag} on spare {ident[:8]} (pid {p.pid}) -> {OUT / (tag + '.log')}")

    say("all probes launched; waiting for the last ones to finish")
    for tag, p in procs:
        rc = p.wait()
        say(f"{tag} finished rc={rc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

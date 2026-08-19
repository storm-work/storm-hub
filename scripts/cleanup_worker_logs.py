#!/usr/bin/env python3
"""cleanup_worker_logs.py -- the sanctioned way to clear var/worker-logs/.

Same house rule as cleanup_checkpoints.py (Phase F8): dry-run by default, scoped to ONE directory,
no arbitrary path argument. The project's original data loss was an unscoped manual `rm` run under
pressure; every cleanup here is a reviewed script instead.

These files are the worker's raw stdout -- the `[session]` lines that carry the portal's own words
("Por favor, confirma o reCAPTCHA."), the ANTIDETECT flags, and the solver trace. Nothing else keeps
them, so deleting one throws away the only record of what a worker actually saw.

Selection (combine freely; with none given, nothing is selected and it just reports):
  --identity NAME|ID   every log belonging to that identity's workers (repeatable, name is a
                       case-insensitive substring). This is the usual one: "clear the logs for this
                       person".
  --orphaned           logs whose worker row no longer exists. Detaching a worker DELETES the row,
                       so its log lingers with nothing left to attribute it to -- this is where the
                       bulk of an old directory ends up.
                       READ THIS FIRST: detach also deletes the worker's ProxyLease and SlotLease
                       rows, so for a run whose workers were detached, these files can be the ONLY
                       surviving record of what it did -- which pool it used, what the portal said.
                       Reviewing a finished run first is the difference between tidying and
                       destroying the evidence.
  --older-than-days N  logs not modified in N days.
  --all                everything in the directory (still dry-run until --confirm).

Never deletes the log of a worker that is currently RUNNING: the file is open and being written to,
and reading it is how you diagnose the run in progress. Pass --force to override.

Usage:
  python scripts/cleanup_worker_logs.py --orphaned
  python scripts/cleanup_worker_logs.py --identity "RICARDO" --confirm
  python scripts/cleanup_worker_logs.py --older-than-days 7 --confirm
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
_LOG_DIR = _ROOT / "var" / "worker-logs"
_NAME_RE = re.compile(r"^(?:worker|registrar)-(?P<key>.+)\.log$")


def _human(n: int) -> str:
    return f"{n/1024/1024:.1f}MB" if n >= 1024 * 1024 else f"{n/1024:.0f}KB"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--identity", action="append", default=[],
                   help="identity name (substring) or id; repeatable")
    p.add_argument("--orphaned", action="store_true", help="logs whose worker row no longer exists")
    p.add_argument("--older-than-days", type=float, default=None)
    p.add_argument("--all", action="store_true", help="every log in the directory")
    p.add_argument("--confirm", action="store_true", help="actually delete (default: dry run)")
    p.add_argument("--force", action="store_true", help="include logs of RUNNING workers")
    args = p.parse_args()

    if not _LOG_DIR.is_dir():
        print(f"no log directory at {_LOG_DIR}")
        return 0

    from sqlalchemy import select

    from apps.hub.db import SessionLocal
    from libs.db import models as M
    from libs.domain import enums as E

    db = SessionLocal()
    try:
        workers = {w.id: w for w in db.scalars(select(M.Worker)).all()}
        names = {i.id: i.full_name for i in db.scalars(select(M.Identity)).all()}
        running = {w.id for w in workers.values() if w.status == E.WorkerStatus.RUNNING}
    finally:
        db.close()

    wanted_ids: set[str] = set()
    for term in args.identity:
        t = term.strip().lower()
        for iid, nm in names.items():
            if t == iid.lower() or t in (nm or "").lower():
                wanted_ids.add(iid)
    if args.identity and not wanted_ids:
        print(f"no identity matched {args.identity}", file=sys.stderr)
        return 2

    selected: list[tuple[Path, str]] = []
    kept_running: list[Path] = []
    total = 0
    for f in sorted(_LOG_DIR.glob("*.log")):
        total += 1
        m = _NAME_RE.match(f.name)
        key = m.group("key") if m else None
        w = workers.get(key) if key else None
        reasons = []
        if args.all:
            reasons.append("all")
        if args.orphaned and w is None:
            reasons.append("orphaned")
        if wanted_ids and w is not None and w.identity_id in wanted_ids:
            reasons.append(f"identity={names.get(w.identity_id, '?')}")
        if args.older_than_days is not None and \
                (time.time() - f.stat().st_mtime) > args.older_than_days * 86400:
            reasons.append(f"older than {args.older_than_days}d")
        if not reasons:
            continue
        if key in running and not args.force:
            kept_running.append(f)
            continue
        selected.append((f, ", ".join(reasons)))

    freed = sum(f.stat().st_size for f, _ in selected)
    print(f"{_LOG_DIR}: {total} log file(s)")
    for f, why in selected:
        print(f"  {'DELETE' if args.confirm else 'would delete'}  {f.name}  "
              f"{_human(f.stat().st_size)}  ({why})")
    if kept_running:
        print(f"  skipped {len(kept_running)} log(s) of RUNNING workers "
              f"(--force to include): {[f.name[:20] for f in kept_running]}")
    if not selected:
        print("  nothing selected -- pass --identity / --orphaned / --older-than-days / --all")
        return 0

    print(f"\n{len(selected)} file(s), {_human(freed)}"
          + ("" if args.confirm else "  [DRY RUN -- pass --confirm to delete]"))
    if args.confirm:
        for f, _ in selected:
            f.unlink()
        print("deleted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

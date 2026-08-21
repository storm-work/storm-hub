#!/usr/bin/env python3
"""retune_soax_credentials.py -- rewrite bindttl / opt-wb on existing SOAX proxy rows.

Same house rule as the other cleanup scripts (F8): dry-run by default, scoped to one thing, no
arbitrary SQL. This edits credentials_ref in place rather than re-importing, so the pool keeps its
identity: last_used_at (the rotation cursor), score, and the geo backfill all survive. A re-import
would create new rows and restart the cursor from nothing.

WHY bindttl: it is how long SOAX keeps trying to re-bind the SAME exit IP after it drops before
handing over a new one. On a ROTATING pool a long value means a worker sits on a dead address for
that whole window -- 600 was ten minutes. SOAX support recommended 5-10 (2026-08-19).

WHY opt-wb: the whitelist-bypass flag the working pools already carry; this ensures none are missing
it. Note it is soax-only -- webshare rows use a different credential format entirely and are never
touched by this script.

Usage:
  python scripts/retune_soax_credentials.py                     # dry run, shows the rewrite
  python scripts/retune_soax_credentials.py --bindttl 5 --confirm
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_BINDTTL_RE = re.compile(r"-bindttl-(\d+)")


def retune(cred: str, bindttl: int, *, opt_wb: bool = True) -> str:
    """Pure: the rewritten credential. Only touches bindttl and the -opt-wb flag.

    `opt_wb=False` STRIPS the flag. Measured 2026-08-21 against the real portal, soax/mobile,
    n=40 per arm with a fresh sessionid each:

        opt-wb ON    35/40 reached the portal (88%)   -- 5 x SSLError
        opt-wb OFF   40/40 reached the portal (100%)  -- none

    SSLError is a TLS handshake drop, which is exactly what the browser reports as
    net::ERR_CONNECTION_CLOSED -- the error the workers were logging. SOAX support had already
    recommended turning opt-wb off (2026-08-19); this is the measurement that agrees with them.
    """
    out = _BINDTTL_RE.sub(f"-bindttl-{bindttl}", cred)
    if opt_wb:
        if "-opt-wb" not in out:
            # opt-wb sits immediately before the ":password" separator, matching the working format.
            user, sep, pw = out.partition(":")
            out = f"{user}-opt-wb{sep}{pw}" if sep else f"{out}-opt-wb"
    else:
        out = out.replace("-opt-wb", "")
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bindttl", type=int, default=5)
    p.add_argument("--strip-opt-wb", action="store_true",
                   help="REMOVE -opt-wb instead of ensuring it (see retune()'s measurement)")
    p.add_argument("--confirm", action="store_true", help="actually write (default: dry run)")
    p.add_argument("--force", action="store_true", help="proceed while workers are RUNNING")
    args = p.parse_args()

    from sqlalchemy import select

    from apps.hub.db import SessionLocal
    from libs.db import models as M
    from libs.domain import enums as E

    db = SessionLocal()
    try:
        running = db.scalars(select(M.Worker).where(
            M.Worker.status == E.WorkerStatus.RUNNING)).all()
        if running and not args.force:
            print(f"REFUSING: {len(running)} worker(s) are RUNNING and hold leases built from these "
                  f"credentials.\nStop the booking first, or pass --force.", file=sys.stderr)
            return 2

        rows = db.scalars(select(M.Proxy).where(M.Proxy.credentials_ref.like("package-%"))).all()
        changed, before = [], Counter()
        for pr in rows:
            m = _BINDTTL_RE.search(pr.credentials_ref or "")
            before[(m.group(1) if m else "none", "-opt-wb" in (pr.credentials_ref or ""))] += 1
            new = retune(pr.credentials_ref, args.bindttl, opt_wb=not args.strip_opt_wb)
            if new != pr.credentials_ref:
                changed.append((pr, new))

        print(f"soax rows: {len(rows)}")
        for (ttl, wb), n in sorted(before.items()):
            print(f"   currently bindttl={ttl:<5} opt-wb={wb!s:<6} n={n}")
        print(f"\n{len(changed)} row(s) would change -> bindttl={args.bindttl}, opt-wb present")
        for pr, new in changed[:2]:
            print(f"   {pr.credentials_ref.split(':')[0][:70]}")
            print(f"   -> {new.split(':')[0][:70]}")
        if not changed:
            print("   nothing to do")
            return 0
        if args.confirm:
            for pr, new in changed:
                pr.credentials_ref = new
            db.commit()
            print(f"\nrewrote {len(changed)} row(s).")
        else:
            print("\n[DRY RUN -- pass --confirm to write]")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

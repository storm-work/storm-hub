#!/usr/bin/env python3
"""Does -opt-wb cause net::ERR_CONNECTION_CLOSED? A/B with a REAL BROWSER this time.

WHY THIS EXISTS. The 2026-08-21 A/B in scripts/retune_soax_credentials.py's docstring compared
opt-wb on/off with a plain `requests` GET -- the one workload where SOAX's Web Browser mode has
nothing to do, so it could not have measured what it claimed to. Stripping opt-wb on the strength of
it moved production login success 38% -> 36%, i.e. not at all, and the recommendation was retracted.

The question came back because production now says something specific: of ~700 login failures on
"Booking 0821 soax" after the 13:11 restart, 509 are net::ERR_CONNECTION_CLOSED -- a TLS handshake
drop -- against only ~20 reCAPTCHA/portal-level rejections. The wall is at the transport layer, and
opt-wb is the parameter that sits there.

This runs the SAME workload the workers run: libs/proxies/health.check_proxy, a real headless
Chromium navigating the real portal home page. Same proxy string in both arms, differing only by
-opt-wb, so the pairing controls for the exit node itself.

Read-only: navigations only, no login, no form POST, no DB writes.

    .venv/bin/python probes/optwb_browser_ab.py --n 12
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _classify(msg: str) -> str:
    m = (msg or "").upper()
    for marker in ("ERR_CONNECTION_CLOSED", "ERR_TUNNEL", "ERR_TIMED_OUT",
                   "ERR_SOCKET_NOT_CONNECTED", "ERR_PROXY"):
        if marker in m:
            return marker
    if "TIMEOUT" in m:
        return "TIMEOUT"
    if "FLAGGED" in m or "CHALLENGE" in m:
        return "BOT_FLAGGED"
    return (msg or "?")[:28]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=12, help="proxies per arm (each tested BOTH ways)")
    ap.add_argument("--kind", default="rotating")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    from sqlalchemy import select

    from apps.hub.db import SessionLocal
    from libs.db import models as M
    from libs.proxies.health import check_proxy
    from libs.proxies.lease import conn_url

    db = SessionLocal()
    try:
        rows = db.scalars(
            select(M.Proxy).where(M.Proxy.provider == "soax", M.Proxy.kind == args.kind)
            .limit(args.n)
        ).all()
    finally:
        db.close()
    if not rows:
        print(f"no soax/{args.kind} rows")
        return 1

    jobs = []
    for pr in rows:
        base = conn_url(pr)
        jobs.append(("opt-wb ON", base if "-opt-wb" in base else base.replace(":", "-opt-wb:", 1)))
        jobs.append(("opt-wb OFF", base.replace("-opt-wb", "")))

    print(f"soax/{args.kind}: {len(rows)} exits x 2 arms = {len(jobs)} browser navs "
          f"to the real portal\n")

    results: dict[str, list] = collections.defaultdict(list)
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(check_proxy, conn): arm for arm, conn in jobs}
        for f in cf.as_completed(futs):
            arm = futs[f]
            try:
                ok, msg = f.result()
            except Exception as e:
                ok, msg = False, f"{type(e).__name__}: {e}"
            results[arm].append((ok, _classify(msg) if not ok else "ok"))

    print(f"{'arm':<12} {'reached':>8} {'failed':>7}  {'pass':>6}")
    print("-" * 40)
    for arm in ("opt-wb ON", "opt-wb OFF"):
        rs = results[arm]
        ok = sum(1 for o, _ in rs if o)
        print(f"{arm:<12} {ok:8d} {len(rs)-ok:7d}  {ok/len(rs)*100 if rs else 0:5.0f}%")
    print()
    for arm in ("opt-wb ON", "opt-wb OFF"):
        c = collections.Counter(k for o, k in results[arm] if not o)
        if c:
            print(f"{arm} failure modes: {dict(c)}")
    print("\nThe production signature to explain is net::ERR_CONNECTION_CLOSED (509 of ~700 login "
          "failures).\nIf it is concentrated in one arm, that arm's setting is the cause.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

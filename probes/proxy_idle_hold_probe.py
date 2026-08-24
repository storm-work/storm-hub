#!/usr/bin/env python3
"""Does a SOAX exit IP survive an idle gap the length of our keepalive interval?

WHY THIS EXISTS. A holding worker's only proxy traffic is its keepalive, and _keepalive_window() is
KEEPALIVE_INTERVAL + uniform(0, KEEPALIVE_JITTER) = 600-630s. The SOAX rows carry idlettl-300, which
is how long a node stays bound with NO traffic flowing. Taken literally that means the exit is
released ~300s before every single keepalive, and a held session -- authenticated, warmed, bookable,
the thing we most want stable -- would resume from a different IP each cycle.

But bindttl-600 muddies it: bindttl is how long SOAX keeps trying to re-bind the SAME exit after it
drops. With bindttl-600 still in force at t=600, the same address may well come back. So the two
parameters interact and the answer is not derivable from the docs -- it has to be measured.

This matters for a decision already on the table: SOAX support recommended bindttl 5-10, and
scripts/retune_soax_credentials.py defaults to --bindttl 5. If bindttl-600 is the only reason held
sessions keep their IP, dropping it to 5 would break every hold.

Read-only: one HTTPS GET to api.ipify.org per sample, no portal traffic, no DB writes.

    .venv/bin/python probes/proxy_idle_hold_probe.py --gaps 120,330,660 --samples 4
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

IP_ECHO = "https://api.ipify.org?format=text"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")


def _ip(conn: str, timeout: float = 45.0) -> str:
    import requests
    try:
        r = requests.get(IP_ECHO, proxies={"http": conn, "https": conn},
                         timeout=timeout, headers={"User-Agent": UA})
        return r.text.strip() if r.status_code == 200 else f"ERR:{r.status_code}"
    except Exception as e:
        return f"ERR:{type(e).__name__}"


def _one(conn: str, gap: float) -> tuple[str, str, float]:
    """First IP, IP after `gap` seconds of total silence, and the gap actually slept."""
    a = _ip(conn)
    t0 = time.time()
    time.sleep(gap)
    b = _ip(conn)
    return a, b, time.time() - t0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gaps", default="120,330,660",
                    help="idle gaps in seconds; defaults straddle idlettl-300 and bindttl-600")
    ap.add_argument("--samples", type=int, default=4, help="distinct sticky sessions per gap")
    ap.add_argument("--kind", default="rotating")
    args = ap.parse_args()

    from sqlalchemy import select

    from apps.hub.db import SessionLocal
    from libs.db import models as M
    from libs.proxies.lease import conn_url

    db = SessionLocal()
    try:
        rows = db.scalars(
            select(M.Proxy).where(M.Proxy.provider == "soax", M.Proxy.kind == args.kind)
            .limit(args.samples * 8)
        ).all()
    finally:
        db.close()
    if not rows:
        print(f"no soax/{args.kind} proxies in the DB")
        return 1

    gaps = [float(g) for g in args.gaps.split(",")]
    jobs: list[tuple[float, str]] = []
    cursor = 0
    for gap in gaps:
        for _ in range(args.samples):
            jobs.append((gap, conn_url(rows[cursor % len(rows)])))
            cursor += 1

    print(f"soax/{args.kind}: {len(jobs)} sticky sessions, gaps={gaps}")
    print("each session: GET ip -> sleep(gap) with ZERO traffic -> GET ip again\n")

    results: dict[float, list[tuple[str, str]]] = {g: [] for g in gaps}
    with cf.ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = {ex.submit(_one, conn, gap): gap for gap, conn in jobs}
        for f in cf.as_completed(futs):
            gap = futs[f]
            try:
                a, b, _ = f.result()
            except Exception as e:
                a, b = f"ERR:{type(e).__name__}", "-"
            results[gap].append((a, b))

    print(f"{'gap':>7}  {'same':>5}  {'diff':>5}  {'err':>4}   verdict")
    print("-" * 62)
    for gap in gaps:
        pairs = results[gap]
        err = sum(1 for a, b in pairs if a.startswith("ERR") or b.startswith("ERR"))
        ok = [(a, b) for a, b in pairs if not a.startswith("ERR") and not b.startswith("ERR")]
        same = sum(1 for a, b in ok if a == b)
        diff = len(ok) - same
        if not ok:
            verdict = "no usable samples"
        elif diff == 0:
            verdict = "exit SURVIVED the idle gap"
        elif same == 0:
            verdict = "exit CHANGED every time"
        else:
            verdict = "MIXED -- not reliably sticky"
        print(f"{gap:7.0f}  {same:5d}  {diff:5d}  {err:4d}   {verdict}")

    print("\nreference: idlettl-300 (idle release), bindttl-600 (same-IP re-bind window),"
          "\nkeepalive 600-630s. A gap above idlettl that still returns the SAME ip means bindttl"
          "\nis re-binding it -- and lowering bindttl would end that.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

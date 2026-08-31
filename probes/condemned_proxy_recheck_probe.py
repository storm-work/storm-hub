#!/usr/bin/env python3
"""Are the rows flagged is_healthy=False actually dead, or did the PORTAL condemn them?

MEASURED 2026-08-28: 4,489 webshare rows flipped to is_healthy=False in one afternoon and 73 of 120
workers never launched ("no proxy leased after 5 probe(s): 5 failed health"). The launch probe
(libs/proxies/health.check_proxy) is "one nav to the real portal home page" -- so while the portal
throttles (CONFIRMED_FACTS §35) it fails for EVERY proxy regardless of quality, and apply_result
writes is_healthy=False to the row.

This tests the distinction directly: connect through condemned rows to a NEUTRAL url (an IP echo,
never the portal). If they connect, the flag was measuring the portal, not the proxy.

Also probes a control group of is_healthy=True rows, so a low success rate cannot be blamed on the
probe itself.

READ-ONLY: no DB writes, no health flags touched, no portal traffic, nothing booked. Uses a raw
primp connect on a THROWAWAY session id -- these exits are measured and discarded, never browsed
from afterwards, so the tier-1 poisoning finding (browser-only 5/6 vs primp-then-browser 0/6) does
not apply to this measurement.
"""
from __future__ import annotations
import argparse, pathlib, random, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", type=int, default=20, help="rows to sample per arm")
    a = ap.parse_args()

    from apps.hub.db import SessionLocal
    from libs.db import models as M
    from sqlalchemy import select
    from libs.proxies.lease import conn_url, session_id
    from libs.proxies.exit_ip import observe_exit_ip

    db = SessionLocal()
    arms = {}
    for prov in ("webshare", "soax"):
        for label, flag in (("CONDEMNED", False), ("control-healthy", True)):
            rows = db.scalars(select(M.Proxy).where(
                M.Proxy.provider == prov, M.Proxy.active.is_(True),
                M.Proxy.is_healthy.is_(flag)).limit(400)).all()
            random.shuffle(rows)
            arms[(prov, label)] = rows[:a.n]
    db.close()

    print(f"{'arm':<28}{'n':>4}{'connected':>11}{'rate':>8}   distinct exits")
    print("-" * 72)
    for (prov, label), rows in arms.items():
        if not rows:
            print(f"  {prov}/{label:<18}{0:>4}   (no rows in this arm)")
            continue
        ok = 0; exits = set()
        for p in rows:
            url = conn_url(p)
            ip = observe_exit_ip(url, timeout=8.0)
            if ip:
                ok += 1; exits.add(ip)
        rate = 100.0 * ok / len(rows)
        print(f"  {prov}/{label:<18}{len(rows):>4}{ok:>11}{rate:>7.0f}%   {len(exits)}")
    print()
    print("  READ: if CONDEMNED connects at a rate close to control-healthy, the is_healthy flag")
    print("        was recording portal availability, not proxy availability.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

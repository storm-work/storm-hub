#!/usr/bin/env python3
"""probe_pool.py -- can this proxy pool actually reach the booking portal?

Answers the only question that matters before importing a pool: not "is the proxy alive" but "does
it get a real portal page". Those are different, and the difference is expensive:

  - webshare's gateway reaches the internet fine but has historically refused the portal's HTTPS
    tunnel (see the proxy-status note).
  - SOAX mobile package 350716 fetches google.com and api.ipify.org happily, then answers
    `Tunnel connection failed: 403 Forbidden` for the portal -- the PROVIDER refusing the
    destination, not the portal refusing us. 1000 generated nodes would all have been dead.
  - A pool can also reach the portal and still be useless, if what comes back is a DataDome
    interstitial rather than the site.

Reads the same line formats as data/proxies_*.txt (parsed with the Hub's own parser, so a line that
probes here is a line that imports), samples a few nodes, and reports per-geo:

    geo   ok/n   median s   bytes   datadome   errors

Usage:
    python scripts/probe_pool.py data/proxies_soax.txt
    python scripts/probe_pool.py data/proxies_soax_mobile.txt --sample 5
    python scripts/probe_pool.py data/proxies_webshare.txt --geo DE,IT --sample 10

Read-only: touches no DB row and imports nothing. Deliberately samples rather than sweeping the
whole file -- a full sweep is both slow and a burst of identical requests at the portal, and the
answer ("does this pool work at all") does not need 1000 data points.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PORTAL = "https://pedidodevistos.mne.gov.pt/VistosOnline/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")
# A real portal home page is ~59KB. Anything much smaller that still says 200 is an interstitial,
# an error page, or a provider notice -- not the site.
MIN_REAL_BYTES = 40_000


def _geo_of(conn_url: str) -> str:
    m = re.search(r"-country-([A-Za-z]{2})-", conn_url) or re.search(r"-([A-Z]{2})-\d", conn_url)
    return m.group(1).upper() if m else "??"


def _probe(conn_url: str, timeout: float) -> tuple[str, float, int, bool]:
    import requests
    t = time.time()
    try:
        r = requests.get(PORTAL, proxies={"http": conn_url, "https": conn_url},
                         timeout=timeout, headers={"User-Agent": UA})
        low = r.text[:5000].lower()
        blocked = "datadome" in low or "captcha-delivery" in low
        return (str(r.status_code), time.time() - t, len(r.content), blocked)
    except Exception as e:
        # The provider refusing the destination shows up here, and is the single most useful thing
        # this script reports -- keep the reason, not just the class.
        detail = str(e)
        for marker in ("403 Forbidden", "407", "Connection refused", "timed out",
                       "Unprocessable Entity", "Name or service not known"):
            if marker in detail:
                return (f"ERR:{marker}", time.time() - t, 0, False)
        return (f"ERR:{type(e).__name__}", time.time() - t, 0, False)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pool_file", help="a data/proxies_*.txt file")
    p.add_argument("--sample", type=int, default=6, help="nodes to probe per geo (default 6)")
    p.add_argument("--geo", default=None, help="comma-separated geos to probe (default: all found)")
    p.add_argument("--timeout", type=float, default=30.0)
    args = p.parse_args()

    text = Path(args.pool_file).read_text()
    from apps.hub.importers import parse_proxies
    parsed, errors = parse_proxies(text)
    if not parsed:
        print(f"no usable proxy lines in {args.pool_file}" +
              (f" ({len(errors)} parse error(s): {errors[:2]})" if errors else ""), file=sys.stderr)
        return 2

    from libs.proxies.lease import conn_url as _conn
    from libs.db import models as M

    by_geo: dict[str, list[str]] = collections.defaultdict(list)
    for row in parsed:      # ParsedProxy dataclass: .endpoint / .credentials_ref
        url = _conn(M.Proxy(provider="soax", kind="mobile",
                            endpoint=row.endpoint, credentials_ref=row.credentials_ref))
        by_geo[_geo_of(url)].append(url)

    wanted = {g.strip().upper() for g in args.geo.split(",")} if args.geo else set(by_geo)
    print(f"{args.pool_file}: {len(parsed)} node(s), geos={sorted(by_geo)}"
          + (f", {len(errors)} unparsed" if errors else ""))
    print(f"\n{'geo':<5} {'ok/n':>8} {'median s':>9} {'bytes':>8} {'DD':>3}  errors")

    worst_ok = 1.0
    for geo in sorted(by_geo):
        if geo not in wanted:
            continue
        urls = by_geo[geo][:args.sample]
        with cf.ThreadPoolExecutor(max_workers=min(len(urls), 8)) as ex:
            res = list(ex.map(lambda u: _probe(u, args.timeout), urls))
        ok = [r for r in res if r[0] == "200" and r[2] >= MIN_REAL_BYTES]
        lat = sorted(r[1] for r in ok)
        dd = sum(1 for r in res if r[3])
        errs = collections.Counter(r[0] for r in res if r not in ok)
        med = f"{lat[len(lat)//2]:.1f}" if lat else "-"
        size = ok[0][2] if ok else 0
        worst_ok = min(worst_ok, len(ok) / len(res))
        print(f"{geo:<5} {len(ok):>3}/{len(res):<4} {med:>9} {size:>8} {dd:>3}  "
              f"{dict(errs) if errs else ''}")

    print("\nA node counts as usable only if it returns 200 AND >= "
          f"{MIN_REAL_BYTES:,}B -- a 200 carrying an interstitial is not the portal.")
    return 0 if worst_ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

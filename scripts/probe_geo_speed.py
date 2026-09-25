"""Measure soax rotating net speed per geo and rank the fastest.

For every soax-supported geo (libs/proxies/geos.py) it mints N independent sticky sessions, downloads
a fixed payload through each via primp, and records throughput (KB/s). Prints a per-geo table sorted
fastest-first and a recommended geo list.

  cd /srv/projects/storm-hub && .venv/bin/python scripts/probe_geo_speed.py [--n 5] [--bytes 65536]
      [--geos all|DE,IT,ES] [--concurrency 20] [--timeout 15] [--min-samples 3]

Bandwidth: ~= n_geos * N * bytes. Default (all soax geos, N=5, 64KB) ~= 70 MB of soax traffic.
"""
import argparse
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.hub.db import SessionLocal  # noqa: E402
from apps.hub.main import get_global  # noqa: E402
from libs.proxies import geos as geo_tbl  # noqa: E402
from libs.proxies.rotating import mint_rotating_url  # noqa: E402

_SPEED_URL = "https://speed.cloudflare.com/__down?bytes={n}"


def _one(settings, geo, nbytes, timeout):
    """One session: mint -> download nbytes -> KB/s. Returns (geo, kbps or None, err)."""
    try:
        import primp
    except ImportError:
        return (geo, None, "primp unavailable")
    try:
        url = mint_rotating_url("soax", "rotating", settings=settings, geo=geo)
        c = primp.Client(proxy=url, verify=False, timeout=timeout)
        t0 = time.time()
        r = c.get(_SPEED_URL.format(n=nbytes))
        dt = max(time.time() - t0, 1e-3)
        if r.status_code != 200:
            return (geo, None, f"HTTP {r.status_code}")
        n = len(r.content)
        return (geo, n / 1024.0 / dt, "")
    except Exception as e:
        return (geo, None, f"{type(e).__name__}: {str(e)[:60]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--bytes", type=int, default=65536)
    ap.add_argument("--geos", default="all")
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--min-samples", type=int, default=3)
    a = ap.parse_args()

    d = SessionLocal()
    settings = get_global(d)
    d.close()

    codes = (sorted(geo_tbl.SOAX_GEOS) if a.geos.strip().lower() == "all"
             else [g.strip().upper() for g in a.geos.split(",") if g.strip()])
    jobs = [(g, i) for g in codes for i in range(a.n)]
    print(f"probing {len(codes)} geos x {a.n} = {len(jobs)} sessions, {a.bytes//1024}KB each "
          f"(~{len(jobs)*a.bytes/1024/1024:.0f} MB), concurrency={a.concurrency}\n", flush=True)

    results = {g: [] for g in codes}
    errs = {g: 0 for g in codes}
    done = 0
    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        futs = [ex.submit(_one, settings, g, a.bytes, a.timeout) for g, _ in jobs]
        for f in as_completed(futs):
            geo, kbps, err = f.result()
            if kbps is not None:
                results[geo].append(kbps)
            else:
                errs[geo] += 1
            done += 1
            if done % 50 == 0:
                print(f"  ... {done}/{len(jobs)}", flush=True)

    rows = []
    for g in codes:
        s = results[g]
        if s:
            rows.append((g, statistics.median(s), max(s), len(s), errs[g]))
    rows.sort(key=lambda r: -r[1])

    print(f"\n{'geo':4}{'label':26}{'median KB/s':>12}{'best':>8}{'ok':>4}{'fail':>5}")
    print("-" * 60)
    for g, med, best, ok, fail in rows:
        print(f"{g:4}{geo_tbl.SOAX_GEOS.get(g,'')[:25]:26}{med:>12.0f}{best:>8.0f}{ok:>4}{fail:>5}")

    good = [g for g, med, best, ok, fail in rows if ok >= a.min_samples]
    print("\n=== RECOMMENDED (median KB/s, >= {} good samples) ===".format(a.min_samples))
    print("top 15:", ", ".join(f"{g}({med:.0f})" for g, med, *_ in
                                [r for r in rows if r[3] >= a.min_samples][:15]))
    print("\nPROXY_GEOS (top 8):", ",".join(good[:8]))


if __name__ == "__main__":
    main()

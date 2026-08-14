#!/usr/bin/env python3
"""proxy_health_check.py -- manual, on-demand whole-pool diagnostic tool.

NOT scheduled/periodic: the actual gate that keeps dead proxies out of rotation is
lease_proxy()'s own live pre-lease screening (libs/proxies/health.py's check_proxy, wired into
libs/proxies/lease.py's `health_check` parameter) -- every worker's proxy lease is screened against
the real portal AT the moment it's requested, not on a periodic sweep. A periodic sweep was
considered and deliberately rejected (operator, 2026-08-13): for rotating proxy pools, the actual IP
a provider hands out for "the next connection" can differ entirely from whatever a sweep checked
last, so a stale periodic result proves nothing about the proxy a worker will actually get -- only
an on-demand check at lease time does.

This script is for humans: "is my whole pool basically OK right now" as a one-off manual check (e.g.
after adding a new batch of proxies, or investigating a run that had lease trouble) -- run it by hand,
not via cron/systemd.

Usage:
  python scripts/proxy_health_check.py                    # check every active proxy, default concurrency
  python scripts/proxy_health_check.py --provider soax
  python scripts/proxy_health_check.py --concurrency 4 --timeout 20
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def screen(targets: dict[str, str], *, check_fn=None, concurrency: int = 6,
          timeout: float = 25.0, on_result=None) -> dict[str, tuple[bool, str]]:
    """targets: {proxy_id: conn_url}. Runs `check_fn` (libs.proxies.health.check_proxy by default)
    for each, `concurrency`-wide. `on_result` (optional): callable(proxy_id, ok, detail) invoked as
    each result lands, for progress printing -- kept separate from the return value so tests can
    assert on either without capturing stdout."""
    if check_fn is None:
        from libs.proxies.health import check_proxy as check_fn
    results: dict[str, tuple[bool, str]] = {}
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futs = {ex.submit(check_fn, url, timeout): pid for pid, url in targets.items()}
        for fut in as_completed(futs):
            pid = futs[fut]
            ok, detail = fut.result()
            results[pid] = (ok, detail)
            if on_result is not None:
                on_result(pid, ok, detail)
    return results


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--provider", help="only check this provider (e.g. soax, webshare); default: all")
    p.add_argument("--concurrency", type=int, default=6, help="parallel browser probes (default 6)")
    p.add_argument("--timeout", type=float, default=25.0,
                   help="per-proxy nav timeout, seconds (default 25)")
    args = p.parse_args()

    from sqlalchemy import select

    from apps.hub.db import SessionLocal
    from libs.db import models as M
    from libs.proxies.health import apply_result
    from libs.proxies.lease import conn_url as build_conn_url

    db = SessionLocal()
    try:
        q = select(M.Proxy).where(M.Proxy.active.is_(True))
        if args.provider:
            q = q.where(M.Proxy.provider == args.provider)
        proxies = list(db.scalars(q).all())
        if not proxies:
            print("No active proxies match -- nothing to check.")
            return 0

        targets = {pr.id: build_conn_url(pr) for pr in proxies}
        print(f"Checking {len(targets)} active proxy(ies), concurrency={args.concurrency}, "
              f"timeout={args.timeout}s ...")

        def _print(pid, ok, detail):
            print(f"  {'OK  ' if ok else 'FAIL'} {pid}  {detail}")

        t0 = time.time()
        results = screen(targets, concurrency=args.concurrency, timeout=args.timeout, on_result=_print)

        healthy = 0
        for pr in proxies:
            ok, detail = results.get(pr.id, (False, "not checked"))
            apply_result(pr, ok, detail)
            healthy += int(ok)
        db.commit()

        elapsed = time.time() - t0
        print(f"\n{healthy}/{len(proxies)} healthy in {elapsed:.1f}s.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

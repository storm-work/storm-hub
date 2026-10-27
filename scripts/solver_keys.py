#!/usr/bin/env python3
"""solver_keys.py -- how the reCAPTCHA-solver keys are being used, and what is left on each.

For every key of every solver it shows the last 4 characters (never the key), how many solves it has taken,
how many succeeded / failed, whether it is currently marked dead (out of balance) and for how long, and --
with --balance -- the live balance from the provider.

    .venv/bin/python scripts/solver_keys.py
    .venv/bin/python scripts/solver_keys.py --balance

Counters live in Redis and are shared by every worker, the registrar and the token factory since the
per-solve key book (libs/domain/solver_keys.py); they start from zero when that went live. An even spread
shows as similar `tries` across a solver's live keys; a key with `dead_for` > 0 is being skipped and is
retried automatically when the timeout passes (a top-up is picked up without touching a setting).
Read-only: it changes no setting and marks nothing dead.
"""
from __future__ import annotations

import argparse
import os

_BALANCE_URL = {"anticaptcha": "https://api.anti-captcha.com/getBalance",
                "capsolver": "https://api.capsolver.com/getBalance"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--balance", action="store_true", help="also fetch each key's live balance")
    a = ap.parse_args()

    from apps.hub.db import SessionLocal
    from apps.hub.settings_store import get_global
    from libs.bus import connect
    from libs.domain.solver_keys import KeyBook
    from libs.domain.solver_select import parse_keys

    db = SessionLocal()
    try:
        g = get_global(db)
    finally:
        db.close()
    book = KeyBook(connect(os.environ.get("REDIS_URL", "redis://localhost:6379/0")))

    print(f"{'solver':12s} {'key':8s} {'tries':>6s} {'ok':>6s} {'fail':>6s} {'dead_for':>9s}  balance")
    for solver, setting in (("anticaptcha", "ANTICAPTCHA_KEYS"), ("capsolver", "CAPSOLVER_KEYS")):
        keys = parse_keys(g.get(setting, ""))
        if not keys:
            print(f"{solver:12s} (no keys set)")
            continue
        for key, row in zip(keys, book.stats(solver, keys)):
            bal = ""
            if a.balance:
                try:
                    import httpx
                    r = httpx.post(_BALANCE_URL[solver], json={"clientKey": key}, timeout=15).json()
                    bal = str(r.get("balance")) if "balance" in r else (r.get("errorCode") or "?")
                except Exception as e:
                    bal = f"error: {str(e)[:40]}"
            dead = f"{row['dead_for_s']}s" if row["dead_for_s"] else "-"
            print(f"{solver:12s} {row['key']:8s} {row['tries']:6d} {row['ok']:6d} {row['fail']:6d} {dead:>9s}  {bal}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

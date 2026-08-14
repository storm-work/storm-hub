#!/usr/bin/env python3
"""gen_proxy_pool.py -- generate SOAX/Webshare proxy node lists for data/proxies_{provider}.txt.

Node formats (fixed template + a per-node differentiator that yields a distinct exit IP):
  SOAX:     package-{PKG}-country-{cc}-sessionid-{16-char-random}-sessionlength-{LEN}-
            bindttl-{TTL}-opt-wb:{PASSWORD}@proxy.soax.com:5000
  Webshare: p.webshare.io:80:{PREFIX}-{CC}-{n}:{PASSWORD}   (n = 1..count, resets per country)

Both output lines parse cleanly through apps/hub/importers.py's parse_proxies() (verified against
the real parser, not just the regex) -- paste directly into the dashboard's Proxies page Import
popup, or import in bulk:
  curl -X POST https://<host>/proxies/import -H "Authorization: Bearer <token>" \\
       -H "Content-Type: application/json" \\
       -d "{\\"provider\\":\\"soax\\",\\"kind\\":\\"static\\",\\"text\\":\\"$(cat data/proxies_soax.txt)\\"}"

Usage:
  python scripts/gen_proxy_pool.py soax --count 1000 --countries PT,DE,IT,ES,LU,NL,CZ
  python scripts/gen_proxy_pool.py webshare --count 1000 --countries PT,DE,IT,ES,LU,NL,CZ
  python scripts/gen_proxy_pool.py soax --package 335959 --password ... --out data/proxies_soax.txt

Country counts split as evenly as possible (remainder goes to the first countries in the given
order) -- e.g. 1000 over 7 countries = six at 143, one at 142.
"""
from __future__ import annotations

import argparse
import os
import random
import string
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _ROOT / "data"

# Non-secret defaults confirmed working as of 2026-08-13 -- update these (or pass
# --package/--prefix) when the account/package changes.
_SOAX_PACKAGE = "335959"
_SOAX_SESSIONLENGTH = 3600
_SOAX_BINDTTL = 600
_WEBSHARE_PREFIX = "sodade2026"

# Provider account passwords come from the environment (or .env, which is gitignored) -- never
# hardcoded here. They were literal string constants until 2026-08-14, which would have put live
# proxy credentials into git history permanently on the first commit of this file.
_SOAX_PASSWORD = os.environ.get("SOAX_PASSWORD", "")
_WEBSHARE_PASSWORD = os.environ.get("WEBSHARE_PASSWORD", "")


def _split_counts(total: int, n: int) -> list[int]:
    base, rem = divmod(total, n)
    return [base + 1 if i < rem else base for i in range(n)]


def _gen_soax(countries: list[str], counts: list[int], package: str, password: str,
             sessionlength: int, bindttl: int) -> list[str]:
    seen: set[str] = set()

    def sessionid() -> str:
        while True:
            sid = "".join(random.choices(string.ascii_letters + string.digits, k=16))
            if sid not in seen:
                seen.add(sid)
                return sid

    lines = []
    for cc, n in zip(countries, counts):
        for _ in range(n):
            lines.append(
                f"package-{package}-country-{cc.lower()}-sessionid-{sessionid()}-"
                f"sessionlength-{sessionlength}-bindttl-{bindttl}-opt-wb:{password}@proxy.soax.com:5000"
            )
    return lines


def _gen_webshare(countries: list[str], counts: list[int], prefix: str, password: str) -> list[str]:
    lines = []
    for cc, n in zip(countries, counts):
        for i in range(1, n + 1):
            lines.append(f"p.webshare.io:80:{prefix}-{cc.upper()}-{i}:{password}")
    return lines


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("provider", choices=("soax", "webshare"))
    p.add_argument("--count", type=int, default=1000, help="total nodes to generate (default 1000)")
    p.add_argument("--countries", default="PT,DE,IT,ES,LU,NL,CZ",
                   help="comma-separated country codes, split as evenly as possible")
    p.add_argument("--out", default=None, help="output path (default data/proxies_<provider>.txt)")
    p.add_argument("--package", default=_SOAX_PACKAGE, help="[soax] package id")
    p.add_argument("--password", default=None, help="account password (default depends on provider)")
    p.add_argument("--sessionlength", type=int, default=_SOAX_SESSIONLENGTH, help="[soax] sessionlength seconds")
    p.add_argument("--bindttl", type=int, default=_SOAX_BINDTTL, help="[soax] bindttl seconds")
    p.add_argument("--prefix", default=_WEBSHARE_PREFIX, help="[webshare] username prefix")
    args = p.parse_args()

    countries = [c.strip() for c in args.countries.split(",") if c.strip()]
    if not countries:
        print("no countries given", file=sys.stderr)
        return 2
    counts = _split_counts(args.count, len(countries))

    if args.provider == "soax":
        password = args.password or _SOAX_PASSWORD
        env_var = "SOAX_PASSWORD"
    else:
        password = args.password or _WEBSHARE_PASSWORD
        env_var = "WEBSHARE_PASSWORD"
    if not password:
        print(f"no {args.provider} password: pass --password or set {env_var} (see .env.example)",
              file=sys.stderr)
        return 2

    if args.provider == "soax":
        lines = _gen_soax(countries, counts, args.package, password, args.sessionlength, args.bindttl)
    else:
        lines = _gen_webshare(countries, counts, args.prefix, password)

    out_path = Path(args.out) if args.out else _DATA_DIR / f"proxies_{args.provider}.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")

    print(f"wrote {len(lines)} {args.provider} node(s) -> {out_path}")
    print("per-country counts:", dict(zip(countries, counts)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Lift the rotating-pool credentials out of the `proxies` rows into GLOBAL SETTINGS.

TWO SOURCES, tried in order. Run this BEFORE `alembic upgrade head` and it reads the proxy rows;
run it after and the rows are gone, so it falls back to the generator's own output files in data/
(proxies_soax.txt, proxies_soax_mobile.txt, proxies_webshare.txt), which is what actually saved
this on 2026-09-16 when the migration ran before the backfill did.

The files are the BETTER source when they exist: one package and one password each, no ambiguity
to resolve. The row heuristic below only matters for the DB path, where several generations of
credentials coexist in one table.

Why it is needed at all: as of 2026-09-15 (DECISIONS.md D39) a rotating proxy is minted at runtime
from settings rather than leased from a row. If SOAX_ROTATING_PACKAGE/SOAX_ROTATING_PASSWORD (etc.)
are unset, `mint_rotating_url` raises and EVERY worker launch returns 409. Verified on this host
2026-09-16: all nine settings were unset while 42,600 rows still held the credentials.

WHICH PASSWORD GOES WITH WHICH PACKAGE. The live data holds three soax packages and three
passwords, mismatched in places. Ground truth is commit 0c4846b: "Old package (335959) retired
2026-09-03 in favor of a new one (352438) with its own password, separate from SOAX_PASSWORD
(which stays mobile's)."

  package 352438 (rotating) + pw A  -- 10,000 rows created 2026-09-03; the ONLY rotating rows with
                                       any 'ok' probe verdict (17); used through the 09-07..09-11
                                       runs. The CURRENT rotating package. pw A appears nowhere in
                                       .env, which is why the rows are needed at all.
  package 335959 (rotating) + pw B  -- 11,000 rows, created 2026-08-13..08-21. RETIRED. pw B is
                                       SOAX_PASSWORD -- i.e. MOBILE's password. These rows are
                                       gen_proxy_pool.py's documented trap made real: rotating
                                       generated with the mobile password.
  package 350716 (mobile)   + pw B  -- 10,000 rows created 2026-08-21, never used. CORRECT: pw B
                                       (=SOAX_PASSWORD) is mobile's current password. "Never used"
                                       is not evidence against it.
  package 350716 (mobile)   + pw C  --    600 rows created 2026-08-19, used 08-20..08-21. pw C is
                                       a SINCE-ROTATED mobile password. Its usage history is the
                                       reason the row heuristic alone picks it and is wrong.

PRECEDENCE: .env wins for any password it carries; the rows are used only for what .env lacks
(SOAX_ROTATING_PASSWORD, whose package postdates those env vars) and for the package ids/prefix.
The row heuristic alone got soax/mobile WRONG -- it cannot see a password rotation, because usage
proves a credential worked once, not that it still does. See the precedence block in main().

It prints the full derivation and requires --commit before writing anything.

Usage:
  python scripts/backfill_rotating_credentials.py            # dry run: show what it would write
  python scripts/backfill_rotating_credentials.py --commit   # write the settings
"""
from __future__ import annotations

import argparse
import collections
import os
import re
import sys
from pathlib import Path

# Run from anywhere: scripts/ is not on sys.path by default, and this script imports the app's own
# settings writer rather than hand-rolling SQL against the settings table.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv           # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

# Settings this script owns. Anything already set is left alone unless --overwrite is given: an
# operator who has already typed the real credential into the dashboard outranks this inference.
_TTL_KEYS = {"SOAX_SESSIONLENGTH": "sessionlength", "SOAX_BINDTTL": "bindttl",
             "SOAX_IDLETTL": "idlettl"}


def _redact(pw: str) -> str:
    return pw[:2] + "*" * max(0, len(pw) - 4) + pw[-2:] if len(pw) > 6 else "***"


def _score(rows):
    """Rank candidates: proven-used first, then MOST RECENTLY CREATED. Never by row/use COUNT.

    Both halves are load-bearing and the order between them matters:

    * "was it ever used" is the only positive evidence in the rows, because `is_healthy` on this
      pool is worthless -- the mass condemnations came from a probe running with stealth off.
      It is WEAK evidence and it is overridden by .env: usage proves a credential worked once, not
      that it still does, and on soax/mobile this heuristic picks a since-rotated password purely
      because the rows carrying it predate the rotation. See the precedence block in main().

    * recency has to outrank USE COUNT, and this is the trap the first version of this script fell
      into: soax/rotating holds a retired package 335959 (11,000 rows, 10,990 used, created 08-21)
      alongside the current 352438 (10,000 rows, 10,000 used, created 09-03). Ranking by use count
      picks the RETIRED package -- it was used more only because it existed longer -- and every
      login would then authenticate against a package that no longer exists. Caught by the dry run.
    """
    used = any(r.last_used_at is not None for r in rows)
    newest = max((r.created_at for r in rows if r.created_at), default=None)
    return (1 if used else 0, newest or "")


_POOL_FILES = {
    ("soax", "rotating"): "data/proxies_soax.txt",
    ("soax", "mobile"): "data/proxies_soax_mobile.txt",
    ("webshare", "rotating"): "data/proxies_webshare.txt",
}


def _from_pool_files(root: Path) -> dict:
    """Recover credentials from scripts/gen_proxy_pool.py's own output files.

    Unambiguous by construction: each file is one generator run, so it holds exactly one package
    and one password. If a file somehow holds more than one of either, we say so and skip it rather
    than guess -- a wrong proxy password authenticates as nobody and reads as a portal failure.
    """
    found: dict = {}
    for (provider, kind), rel in _POOL_FILES.items():
        path = root / rel
        if not path.is_file():
            continue
        pkgs, prefixes, pws = set(), set(), set()
        for line in path.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            if "@" in line:                       # soax: user:pass@host:port
                head = line.rsplit("@", 1)[0]
                pws.add(head.rsplit(":", 1)[1])
                m = re.search(r"package-(\d+)", head)
                if m:
                    pkgs.add(m.group(1))
            else:                                  # webshare: host:port:user:pass
                parts = line.split(":")
                if len(parts) == 4:
                    pws.add(parts[3])
                    m = re.match(r"([A-Za-z0-9]+?)-(?:[A-Z]{2}-)?\d+$", parts[2])
                    if m:
                        prefixes.add(m.group(1))
        ident = (pkgs | prefixes)
        if len(pws) != 1 or len(ident) != 1:
            print(f"  !! {rel}: expected one identity and one password, found "
                  f"{len(ident)} / {len(pws)} -- skipping rather than guessing", file=sys.stderr)
            continue
        print(f"  {rel}: {provider}/{kind} id={next(iter(ident))} ({len(pws)} password)")
        found[(provider, kind)] = (next(iter(ident)), next(iter(pws)))
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="store_true", help="actually write (default: dry run)")
    ap.add_argument("--overwrite", action="store_true",
                    help="replace settings that already have a value (default: leave them alone)")
    ap.add_argument("--env", default="/srv/projects/storm-hub/.env")
    args = ap.parse_args()

    load_dotenv(args.env)
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    engine = create_engine(url)

    with engine.begin() as c:
        rows = c.execute(text(
            "SELECT provider, kind, credentials_ref, created_at, last_used_at "
            "FROM proxies WHERE resource_type <> 'static'")).all()
        if not rows:
            # The rows are gone (ci000droprot has run). Fall back to the generator's output files,
            # which is the normal path once the migration is applied and is strictly the cleaner
            # source -- see the module docstring.
            print("No rotating proxy rows (migration ci000droprot has run).")
            print("Falling back to the generator's output files:\n")
            found = _from_pool_files(Path(__file__).resolve().parents[1])
            if not found:
                print("\nNo usable data/proxies_*.txt either. The packages/passwords must be read "
                      "from the SOAX/Webshare dashboards and added to .env by hand.", file=sys.stderr)
                return 1
            writes = {}
            for (provider, kind), (ident, pw) in found.items():
                if provider == "soax" and kind == "rotating":
                    writes["SOAX_ROTATING_PACKAGE"], writes["SOAX_ROTATING_PASSWORD"] = ident, pw
                elif provider == "soax" and kind == "mobile":
                    writes["SOAX_MOBILE_PACKAGE"], writes["SOAX_MOBILE_PASSWORD"] = ident, pw
                elif provider == "webshare":
                    writes["WEBSHARE_PREFIX"], writes["WEBSHARE_PASSWORD"] = ident, pw
            if not args.commit:
                writes = {k: (_redact(str(v)) if "PASSWORD" in k else v)
                          for k, v in writes.items()}
            print("\n=== add these to .env ===")
            print("# Recovered from scripts/gen_proxy_pool.py output in data/.")
            for k, v in sorted(writes.items()):
                print(f"{k}={v}")
            if not args.commit:
                print("\nDRY RUN -- passwords REDACTED above. Re-run with --commit to print them.")
            return 0

        # ── derive ────────────────────────────────────────────────────────────────────────────
        # (provider, kind, package) -> {password: [rows]}
        cands: dict = collections.defaultdict(lambda: collections.defaultdict(list))
        ttls: collections.Counter = collections.Counter()
        prefixes: collections.Counter = collections.Counter()
        for r in rows:
            user, _, pw = r.credentials_ref.partition(":")
            if r.provider == "soax":
                m = re.search(r"package-(\d+)", user)
                if not m:
                    continue
                cands[("soax", r.kind, m.group(1))][pw].append(r)
                t = re.search(r"sessionlength-(\d+)-bindttl-(\d+)-idlettl-(\d+)", user)
                if t:
                    ttls[t.groups()] += 1
            elif r.provider == "webshare":
                m = re.match(r"([A-Za-z0-9]+?)-(?:[A-Z]{2}-)?\d+$", user)
                if m:
                    prefixes[m.group(1)] += 1
                    cands[("webshare", r.kind, m.group(1))][pw].append(r)

        # Within one pool (provider+kind) several packages may coexist -- soax/rotating holds both
        # the current 352438 and the retired 335959. Pick the package the same way as the password:
        # used-in-anger first, then newest. Picking by row COUNT would have chosen the retired one.
        chosen: dict = {}
        print("=== derivation ===")
        for (prov, kind, ident), pw_map in sorted(cands.items()):
            best_pw = max(pw_map.items(), key=lambda kv: _score(kv[1]))
            allrows = [r for rs in pw_map.values() for r in rs]
            print(f"\n{prov}/{kind}  id={ident}  ({len(allrows)} rows)")
            for pw, rs in sorted(pw_map.items(), key=lambda kv: _score(kv[1]), reverse=True):
                used = sum(1 for r in rs if r.last_used_at is not None)
                # "best for THIS id" -- the pool-level winner across ids is decided
                # below and announced in the settings block, so do not read this as final.
                mark = "  <-- best for this id" if pw == best_pw[0] else ""
                print(f"    pw {_redact(pw)}  rows={len(rs):6}  used={used:6}  "
                      f"created={max((r.created_at for r in rs if r.created_at), default='-')}{mark}")
            prev = chosen.get((prov, kind))
            if prev is None or _score(best_pw[1]) > _score(prev[2]):
                chosen[(prov, kind)] = (ident, best_pw[0], best_pw[1])

        for (prov, kind), (ident, _pw, _rs) in sorted(chosen.items()):
            print(f"\n  => {prov}/{kind}: id {ident} wins the pool")

        writes: dict = {}
        for (prov, kind), (ident, pw, _rs) in chosen.items():
            if prov == "soax" and kind == "rotating":
                writes["SOAX_ROTATING_PACKAGE"], writes["SOAX_ROTATING_PASSWORD"] = ident, pw
            elif prov == "soax" and kind == "mobile":
                writes["SOAX_MOBILE_PACKAGE"], writes["SOAX_MOBILE_PASSWORD"] = ident, pw
            elif prov == "webshare":
                writes["WEBSHARE_PREFIX"], writes["WEBSHARE_PASSWORD"] = ident, pw

        # ── .env OUTRANKS the rows for passwords ──────────────────────────────────────────────
        # The row heuristic cannot see a password ROTATION, and that is not hypothetical: it got
        # soax/mobile WRONG here. `used` looked decisive -- 600 mobile rows with pw `Nk..3F` had
        # carried real traffic on 08-20..08-21, while the 10,000 rows created 08-21 with `ul..H0`
        # had never been touched -- so the heuristic chose `Nk..3F`. But commit 0c4846b states the
        # ground truth outright: "Old package (335959) retired 2026-09-03 in favor of a new one
        # (352438) with its own password, separate from SOAX_PASSWORD (which stays mobile's)".
        # SOAX_PASSWORD in .env is `ul..H0`. So `ul..H0` IS mobile's current password, the 600
        # used rows carry a SINCE-ROTATED one, and the untouched 10,000 are the correct batch.
        # Usage proves a credential worked ONCE, never that it still does.
        #
        # So: take the password from .env wherever .env has one, and fall back to the rows only for
        # what .env does not carry -- which is exactly SOAX_ROTATING_PASSWORD, the package that
        # postdates these env vars. Disagreements are printed, never silently resolved.
        env_pw = {"SOAX_MOBILE_PASSWORD": os.environ.get("SOAX_PASSWORD"),
                  "WEBSHARE_PASSWORD": os.environ.get("WEBSHARE_PASSWORD"),
                  "SOAX_ROTATING_PASSWORD": os.environ.get("SOAX_ROTATING_PASSWORD")}
        for key, val in env_pw.items():
            if not val:
                continue
            derived = writes.get(key)
            if derived and derived != val:
                print(f"\n  !! {key}: rows say {_redact(derived)}, .env says {_redact(val)} "
                      f"-- using .env (a rotated password still appears on old used rows)")
            writes[key] = val

        if len(ttls) == 1:
            triple = next(iter(ttls))
            for key, _name in zip(("SOAX_SESSIONLENGTH", "SOAX_BINDTTL", "SOAX_IDLETTL"), triple):
                writes[key] = triple[("SOAX_SESSIONLENGTH", "SOAX_BINDTTL",
                                      "SOAX_IDLETTL").index(key)]
        elif ttls:
            print(f"\n!! soax TTLs are not uniform across rows ({dict(ttls)}) -- not writing them; "
                  "set SOAX_SESSIONLENGTH/BINDTTL/IDLETTL by hand.", file=sys.stderr)

        # OUTPUT .env LINES, do not write the database (2026-09-16). Rotating credentials stopped
        # being settings: they are deployment facts and secrets, so they live in the environment and
        # are declared per pool in libs/proxies/providers/. Writing them into `settings` would put a
        # proxy password somewhere a web dashboard can edit and any DB connection can read -- and it
        # is what made the TEST SUITE resolve live production credentials, because
        # apps/hub/config.py loads .env at import and resolution consulted ambient os.environ.
        #
        # This script's remaining job is therefore recovery, not installation: SOAX_ROTATING_PASSWORD
        # exists nowhere on this host except inside the proxy rows that ci000droprot deletes. It
        # prints the lines; the operator adds them to .env. Deliberately NOT self-writing .env --
        # that file also holds DATABASE_URL and the admin credentials, and a script that rewrites it
        # in place is a worse failure mode than a paste.
        print("\n=== add these to .env ===")
        print("# Recovered from the rotating proxy rows before migration ci000droprot deletes them.")
        for k, v in sorted(writes.items()):
            print(f"{k}={v}")

        if not args.commit:
            print("\nDRY RUN -- values are REDACTED above unless --commit is passed.")
        print("\nThen: .venv/bin/alembic upgrade head && sudo systemctl restart storm-hub.service")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

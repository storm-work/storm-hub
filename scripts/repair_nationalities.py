#!/usr/bin/env python3
"""Re-map stored identity nationalities through libs/domain/nationality.to_iso3.

WHY. Before 2026-09-19 the importer's nationality map had five entries and a fallback that
TRUNCATED any unknown demonym to 8 chars and stored it as a code. Those truncated values were sent
to the portal's registration form verbatim and failed 100% of the time (GUINEENS 0/14, GUINÉENN
0/4, GHANAIAN 0/2). The importer is fixed; this repairs identities already in the database.

Only values that RESOLVE to a different valid ISO-3 code are rewritten. A value that is already a
code, or that the map cannot resolve, is left untouched and reported -- this script never guesses.

    python scripts/repair_nationalities.py            # dry run
    python scripts/repair_nationalities.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv                       # noqa: E402
from sqlalchemy import create_engine, text           # noqa: E402

from libs.domain.nationality import to_iso3          # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--env", default="/srv/projects/storm-hub/.env")
    args = ap.parse_args()
    load_dotenv(args.env)

    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.begin() as c:
        distinct = c.execute(text(
            "SELECT nationality, count(*) FROM identities "
            "WHERE nationality IS NOT NULL GROUP BY nationality ORDER BY nationality")).all()
        changes, unresolved = {}, []
        for value, n in distinct:
            code, err = to_iso3(value)
            if err:
                unresolved.append((value, n, err))
            elif code and code != value:
                changes[value] = (code, n)

        print("=== identities: nationality repair ===")
        for old, (new, n) in sorted(changes.items()):
            print(f"   {old!r:12} -> {new!r}   ({n} identit{'y' if n == 1 else 'ies'})")
        for value, n, err in unresolved:
            print(f"   {value!r:12}    LEFT AS-IS ({n}) -- {err}")
        if not changes:
            print("   nothing to change.")
            return 0

        if not args.commit:
            print("\nDRY RUN -- re-run with --commit to apply.")
            return 0

        total = 0
        for old, (new, _n) in changes.items():
            total += c.execute(text("UPDATE identities SET nationality=:new WHERE nationality=:old"),
                               {"new": new, "old": old}).rowcount
        print(f"\nUpdated {total} identity row(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

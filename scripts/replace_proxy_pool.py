#!/usr/bin/env python3
"""replace_proxy_pool.py -- swap a provider's whole proxy pool for a freshly generated one.

Same house rule as the other destructive scripts (F8): dry-run by default, scoped to one provider,
no arbitrary SQL, and it says exactly what it will do before it does it.

WHY IT IS TWO STEPS. Old rows are referenced by ProxyLease rows, and a live booking's workers hold
active leases right now -- deleting the proxies out from under them would hit a foreign key (the
same shape as the detach-identity FK bug) or strand a running worker. So:

  1. IMPORT the new nodes (purely additive -- nothing in flight is touched).
  2. RETIRE the old ones by setting active=False. lease_proxy only ever selects active rows, so they
     leave rotation immediately, but every existing lease stays valid and every FK still resolves.
  3. PURGE (--purge, separate run) hard-deletes retired rows that no longer have ANY lease pointing
     at them. Run it once the booking has stopped; it skips anything still referenced.

Retire is reversible (flip active back); purge is not, which is exactly why they are separate flags
rather than one "replace" that does everything at once.

    python scripts/replace_proxy_pool.py soax                        # dry run
    python scripts/replace_proxy_pool.py soax --confirm              # import + retire old
    python scripts/replace_proxy_pool.py soax --purge --confirm      # delete unreferenced retired
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("provider", choices=("soax", "webshare"))
    ap.add_argument("--file", default=None, help="default data/proxies_<provider>.txt")
    ap.add_argument("--kind", default=None,
                    help="kind to record on the new rows. ALSO scopes the retire: only rows of this "
                         "kind are taken out of rotation. soax carries TWO independent pools on "
                         "different packages (rotating=335959, mobile=350716), so a provider-wide "
                         "retire would knock out the pool you are not replacing -- and a second "
                         "import run would retire the rows the first one just added.")
    ap.add_argument("--confirm", action="store_true", help="actually write (default: dry run)")
    ap.add_argument("--purge", action="store_true",
                    help="hard-delete retired rows with no lease referencing them, instead of "
                         "importing")
    args = ap.parse_args()

    from sqlalchemy import func, select

    from apps.hub.db import SessionLocal
    from apps.hub.importers import parse_proxies
    from libs.db import models as M

    db = SessionLocal()
    try:
        prov = args.provider

        # ── purge mode ────────────────────────────────────────────────────────
        if args.purge:
            q = select(M.Proxy).where(M.Proxy.provider == prov, M.Proxy.active.is_(False))
            if args.kind:
                q = q.where(M.Proxy.kind == args.kind)
            retired = db.scalars(q).all()
            referenced = {
                pid for (pid,) in db.execute(
                    select(M.ProxyLease.proxy_id).where(
                        M.ProxyLease.proxy_id.in_([p.id for p in retired] or [""]))
                ).all()
            }
            deletable = [p for p in retired if p.id not in referenced]
            print(f"{prov}: {len(retired)} retired row(s); {len(referenced)} still referenced by a "
                  f"lease; {len(deletable)} deletable")
            if args.confirm and deletable:
                for p in deletable:
                    db.delete(p)
                db.commit()
                print(f"deleted {len(deletable)} row(s).")
            elif not args.confirm:
                print("\n[DRY RUN -- pass --confirm to delete]")
            return 0

        # ── import + retire ───────────────────────────────────────────────────
        path = Path(args.file) if args.file else _ROOT / "data" / f"proxies_{prov}.txt"
        if not path.exists():
            print(f"no such file: {path}", file=sys.stderr)
            return 2
        res = parse_proxies(path.read_text())
        rows = res[0] if isinstance(res, tuple) else res
        if not rows:
            print("parsed 0 nodes -- refusing", file=sys.stderr)
            return 2

        kind = args.kind
        if kind is not None:
            existing = db.scalars(select(M.Proxy).where(
                M.Proxy.provider == prov, M.Proxy.kind == kind)).all()
        else:
            existing = db.scalars(select(M.Proxy).where(M.Proxy.provider == prov)).all()
        if kind is None:
            kinds = db.execute(
                select(M.Proxy.kind, func.count()).where(M.Proxy.provider == prov)
                .group_by(M.Proxy.kind).order_by(func.count().desc())
            ).first()
            kind = kinds[0] if kinds else "rotating"

        have = {p.credentials_ref for p in existing}
        new_rows = [r for r in rows if r.credentials_ref not in have]

        print(f"{prov} / kind={kind}:")
        print(f"   existing rows        : {len(existing)}")
        print(f"   parsed from file     : {len(rows)}")
        print(f"   genuinely new        : {len(new_rows)}")
        print(f"   would retire (active): {sum(1 for p in existing if p.active)}")
        print(f"   kind for new rows    : {kind}")
        print(f"   geo on new rows      : NULL (applied at lease time)")

        if not args.confirm:
            print("\n[DRY RUN -- pass --confirm to import + retire]")
            return 0

        for r in new_rows:
            db.add(M.Proxy(endpoint=r.endpoint, credentials_ref=r.credentials_ref,
                           provider=prov, kind=kind, active=True, is_healthy=True,
                           health=None, score=0.0, geo=None))
        retired = 0
        for p in existing:
            if p.active:
                p.active = False
                retired += 1
        db.commit()
        print(f"\nimported {len(new_rows)} new row(s); retired {retired} old row(s).")
        print("Old rows are OUT of rotation but still resolve for in-flight leases. Run with "
              "--purge --confirm once the booking has stopped.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

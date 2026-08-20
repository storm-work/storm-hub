#!/usr/bin/env python3
"""unpin_template_setting.py -- remove a pinned key from worker templates' settings_json.

Exists because of the settings precedence order (WORKER > TEMPLATE > BOOKING > GLOBAL). Making a
key settable at BOOKING does NOT make a booking-level value take effect: any template that pins the
same key still outranks it. That is exactly how "Apply/Scout Proxy-aware" pinned
RECAPTCHA_SOLVE_PROXY=True and made the new per-booking switch inert for every worker armed with
those templates -- the switch would flip in the UI, save fine, and change nothing.

This is the sanctioned way to unpin such a key, rather than ad-hoc SQL against a live table:
dry-run by default, scoped to one catalog key, prints the exact before/after per template, and only
writes when given --confirm.

Usage:
    python scripts/unpin_template_setting.py RECAPTCHA_SOLVE_PROXY
    python scripts/unpin_template_setting.py RECAPTCHA_SOLVE_PROXY --confirm
    python scripts/unpin_template_setting.py RECAPTCHA_SOLVE_PROXY --template "Apply Proxy-aware"

Unpinning is not the same as setting the key to False: it REMOVES the override, so the value falls
through to whatever the booking (or global) says. That is the point -- one control point, not two.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("key", help="catalog key to unpin (e.g. RECAPTCHA_SOLVE_PROXY)")
    p.add_argument("--template", default=None,
                   help="only this template, by exact name (default: every template pinning the key)")
    p.add_argument("--confirm", action="store_true", help="actually write (default: dry run)")
    args = p.parse_args()

    from apps.hub.db import SessionLocal
    from libs.db import models as M
    from libs.domain.settings.catalog import CATALOG

    if args.key not in CATALOG:
        print(f"unknown setting {args.key!r} -- not in the catalog", file=sys.stderr)
        return 2

    db = SessionLocal()
    try:
        templates = db.query(M.WorkerTemplate).all()
        hits = [
            t for t in templates
            if args.key in (t.settings_json or {})
            and (args.template is None or t.name == args.template)
        ]
        if not hits:
            scope = f" named {args.template!r}" if args.template else ""
            print(f"no template{scope} pins {args.key} -- nothing to do")
            return 0

        for t in hits:
            before = dict(t.settings_json or {})
            after = {k: v for k, v in before.items() if k != args.key}
            print(f"\n  template : {t.name}")
            print(f"  before   : {before}")
            print(f"  after    : {after}")
            if args.confirm:
                # Reassign rather than mutate: settings_json is a JSON column and SQLAlchemy will
                # not detect an in-place dict edit as dirty, so a mutation would silently not save.
                t.settings_json = after

        if args.confirm:
            db.commit()
            print(f"\nunpinned {args.key} from {len(hits)} template(s). "
                  f"Its value now comes from the booking (or global) layer.")
        else:
            print(f"\nDRY RUN -- {len(hits)} template(s) would change. Re-run with --confirm to write.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""How long does the booking anchor survive a hold, and does a bare nav actually RENEW it?

THE QUESTION. A warmed worker is "bookable" only while the server-side workflow anchor is live --
Schedule.jsp carrying getSlots / captchaDiv / SubmeterVistoCriaPDF / f_date_c / cmbPeriodo. The hold
phase exists to keep exactly that alive. It currently does so by navigating Schedule.jsp every
KEEPALIVE_INTERVAL (600s) and re-warming if the markers are gone.

Two things about that were never measured, and the operator called it (2026-08-22):

  1. 600s is a GUESS. Nobody measured how long the anchor actually lives, so the keepalive interval
     is not derived from the thing it is supposed to stay ahead of.

  2. The keepalive NAVIGATES and CHECKS. That VERIFIES the anchor; it may not RENEW it. If the
     portal keeps the workflow alive on real form-scoped activity (getPeriodosOcupados / gettime --
     what discovery does) rather than on page loads, then the hold reads the gauge without adding
     fuel, and every lapse it "repairs" was one it never prevented.

The live evidence that prompted this: 147 "form-state lapsed" against 42 "re-warm restored" in one
window -- a hold spending most of its cycles rebuilding what it was meant to be holding. And the
asymmetry: SCOUTS (which call getPeriodosOcupados continuously) hold fine; APPLIERS (which only
navigate) are the ones that lapse.

WHAT IT DOES. Logs in and warms ONE session per arm, then polls at a fixed cadence until the
scheduling markers disappear, recording the elapsed time:

    arm "nav"      -- re-navigate Schedule.jsp each tick (what the keepalive does today)
    arm "periodos" -- call getPeriodosOcupados each tick (what a scout does)

If both die at about the same age, the anchor has a fixed server-side TTL and the fix is simply to
set KEEPALIVE_INTERVAL below it. If "periodos" outlives "nav", the keepalive is touching the wrong
thing and that is the root cause.

COST, be aware: one real login (CAPTCHA solve + a slice of that sub-account's rate limit) per arm.
It NEVER submits a booking -- no SubmeterVistoCriaPDF, no MostrarPdf. Read-only against the portal.

    .venv/bin/python probes/anchor_ttl_probe.py --arms nav,periodos --max-minutes 90
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

_SCHED_MARKERS = ("getSlots", "captchaDiv", "SubmeterVistoCriaPDF", "f_date_c", "cmbPeriodo")


def _load_env() -> None:
    env = _ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def _build_ops(db, ident, sub, proxy_url, capsolver_keys, posto_id):
    from apps.hub.config_store import get_config
    from libs.domain.enums import SettingsLayer as L
    from libs.domain.settings import resolve
    from libs.engine.config import EngineConfig
    from apps.worker.ops_live import LiveEngineOps

    eff = resolve({L.TEMPLATE: {"ROLE": "apply"}})
    cfg = EngineConfig.from_settings(eff)
    return LiveEngineOps(
        cfg=cfg,
        account={"username": sub.username, "password": sub.password,
                 "email": getattr(sub, "email", None) or "x@example.com"},
        posto_id=posto_id,
        nationality=ident.nationality or "CPV",
        residence=ident.nationality or "CPV",
        quest_steps=get_config(db, "quest_steps") or {},
        form_defaults=get_config(db, "form_defaults") or {},
        capsolver_keys=capsolver_keys,
        warm_proxy=proxy_url,
        sub_account_id=sub.id,
        identity_id=ident.id,
    )


def _still_anchored(ops, arm: str) -> tuple[bool, str]:
    """One poll tick. Returns (markers_present, note)."""
    from apps.worker.ops_live import GETPERIODOS_URL, SCHED_JSP
    import datetime as _dt

    try:
        if arm == "periodos":
            d = (_dt.date.today() + _dt.timedelta(days=30)).strftime("%Y/%m/%d")
            ops.session.browser_nav(
                f"{GETPERIODOS_URL}?id_posto={ops.posto_id}&data_agendamento={d}", timeout=30)
        # BOTH arms then read Schedule.jsp, so the verdict is measured identically -- the arms
        # differ only in what they DO between checks, never in how they are judged.
        ops.session.browser_nav(SCHED_JSP + "?posto_id=" + str(ops.posto_pdf or ops.posto_id),
                                timeout=30)
        html = ops.session.browser_eval("document.documentElement.outerHTML") or ""
    except Exception as e:
        return False, f"nav failed: {type(e).__name__}: {str(e)[:80]}"
    hit = [m for m in _SCHED_MARKERS if m in html]
    return (len(hit) > 0), f"{len(html)}B markers={len(hit)}/{len(_SCHED_MARKERS)}"


def run_arm(arm: str, db, candidates, capsolver_keys, posto_id, every: float,
            max_minutes: float) -> None:
    from libs.proxies.lease import conn_url

    print(f"\n=== arm '{arm}' ===", flush=True)
    ops = None
    for ident, sub, proxy in candidates:
        url = conn_url(proxy)
        print(f"  trying {ident.full_name[:24]} / {sub.username[:14]} ...", flush=True)
        o = _build_ops(db, ident, sub, url, capsolver_keys, posto_id)
        try:
            if not o.do_login():
                print("    login failed", flush=True)
                continue
            if not o.do_warmup():
                print("    warmup failed", flush=True)
                continue
        except Exception as e:
            print(f"    error: {type(e).__name__}: {str(e)[:90]}", flush=True)
            continue
        ops = o
        print(f"    WARMED (posto_pdf={o.posto_pdf})", flush=True)
        break
    if ops is None:
        print("  could not warm any candidate -- arm aborted", flush=True)
        return

    t0 = time.time()
    deadline = t0 + max_minutes * 60
    while time.time() < deadline:
        time.sleep(every)
        age = time.time() - t0
        ok, note = _still_anchored(ops, arm)
        print(f"  t+{age:6.0f}s  anchored={ok!s:5s}  {note}", flush=True)
        if not ok:
            print(f"  >>> arm '{arm}': ANCHOR LOST at t+{age:.0f}s ({age/60:.1f} min)", flush=True)
            break
    else:
        print(f"  >>> arm '{arm}': still anchored at the {max_minutes:.0f} min cap", flush=True)
    try:
        ops.session.close_browser()
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arms", default="nav,periodos")
    ap.add_argument("--every", type=float, default=60.0, help="seconds between polls")
    ap.add_argument("--max-minutes", type=float, default=90.0)
    ap.add_argument("--posto", default="2032")
    ap.add_argument("--candidates", type=int, default=6, help="identities to try per arm")
    args = ap.parse_args()

    _load_env()
    from sqlalchemy import select

    from apps.hub.db import SessionLocal
    from apps.hub.settings_store import get_global
    from libs.db import models as M

    db = SessionLocal()
    try:
        keys = get_global(db).get("CAPSOLVER_KEYS") or []
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split(",") if k.strip()]
        if not keys:
            print("no CAPSOLVER_KEYS in global settings", file=sys.stderr)
            return 2

        proxies = db.scalars(select(M.Proxy).where(
            M.Proxy.provider == "soax", M.Proxy.kind == "rotating",
            M.Proxy.active.is_(True)).limit(64)).all()
        idents = db.scalars(select(M.Identity).where(
            M.Identity.type == "test", M.Identity.status == "active").limit(40)).all()
        cands = []
        for n, ident in enumerate(idents):
            sub = db.scalars(select(M.SubAccount).where(
                M.SubAccount.identity_id == ident.id,
                M.SubAccount.status == "verified")).first()
            if sub is not None and proxies:
                cands.append((ident, sub, proxies[n % len(proxies)]))
        if not cands:
            print("no (identity, verified sub-account, proxy) triple available", file=sys.stderr)
            return 2
        print(f"{len(cands)} candidate(s) available; posto={args.posto}")

        per = max(1, len(cands) // max(1, len(args.arms.split(","))))
        for i, arm in enumerate(a.strip() for a in args.arms.split(",") if a.strip()):
            run_arm(arm, db, cands[i * per:(i + 1) * per + args.candidates],
                    keys, args.posto, args.every, args.max_minutes)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

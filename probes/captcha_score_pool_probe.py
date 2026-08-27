#!/usr/bin/env python3
"""Do OLD-pool exits earn better reCAPTCHA treatment than the CURRENT pool?

WHY THIS MATTERS. 17 appointments were booked on a 2,600-row geo-tagged proxy pool. On
2026-08-21 15:31 that pool was deactivated and replaced with 30,000 rows carrying no geo, and
nothing has booked since. If the old exits earn Google's auto-pass checkbox while the new ones are
forced through an image challenge, that single difference plausibly explains the login rejections,
the /slots ReCaptchaError, and the form_stale submit.

WHAT IS ACTUALLY MEASURED. `solver.score_token()` is NOT a reported reCAPTCHA score -- it is a
LENGTH HEURISTIC (<2320 chars -> 100 "auto-pass, no image challenge"; >=2550 -> 10). So the honest
measurement is TOKEN LENGTH, and the derived bucket is reported alongside it as this codebase's own
interpretation, not as ground truth from Google.

    short token  == auto-pass checkbox == Google trusted this exit
    long  token  == image challenge solved by the provider == Google did not

READ-ONLY. Solves CAPTCHAs and nothing else: no login, no portal request, no booking, no DB write.
"""
from __future__ import annotations

import argparse, datetime as dt, json, pathlib, random, statistics, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

SD = pathlib.Path("/tmp/claude-1001/-srv-projects-storm-hub/"
                  "7609ba5c-b27c-43a3-ac46-d6222e36f6d2/scratchpad")
CUT = dt.datetime(2026, 8, 21, tzinfo=dt.timezone.utc)


def sample(label: str, proxies, caps, n: int, log) -> list[dict]:
    from libs.engine.vendor import race_all
    from libs.engine.vendor.solver import score_token
    from libs.proxies.lease import conn_url, session_id
    out = []
    for i in range(n):
        p = proxies[i % len(proxies)]
        url = conn_url(p, geo=(p.geo or None))
        rec = {"arm": label, "i": i + 1, "proxy_id": p.id[:8], "geo": p.geo,
               "session_id": session_id(url)}
        t0 = time.time()
        try:
            tok = race_all(caps, [], [], [], "SCHEDULE_EVISA", url, min_score=100) or ""
            rec.update(token_len=len(tok), derived_score=score_token(tok),
                       secs=round(time.time() - t0, 1))
            log(f"  [{label}] {i+1}/{n} geo={p.geo} token_len={len(tok)} "
                f"derived={score_token(tok)} ({rec['secs']}s)")
        except Exception as e:
            rec.update(error=f"{type(e).__name__}: {str(e)[:120]}",
                       secs=round(time.time() - t0, 1))
            log(f"  [{label}] {i+1}/{n} geo={p.geo} FAILED: {rec['error']}")
        out.append(rec)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", type=int, default=5, help="samples per arm")
    a = ap.parse_args()

    def log(t): print(f"  {time.strftime('%H:%M:%S')} {t}", flush=True)

    from apps.hub.db import SessionLocal
    from libs.db import models as M
    from sqlalchemy import select
    from apps.hub.settings_store import get_global

    db = SessionLocal()
    caps = [k.strip() for k in (get_global(db).get("CAPSOLVER_KEYS", "") or "").split(",") if k.strip()]
    allp = db.scalars(select(M.Proxy).where(M.Proxy.provider == "soax",
                                            M.Proxy.kind == "rotating")).all()
    db.close()
    old = [p for p in allp if p.created_at < CUT]
    new = [p for p in allp if p.created_at >= CUT]
    random.shuffle(old); random.shuffle(new)
    log(f"OLD pool (pre-08-21, geo-tagged, deactivated): {len(old)}")
    log(f"NEW pool (08-21 regeneration, geo=None)      : {len(new)}")
    if not old or not new or not caps:
        log("missing pool or solver keys -- cannot compare"); return 2

    res = sample("OLD", old, caps, a.n, log) + sample("NEW", new, caps, a.n, log)
    (SD / "captcha_score_pool.json").write_text(json.dumps(res, indent=2, default=str))

    print("\n=== TOKEN LENGTH by pool (shorter = auto-pass = trusted exit) ===")
    for arm in ("OLD", "NEW"):
        lens = [r["token_len"] for r in res if r["arm"] == arm and "token_len" in r]
        scs = [r["derived_score"] for r in res if r["arm"] == arm and "derived_score" in r]
        errs = sum(1 for r in res if r["arm"] == arm and "error" in r)
        if lens:
            print(f"  {arm}: n={len(lens)} median_len={statistics.median(lens):.0f} "
                  f"min={min(lens)} max={max(lens)} | derived median={statistics.median(scs):.0f} "
                  f"| autopass(<2320)={sum(1 for l in lens if l < 2320)}/{len(lens)} | errors={errs}")
        else:
            print(f"  {arm}: no successful solves (errors={errs})")
    print(f"  raw: {SD/'captcha_score_pool.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

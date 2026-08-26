#!/usr/bin/env python3
"""Can a real warmed worker DISCOVER slots and classify the portal's answer correctly?

Uses the production discovery path verbatim -- LiveEngineOps.do_discover(dates) -- which is
gettime + getPeriodosOcupados via real browser navigations (never raw fetch: DataDome 403s those
regardless of session health). Nothing here re-implements discovery or its parser.

THE DISTINCTION UNDER TEST. do_discover returns a 3-way per-date outcome and the whole point is
that these are NOT the same thing:

    found   free periods parsed out of periodos_ocupados
    empty   parsed cleanly, zero free periods  -> NO SLOTS, a normal successful answer
    failed  gettime nav failed / WAF or busy page / marker absent / token untokenizable -> UNKNOWN

Treating `empty` as failure (or `failed` as empty) is the bug this classification exists to prevent,
so the probe reports the raw evidence behind each verdict rather than just the verdict.

IT ALSO TESTS RECOVERY. Form state expires ~4-6 min after warm-up and nothing extends it, so the
probe deliberately waits past that and discovers again, then invokes the EXISTING re-warm
(_run_warmup_steps) and discovers a third time. The target behaviour is
    discover -> detect expired form -> re-warm -> discover again
never
    discover -> permanently fail

SAFETY: discovery only. No do_apply, no slot lease, no ScheduleController submit, no
SubmeterVistoCriaPDF, no PDF. Real slots, if found, are RECORDED AND NOT ACTED ON.
"""
from __future__ import annotations

import argparse, json, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


def raw_probe(ops, iso_date):
    """Fetch ONE getPeriodosOcupados page directly so the report can show the evidence behind the
    verdict (status/size/marker/token) without re-implementing the parser."""
    import re
    from apps.worker.ops_live import GETPERIODOS_URL
    from datetime import date as _d
    d = _d.fromisoformat(iso_date)
    url = f"{GETPERIODOS_URL}?id_posto={ops.posto_id}&data_agendamento={d.strftime('%Y/%m/%d')}"
    out = {"date": iso_date}
    try:
        final = ops.session.browser_nav(url, timeout=30)
        html = ops.session.browser_eval("document.documentElement.outerHTML") or ""
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"[:120]
        return out
    out["final_url_tail"] = str(final)[-60:]
    out["bytes"] = len(html)
    m = re.search(r"periodos_ocupados='([0-9]*)", html)
    out["marker_present"] = bool(m)
    out["token"] = m.group(1) if m else None          # digits only -- no PII
    out["token_len"] = len(m.group(1)) if m else 0
    return out


def discover_round(ops, dates, label, log):
    t0 = time.time()
    res = ops.do_discover(list(dates))
    dt = time.time() - t0
    from collections import Counter
    c = Counter(r.get("outcome") for r in res)
    found = [r for r in res if r.get("outcome") == "found"]
    log(f"[{label}] {len(dates)} date(s) in {dt:.1f}s -> {dict(c)}")
    for r in found:
        log(f"[{label}]   FOUND {r['date']} periods={r.get('periods')}")
    return {"label": label, "secs": round(dt, 1), "counts": dict(c),
            "found": [{"date": r["date"], "periods": r.get("periods")} for r in found],
            "raw": res}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dates", type=int, default=5, help="how many candidate dates to scan")
    ap.add_argument("--expiry-wait", type=float, default=420.0, help="seconds to wait past form TTL")
    ap.add_argument("--geo", default="it")
    ap.add_argument("--out", default="var/discovery_validation.json")
    a = ap.parse_args()

    def log(t): print(f"  {time.strftime('%H:%M:%S')} {t}", flush=True)
    from formstate_ttl_probe import warm_one
    from apps.worker.ops_live import _SCHED_PAGE_MARKERS, SCHED_JSP

    ops = warm_one("discover", a.geo, log)
    if ops is None:
        print("could not warm -- login wall; discovery not exercised"); return 2

    # Production's own candidate window for this booking.
    from libs.engine.pool_manager import default_date_window
    from datetime import date as _date
    from apps.hub.db import SessionLocal
    from libs.db import models as M
    from sqlalchemy import select
    from libs.domain.settings.resolver import resolve
    from libs.domain.enums import SettingsLayer as L
    from apps.hub.settings_store import get_global
    db = SessionLocal()
    bk = db.scalar(select(M.Booking).where(M.Booking.name == "Test 2032"))
    eff = resolve({L.GLOBAL: get_global(db), L.BOOKING: bk.settings_json or {}})
    db.close()
    dates = default_date_window(start_date=str(eff.get("CAL_START_DATE") or ""),
                                end_date=str(eff.get("CAL_END_DATE") or ""),
                                num_days=int(eff.get("CAL_NUM_DAYS") or 0),
                                today=_date.today())[:a.dates]
    log(f"candidate dates: {dates}")

    rec = {"dates": dates, "rounds": []}
    try:
        # --- Round 1: fresh warm, form state alive -------------------------------
        rec["raw_evidence_fresh"] = raw_probe(ops, dates[0])
        log(f"raw evidence (fresh): {json.dumps(rec['raw_evidence_fresh'])[:200]}")
        rec["rounds"].append(discover_round(ops, dates, "fresh", log))

        # --- Round 2: after the form state has expired ---------------------------
        log(f"waiting {a.expiry_wait/60:.1f} min for the form state to expire...")
        end = time.time() + a.expiry_wait
        while time.time() < end:
            time.sleep(min(15, end - time.time()))
        try:
            ops.session.browser_nav(SCHED_JSP + "?posto_id=" + str(ops.posto_pdf or ops.posto_id), timeout=45)
            h = ops.session.browser_eval("document.documentElement.outerHTML") or ""
            rec["form_alive_before_round2"] = any(m in h for m in _SCHED_PAGE_MARKERS)
            rec["schedule_bytes_before_round2"] = len(h)
        except Exception as e:
            rec["form_alive_before_round2"] = None
        log(f"form state before round 2: alive={rec['form_alive_before_round2']}")
        rec["raw_evidence_expired"] = raw_probe(ops, dates[0])
        log(f"raw evidence (expired): {json.dumps(rec['raw_evidence_expired'])[:200]}")
        rec["rounds"].append(discover_round(ops, dates, "after-expiry", log))

        # --- Round 3: existing re-warm, then discover again ----------------------
        log("invoking the EXISTING re-warm (_run_warmup_steps)...")
        t0 = time.time()
        ok = ops._run_warmup_steps()
        rec["rewarm_ok"] = bool(ok); rec["rewarm_secs"] = round(time.time() - t0, 1)
        log(f"re-warm ok={ok} in {rec['rewarm_secs']}s")
        if ok:
            rec["rounds"].append(discover_round(ops, dates, "after-rewarm", log))
    finally:
        try: ops._close_session_quietly()
        except Exception: pass
    pathlib.Path(a.out).write_text(json.dumps(rec, indent=1))
    print("\n=== SUMMARY ===")
    for r in rec["rounds"]:
        print(f"  {r['label']:<14} {r['counts']}  found={len(r['found'])}  ({r['secs']}s)")
    print(f"  form alive before round2: {rec.get('form_alive_before_round2')}   "
          f"re-warm: {rec.get('rewarm_ok')} in {rec.get('rewarm_secs')}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

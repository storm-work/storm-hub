#!/usr/bin/env python3
"""Which portal endpoints preserve the booking cursor, and which destroy it?

We proved 3/3 that the CAPTCHA-less /slots POST collapses Schedule.jsp from ~55KB with all five
_SCHED_PAGE_MARKERS to a 39-byte marker-less response -- i.e. it destroys the server-side booking
cursor. That was found by accident, one endpoint at a time. This builds the map deliberately:

    warm -> confirm cursor -> touch ONE endpoint -> confirm cursor -> (re-warm if lost) -> next

Every endpoint the two codebases know about is probed, in a deliberately safe order (reads first).
The only mutating call in the list is the /slots POST we already know about, kept as the positive
control that the detector works.

NEVER SUBMITS. SubmeterVistoCriaPDF is not called under any arm. Nothing is leased or booked.
"""
from __future__ import annotations

import argparse, json, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

SD = pathlib.Path("/tmp/claude-1001/-srv-projects-storm-hub/"
                  "7609ba5c-b27c-43a3-ac46-d6222e36f6d2/scratchpad")


def cursor_ok(ops) -> dict:
    from apps.worker.ops_live import SCHED_JSP, _SCHED_PAGE_MARKERS
    try:
        ops.session.browser_nav(SCHED_JSP + "?posto_id=" + str(ops.posto_pdf or ops.posto_id),
                                timeout=30)
        html = ""
        for i in range(4):
            try:
                html = ops.session.browser_eval("document.documentElement.outerHTML") or ""
                if html:
                    break
            except Exception:
                if i == 3:
                    raise
                time.sleep(1.5)
        return {"ok": any(m in html for m in _SCHED_PAGE_MARKERS), "bytes": len(html)}
    except Exception as e:
        return {"ok": None, "error": f"{type(e).__name__}: {str(e)[:90]}"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--geo", default="DE"); ap.add_argument("--provider", default="soax")
    ap.add_argument("--booking", default="E2E VALIDATION 0827")
    a = ap.parse_args()
    def log(t): print(f"  {time.strftime('%H:%M:%S')} {t}", flush=True)

    from formstate_ttl_probe import warm_one
    from apps.worker.ops_live import (BASE, HOME_URL, QUEST_URL, GETTIME_URL, GETPERIODOS_URL,
                                      SLOTS_URL, SCHED_JSP)
    ops = warm_one("matrix", a.geo, log, booking_name=a.booking, provider=a.provider)
    if ops is None:
        log("could not warm -- login wall"); return 2
    posto = str(ops.posto_pdf or ops.posto_id)

    # (label, kind, url, body) -- body None means a plain navigation (GET)
    from datetime import date, timedelta
    d = (date.today() + timedelta(days=2)).strftime("%Y/%m/%d")
    probes = [
        ("GET  /VistosOnline/",        "nav",  HOME_URL, None),
        ("GET  Questionario",          "nav",  QUEST_URL, None),
        ("GET  profile.jsp",           "nav",  BASE + "/profile.jsp", None),
        ("GET  gettime",               "nav",  GETTIME_URL + f"?id_posto={posto}", None),
        ("GET  getPeriodosOcupados",   "nav",  GETPERIODOS_URL + f"?id_posto={posto}&data_agendamento={d}", None),
        ("GET  Schedule.jsp",          "nav",  SCHED_JSP + f"?posto_id={posto}", None),
        ("POST /slots (captcha='')",   "post", SLOTS_URL + f"?posto_id={posto}",
                                               {"posto_id": posto, "captcha": ""}),
    ]

    out = []
    base = cursor_ok(ops)
    log(f"baseline cursor: {base}")
    for label, kind, url, body in probes:
        rec = {"endpoint": label, "kind": kind}
        t0 = time.time()
        try:
            if kind == "post":
                fu, html = ops.session.browser_form_post(url, body, timeout=45)
            else:
                fu = ops.session.browser_nav(url, timeout=30)
                html = ops.session.browser_eval("document.documentElement.outerHTML") or ""
            rec.update(secs=round(time.time()-t0, 1), bytes=len(html or ""), final_url=fu)
        except Exception as e:
            rec.update(secs=round(time.time()-t0, 1), raised=f"{type(e).__name__}: {str(e)[:110]}")
        after = cursor_ok(ops)
        rec["cursor_after"] = after
        rec["destroys_cursor"] = (after.get("ok") is False)
        log(f"  {label:<28} {rec.get('bytes', rec.get('raised'))!s:<10} "
            f"cursor_after={after.get('ok')} ({after.get('bytes')}B)")
        out.append(rec)
        if rec["destroys_cursor"]:
            log(f"  {'':<28} -> cursor LOST, re-warming before the next probe")
            try:
                log(f"  {'':<28} -> re-warm {'ok' if ops._run_warmup_steps() else 'FAILED'}")
            except Exception as e:
                log(f"  {'':<28} -> re-warm raised {str(e)[:80]}")
    try: ops._close_session_quietly()
    except Exception: pass

    (SD / "endpoint_cursor_matrix.json").write_text(json.dumps(out, indent=2, default=str))
    print("\n=== ENDPOINT -> CURSOR MATRIX ===")
    for r in out:
        verdict = ("DESTROYS" if r["destroys_cursor"]
                   else "preserves" if r["cursor_after"].get("ok") else "unknown")
        print(f"  {r['endpoint']:<30} {str(r.get('bytes', r.get('raised')))[:24]:<26} {verdict}")
    print(f"  raw: {SD/'endpoint_cursor_matrix.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

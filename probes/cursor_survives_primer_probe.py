#!/usr/bin/env python3
"""Does the rejected /slots POST destroy the portal's server-side booking cursor?

WHAT WE KNOW. The captured submit response (var/submits/, 72,211B) is the QUESTIONNAIRE start page
served back in answer to SubmeterVistoCriaPDF, carrying the authenticated nav (Logout / Change
Password). So the auth session is alive and the server-side BOOKING CURSOR is gone. Meanwhile
_run_warmup_steps' guard passes -- it navigates Schedule.jsp and finds _SCHED_PAGE_MARKERS -- seven
seconds before that submit. The cursor is therefore provably good at warm-up and gone at submit,
and only one thing happens in between: the /slots primer, whose POST the portal rejects.

THE TEST. Schedule.jsp carrying the scheduling markers IS the production definition of "cursor
present" (it is the exact check do_warmup uses). So:

    warm -> markers?  ->  RUN THE PRIMER  ->  markers?      (primer arm)
    warm -> markers?  ->  wait the same   ->  markers?      (control arm)

The control matters: form state also lapses on its own (~4-6 min measured), so without it a
disappearance proves nothing. Both arms use a fresh session.

NEVER SUBMITS. SubmeterVistoCriaPDF is not called. Nothing is leased, written or booked.
"""
from __future__ import annotations

import argparse, json, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

SD = pathlib.Path("/tmp/claude-1001/-srv-projects-storm-hub/"
                  "7609ba5c-b27c-43a3-ac46-d6222e36f6d2/scratchpad")


def cursor_state(ops, tag: str) -> dict:
    """The production definition of a live booking cursor: Schedule.jsp shows scheduling markers."""
    from apps.worker.ops_live import SCHED_JSP, _SCHED_PAGE_MARKERS
    import re
    d = {"tag": tag}
    try:
        ops.session.browser_nav(SCHED_JSP + "?posto_id=" + str(ops.posto_pdf or ops.posto_id),
                                timeout=30)
        # browser_nav can return while the document is still settling, and the eval then dies with
        # "Execution context was destroyed". That is a READ failure, not an absent cursor -- reading
        # it as marker-absence manufactures exactly the result this probe is testing for.
        html = ""
        for attempt in range(4):
            try:
                html = ops.session.browser_eval("document.documentElement.outerHTML") or ""
                if html:
                    break
            except Exception as ee:
                if attempt == 3:
                    raise
                time.sleep(1.5)
        t = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        d.update(bytes=len(html), title=(t.group(1).strip()[:70] if t else None),
                 markers=[m for m in _SCHED_PAGE_MARKERS if m in html])
        d["cursor_ok"] = bool(d["markers"])
    except Exception as e:
        d.update(error=f"{type(e).__name__}: {str(e)[:120]}", cursor_ok=False)
    return d


def arm(label: str, do_primer: bool, geo, provider, booking, gap, log) -> dict:
    from formstate_ttl_probe import warm_one
    from apps.worker.ops_live import SLOTS_URL
    rec = {"arm": label, "primer": do_primer}
    ops = warm_one(label, geo, log, booking_name=booking, provider=provider)
    if ops is None:
        rec["result"] = "login_wall"; return rec
    try:
        rec["before"] = cursor_state(ops, "before")
        log(f"[{label}] BEFORE  cursor_ok={rec['before']['cursor_ok']} "
            f"markers={rec['before'].get('markers')} title={rec['before'].get('title')!r}")
        if do_primer:
            t0 = time.time()
            try:
                fu, html = ops.session.browser_form_post(
                    SLOTS_URL + "?posto_id=" + str(ops.posto_pdf),
                    {"posto_id": ops.posto_pdf, "captcha": ""}, timeout=45)
                rec["primer_result"] = {"secs": round(time.time()-t0, 1),
                                        "bytes": len(html or ""), "final_url": fu}
                log(f"[{label}] PRIMER  {len(html or '')}B {fu}")
            except Exception as e:
                rec["primer_result"] = {"raised": str(e)[:150]}
                log(f"[{label}] PRIMER raised: {str(e)[:120]}")
        else:
            log(f"[{label}] CONTROL: no primer, waiting {gap:.0f}s instead")
            time.sleep(gap)
        rec["after"] = cursor_state(ops, "after")
        log(f"[{label}] AFTER   cursor_ok={rec['after']['cursor_ok']} "
            f"markers={rec['after'].get('markers')} title={rec['after'].get('title')!r}")
        rec["cursor_lost"] = bool(rec["before"].get("cursor_ok")) and not rec["after"].get("cursor_ok")
        log(f"[{label}] => cursor_lost={rec['cursor_lost']}   (nothing submitted)")
    finally:
        try: ops._close_session_quietly()
        except Exception: pass
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--geo", default="DE"); ap.add_argument("--provider", default="soax")
    ap.add_argument("--booking", default="E2E VALIDATION 0827")
    ap.add_argument("--gap", type=float, default=6.0, help="control wait, ~= primer duration")
    a = ap.parse_args()
    def log(t): print(f"  {time.strftime('%H:%M:%S')} {t}", flush=True)

    out = [arm("CONTROL", False, a.geo, a.provider, a.booking, a.gap, log),
           arm("PRIMER", True, a.geo, a.provider, a.booking, a.gap, log)]
    (SD / "cursor_survives_primer.json").write_text(json.dumps(out, indent=2, default=str))
    print("\n=== DOES THE CURSOR SURVIVE? ===")
    for r in out:
        print(f"  {r['arm']:<8} before={(r.get('before') or {}).get('cursor_ok')} "
              f"after={(r.get('after') or {}).get('cursor_ok')} lost={r.get('cursor_lost')}")
    print(f"  raw: {SD/'cursor_survives_primer.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

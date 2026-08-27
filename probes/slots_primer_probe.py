#!/usr/bin/env python3
"""Does storm-hub's CAPTCHA-less /slots primer poison the document the submit is built in?

THE HYPOTHESIS. do_apply sends, immediately before the booking submit:

    browser_form_post(SLOTS_URL + "?posto_id=<pdf>", {"posto_id": <pdf>, "captcha": ""}, timeout=45)

with the CAPTCHA field EMPTY, as a real page NAVIGATION, and discards the result. storm-auto's
corresponding call sends a solved CAPTCHA token and inspects what came back. Because
browser_form_post builds its form in the CURRENT document
(`document.createElement('form') ... document.body.appendChild(f); f.submit()`), a primer that
parks the page on chrome-error://chromewebdata/ leaves the SUBMIT unsendable no matter how healthy
the network is.

WHAT THIS PROBE DOES NOT DO. It never calls SubmeterVistoCriaPDF. It never leases a slot, never
writes a Booking/Worker/Slot/Appointment row, and never books. It stops at exactly the point
do_apply would submit, and reports the document state there -- which is the evidence the causal
question needs and the submit itself would only destroy.

ARMS, per round:
  control  capture document state, DO NOT prime, capture again   -> does the page stay healthy?
  primer   capture document state, RUN THE EXACT PRIMER, capture -> does the primer break it?

Running both against the same warmed session is what separates "the primer broke it" from "the
page was already dying". Repeat with --rounds for Case D (does the correlation hold?).
"""
from __future__ import annotations

import argparse, json, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

_STATE_JS = ("JSON.stringify({o:location.origin,h:location.href,"
             "t:(document.title||''),len:(document.documentElement.outerHTML||'').length})")


def doc_state(ops, tag: str) -> dict:
    """The document the submit would be built in, RIGHT NOW. One eval, no navigation."""
    d = {"tag": tag}
    try:
        raw = ops.session.browser_eval(_STATE_JS, timeout=20)
        st = json.loads(raw) if isinstance(raw, str) else (raw or {})
        d.update(origin=st.get("o"), href=st.get("h"), title=st.get("t"), html_len=st.get("len"))
    except Exception as e:
        d.update(error=f"{type(e).__name__}: {str(e)[:120]}")
    from apps.worker.ops_live import _PORTAL_ORIGIN, BASE
    d["origin_ok"] = (str(d.get("origin") or "") == _PORTAL_ORIGIN
                      and str(d.get("href") or "").startswith(BASE))
    d["is_chrome_error"] = "chrome-error" in str(d.get("href") or "")
    return d


def run_primer(ops, log) -> dict:
    """The EXACT call do_apply makes -- same URL, same empty captcha, same timeout, same method."""
    from apps.worker.ops_live import SLOTS_URL
    out = {"sent": {"url": SLOTS_URL + "?posto_id=" + str(ops.posto_pdf),
                    "body": {"posto_id": ops.posto_pdf, "captcha": ""}, "timeout": 45,
                    "method": "browser_form_post (navigation)"}}
    t0 = time.time()
    try:
        final_url, html = ops.session.browser_form_post(
            SLOTS_URL + "?posto_id=" + str(ops.posto_pdf),
            {"posto_id": ops.posto_pdf, "captcha": ""},
            timeout=45,
        )
        body = html or ""
        low = body.lower()
        # storm-auto documents a 28501-byte DataDome page for the equivalent /slots request
        # (batch_apply.py:1961). Ours came back 28,514B. Settle it on markers, not on size.
        markers = [m for m in ("datadome", "captcha-delivery", "/ch/bd.js", "geo.captcha-delivery",
                               "body{margin:0;background:#fff}", "interstitial", "blocked",
                               "are you a human", "verificar", "recaptcha")
                   if m in low]
        looks_json = body.strip()[:1] in ("[", "{")
        out.update(secs=round(time.time() - t0, 1), final_url=final_url,
                   bytes=len(body), snippet=body[:220].replace("\n", " "),
                   challenge_markers=markers, looks_like_json=looks_json,
                   title=(body.split("<title>")[1].split("</title>")[0][:90]
                          if "<title>" in body else None))
        _dump = pathlib.Path("/tmp/claude-1001/-srv-projects-storm-hub/"
                             "7609ba5c-b27c-43a3-ac46-d6222e36f6d2/scratchpad/"
                             f"primer_body_{int(time.time())}.html")
        _dump.write_text(body)
        out["body_file"] = str(_dump)
    except Exception as e:
        out.update(secs=round(time.time() - t0, 1),
                   raised=f"{type(e).__name__}: {str(e)[:160]}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--geo", default="DE")
    ap.add_argument("--provider", default="soax")
    ap.add_argument("--booking", default="E2E VALIDATION 0827")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--gap", type=float, default=20.0)
    a = ap.parse_args()

    def log(t): print(f"  {time.strftime('%H:%M:%S')} {t}", flush=True)

    from formstate_ttl_probe import warm_one
    log(f"warming one session (provider={a.provider} geo={a.geo} booking={a.booking!r})")
    ops = warm_one("primer", a.geo, log, booking_name=a.booking, provider=a.provider)
    if ops is None:
        log("could not warm -- login wall. The primer question is NOT exercised.")
        return 2

    results = []
    try:
        for i in range(1, a.rounds + 1):
            log(f"--- round {i}: CONTROL (no primer) ---")
            before_c = doc_state(ops, "control/before")
            time.sleep(2)
            after_c = doc_state(ops, "control/after")
            log(f"    control  origin_ok {before_c['origin_ok']} -> {after_c['origin_ok']}  "
                f"href={str(after_c.get('href'))[:70]}")

            log(f"--- round {i}: PRIMER ---")
            before_p = doc_state(ops, "primer/before")
            pr = run_primer(ops, log)
            after_p = doc_state(ops, "primer/after")
            log(f"    primer   -> {pr.get('secs')}s {pr.get('bytes', pr.get('raised'))}"
                f" final_url={str(pr.get('final_url'))[:70]}")
            log(f"    document origin_ok {before_p['origin_ok']} -> {after_p['origin_ok']}"
                f"  chrome_error={after_p['is_chrome_error']}  href={str(after_p.get('href'))[:70]}")
            log("    STOPPING HERE -- this is exactly where do_apply would submit. Nothing submitted.")

            results.append({"round": i, "control_before": before_c, "control_after": after_c,
                            "primer_before": before_p, "primer": pr, "primer_after": after_p})
            if i < a.rounds:
                # Re-warm so the next round starts from a live document, exactly as do_apply's
                # round-start re-warm would. Without this, round 2+ only measures round 1's damage.
                log(f"    re-warming before round {i+1}")
                try:
                    ok = ops._run_warmup_steps()
                    log(f"    re-warm {'ok' if ok else 'FAILED'}")
                except Exception as e:
                    log(f"    re-warm raised: {type(e).__name__}: {str(e)[:100]}")
                time.sleep(a.gap)
    finally:
        try: ops._close_session_quietly()
        except Exception: pass

    out = pathlib.Path("/tmp/claude-1001/-srv-projects-storm-hub/"
                       "7609ba5c-b27c-43a3-ac46-d6222e36f6d2/scratchpad/slots_primer_probe.json")
    out.write_text(json.dumps(results, indent=2))
    n = len(results)
    broke = sum(1 for r in results if r["primer_after"]["is_chrome_error"]
                and not r["primer_before"]["is_chrome_error"])
    ctrl_broke = sum(1 for r in results if r["control_after"]["is_chrome_error"])
    print(f"\n=== VERDICT INPUT ===")
    print(f"  rounds: {n}")
    print(f"  primer turned a healthy document into chrome-error : {broke}/{n}")
    print(f"  control (no primer) went to chrome-error by itself : {ctrl_broke}/{n}")
    print(f"  raw: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

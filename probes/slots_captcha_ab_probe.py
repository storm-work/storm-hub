#!/usr/bin/env python3
"""A/B: does a SOLVED CAPTCHA change what /slots returns, and therefore the pre-submit state?

THE QUESTION. storm-hub's do_apply sends /slots with `captcha: ""` and gets 175 B back; the submit
that follows is bounced to `questionario` (form_stale). storm-auto solves a SCHEDULE_EVISA token
first (`race_all(..., "SCHEDULE_EVISA", proxy, min_score=50)`) and its /slots returns the real slot
list. The hypothesis is that a CAPTCHA-authenticated /slots arms the portal's booking cursor.

THIS PROBE IS THE CHEAP GATE BEFORE SPENDING A REAL BOOKING. It runs both arms and stops at the
/slots boundary:

    arm A   captcha=""              -> record status/bytes/final_url/markers
    arm B   captcha=<solved token>  -> record the same

If B's /slots is indistinguishable from A's, the cursor hypothesis is already weakened and there is
no point spending a real submit on it. If B returns a genuine slot payload, the submit question
becomes worth the irreversible run.

IT NEVER SUBMITS. SubmeterVistoCriaPDF is not called. No slot is leased, no row is written, nothing
is booked. Fresh session per arm -- no state is shared between them.

The token is never logged; only whether one was obtained and its length.
"""
from __future__ import annotations

import argparse, json, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

SD = pathlib.Path("/tmp/claude-1001/-srv-projects-storm-hub/"
                  "7609ba5c-b27c-43a3-ac46-d6222e36f6d2/scratchpad")
_STATE_JS = ("JSON.stringify({o:location.origin,h:location.href,"
             "t:(document.title||''),len:(document.documentElement.outerHTML||'').length})")
_MARKERS = ("datadome", "captcha-delivery", "/ch/bd.js", "questionario", "getslots",
            "cmbperiodo", "captchadiv", "não foi possível", "request could not be processed",
            "authentication.jsp", "sessao", "erro")


def doc(ops, tag):
    d = {"tag": tag}
    try:
        raw = ops.session.browser_eval(_STATE_JS, timeout=20)
        st = json.loads(raw) if isinstance(raw, str) else (raw or {})
        d.update(origin=st.get("o"), href=st.get("h"), title=st.get("t"))
    except Exception as e:
        d["error"] = f"{type(e).__name__}: {str(e)[:100]}"
    from apps.worker.ops_live import _PORTAL_ORIGIN, BASE
    d["origin_ok"] = (str(d.get("origin") or "") == _PORTAL_ORIGIN
                      and str(d.get("href") or "").startswith(BASE))
    return d


def one_arm(arm: str, want_token: bool, geo: str, provider: str, booking: str, log) -> dict:
    from formstate_ttl_probe import warm_one
    from apps.worker.ops_live import SLOTS_URL
    rec = {"arm": arm, "captcha_requested": want_token}

    ops = warm_one(arm, geo, log, booking_name=booking, provider=provider)
    if ops is None:
        rec["result"] = "login_wall"
        return rec
    rec["identity"] = getattr(ops, "identity_id", None)
    rec["posto_pdf"] = ops.posto_pdf
    try:
        token = ""
        if want_token:
            from libs.engine.vendor import race_all
            t0 = time.time()
            try:
                token = race_all(ops.capsolver_keys, [], [], [],
                                 "SCHEDULE_EVISA", ops.warm_proxy, min_score=50) or ""
                rec["solve_secs"] = round(time.time() - t0, 1)
            except Exception as e:
                rec["solve_error"] = f"{type(e).__name__}: {str(e)[:140]}"
        rec["captcha_present"] = bool(token)
        rec["captcha_length"] = len(token)          # never the token itself
        log(f"[{arm}] captcha_present={bool(token)} length={len(token)}")

        rec["pre_slots"] = doc(ops, "pre-slots")
        url = SLOTS_URL + "?posto_id=" + str(ops.posto_pdf)
        rec["request"] = {"url": url, "body_keys": ["posto_id", "captcha"],
                          "method": "browser_form_post (navigation)", "timeout": 45}
        t0 = time.time()
        try:
            fu, html = ops.session.browser_form_post(
                url, {"posto_id": ops.posto_pdf, "captcha": token}, timeout=45)
            body = html or ""
            low = body.lower()
            rec["response"] = {
                "elapsed_ms": int((time.time() - t0) * 1000),
                "bytes": len(body), "final_url": fu,
                "title": (body.split("<title>")[1].split("</title>")[0].strip()[:90]
                          if "<title>" in body else None),
                "looks_like_json": body.strip()[:1] in ("[", "{"),
                "markers": [m for m in _MARKERS if m in low],
                "head": body[:200].replace("\n", " "),
            }
            (SD / f"slots_{arm}_{int(time.time())}.html").write_text(body)
            log(f"[{arm}] /slots -> {rec['response']['elapsed_ms']}ms "
                f"{rec['response']['bytes']}B json={rec['response']['looks_like_json']} "
                f"title={rec['response']['title']!r}")
        except Exception as e:
            rec["response"] = {"raised": f"{type(e).__name__}: {str(e)[:160]}",
                               "elapsed_ms": int((time.time() - t0) * 1000)}
            log(f"[{arm}] /slots raised: {rec['response']['raised']}")
        rec["post_slots"] = doc(ops, "post-slots")
        log(f"[{arm}] STOPPING -- SubmeterVistoCriaPDF is NOT called. Nothing booked.")
    finally:
        try: ops._close_session_quietly()
        except Exception: pass
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--geo", default="DE")
    ap.add_argument("--provider", default="soax")
    ap.add_argument("--booking", default="E2E VALIDATION 0827")
    a = ap.parse_args()

    def log(t): print(f"  {time.strftime('%H:%M:%S')} {t}", flush=True)

    out = []
    for arm, want in (("A-empty", False), ("B-solved", True)):
        log(f"########## ARM {arm} ##########")
        out.append(one_arm(arm, want, a.geo, a.provider, a.booking, log))
    (SD / "slots_captcha_ab.json").write_text(json.dumps(out, indent=2, default=str))

    print("\n=== A/B on the /slots response ===")
    for r in out:
        resp = r.get("response") or {}
        print(f"  {r['arm']:<9} captcha_len={r.get('captcha_length')} "
              f"bytes={resp.get('bytes')} json={resp.get('looks_like_json')} "
              f"title={str(resp.get('title'))[:52]!r} markers={resp.get('markers')}")
    print(f"  raw: {SD/'slots_captcha_ab.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

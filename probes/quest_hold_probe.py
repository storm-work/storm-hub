#!/usr/bin/env python3
"""QUEST-HOLD endurance probe: park a logged-in browser session on Questionario and keep it alive.

HYPOTHESIS (operator, 2026-10-29): the hold does not need the heavy Schedule.jsp re-warm chain. Parking
on Questionario and issuing ONE quest step (QuestNextQuestion) at a jittered interval, then reloading
Questionario once every step has been answered, renews the portal session/cursor indefinitely -- with
a fraction of the traffic, all through the real browser (the strict anti-bot window challenges
detached primp, not the browser).

What it does (never submits, never books -- the identity stays unspent):
  1. login via the production LiveEngineOps (login only, no warm-up)
  2. home-lang POST + Questionario nav, then every INTERVAL (uniform jitter in --min-s..--max-s):
       - step i of the identity's quest-step list, as the same in-page fetch the warm-up chain uses
       - after the last step: reload Questionario and start over
  3. every event is classified (ok | challenge | session_expired | dead_exit | portal_error | http_N)
     and logged to var/quest_hold_probe_<tag>.jsonl
  4. stops at --minutes or on the first FATAL verdict (session_expired / dead_exit). A challenge or
     5xx is recorded and the hold continues (portal pressure is not session death).
  5. at the end (survivor) runs the full browser warm-up chain once: did the held session still reach
     the bookable Schedule.jsp cursor?

Usage:
  cd /srv/projects/storm-hub
  .venv/bin/python probes/quest_hold_probe.py --booking "Test 2032 Donut" --pool soax --geo DE \
      --min-s 60 --max-s 300 --minutes 40 --tag jit60-300
  ... --login-only --tag wall-0905     # login attempt only (login-wall mapping), then exit
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from probes.hybrid_booking_probe import _arg, _load_env, _warm  # noqa: E402


def _verdict(ops, status, body_or_url: str, final_url: str = "") -> str:
    from apps.worker.ops_live import SessionState, _is_challenge_body, classify_session_state
    if ops._is_dead_exit_response(body_or_url, final_url):
        return "dead_exit"
    if _is_challenge_body(body_or_url):
        return "challenge"
    if isinstance(status, int) and status != 200:
        return f"http_{status}"
    st = classify_session_state(body_or_url, final_url, warmed=False)
    if st == SessionState.SESSION_EXPIRED:
        return "session_expired"
    if st == SessionState.PORTAL_ERROR:
        return "portal_error"
    return "ok"


def main() -> int:
    _load_env()
    pool = _arg("--pool", "soax").lower()
    geo = _arg("--geo")
    lo = float(_arg("--min-s", "60"))
    hi = float(_arg("--max-s", "300"))
    minutes = float(_arg("--minutes", "40"))
    tag = _arg("--tag", f"{int(lo)}-{int(hi)}")
    booking_name = _arg("--booking")

    from apps.hub.db import SessionLocal
    from sqlalchemy import select
    from libs.db import models as M
    db = SessionLocal()
    bk = db.scalar(select(M.Booking).where(M.Booking.name == booking_name)) if booking_name else None
    if bk is None:
        bk = db.scalar(select(M.Booking).order_by(M.Booking.created_at.desc()))
    posto, nat, res = str(bk.posto_id), bk.nationality, bk.residence
    db.close()

    out = _ROOT / "var" / f"quest_hold_probe_{tag}.jsonl"
    out.parent.mkdir(exist_ok=True)
    t0 = time.time()

    def log(m):
        print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

    def rec(**kw):
        kw["t"] = round(time.time() - t0, 1)
        with out.open("a") as f:
            f.write(json.dumps(kw) + "\n")

    log(f"posto={posto} nat={nat} res={res} pool={pool} geo={geo} interval={lo}-{hi}s for {minutes}min")
    ops, quest_steps, res = _warm(pool, geo, posto, nat, res, log, login_only=True)
    if ops is None:
        log("could not LOG IN any identity -- re-run (login is a lottery)")
        return 1
    if "--login-only" in sys.argv:
        # login-wall mapping: one login attempt and out -- no hold, no warm-up chain
        log("LOGIN-ONLY: logged in; closing")
        rec(ev="login_only", ok=True)
        ops._close_session_quietly()
        return 0
    from apps.worker.ops_live import HOME_URL, QUEST_NEXT, QUEST_URL
    steps = quest_steps.get(res if res != nat else nat, [])
    if not steps:
        log("no quest steps for this nationality/residence"); return 2
    base_qs = {"lang": "PT", "nacionalidade": nat}
    rec(ev="start", steps=len(steps), lo=lo, hi=hi, minutes=minutes)

    sess = ops.session
    survived = True
    fatal = ""

    def reload_quest() -> str:
        sess.browser_eval(
            "(async()=>{const r=await fetch('%s',{method:'POST',credentials:'include',"
            "headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'lang=PT'});"
            "return r.status;})()" % HOME_URL)
        final = sess.browser_nav(QUEST_URL, timeout=35, skip_cookie_sync=True)
        page = ops._keepalive_read_page()
        v = _verdict(ops, 200, page, final or "")
        rec(ev="reload", verdict=v, len=len(page), url=(final or "")[:120])
        log(f"reload Questionario -> {v} ({len(page)}B)")
        return v

    def do_step(i: int) -> str:
        url = f"{QUEST_NEXT}?{urlencode({**base_qs, **steps[i]})}"
        js = ("(async()=>{const r=await fetch(%r,{method:'GET',credentials:'include',"
              "headers:{'Accept':'text/plain, */*; q=0.01','X-Requested-With':'XMLHttpRequest',"
              "'Sec-Fetch-Mode':'cors','Sec-Fetch-Dest':'empty','Sec-Fetch-Site':'same-origin',"
              "'Referer':%r}});const b=await r.text();return {status:r.status,len:b.length,"
              "body:b.substring(0,4000)};})()") % (url, QUEST_URL)
        res_ = sess.browser_eval(js)
        status = res_.get("status") if isinstance(res_, dict) else None
        body = (res_.get("body") if isinstance(res_, dict) else str(res_)) or ""
        v = _verdict(ops, status, body, "")
        # a step answer is a questionnaire fragment, not a page: classify_session_state(warmed=False)
        # can only say "ok" or an explicit portal/expiry signal for it, which is what we want
        rec(ev="step", i=i + 1, status=status, verdict=v, len=(res_ or {}).get("len") if isinstance(res_, dict) else None)
        log(f"step {i + 1}/{len(steps)} -> status={status} {v}")
        return v

    try:
        v = reload_quest()
        i = 0
        deadline = time.time() + minutes * 60
        n = 0
        while time.time() < deadline:
            wait = random.uniform(lo, hi)
            rec(ev="wait", s=round(wait, 1))
            time.sleep(wait)
            n += 1
            try:
                v = reload_quest() if i >= len(steps) else do_step(i)
                if i >= len(steps):
                    i = 0
                else:
                    i += 1
            except Exception as e:
                v = "dead_exit" if "Failed to fetch" in str(e) or "chrome-error" in str(e) else "exception"
                rec(ev="error", verdict=v, err=str(e)[:200])
                log(f"event raised {type(e).__name__}: {str(e)[:120]} -> {v}")
            if v in ("session_expired", "dead_exit", "exception"):
                survived, fatal = False, v
                break
        held = round((time.time() - t0) / 60, 1)
        log(f"HOLD ENDED after {held} min, {n} events, survived={survived} fatal={fatal or '-'}")
        rec(ev="hold_end", min=held, events=n, survived=survived, fatal=fatal)
        if survived:
            resume = 0 < i < len(steps)
            log(f"final check: browser warm-up chain on the held session "
                f"({'RESUMING at step %d/%d' % (i + 1, len(steps)) if resume else 'full chain'}) ...")
            t1 = time.time()
            if resume:
                ops._qh_pos = i          # the engine's own hand-off (ops_live._qh_take)
            ok = ops.do_warmup()
            warmed = bool(ok and ops.is_warmed())
            rec(ev="final_warm", resumed=resume, ok=bool(ok), warmed=warmed, s=round(time.time() - t1, 1),
                err=getattr(ops, "_warmup_last_error", ""))
            log(f"FINAL WARM-UP -> ok={ok} warmed={warmed} ({time.time() - t1:.0f}s) "
                f"err={getattr(ops, '_warmup_last_error', '')!r}")
    finally:
        try:
            ops._close_session_quietly()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

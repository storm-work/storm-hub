#!/usr/bin/env python3
"""What actually keeps a WARMED session usable? (hold/keepalive validation)

Two lifetimes are involved and they are NOT the same thing:

    Vistos_sid          the authenticated session cookie
    server-side form    the ScheduleController draft/cursor that a submit needs

A session can be perfectly authenticated while its form state is gone, so "the browser is still
open" and "an authenticated request returned 200" prove nothing about bookability.

THE CONFOUND THIS PROBE IS BUILT AROUND. Production's keepalive, once warmed, navigates
Schedule.jsp and checks for the scheduling markers -- so the observation request and the candidate
keepalive request are THE SAME REQUEST. Probing repeatedly therefore cannot, on its own, tell
"the GET is holding the state open" apart from "the state simply lasts this long". Two arms are
needed:

    poll     warm, then GET Schedule.jsp every --interval        (production's actual request)
    silent   warm, then NO portal traffic at all until --silent-at, then ONE GET

Compared at the same elapsed time:
    poll alive + silent DEAD   -> the GET extends the form state          (keepalive works)
    poll alive + silent alive  -> TTL simply exceeds the window; GET's role NOT proven
    poll DEAD  + silent DEAD   -> the GET does NOT extend it              (keepalive insufficient)

WHAT IT OBSERVES, and the honest limit. "Form state alive" here means Schedule.jsp still renders
with _SCHED_PAGE_MARKERS -- the exact test production's keepalive uses to decide whether to
re-warm. It does NOT prove a submit would succeed; only a real submit proves that, and this probe
never submits. Reported as "schedule page still bookable", not as "the cursor is valid".

SAFETY: browser_nav GETs only after warm-up. No ScheduleController POST beyond the warm-up chain
itself, no slot selection, no do_apply, no submit. Nothing here can book.

    .venv/bin/python probes/formstate_ttl_probe.py --minutes 20 --interval 120 --silent-at 480
"""
from __future__ import annotations

import argparse, json, pathlib, sys, threading, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# One fixed weekday, reused every round by the discovery arm: a changing date must never be
# mistakable for a changing form state. Format matches do_discover: YYYY/MM/DD.
_DISCOVER_DATE = "2026/09/08"


def observe(ops, tag: str) -> dict:
    """One Schedule.jsp GET + classification. Never raises."""
    from apps.worker.ops_live import SCHED_JSP, _SCHED_PAGE_MARKERS, _HOME_MARKERS
    d = {"tag": tag, "ts": time.time()}
    url = SCHED_JSP + "?posto_id=" + str(ops.posto_pdf or ops.posto_id)
    try:
        final = ops.session.browser_nav(url, timeout=45)
    except Exception as e:
        d.update(ok=False, error=f"{type(e).__name__}: {e}"[:140])
        return d
    d["final_url"] = final
    try:
        html = ops.session.browser_eval("document.documentElement.outerHTML") or ""
    except Exception as e:
        d.update(ok=False, error=f"{type(e).__name__}: {e}"[:140])
        return d
    low = html.lower()
    d["ok"] = True
    d["bytes"] = len(html)
    d["markers"] = [m for m in _SCHED_PAGE_MARKERS if m in html]
    d["form_state_alive"] = len(d["markers"]) > 0
    # Did the portal bounce us off Schedule.jsp entirely? (the 302-equivalent, post-redirect)
    d["redirected_off_schedule"] = "schedule.jsp" not in (final or "").lower()
    d["session_lost"] = "sessionlost" in (final or "").lower()
    d["auth_redirect"] = "authentication.jsp" in (final or "").lower()
    # Vistos_sid still in the jar?
    try:
        d["vistos_sid"] = any(c.get("name") == "Vistos_sid" for c in ops.session.get_all_cookies())
    except Exception:
        d["vistos_sid"] = None
    # authenticated at all? (logout link only exists with a live session -- validated 2026-08-26)
    d["authenticated"] = "terminar sess" in low
    return d


def warm_one(arm: str, geo: str, log, booking_name: str = "Test2") -> object | None:
    """Real login + warm-up through the production code path. Returns LiveEngineOps or None."""
    from sqlalchemy import select
    from apps.hub.db import SessionLocal
    from libs.db import models as M
    from libs.domain.enums import SettingsLayer as L, SubAccountStatus, BOOKING_ACTIVE_STATUSES
    from libs.domain.settings.resolver import resolve
    from libs.engine.config import EngineConfig
    from libs.engine import vendor_env
    from libs.proxies.lease import conn_url, session_id
    from apps.hub.settings_store import get_global
    from apps.hub.main import _live_account, _rank_live_sub_accounts
    import random as _r

    db = SessionLocal()
    bk = db.scalar(select(M.Booking).where(M.Booking.name == booking_name))
    if bk is None:
        db.close()
        log(f"[{arm}] no booking named {booking_name!r} -- cannot source posto/nationality")
        return None
    eff = resolve({L.GLOBAL: get_global(db), L.BOOKING: (bk.settings_json or {})})
    eff["ROLE"] = "apply"; eff["WARMUP_ONLY"] = True; eff["CAPTURE_ONLY"] = True
    eff["LOGIN_TRANSIENT_RETRY_MAX"] = 10
    cfg = EngineConfig.from_settings(eff, no_book=True)
    vendor_env.apply(cfg.raw)
    from apps.worker.ops_live import LiveEngineOps

    appt = {r[0] for r in db.execute(select(M.BookedAppointment.identity_id)).all() if r[0]}
    cands = db.scalars(select(M.Identity).where(M.Identity.status == "ACTIVE").limit(300)).all()
    _r.shuffle(cands)
    pool = db.scalars(select(M.Proxy).where(M.Proxy.provider == "webshare",
                                            M.Proxy.active.is_(True)).limit(1500)).all()
    _r.shuffle(pool)
    caps = [k.strip() for k in (get_global(db).get("CAPSOLVER_KEYS", "") or "").split(",") if k.strip()]
    quest = db.scalar(select(M.AppConfig).where(M.AppConfig.key == "quest_steps"))
    formd = db.scalar(select(M.AppConfig).where(M.AppConfig.key == "form_defaults"))

    tried = 0
    for ident in cands:
        if tried >= 6:
            break
        if ident.id in appt:
            continue
        subs = [s for s in _rank_live_sub_accounts(db, ident.id)
                if s.status != SubAccountStatus.BURNED]
        if not subs:
            continue
        tried += 1
        url = conn_url(pool[tried], geo=geo)
        log(f"[{arm}] login try {tried} {ident.full_name[:22]!r} webshare#{session_id(url)}")
        ops = LiveEngineOps(
            cfg, account=_live_account(ident, subs[0]), posto_id=str(bk.posto_id),
            nationality=bk.nationality, residence=bk.residence,
            quest_steps=(quest.value if quest else {}), form_defaults=(formd.value if formd else {}),
            capsolver_keys=caps, warm_proxy=url, headless=True, sub_account_id=subs[0].id,
            accounts=[dict(_live_account(ident, s), sub_account_id=s.id) for s in subs],
            identity_id=ident.id)
        ops._log = lambda t, a=arm: log(f"[{a}] {t}"[:150])
        try:
            if not ops.do_login():
                ops._close_session_quietly(); continue
            if not ops.do_warmup():
                log(f"[{arm}] warm failed")
                ops._close_session_quietly(); continue
        except Exception as e:
            log(f"[{arm}] exception: {type(e).__name__}: {e}")
            try: ops._close_session_quietly()
            except Exception: pass
            continue
        db.close()
        log(f"[{arm}] WARMED (posto_pdf={ops.posto_pdf})")
        return ops
    db.close()
    return None


def observe_discover(ops, tag: str) -> dict:
    """One getPeriodosOcupados round -- the candidate keepalive AND its own liveness indicator.

    Symmetric with observe() by design: in the Schedule.jsp arm the polling request and the
    liveness test are the same request, so the discovery arm must work the same way or the
    comparison is not like-for-like. The response tells us both things at once, because
    getPeriodosOcupados depends on the warmed context (MEASURED 2026-08-26):

        warm context : ~192B, `periodos_ocupados='...'` marker present
        expired      : ~168B, marker ABSENT

    Uses the SAME date every round so a changing date can never be mistaken for a changing state.
    """
    from apps.worker.ops_live import GETPERIODOS_URL
    import re
    d = {"tag": tag, "ts": time.time()}
    url = f"{GETPERIODOS_URL}?id_posto={ops.posto_pdf or ops.posto_id}&data_agendamento={_DISCOVER_DATE}"
    try:
        final = ops.session.browser_nav(url, timeout=45)
    except Exception as e:
        d.update(ok=False, error=f"{type(e).__name__}: {e}"[:140])
        return d
    try:
        html = ops.session.browser_eval("document.documentElement.outerHTML") or ""
    except Exception as e:
        d.update(ok=False, error=f"{type(e).__name__}: {e}"[:140])
        return d
    m = re.search(r"periodos_ocupados='([0-9]*)", html)
    d["ok"] = True
    d["final_url"] = final
    d["bytes"] = len(html)
    d["markers"] = ["periodos_ocupados"] if m else []
    d["token"] = m.group(1) if m else None      # digits only -- no PII
    d["form_state_alive"] = bool(m)
    d["redirected_off_schedule"] = False
    d["session_lost"] = "sessionlost" in (final or "").lower()
    d["auth_redirect"] = "authentication.jsp" in (final or "").lower()
    try:
        d["vistos_sid"] = any(c.get("name") == "Vistos_sid" for c in ops.session.get_all_cookies())
    except Exception:
        d["vistos_sid"] = None
    d["authenticated"] = None                   # not readable from this endpoint -- do not guess
    return d


def run_arm(arm, mode, geo, minutes, interval, silent_at, sink, lock, log, booking="Test2"):
    ops = warm_one(arm, geo, log, booking)
    if ops is None:
        with lock:
            sink.write(json.dumps({"arm": arm, "mode": mode, "fatal": "could not warm"}) + "\n"); sink.flush()
        return
    t0 = time.time()
    deadline = t0 + minutes * 60
    try:
        # Baseline immediately after warm-up -- both arms take this, it is the reference point.
        _obs = observe_discover if mode == "discover" else observe
        d = _obs(ops, "baseline"); d.update(arm=arm, mode=mode, elapsed_s=round(time.time()-t0, 1))
        with lock: sink.write(json.dumps(d)+"\n"); sink.flush()
        log(f"[{arm}] t=0.0m form_state={d.get('form_state_alive')} markers={len(d.get('markers') or [])}")
        if mode == "silent":
            # NO portal traffic at all until silent_at, then exactly one GET.
            end = min(deadline, t0 + silent_at)
            while time.time() < end:
                time.sleep(min(10, end - time.time()))
            d = _obs(ops, "after-silence"); d.update(arm=arm, mode=mode, elapsed_s=round(time.time()-t0,1))
            with lock: sink.write(json.dumps(d)+"\n"); sink.flush()
            log(f"[{arm}] t={d['elapsed_s']/60:.1f}m AFTER SILENCE form_state={d.get('form_state_alive')} "
                f"auth={d.get('authenticated')} url={str(d.get('final_url'))[-40:]}")
        else:
            nxt = t0 + interval
            while time.time() < deadline:
                while time.time() < min(nxt, deadline):
                    time.sleep(min(5, max(0.1, min(nxt, deadline) - time.time())))
                if time.time() >= deadline: break
                d = _obs(ops, "poll"); d.update(arm=arm, mode=mode, elapsed_s=round(time.time()-t0,1))
                with lock: sink.write(json.dumps(d)+"\n"); sink.flush()
                log(f"[{arm}] t={d['elapsed_s']/60:5.1f}m form_state={d.get('form_state_alive')} "
                    f"auth={d.get('authenticated')} bytes={d.get('bytes')}")
                nxt += interval
    finally:
        try: ops._close_session_quietly()
        except Exception: pass


def report(path):
    rows = [json.loads(l) for l in pathlib.Path(path).read_text().splitlines() if l.strip()]
    from collections import defaultdict
    by = defaultdict(list)
    for r in rows: by[r.get("arm")].append(r)
    print(f"\n=== form-state TTL: {len(rows)} observations ===\n")
    for arm, rs in sorted(by.items()):
        rs.sort(key=lambda r: r.get("elapsed_s", 0))
        if any("fatal" in r for r in rs):
            print(f"-- {arm}: FATAL {[r['fatal'] for r in rs if 'fatal' in r]}"); continue
        print(f"-- {arm} [{rs[0].get('mode')}]")
        for r in rs:
            print(f"   t={r.get('elapsed_s',0)/60:5.1f}m  form_state={str(r.get('form_state_alive')):<5} "
                  f"auth={str(r.get('authenticated')):<5} sid={str(r.get('vistos_sid')):<5} "
                  f"bytes={r.get('bytes')} markers={len(r.get('markers') or [])} "
                  f"redirected={r.get('redirected_off_schedule')} err={r.get('error','')}")
        alive=[r for r in rs if r.get("form_state_alive")]
        dead=[r for r in rs if r.get("ok") and not r.get("form_state_alive")]
        print(f"   last form-state ALIVE: t={alive[-1]['elapsed_s']/60:.1f}m" if alive else "   never alive")
        if dead: print(f"   first form-state DEAD : t={dead[0]['elapsed_s']/60:.1f}m")
        print()
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=float, default=20.0)
    ap.add_argument("--interval", type=float, default=120.0, help="poll arm: seconds between GETs")
    ap.add_argument("--silent-at", type=float, default=480.0, help="silent arm: seconds before its ONE GET")
    ap.add_argument("--geo", default="it")
    ap.add_argument("--booking", default="Test2",
                    help="booking to source posto/nationality/residence from")
    ap.add_argument("--out", default="var/formstate_ttl.jsonl")
    ap.add_argument("--report")
    a = ap.parse_args()
    if a.report: return report(a.report)
    def log(t): print(f"  {time.strftime('%H:%M:%S')} {t}", flush=True)
    out = pathlib.Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    with out.open("w") as sink:
        ths = [threading.Thread(target=run_arm, args=("schedule_poll","poll",a.geo,a.minutes,a.interval,a.silent_at,sink,lock,log,a.booking)),
               threading.Thread(target=run_arm, args=("discover_poll","discover",a.geo,a.minutes,a.interval,a.silent_at,sink,lock,log,a.booking)),
               threading.Thread(target=run_arm, args=("silent","silent",a.geo,a.minutes,a.interval,a.silent_at,sink,lock,log,a.booking))]
        for t in ths: t.start()
        for t in ths: t.join()
    return report(str(out))


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""FULL hybrid-approach booking probe: browser login+warmup -> DETACHED (primp) submit -> %PDF.

The quick read-test (httpx_postlogin_probe.py) showed detached clients get real /slots answers, but
it SKIPPED warmup -- so the cursor wasn't established and the submit was never tested. This probe
runs the REAL system path through the production code (LiveEngineOps.do_login + do_warmup +
do_discover), then performs the SubmeterVistoCriaPDF submit with a DETACHED primp client (same exit,
synced cookies) and verifies the %PDF -- proving (or disproving) that the hybrid books for real.

Flow (mirrors apps/worker + the confirmed HAR):
  1. LiveEngineOps.do_login()                      -> authed (browser; reCAPTCHA needs it)
  2. LiveEngineOps.do_warmup()                      -> Schedule.jsp cursor (posto_pdf) established
  3. LiveEngineOps.do_discover([dates])             -> find a REAL available (date, period)
  4. [--book] primp POST SubmeterVistoCriaPDF       -> detached submit (booking_payload, synced cookies)
  5. [--book] parse MostrarPdf -> primp GET         -> %PDF bytes -> verified booking

SAFETY: without --book it STOPS after discovery (no submit, nothing booked) and just reports what it
WOULD submit + that discovery worked. WITH --book it makes a REAL appointment on a real identity --
operator-run only, exactly like a live apply. Retries login+warmup across identities/exits so a login
failure doesn't rob the test.

Usage:
  cd /srv/projects/storm-hub
  .venv/bin/python probes/hybrid_booking_probe.py --booking "Test2" --pool soax --geo DE [--book]
  .venv/bin/python probes/hybrid_booking_probe.py --posto 3059 --nat CPV --res IRL --pool soax [--book]
"""
import os
import random
import sys
import re
from pathlib import Path
from urllib.parse import urlencode

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

ORIGIN = "https://pedidodevistos.mne.gov.pt"
BASE = ORIGIN + "/VistosOnline"
SUBMIT = BASE + "/SubmeterVistoCriaPDF"
SCHED = BASE + "/Schedule.jsp"


def _load_env():
    env = _ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def _arg(flag, default=None):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def _mint_proxy(pool, settings, geo):
    if pool in ("soax", "rotating"):
        from libs.proxies.rotating import mint_rotating_url
        return mint_rotating_url("soax", "rotating", settings=settings, geo=geo or None)
    from sqlalchemy import select
    from apps.hub.db import SessionLocal
    from libs.db import models as M
    from libs.proxies.lease import conn_url
    db = SessionLocal()
    rows = db.scalars(select(M.Proxy).where(M.Proxy.provider == pool, M.Proxy.active.is_(True))).all()
    db.close()
    return conn_url(random.choice(rows), geo=geo) if rows else None


def _warm(pool, geo, posto, nat, res, log, headless=True, tries=8, exit_tries=4):
    """Login + warmup via the production LiveEngineOps, retrying identities/exits. Returns ops|None."""
    from sqlalchemy import select
    from apps.hub.db import SessionLocal
    from libs.db import models as M
    from libs.domain.enums import SettingsLayer as L
    from libs.domain.settings.resolver import resolve
    from libs.engine.config import EngineConfig
    from libs.engine import vendor_env
    from apps.hub.settings_store import get_global
    from apps.hub.main import _live_account, _rank_live_sub_accounts
    from libs.proxies.rotating import roll_rotating_url

    db = SessionLocal()
    g = get_global(db)
    caps = [k.strip() for k in (g.get("CAPSOLVER_KEYS", "") or "").split(",") if k.strip()]
    quest = db.scalar(select(M.AppConfig).where(M.AppConfig.key == "quest_steps"))
    formd = db.scalar(select(M.AppConfig).where(M.AppConfig.key == "form_defaults"))
    quest_steps = (quest.value if quest else {})
    form_defaults = (formd.value if formd else {})
    booked = {r[0] for r in db.execute(select(M.BookedAppointment.identity_id)).all() if r[0]}
    running = {r[0] for r in db.execute(select(M.Worker.identity_id).where(M.Worker.status == "RUNNING")).all()}
    cands = db.scalars(select(M.Identity).where(M.Identity.status == "ACTIVE").limit(400)).all()
    random.shuffle(cands)
    # SESSION CACHE: prefer the last identity we warmed, so do_login restores ITS fresh checkpoint
    # (skip the whole login lottery) when re-run within the Vistos_sid TTL. First run fights login
    # once; re-runs ride the banked session.
    _cache = _ROOT / "var" / "probe_session_identity.txt"
    _pref = _cache.read_text().strip() if _cache.exists() else ""
    if _pref:
        cands.sort(key=lambda i: 0 if i.id == _pref else 1)
        if cands and cands[0].id == _pref:
            log(f"session cache: trying banked identity {_pref[:8]} first (checkpoint restore)")

    cfg = EngineConfig.from_settings(resolve({L.GLOBAL: g, L.BOOKING: {
        "DISCOVERY_SOURCE": "slots", "KEEPALIVE_MODE": "schedule"}}))
    vendor_env.apply(resolve({L.GLOBAL: g}))
    # Faster login for the experiment: HUMAN_BEHAVIOR pacing lengthens the login, widening the window
    # in which a rotating exit can move mid-solve ('confirma o reCAPTCHA'). The warmup/submit do not
    # depend on it. Set --human to keep the production pacing. (Must precede the ops_live import --
    # the vendor session reads this at import time.)
    if "--human" not in sys.argv:
        os.environ["HUMAN_BEHAVIOR"] = "0"
    from apps.worker.ops_live import LiveEngineOps

    tried = 0
    for ident in cands:
        if tried >= tries:
            break
        if ident.id in booked or ident.id in running:
            continue
        subs = _rank_live_sub_accounts(db, ident.id)
        if not subs:
            continue
        tried += 1
        proxy = _mint_proxy(pool, g, geo)
        for k in range(1, exit_tries + 1):
            log(f"warm try {tried} {ident.full_name[:22]!r} exit {k}/{exit_tries}")
            ops = LiveEngineOps(
                cfg, account=_live_account(ident, subs[0]), posto_id=str(posto),
                nationality=nat, residence=res, quest_steps=quest_steps, form_defaults=form_defaults,
                capsolver_keys=caps, warm_proxy=proxy, headless=headless, sub_account_id=subs[0].id,
                accounts=[dict(_live_account(ident, s), sub_account_id=s.id) for s in subs],
                identity_id=ident.id)
            ops._log = lambda t: log("  " + str(t)[:140])
            try:
                if not ops.do_login():
                    ops._close_session_quietly(); proxy = roll_rotating_url(proxy); continue
                if not ops.do_warmup():
                    log("  warmup failed"); ops._close_session_quietly()
                    proxy = roll_rotating_url(proxy); continue
                db.close()
                try:
                    ops.save_checkpoint_now()
                    _cache.parent.mkdir(parents=True, exist_ok=True)
                    _cache.write_text(ident.id)
                    log("session cache: checkpoint banked -- re-run to restore (skips login)")
                except Exception as _ce:
                    log(f"session cache: save failed ({str(_ce)[:60]})")
                log(f"WARMED ✓ posto_pdf={ops.posto_pdf} ({ident.full_name[:22]!r})")
                return ops, quest_steps, res
            except Exception as e:
                log(f"  exception {type(e).__name__}: {str(e)[:100]}")
                try: ops._close_session_quietly()
                except Exception: pass
                proxy = roll_rotating_url(proxy)
    db.close()
    return None, quest_steps, res


def _detached_submit(ops, slot, quest_steps, res, log):
    """The HYBRID: SubmeterVistoCriaPDF via primp from the same exit + synced cookies. Returns dict."""
    import primp
    from libs.engine.payload import booking_payload
    cookies = {c["name"]: c["value"]
               for c in ops.session.get_all_cookies()
               if c.get("name") in ("Vistos_sid", "cookiesession1")}
    ua = (ops.session.browser_eval("navigator.userAgent") or "").strip()
    posto = str(ops.posto_pdf)
    body = urlencode(booking_payload(quest_steps, slot, residence=res))
    headers = {
        "User-Agent": ua or "Mozilla/5.0",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": SCHED + "?posto_id=" + posto,
        "Origin": ORIGIN,
        "sec-fetch-site": "same-origin",
        "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
    }
    pc = primp.Client(proxy=ops.warm_proxy, verify=False, timeout=90, follow_redirects=True)
    try: pc.set_cookies(ORIGIN + "/", cookies)
    except Exception: pass
    log(f"[hybrid] primp POST SubmeterVistoCriaPDF?posto_id={posto}  body={body}")
    r = pc.post(SUBMIT + "?posto_id=" + posto, data=body, headers=headers)
    html = r.text or ""
    log(f"[hybrid] submit -> status={r.status_code} len={len(html)} body[:160]={html[:160]!r}")
    # verify: parse MostrarPdf out of the response, fetch the PDF detached
    pdf = b""
    m = re.search(r'["\']([^"\']*MostrarPdf[^"\']*)["\']', html, re.I)
    if m:
        action = m.group(1)
        url = action if action.startswith("http") else (ORIGIN + action if action.startswith("/")
                                                         else BASE + "/" + action.lstrip("/"))
        inner = re.search(r'<form[^>]*MostrarPdf[^>]*>(.*?)</form>', html, re.I | re.S)
        params = dict(re.findall(r'name\s*=\s*["\']([^"\']+)["\'][^>]*value\s*=\s*["\']([^"\']*)["\']',
                                 inner.group(1) if inner else "", re.I))
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        try:
            rp = pc.get(url)
            pdf = rp.content if hasattr(rp, "content") else (rp.text or "").encode("latin-1", "replace")
            log(f"[hybrid] primp GET MostrarPdf -> status={rp.status_code} bytes={len(pdf)} "
                f"%PDF={pdf[:4]==b'%PDF'}")
        except Exception as e:
            log(f"[hybrid] MostrarPdf fetch error: {type(e).__name__}: {str(e)[:100]}")
    booked = pdf[:4] == b"%PDF"
    return {"status": r.status_code, "resp_len": len(html), "pdf_bytes": len(pdf), "booked": booked,
            "pdf": pdf}


def main() -> int:
    _load_env()
    pool = (_arg("--pool", "soax")).lower()
    geo = _arg("--geo")
    do_book = "--book" in sys.argv
    headless = "--headed" not in sys.argv
    posto = _arg("--posto"); nat = _arg("--nat"); res = _arg("--res")
    booking_name = _arg("--booking")

    if not posto:
        from apps.hub.db import SessionLocal
        from sqlalchemy import select
        from libs.db import models as M
        db = SessionLocal()
        bk = db.scalar(select(M.Booking).where(M.Booking.name == booking_name)) if booking_name else None
        if bk is None and booking_name:
            print(f"[probe] no booking named {booking_name!r} -- falling back to the most recent booking")
        if bk is None:
            bk = db.scalar(select(M.Booking).order_by(M.Booking.created_at.desc()))
        if bk is None:
            print("ERROR: no bookings exist -- give --posto/--nat/--res explicitly"); db.close(); return 2
        posto, nat, res = str(bk.posto_id), bk.nationality, bk.residence
        print(f"[probe] using booking {bk.name!r}")
        db.close()
    print(f"[probe] posto={posto} nat={nat} res={res} pool={pool} geo={geo} BOOK={do_book}")

    def log(m): print(m, flush=True)
    ops, quest_steps, res = _warm(pool, geo, posto, nat, res, log, headless=headless)
    if ops is None:
        print("[probe] could not warm any identity -- re-run (login is a lottery)."); return 1
    try:
        from datetime import date, timedelta
        from libs.engine.config import EngineConfig
        d = ops.cfg.discovery
        start = d.start_date or str(date.today() + timedelta(days=30))
        end = d.end_date or str(date.today() + timedelta(days=90))
        dates = []
        cur = date.fromisoformat(start)
        while cur <= date.fromisoformat(end) and len(dates) < 25:
            if cur.weekday() < 5:
                dates.append(cur.isoformat())
            cur += timedelta(days=1)
        print(f"[probe] discovering {len(dates)} dates via /slots (browser) ...")
        results = ops.do_discover(dates)
        found = [r for r in results if r.get("outcome") == "found" and r.get("periods")]
        print(f"[probe] discovery: {sum(1 for r in results if r.get('outcome')=='found')} found, "
              f"{sum(1 for r in results if r.get('outcome')=='empty')} empty, "
              f"{sum(1 for r in results if r.get('outcome')=='failed')} failed")
        if not found:
            print("[probe] NO AVAILABLE SLOT right now -- the detached /slots path works (see the "
                  "quick probe) but there is nothing to submit. Re-run when a slot is open.")
            return 0
        slot = {"date": found[0]["date"], "period": found[0]["periods"][0]}
        print(f"[probe] AVAILABLE slot to test: {slot}")
        if not do_book:
            print("[probe] DRY RUN (no --book): warmup + discovery via the real path SUCCEEDED and a "
                  "real slot is available. Re-run with --book to make the hybrid DETACHED submit and "
                  "verify the %PDF (this BOOKS a real appointment).")
            return 0
        out = _detached_submit(ops, slot, quest_steps, res, log)
        if out["booked"]:
            p = _ROOT / "var" / "artifacts" / f"hybrid_{ops.identity_id}_{int(__import__('time').time())}.pdf"
            p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(out["pdf"])
            print(f"\n[probe] ✅ HYBRID BOOKED -- DETACHED primp submit produced a %PDF "
                  f"({out['pdf_bytes']}B) -> {p}\n           The post-login submit does NOT need the browser.")
        else:
            print(f"\n[probe] ❌ detached submit did NOT confirm (status={out['status']} "
                  f"resp_len={out['resp_len']} pdf_bytes={out['pdf_bytes']}). "
                  f"resp_len~1.5KB=booked / ~66KB=form_stale(slot taken) / redirect=session not accepted.")
        return 0
    finally:
        try: ops._close_session_quietly()
        except Exception: pass


if __name__ == "__main__":
    raise SystemExit(main())

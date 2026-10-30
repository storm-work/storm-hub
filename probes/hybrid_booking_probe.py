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

EXPERIMENT #1 (--warmup-via primp): the browser does LOGIN ONLY; the ENTIRE warm-up quest chain
(home-lang -> Questionario -> quest steps -> Formulario -> ScheduleController -> Schedule.jsp) runs
on a DETACHED primp client on the same exit + cookies. If it establishes the booking-form markers
AND a following primp submit books, then warm-up does NOT need the browser -- the gate for moving
KEEPING (which must survive the portal's unstable hot period) off the heavy browser and onto primp.
Default --warmup-via browser keeps the proven path (browser login+warmup, primp submit only).

Usage:
  cd /srv/projects/storm-hub
  .venv/bin/python probes/hybrid_booking_probe.py --booking "Test2" --pool soax --geo DE [--book]
  .venv/bin/python probes/hybrid_booking_probe.py --posto 3059 --nat CPV --res IRL --pool soax [--book]
  # Experiment #1 -- warm-up on primp (browser logs in only):
  .venv/bin/python probes/hybrid_booking_probe.py --booking "Test2" --pool soax --geo DE --warmup-via primp [--book]

Discovery flags (find the real open slots instead of an arbitrary window):
  --discovery-source {both|calendar|slots}  default both: /slots first, then the CALENDAR
                                             (gettime+getPeriodosOcupados) fallback -- the proven path.
  --start YYYY-MM-DD / --end YYYY-MM-DD      scan window (default today+1 .. today+120)
  --max-dates N (default 90)                 cap on dates scanned
  --weekends                                 include Sat/Sun (default weekdays only)
  --close-browser                            EXPERIMENT #5 (Phase C): after login, CLOSE the browser
                                             and run the ENTIRE hot path browserless on primp (implies
                                             --discovery-via primp); reopen a cookie-injected browser
                                             only for the PDF. Proves the portal session survives the
                                             browser closing. Requires --warmup-via primp.
  --discovery-via {browser|primp}            EXPERIMENT #4: 'primp' runs the CALENDAR discovery
                                             (gettime+getPeriodosOcupados) as detached primp GETs --
                                             they carry NO reCAPTCHA token, only the session cookie, so
                                             primp should return the same bodies the browser nav does.
                                             Default 'browser' keeps the proven do_discover path.

EXPERIMENT #2 -- keeping endurance (--keep-min N [--keep-interval S]): after warm-up, HOLD the session
N minutes by re-warming every S seconds (default 180) on the warm-up transport (primp for
--warmup-via primp), browser otherwise PARKED. Proves a detached hold carries a bookable cursor
through a long / down window, then discovers + submits. E.g. hold 15min on primp then book:
  .venv/bin/python probes/hybrid_booking_probe.py --booking "Test 3" --pool soax --geo PT \
    --warmup-via primp --discovery-source both --start 2026-11-01 --end 2026-11-30 \
    --keep-min 15 --keep-interval 180 --book
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


# Local ledger of appointments THIS probe booked. The probe books on the live portal but never
# writes a BookedAppointment row (it is not the Hub), so without this the DB-side `booked` filter
# cannot see the probe's own bookings and the session cache happily re-picks a spent identity (which
# then hits the portal's "applicant already has a request"). Keyed by identity_id; records posto for
# visibility. Any identity in here is excluded from the candidate pool AND the session-cache prefer.
_BOOKED_LEDGER = _ROOT / "var" / "probe_booked_identities.json"


def _load_probe_booked() -> dict:
    import json
    try:
        return json.loads(_BOOKED_LEDGER.read_text()) if _BOOKED_LEDGER.exists() else {}
    except Exception:
        return {}


def _record_probe_booked(identity_id: str, posto, nat, res) -> None:
    import json, time as _t
    data = _load_probe_booked()
    data[identity_id] = {"posto": str(posto), "nat": nat, "res": res, "ts": int(_t.time())}
    try:
        _BOOKED_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        _BOOKED_LEDGER.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


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


def _warm(pool, geo, posto, nat, res, log, headless=True, tries=8, exit_tries=4, login_only=False):
    """Login + warmup via the production LiveEngineOps, retrying identities/exits. Returns ops|None.

    login_only=True stops after a successful do_login (NO do_warmup) -- used by the warmup-via-primp
    experiment so the browser only ever carries the reCAPTCHA login and primp is left to prove the
    warm-up chain on its own. The session cache is NOT banked in this mode (nothing warmed)."""
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
    # Fold in the probe's OWN bookings (invisible to the DB -- see _BOOKED_LEDGER). An identity the
    # probe already booked is spent portal-side ("applicant already has a request"), so exclude it
    # exactly like a real BookedAppointment, whatever posto it was.
    probe_booked = set(_load_probe_booked().keys())
    if probe_booked:
        log(f"excluding {len(probe_booked)} identity(ies) this probe already booked (local ledger)")
    booked |= probe_booked
    running = {r[0] for r in db.execute(select(M.Worker.identity_id).where(M.Worker.status == "RUNNING")).all()}
    cands = db.scalars(select(M.Identity).where(M.Identity.status == "ACTIVE").limit(400)).all()
    random.shuffle(cands)
    # SESSION CACHE: prefer the last identity we warmed, so do_login restores ITS fresh checkpoint
    # (skip the whole login lottery) when re-run within the Vistos_sid TTL. First run fights login
    # once; re-runs ride the banked session.
    _cache = _ROOT / "var" / "probe_session_identity.txt"
    _pref = _cache.read_text().strip() if _cache.exists() else ""
    if _pref and _pref in booked:
        # The banked identity is now spent (booked by the probe or for real) -- never prefer it, or
        # every re-run restores a checkpoint for an identity the portal will reject as already-having
        # a request. Drop the stale cache so the next warm banks a fresh one.
        log(f"session cache: banked identity {_pref[:8]} is spent (booked) -- ignoring + clearing cache")
        try: _cache.unlink()
        except Exception: pass
        _pref = ""
    if _pref:
        cands.sort(key=lambda i: 0 if i.id == _pref else 1)
        if cands and cands[0].id == _pref:
            log(f"session cache: trying banked identity {_pref[:8]} first (checkpoint restore)")

    # DISCOVERY_SOURCE: default 'both' -- /slots FIRST, then the CALENDAR (gettime +
    # getPeriodosOcupados) fallback for every date /slots did not definitively answer. /slots alone
    # returns {"data":[]} even when the posto has open slots (it reads the live page's own feed, which
    # lags / needs its own token), so 'both' is what actually surfaces availability -- the calendar is
    # this project's PROVEN discovery path ([[confirmed_booking_pipeline]]). Override with
    # --discovery-source {both|calendar|slots}.
    disc_source = (_arg("--discovery-source", "both")).lower()
    cfg = EngineConfig.from_settings(resolve({L.GLOBAL: g, L.BOOKING: {
        "DISCOVERY_SOURCE": disc_source, "KEEPALIVE_MODE": "schedule"}}))
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
            capped = []

            def _ops_log(t, _capped=capped):
                # The engine rests an over-tried username for an hour; retrying THAT identity on a new
                # exit just waits the hour out (run C sat 6h on one). Remember the cap so we move on.
                if "shot cap" in str(t) or "3-shots/60min" in str(t):
                    _capped.append(1)
                log("  " + str(t)[:140])
            ops._log = _ops_log
            try:
                if not ops.do_login():
                    ops._close_session_quietly()
                    if capped:
                        log("  identity is at its login shot cap -- rolling to the next identity")
                        break
                    proxy = roll_rotating_url(proxy); continue
                if login_only:
                    db.close()
                    log(f"LOGGED IN ✓ (login-only, warm-up deferred to primp) ({ident.full_name[:22]!r})")
                    return ops, quest_steps, res
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


# EXPERIMENT #5 state: once the browser is CLOSED after login, primp can no longer read cookies/UA
# off the browser (those are browser commands). We cache them here while the browser is still alive,
# and _primp_creds() serves the cache after close. Empty = browser still open (normal Phase B).
_CLOSED_CREDS: dict = {}


def _primp_creds(ops):
    """(cookies_dict, ua) for a detached primp request. Uses the pre-close cache when the browser has
    been closed (Exp #5), else reads live from the browser (Phase B -- browser parked but open)."""
    if _CLOSED_CREDS:
        return dict(_CLOSED_CREDS["cookies"]), _CLOSED_CREDS["ua"]
    cookies = {c["name"]: c["value"] for c in (ops.session.get_all_cookies() or [])
               if isinstance(c, dict) and c.get("name") in ("Vistos_sid", "cookiesession1")}
    try:
        ua = (ops.session.browser_eval("navigator.userAgent") or "").strip()
    except Exception:
        ua = ""
    return cookies, ua or "Mozilla/5.0"


def _parse_inputs(html: str) -> dict:
    """Mirror the browser's `querySelectorAll('input')` field harvest with two regexes (name-then-
    value AND value-then-name), so a detached warmup gathers the same Formulario fields the in-page
    chain does regardless of attribute order."""
    fields: dict = {}
    for m in re.finditer(r'<input\b[^>]*\bname=["\']([^"\']+)["\'][^>]*\bvalue=["\']([^"\']*)["\']', html, re.I):
        fields.setdefault(m.group(1), m.group(2))
    for m in re.finditer(r'<input\b[^>]*\bvalue=["\']([^"\']*)["\'][^>]*\bname=["\']([^"\']+)["\']', html, re.I):
        fields.setdefault(m.group(2), m.group(1))
    return fields


def _warmup_via_primp(ops, quest_steps, res, log):
    """EXPERIMENT #1 -- run the WHOLE warm-up quest chain on a DETACHED primp client (same exit +
    cookies as the just-completed BROWSER login), never the browser. Mirrors _run_warmup_steps step
    for step, reusing the engine's own payload builders so it is faithful, not a re-guess:

        home-lang POST -> Questionario GET -> N quest-step GETs -> Formulario POST (csrf) ->
        ScheduleController POST (multipart) -> Schedule.jsp GET (markers)

    Returns (chain_ok, markers_present). chain_ok = every step answered as expected and a CSRF came
    back; markers_present = the final Schedule.jsp carried the booking-form markers (the positive
    proof the detached chain actually established a bookable cursor). If this returns (True, True)
    AND a subsequent primp submit books, warm-up does NOT need the browser -- which is the gate for
    moving KEEPING onto primp (so it survives the hot period without the heavy browser)."""
    import primp
    from apps.worker.ops_live import (
        HOME_URL, QUEST_URL, QUEST_NEXT, FORMULARIO, SCHED_CTRL, SCHED_JSP,
        _extract_csrf, _SCHED_PAGE_MARKERS,
    )
    from libs.engine.payload import schedule_controller_payload

    cookies, ua = _primp_creds(ops)
    ck = "; ".join(f"{k}={v}" for k, v in cookies.items())
    H = {"User-Agent": ua, "Cookie": ck}
    pc = primp.Client(proxy=ops.warm_proxy, verify=False, timeout=100, follow_redirects=True)
    try: pc.set_cookies(ORIGIN + "/", cookies)
    except Exception: pass

    # 1. home-lang POST
    r = pc.post(HOME_URL, data="lang=PT",
                headers={**H, "Content-Type": "application/x-www-form-urlencoded"})
    log(f"[primp-warm] home-lang POST -> {r.status_code}")

    # 2. Questionario GET
    r = pc.get(QUEST_URL, headers=H)
    log(f"[primp-warm] Questionario GET -> {r.status_code} len={len(r.text or '')}")

    # 3. quest steps (base_qs merged into EVERY step, exactly like _run_warmup_steps)
    quest_key = ops.residence if ops.residence != ops.nationality else ops.nationality
    steps = quest_steps.get(quest_key, [])
    if not steps:
        log(f"[primp-warm] NO quest steps for key {quest_key!r} -- cannot warm"); return False, False
    base_qs = {"lang": "PT", "nacionalidade": ops.nationality}
    last_html = ""
    for i, step in enumerate(steps):
        url = f"{QUEST_NEXT}?{urlencode({**base_qs, **step})}"
        r = pc.get(url, headers={**H, "Accept": "text/plain, */*; q=0.01",
                                 "X-Requested-With": "XMLHttpRequest", "Referer": QUEST_URL,
                                 "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty",
                                 "Sec-Fetch-Site": "same-origin"})
        log(f"[primp-warm] quest step {i+1}/{len(steps)} -> {r.status_code}")
        if r.status_code != 200:
            log(f"[primp-warm] quest step {i+1} non-200 -- ABORT (stateful chain is doomed)")
            return False, False
        last_html = r.text or ""

    last_fields = _parse_inputs(last_html)
    if not last_fields:
        log(f"[primp-warm] quest_state_stale: last step yielded NO input fields (len={len(last_html)})")
        return False, False
    log(f"[primp-warm] last quest step yielded {len(last_fields)} fields")

    # 4. Formulario POST (form-encoded) -> CSRF
    r = pc.post(FORMULARIO + "?copy=true", data=urlencode(last_fields),
                headers={**H, "Content-Type": "application/x-www-form-urlencoded", "Referer": QUEST_URL})
    form_html = r.text or ""
    csrf = _extract_csrf(form_html)
    if not csrf:
        r = pc.post(FORMULARIO, data=urlencode(last_fields),
                    headers={**H, "Content-Type": "application/x-www-form-urlencoded", "Referer": QUEST_URL})
        form_html = r.text or ""
        csrf = _extract_csrf(form_html)
    log(f"[primp-warm] Formulario POST -> {r.status_code} len={len(form_html)} csrf={'yes' if csrf else 'MISSING'}")
    if not csrf:
        return False, False

    # 5. ScheduleController POST -- MULTIPART (empty file parts foto/file1-4), like browser_fetch.
    # primp's `files=` reads each value as a FILE PATH (not content), so it can't make an EMPTY part;
    # build the multipart body by hand instead -- exact control: empty filename + empty content,
    # byte-identical to what the unset file inputs send.
    sc_payload = schedule_controller_payload(
        ops.account, ops.form_defaults, posto_id=ops.posto_id, csrf=csrf,
        nationality=ops.nationality, residence=res,
    )
    boundary = "----primpWarmup" + "".join(random.choice("0123456789abcdef") for _ in range(16))
    parts = []
    for k, v in sc_payload.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n")
    for f in ("foto", "file1", "file2", "file3", "file4"):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{f}\"; filename=\"\"\r\n"
                     f"Content-Type: application/octet-stream\r\n\r\n\r\n")
    multipart = ("".join(parts) + f"--{boundary}--\r\n").encode("utf-8")
    r = pc.post(SCHED_CTRL + "?posto_id=" + str(ops.posto_id), content=multipart,
                headers={**H, "Referer": FORMULARIO,
                         "Content-Type": f"multipart/form-data; boundary={boundary}"})
    sc_body = r.text or ""
    # Signature the body carries: a re-rendered Formulario (len ~1.3MB, has the questionnaire form)
    # = VALIDATION REJECT; an Authentication.jsp = session lost; a schedule page = accepted.
    sc_sig = ("formulario_rerender" if ("Formulario" in sc_body and "cmbPeriodo" not in sc_body
                                        and len(sc_body) > 500000)
              else "auth_redirect" if "Authentication.jsp" in sc_body or "authentication" in sc_body.lower()[:3000]
              else "schedule_markers" if any(m in sc_body for m in _SCHED_PAGE_MARKERS)
              else "other")
    log(f"[primp-warm] ScheduleController POST -> {r.status_code} len={len(sc_body)} sig={sc_sig}")

    # 6. Schedule.jsp GET -> the positive marker check (same markers the browser warmup verifies)
    r = pc.get(SCHED_JSP + "?posto_id=" + str(ops.posto_id), headers={**H, "Referer": FORMULARIO})
    page = r.text or ""
    markers = any(m in page for m in _SCHED_PAGE_MARKERS)
    present = [m for m in _SCHED_PAGE_MARKERS if m in page]
    log(f"[primp-warm] Schedule.jsp GET -> {r.status_code} len={len(page)} markers={markers} {present}")
    if not markers:
        log(f"[primp-warm] NO markers. ScheduleController sig={sc_sig} -- "
            + ("the POST was WAF-passed but the app REJECTED the form (field-level: the detached "
               "multipart differs from the browser's FormData, OR this posto/residence was declined)."
               if sc_sig == "formulario_rerender" else
               "the session was lost mid-chain (auth redirect)." if sc_sig == "auth_redirect" else
               "unclassified -- inspect the body."))
    return True, markers


def _discover_calendar_via_primp(ops, dates, log):
    """EXPERIMENT #4 -- calendar discovery (gettime + getPeriodosOcupados) on a DETACHED primp client.

    These are plain session-cookie GETs with NO reCAPTCHA token (ops_live._discover_via_calendar uses
    browser_nav only by convention, not necessity), so primp should get the SAME bodies the browser
    does -- and the SAME parsers (classify_gettime_body / parse_special_days / parse_periodos_ocupados)
    read them, transport-independent. If this matches the browser calendar, discovery leaves the
    browser. Returns the do_discover-shaped list [{date, outcome, periods?}] so it slots in unchanged."""
    import primp
    from datetime import date as _date
    from apps.worker.ops_live import GETTIME_URL, GETPERIODOS_URL, _PORTAL_ORIGIN
    from libs.engine.discovery.gettime import (
        GettimeStatus, classify_gettime_body, parse_special_days)
    from libs.engine.discovery.periodos import parse_periodos_ocupados
    import re as _re

    cookies, ua = _primp_creds(ops)
    H = {"User-Agent": ua, "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}
    pc = primp.Client(proxy=ops.warm_proxy, verify=False, timeout=30, follow_redirects=True)
    try: pc.set_cookies(_PORTAL_ORIGIN + "/", cookies)
    except Exception: pass

    # 1. gettime (special/closed days) -- one GET for the whole posto
    r = pc.get(f"{GETTIME_URL}?id_posto={ops.posto_id}", headers=H)
    body = r.text or ""
    status = classify_gettime_body(body)
    log(f"[primp-cal] gettime -> {r.status_code} len={len(body)} status={status}")
    if status != GettimeStatus.OK:
        log(f"[primp-cal] gettime BLOCKED/not-OK via primp -- all {len(dates)} dates FAILED")
        return [{"date": d, "outcome": "failed"} for d in dates]
    special_days = parse_special_days(body)

    # 2. getPeriodosOcupados per date (plain GET; marker periodos_ocupados='...')
    out = []
    for d in dates:
        try:
            iso = _date.fromisoformat(d)
        except ValueError:
            out.append({"date": d, "outcome": "failed"}); continue
        if iso.day in special_days.get(iso.year, {}).get(iso.month, set()):
            out.append({"date": d, "outcome": "empty"}); continue
        url = f"{GETPERIODOS_URL}?id_posto={ops.posto_id}&data_agendamento={iso.strftime('%Y/%m/%d')}"
        try:
            rp = pc.get(url, headers=H)
            html = rp.text or ""
        except Exception as e:
            log(f"[primp-cal] {d} GET error {type(e).__name__}: {str(e)[:60]} -- FAILED")
            out.append({"date": d, "outcome": "failed"}); continue
        m = _re.search(r"periodos_ocupados='([0-9]*)", html)
        if not m:
            out.append({"date": d, "outcome": "failed"}); continue
        free, _occ = parse_periodos_ocupados(m.group(1), max_period=ops.cfg.discovery.max_period)
        if free is None:
            out.append({"date": d, "outcome": "failed"})
        elif free:
            out.append({"date": d, "outcome": "found", "periods": free, "raw": m.group(1)})
        else:
            out.append({"date": d, "outcome": "empty"})
    _f = sum(1 for r in out if r["outcome"] == "found")
    _e = sum(1 for r in out if r["outcome"] == "empty")
    _x = sum(1 for r in out if r["outcome"] == "failed")
    log(f"[primp-cal] calendar via primp: {_f} found, {_e} empty, {_x} failed")
    return out


def _keep_loop(ops, quest_steps, res, log, minutes: float, interval: float, via: str):
    """EXPERIMENT #2 -- hold a BOOKABLE session for `minutes` by RE-WARMING every `interval`s, on the
    chosen transport (primp or browser), with the browser otherwise PARKED (idle, never navigated).

    This is the keeping-on-primp feasibility test: can a detached client hold a live Schedule.jsp
    cursor across a long window (ideally straddling a portal down-window) so that, when a slot opens,
    the worker applies DIRECTLY instead of paying a cold re-warm during the unstable hot period? Each
    cycle re-warms and reports whether the cursor came back (markers=True) and how long it took --
    plus a bare liveness read between cycles. Returns the count of (ok, failed) re-warm cycles."""
    import time as _t
    deadline = _t.time() + minutes * 60.0
    ok = fail = cycle = 0
    log(f"[keep] EXPERIMENT #2: holding via {via} for {minutes:.0f}min, re-warm every {interval:.0f}s "
        f"(browser parked)")
    while _t.time() < deadline:
        # idle until the next cycle (the browser does NOTHING here -- that is the point: a primp hold
        # must not depend on the browser touching the portal)
        nap = min(interval, max(1.0, deadline - _t.time()))
        _t.sleep(nap)
        if _t.time() >= deadline:
            break
        cycle += 1
        t0 = _t.time()
        if via == "primp":
            _ok, markers = _warmup_via_primp(ops, quest_steps, res, log)
        else:
            try:
                markers = bool(ops.do_warmup() and ops.is_warmed())
            except Exception as e:
                markers = False
                log(f"[keep] cycle {cycle}: browser re-warm raised {type(e).__name__}: {str(e)[:80]}")
        dt = _t.time() - t0
        if markers:
            ok += 1
            ops.posto_pdf = ops.posto_id; ops._set_anchor("chain")
            log(f"[keep] cycle {cycle}: cursor HELD (re-warm {dt:.0f}s, {ok} ok / {fail} fail)")
        else:
            fail += 1
            log(f"[keep] cycle {cycle}: cursor LOST this cycle (re-warm {dt:.0f}s, {ok} ok / {fail} fail) "
                f"-- a transient portal reject; the hold continues")
    log(f"[keep] done: {ok} held / {fail} lost over {cycle} cycles in {minutes:.0f}min")
    return ok, fail


def _reopen_pdf(ops, submit_html, log) -> bytes:
    """EXPERIMENT #5 PDF step: the browser was closed for the hot path, so reopen a FRESH browser on
    the same exit with the saved cookie jar injected (get_session(inject_cookies_full=...), the engine's
    own close-then-reopen-with-cookies path), fetch the %PDF, then close it. Returns the PDF bytes."""
    from libs.engine.vendor import get_session
    full_jar = (_CLOSED_CREDS or {}).get("full_jar") or []
    log(f"[reopen] launching a fresh browser on the warm exit with {len(full_jar)} injected cookies "
        f"for the PDF ...")
    pdf_sess = get_session(proxy=ops.warm_proxy, inject_cookies_full=full_jar, headless=True)
    saved = ops.session
    try:
        ops.session = pdf_sess
        pdf = ops._fetch_pdf_from_submit_body(submit_html)
        log(f"[reopen] PDF via reopened browser -> {len(pdf)}B %PDF={pdf[:4]==b'%PDF'}")
        return pdf
    finally:
        ops.session = saved
        try: pdf_sess.close()
        except Exception: pass


def _detached_submit(ops, slot, quest_steps, res, log):
    """The HYBRID: SubmeterVistoCriaPDF via primp from the same exit + synced cookies. Returns dict."""
    import primp
    from libs.engine.payload import booking_payload
    cookies, ua = _primp_creds(ops)
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
    # VERDICT via the production classifier -- the submit RESPONSE is the confirmation (~1.5KB
    # booked / ~66KB form_stale). CONFIRMED live: a detached primp submit of 1566B is the booked
    # signature. The %PDF is the proof (success_detection_false_negative), fetched via the BROWSER:
    # the MostrarPdf artifact endpoint 403s a detached client even though the SUBMIT books, so the
    # hybrid is DETACHED-SUBMIT + BROWSER-PDF -- and the booking is already made by the time we fetch
    # the artifact.
    verdict = ops._classify_submit_response(html, "")
    pdf = b""
    try:
        if _CLOSED_CREDS:
            # EXPERIMENT #5: the browser was CLOSED for the hot path. REOPEN one with the saved cookie
            # jar injected (the engine's own "reopen after close" mechanism), fetch the %PDF, close it.
            pdf = _reopen_pdf(ops, html, log)
        else:
            pdf = ops._fetch_pdf_from_submit_body(html)  # in-page browser fetch (credentials:include)
        log(f"[hybrid] BROWSER PDF fetch -> bytes={len(pdf)} %PDF={pdf[:4]==b'%PDF'}")
    except Exception as e:
        log(f"[hybrid] browser PDF fetch error: {type(e).__name__}: {str(e)[:100]}")
    # for the record, also try the detached PDF fetch (expected to 403 -- artifact endpoint is stricter)
    try:
        m = re.search(r'["\']([^"\']*MostrarPdf[^"\']*)["\']', html, re.I)
        if m:
            action = m.group(1)
            url = action if action.startswith("http") else (ORIGIN + action if action.startswith("/")
                                                             else BASE + "/" + action.lstrip("/"))
            rp = pc.get(url)
            log(f"[hybrid] (primp GET MostrarPdf -> status={rp.status_code} -- artifact endpoint)")
    except Exception:
        pass
    booked = pdf[:4] == b"%PDF"
    return {"status": r.status_code, "resp_len": len(html), "verdict": verdict,
            "pdf_bytes": len(pdf), "booked": booked, "pdf": pdf}


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

    warmup_via = (_arg("--warmup-via", "browser")).lower()

    def log(m): print(m, flush=True)
    if warmup_via == "primp":
        # EXPERIMENT #1: browser does LOGIN ONLY; the warm-up quest chain runs on DETACHED primp.
        # _warm_login_only logs in (browser; reCAPTCHA) and skips do_warmup, then we warm via primp.
        ops, quest_steps, res = _warm(pool, geo, posto, nat, res, log, headless=headless,
                                      login_only=True)
        if ops is None:
            print("[probe] could not LOG IN any identity -- re-run (login is a lottery)."); return 1
        if "--close-browser" in sys.argv:
            # EXPERIMENT #5: prove the portal session SURVIVES the browser closing. Cache the cookies
            # + UA while the browser is alive, then CLOSE it -- the whole hot path (warm-up, keep,
            # discover, re-warm, submit) then runs on primp with zero browser; the PDF reopens one.
            full_jar = ops.session.get_all_cookies() or []
            cookies = {c["name"]: c["value"] for c in full_jar
                       if isinstance(c, dict) and c.get("name") in ("Vistos_sid", "cookiesession1")}
            try:
                ua = (ops.session.browser_eval("navigator.userAgent") or "").strip() or "Mozilla/5.0"
            except Exception:
                ua = "Mozilla/5.0"
            _CLOSED_CREDS.update(cookies=cookies, ua=ua, full_jar=full_jar)
            print(f"[probe] === EXPERIMENT #5: CLOSING the browser after login "
                  f"({len(cookies)} session cookies cached) -- hot path runs browserless on primp ===")
            try:
                ops.session.close_browser()
            except Exception as e:
                print(f"[probe] close_browser raised: {type(e).__name__}: {str(e)[:100]}")
        print("[probe] === EXPERIMENT #1: warm-up via DETACHED primp (browser did login only) ===")
        chain_ok, markers = _warmup_via_primp(ops, quest_steps, res, log)
        if not (chain_ok and markers):
            if not chain_ok or _CLOSED_CREDS:
                # Broke mid-chain, OR the browser is closed (Exp #5) so the browser-control can't run.
                print(f"[probe] ❌ primp WARM-UP did not establish a cursor (chain_ok={chain_ok} "
                      f"markers={markers}){' [browser closed -- no control possible]' if _CLOSED_CREDS else ''}. "
                      f"Re-run; if it recurs at the same step, that step needs the browser.")
                try: ops._close_session_quietly()
                except Exception: pass
                return 1
            # CONTROL: primp ran the whole chain but got no cursor. Was it a primp FIDELITY gap, or a
            # portal reject the BROWSER would hit too? Run the browser's own warmup on this SAME
            # logged-in session (it re-runs the stateful chain from home-lang, so it's independent).
            print("[probe] primp chain completed but NO cursor -- running the BROWSER warmup on the "
                  "SAME session as a CONTROL to isolate primp-fidelity vs portal-reject ...")
            browser_ok = False
            try:
                browser_ok = ops.do_warmup()
            except Exception as e:
                print(f"[probe] browser control warmup raised: {type(e).__name__}: {str(e)[:120]}")
            if browser_ok and ops.is_warmed():
                print("[probe] 🔬 VERDICT: PRIMP FIDELITY GAP. The browser warmed the SAME session/exit "
                      "where primp could not -- so the detached ScheduleController multipart is not "
                      "byte-equivalent to the browser's FormData. Warmup-via-primp needs the multipart "
                      "fixed, NOT a browser. Continuing to discover+submit on the browser-warmed cursor.")
                # fall through: ops is now browser-warmed, so the existing discover+submit runs.
            else:
                print("[probe] 🔬 VERDICT: NOT A PRIMP PROBLEM. The browser ALSO failed to warm this "
                      "session/exit -- this was a portal/exit reject (or this posto/residence was "
                      "declined), which the browser hits too. primp is EXONERATED for the chain; "
                      "re-run on a fresh exit to get a clean read.")
                try: ops._close_session_quietly()
                except Exception: pass
                return 1
        if markers:
            # The DETACHED chain established the server-side cursor. Set the anchor so the existing
            # browser-discover + primp-submit downstream treat this session as warmed.
            ops.posto_pdf = ops.posto_id
            ops._set_anchor("chain")
            print(f"[probe] ✅ primp WARM-UP established the Schedule.jsp cursor (posto_pdf={ops.posto_pdf}) "
                  f"WITHOUT the browser running the quest chain. Proceeding to discovery + "
                  f"{'submit' if do_book else 'dry-run'} to confirm the primp-warmed cursor is bookable.")
        else:
            # Fell through from the browser CONTROL (primp fidelity gap): ops is browser-warmed now.
            print(f"[probe] (browser-warmed cursor posto_pdf={ops.posto_pdf}) Proceeding to discovery + "
                  f"{'submit' if do_book else 'dry-run'} -- this still exercises the primp SUBMIT, "
                  f"just on a browser-warmed cursor.")
    else:
        ops, quest_steps, res = _warm(pool, geo, posto, nat, res, log, headless=headless)
        if ops is None:
            print("[probe] could not warm any identity -- re-run (login is a lottery)."); return 1
    # EXPERIMENT #2: optional keeping-endurance hold between warm-up and discovery/submit. --keep-min
    # N holds the session N minutes by re-warming every --keep-interval s on the SAME transport as the
    # warm-up (primp for --warmup-via primp), browser otherwise parked -- proving a detached hold can
    # carry a bookable cursor through a long (ideally down-) window.
    _keep_min = float(_arg("--keep-min", "0") or 0)
    if _keep_min > 0:
        _keep_int = float(_arg("--keep-interval", "180") or 180)
        _keep_held, _keep_lost = _keep_loop(
            ops, quest_steps, res, log, _keep_min, _keep_int,
            via=("primp" if warmup_via == "primp" else "browser"))
        if _keep_held == 0:
            print("[probe] ⚠ EXPERIMENT #2: the session never re-warmed during the hold -- cursor not "
                  "held. Proceeding to discover+submit anyway (the pre-submit re-warm will try once more).")
    try:
        from datetime import date, timedelta
        from libs.engine.config import EngineConfig
        # DATE RANGE -- was a fixed today+30..today+90 window, weekdays only, capped at a hardcoded 25.
        # That arbitrary window is a common reason the probe "finds nothing" while the posto DOES have
        # open slots outside it. Now explicit and overridable:
        #   --start YYYY-MM-DD   (default: booking's discovery start, else today+1)
        #   --end   YYYY-MM-DD   (default: booking's discovery end,   else today+120)
        #   --max-dates N        (default 90 -- cap so a huge range can't fire forever)
        #   --weekends           (include Sat/Sun; default weekdays only, matching production)
        d = ops.cfg.discovery
        start = _arg("--start") or d.start_date or str(date.today() + timedelta(days=1))
        end = _arg("--end") or d.end_date or str(date.today() + timedelta(days=120))
        max_dates = int(_arg("--max-dates", "90"))
        weekends = "--weekends" in sys.argv
        dates = []
        cur = date.fromisoformat(start)
        _last = date.fromisoformat(end)
        while cur <= _last and len(dates) < max_dates:
            if weekends or cur.weekday() < 5:
                dates.append(cur.isoformat())
            cur += timedelta(days=1)
        print(f"[probe] DATE RANGE {start} .. {end} ({'all days' if weekends else 'weekdays'}, "
              f"cap {max_dates}) -> {len(dates)} dates; source={ops.cfg.discovery.source!r} "
              f"(slots-first then CALENDAR fallback if 'both')")
        print(f"[probe]   dates: {dates[0]} ... {dates[-1]}" if dates else "[probe]   (no dates!)")
        # EXPERIMENT #4 -- calendar discovery on primp. --discovery-via primp runs gettime +
        # getPeriodosOcupados as detached primp GETs (browser otherwise untouched); default 'browser'
        # keeps the proven do_discover path. If a prior cursor exists we re-warm via primp first so
        # gettime answers on a live session.
        discovery_via = (_arg("--discovery-via", "browser")).lower()
        if _CLOSED_CREDS and discovery_via != "primp":
            print("[probe] --close-browser: forcing --discovery-via primp (no browser to nav)")
            discovery_via = "primp"
        if discovery_via == "primp":
            print("[probe] === EXPERIMENT #4: CALENDAR discovery via DETACHED primp (no browser) ===")
            results = _discover_calendar_via_primp(ops, dates, log)
        else:
            results = ops.do_discover(dates)
        found = [r for r in results if r.get("outcome") == "found" and r.get("periods")]
        print(f"[probe] discovery: {sum(1 for r in results if r.get('outcome')=='found')} found, "
              f"{sum(1 for r in results if r.get('outcome')=='empty')} empty, "
              f"{sum(1 for r in results if r.get('outcome')=='failed')} failed")
        if found:
            print("[probe] AVAILABLE dates: " + ", ".join(
                f"{r['date']}(p{','.join(str(p) for p in r['periods'])})" for r in found[:15]))
        if not found:
            print(f"[probe] NO AVAILABLE SLOT in {start}..{end} via source={ops.cfg.discovery.source!r}. "
                  f"If the posto has open slots you can see on the portal, widen the range "
                  f"(--start/--end) and/or ensure --discovery-source both so the CALENDAR path runs. "
                  f"A /slots-only run shows {{\"data\":[]}} even when slots exist.")
            return 0
        slot = {"date": found[0]["date"], "period": found[0]["periods"][0]}
        print(f"[probe] AVAILABLE slot to test: {slot}")
        if not do_book:
            print("[probe] DRY RUN (no --book): warmup + discovery via the real path SUCCEEDED and a "
                  "real slot is available. Re-run with --book to make the hybrid DETACHED submit and "
                  "verify the %PDF (this BOOKS a real appointment).")
            return 0
        # RE-WARM IMMEDIATELY BEFORE THE SUBMIT -- MANDATORY. The booking cursor is NAVIGATION-bound
        # ([[rewarm_every_round_is_mandatory]] / [[navigation_vs_inpage_fetch]]): the discovery that
        # just ran NAVIGATED the browser (calendar = one nav per date; even /slots navigates), which
        # DESTROYS the server-side form cursor the warm-up established. Production's do_apply re-warms
        # unconditionally at the top of every round for exactly this reason. Without it the submit
        # bounces to the home page = form_stale (confirmed live 2026-10-08: SANDRA, 85-date calendar
        # scan between warm and submit -> submit returned the 59KB home page). Re-warm on the SAME
        # transport we are testing (primp for the primp experiment, else the browser).
        if warmup_via == "primp":
            print("[probe] re-warming via primp before the submit (cursor is nav-bound; discovery "
                  "staled it) ...")
            _ok2, _markers2 = _warmup_via_primp(ops, quest_steps, res, log)
            if _markers2:
                ops.posto_pdf = ops.posto_id; ops._set_anchor("chain")
            else:
                print("[probe] ⚠ pre-submit primp re-warm did NOT re-establish the cursor -- the "
                      "submit will likely form_stale. (Re-run; a transient portal reject here is "
                      "common.)")
        else:
            print("[probe] re-warming via the browser before the submit (cursor is nav-bound) ...")
            try: ops.do_warmup()
            except Exception as e: print(f"[probe] pre-submit browser re-warm raised: {str(e)[:100]}")
        out = _detached_submit(ops, slot, quest_steps, res, log)
        print(f"\n[probe] detached submit: status={out['status']} resp_len={out['resp_len']} "
              f"classifier={out['verdict']!r}  (resp_len~1.5KB & verify_pdf/booked = the DETACHED submit booked)")
        if out["booked"]:
            _record_probe_booked(ops.identity_id, posto, nat, res)  # spend it -- never re-pick
            p = _ROOT / "var" / "artifacts" / f"hybrid_{ops.identity_id}_{int(__import__('time').time())}.pdf"
            p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(out["pdf"])
            print(f"[probe] ✅ HYBRID CONFIRMED -- detached primp submit BOOKED, %PDF verified via "
                  f"browser ({out['pdf_bytes']}B) -> {p}\n           Post-login SUBMIT does NOT need the "
                  f"browser; the browser is only needed for login+warmup (and the PDF artifact).")
        elif out["verdict"] in ("booked", "verify_pdf"):
            _record_probe_booked(ops.identity_id, posto, nat, res)  # booked signature -> spent too
            print(f"[probe] ⚠ LIKELY BOOKED -- the detached submit returned the booked signature "
                  f"(classifier={out['verdict']!r}, resp_len={out['resp_len']}), but the %PDF proof "
                  f"could not be fetched ({out['pdf_bytes']}B). VERIFY ON THE PORTAL. The booking is "
                  f"made the moment the submit confirms; the %PDF is a separate, failable step.")
        else:
            print(f"[probe] ❌ detached submit did NOT book (classifier={out['verdict']!r} "
                  f"resp_len={out['resp_len']}). ~66KB=form_stale(slot taken) / redirect=session not accepted.")
        return 0
    finally:
        try: ops._close_session_quietly()
        except Exception: pass


if __name__ == "__main__":
    raise SystemExit(main())

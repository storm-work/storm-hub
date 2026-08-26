#!/usr/bin/env python3
"""Q19 + Q20: how long does an authenticated portal session live, and does an INVOLUNTARY proxy
exit change kill it?

Q16 established that a session survives a DELIBERATE migration (capture the jar, open a new browser
on a new exit). That is not the same event as an exit moving underneath a live browser, and the two
must not be conflated without evidence. This probe measures both remaining lifetimes.

Q19 -- THE VEHICLE IS REAL PROVIDER BEHAVIOUR, NOT A SIMULATION. Q1 measured that every soax
credential loses its exit at its `sessionlength` ceiling: 20/20 sessions changed between 60.2 and
63.4 minutes while being actively used. So holding an authenticated browser open on a soax proxy for
90+ minutes produces a genuine, involuntary, provider-driven exit transition under a live session,
at a predictable time, without corrupting anything or inventing a synthetic failure.

Q20 -- the portal auth TTL is now the binding constraint on hold duration and is completely unknown:
Q16's positives all completed inside ~70s, and four archived checkpoints aged 4.7h+ were all dead.
So it lies somewhere in a range spanning two orders of magnitude. Two different questions matter and
are measured by separate arms:
    ACTIVE TTL  does a session eventually die DESPITE regular authenticated traffic?
    IDLE TTL    how long can a session go WITHOUT authenticated traffic and still be usable?
The idle arm uses ESCALATING gaps on ONE session (10/20/30/45/60 min) rather than one session per
gap: each survived gap is a datapoint (idle TTL > gap), which matters because logins are the scarce
resource here -- the wall refuses most attempts.

WHAT THIS DOES NOT MEASURE, DELIBERATELY: the server-side FORM/DRAFT state. Establishing it requires
POSTing ScheduleController, which is out of bounds for this phase, so these sessions are NEVER
warmed. The ~90-120s form-state TTL therefore remains a HYPOTHESIS and is untouched by this probe.
The minimum authenticated operation is used instead: a GET of the portal home, which distinguishes
authenticated from logged-out from sessionLost without any POST anywhere.

SAFETY: browser_nav GETs only. No ScheduleController, no slot selection, no application, no PDF, no
do_apply, no warmup. There is no code path from here to a booking. No ProxyLease rows are created and
no cookie secrets are persisted.

    .venv/bin/python probes/session_lifetime_probe.py --hours 4
    .venv/bin/python probes/session_lifetime_probe.py --report var/session_lifetime.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

IP_ECHO = "https://api.ipify.org"

# Escalating idle gaps, minutes. Each survived gap proves idle TTL > that gap, so one session yields
# several datapoints instead of one -- necessary because every extra session costs a scarce login.
IDLE_GAPS_MIN = (10, 20, 30, 45, 60)


def _exit_ip(session) -> str | None:
    try:
        session.browser_nav(IP_ECHO, timeout=30)
        body = (session.browser_eval("document.body ? document.body.innerText : ''") or "").strip()
    except Exception:
        return None
    return body if body and len(body) <= 45 and " " not in body else None


def probe_auth(session, home_url: str) -> tuple[str, dict]:
    """The minimum authenticated operation: GET the portal home and classify.

    Distinguishes -- carefully -- transport failure, WAF challenge, the portal's dedicated
    sessionLost endpoint, an auth redirect, a logged-out page, and a live authenticated page.
    A transport or WAF failure is NEVER reported as session expiry.
    """
    from apps.worker.ops_live import _HOME_MARKERS, _is_challenge_body
    d: dict = {}
    # The nav and the eval that follows it can RACE: browser_nav sometimes returns while the page is
    # still transitioning, so browser_eval lands in a dying execution context ("Execution context was
    # destroyed") and reports the PREVIOUS url. Measured live 2026-08-26 on the idle arm, where a
    # long gap makes the portal nav slow enough to widen the window -- it blinded exactly the probes
    # carrying the idle-TTL signal. Retry rather than record a verdict we cannot stand behind, and
    # never let an instrument failure look like session loss.
    final_url = html = None
    has_pw = None
    for _attempt in range(3):
        try:
            final_url = session.browser_nav(home_url, timeout=45)
        except Exception as e:
            d["error"] = f"{type(e).__name__}: {e}"[:160]
            time.sleep(2)
            continue
        if "pedidodevistos.mne.gov.pt" not in (final_url or ""):
            d["off_origin_url"] = final_url      # stale/foreign url -- re-navigate
            time.sleep(2)
            continue
        try:
            html = session.browser_eval("document.documentElement.outerHTML") or ""
            has_pw = bool(session.browser_eval(
                "document.querySelector('input[type=password]') !== null"))
            break
        except Exception as e:
            d["error"] = f"{type(e).__name__}: {e}"[:160]
            html = None
            time.sleep(2)
    if html is None:
        d["final_url"] = final_url
        return "TRANSPORT_FAIL", d       # instrument/transport, NEVER expiry
    d["final_url"] = final_url
    d["page_bytes"] = len(html)
    d["has_password_field"] = has_pw
    low = html.lower()
    d["home_markers"] = [m for m in _HOME_MARKERS if m in low]
    if _is_challenge_body(html):
        return "WAF_CHALLENGE", d
    if "sessionlost" in (final_url or "").lower():
        return "SESSION_LOST", d
    if "authentication.jsp" in (final_url or "").lower():
        return "AUTH_REDIRECT", d
    # BOTH markers are required, and getting this wrong cost a whole run's verdicts.
    #
    # MEASURED 2026-08-26: the portal home page renders at 59,690B carrying BOTH
    # "solicitar pedido de visto" and "consultar pedidos" while authenticated, and at 58,266B
    # carrying ONLY "solicitar pedido de visto" once the session is gone. "solicitar pedido de
    # visto" is a PUBLIC menu item; only "consultar pedidos" (consult your requests) is gated.
    # Accepting ANY marker therefore reported logged-out pages as AUTHENTICATED for 25+ minutes on
    # two arms. There is also no inline password field on the logged-out home -- the login form
    # lives on Authentication.jsp -- so has_pw is useless as a negative signal here.
    #
    # ops_live.py's own _classify_submit_response already had this right: `all(m in low for m in
    # _HOME_MARKERS)`. This is the production rule, not a new one.
    from apps.worker.ops_live import _HOME_MARKERS as _HM
    if all(m in low for m in _HM):
        return "AUTHENTICATED", d
    if has_pw or d["home_markers"]:
        # Renders the public home (or an explicit login form) => the session is no longer honoured.
        return "LOGGED_OUT", d
    return "INCONCLUSIVE", d


def run_arm(arm: dict, deadline: float, sink, lock, log) -> None:
    """Probe one authenticated session on its schedule until the session is genuinely lost or the
    observation window ends."""
    from apps.worker.ops_live import HOME_URL
    session = arm["session"]
    started = arm["t0"]
    seq = 0
    gaps = list(IDLE_GAPS_MIN) if arm["mode"] == "idle" else None
    consecutive_transport_fail = 0
    while time.time() < deadline:
        # ORDER MATTERS: portal first, echo second. Reading the exit first meant every portal probe
        # began by navigating away from a third-party origin, which is what created the race above.
        verdict, detail = probe_auth(session, HOME_URL)
        ip = _exit_ip(session)
        now = time.time()
        row = {"arm": arm["name"], "mode": arm["mode"], "provider": arm["provider"],
               "session_id": arm["session_id"], "identity_id": arm["identity_id"],
               "seq": seq, "t0": started, "ts": now, "elapsed_s": round(now - started, 1),
               "exit_ip": ip, "exit_changed_since_prev": (
                   ip is not None and arm.get("last_ip") is not None and ip != arm["last_ip"]),
               "prev_exit_ip": arm.get("last_ip"),
               "verdict": verdict, "detail": detail,
               "gap_before_s": arm.get("_last_gap")}
        if row["exit_changed_since_prev"]:
            log(f"[{arm['name']}] *** EXIT CHANGED {arm['last_ip']} -> {ip} at "
                f"t={row['elapsed_s']/60:.1f}m -- session verdict: {verdict}")
        if ip is not None:
            arm["last_ip"] = ip
        with lock:
            sink.write(json.dumps(row) + "\n")
            sink.flush()
        log(f"[{arm['name']}] t={row['elapsed_s']/60:6.1f}m  {verdict:<14} exit={ip}")
        seq += 1

        # Only a portal-side verdict ends the run. Transport/WAF failures are retried -- treating
        # them as expiry is exactly the misattribution this experiment exists to avoid.
        if verdict in ("SESSION_LOST", "AUTH_REDIRECT", "LOGGED_OUT"):
            log(f"[{arm['name']}] SESSION LOST at t={row['elapsed_s']/60:.1f}m ({verdict}) -- stopping arm")
            arm["ended"] = {"verdict": verdict, "elapsed_s": row["elapsed_s"]}
            return
        if verdict == "TRANSPORT_FAIL":
            consecutive_transport_fail += 1
            if consecutive_transport_fail >= 6:
                log(f"[{arm['name']}] 6 consecutive transport failures -- abandoning arm as UNKNOWN")
                arm["ended"] = {"verdict": "ABANDONED_TRANSPORT", "elapsed_s": row["elapsed_s"]}
                return
        else:
            consecutive_transport_fail = 0

        if arm["mode"] == "idle":
            gap = (gaps.pop(0) if gaps else IDLE_GAPS_MIN[-1]) * 60.0
        else:
            gap = arm["interval"]
        arm["_last_gap"] = gap
        end = min(deadline, time.time() + gap)
        while time.time() < end:
            time.sleep(min(15.0, end - time.time()))


def acquire_session(db, bk, provider, geo, headless, log, used: set) -> dict | None:
    """One authenticated session. Login only -- deliberately NOT warmed (warmup POSTs
    ScheduleController, which this phase forbids)."""
    from libs.domain.enums import SettingsLayer as L, SubAccountStatus
    from libs.domain.settings.resolver import resolve
    from libs.engine.config import EngineConfig
    from libs.engine import vendor_env
    from libs.proxies.lease import conn_url, session_id
    from apps.hub.settings_store import get_global
    from apps.hub.main import _live_account, _rank_live_sub_accounts
    from libs.db import models as M
    from sqlalchemy import select
    import random as _r

    eff = resolve({L.GLOBAL: get_global(db), L.BOOKING: (bk.settings_json or {})})
    eff["ROLE"] = "apply"; eff["WARMUP_ONLY"] = True; eff["CAPTURE_ONLY"] = True
    eff["LOGIN_TRANSIENT_RETRY_MAX"] = 10
    cfg = EngineConfig.from_settings(eff, no_book=True)
    vendor_env.apply(cfg.raw)
    from apps.worker.ops_live import LiveEngineOps

    cands = db.scalars(select(M.Identity).where(M.Identity.status == "active").limit(200)).all()
    _r.shuffle(cands)
    ident = None
    for c in cands:
        if c.id in used:
            continue
        subs = [s for s in _rank_live_sub_accounts(db, c.id) if s.status != SubAccountStatus.BURNED]
        if subs:
            ident, isubs = c, subs
            break
    if ident is None:
        return None
    used.add(ident.id)

    pool = db.scalars(select(M.Proxy).where(
        M.Proxy.provider == provider, M.Proxy.active.is_(True)).limit(2000)).all()
    _r.shuffle(pool)
    url = conn_url(pool[0], geo=geo)
    caps = [k.strip() for k in (get_global(db).get("CAPSOLVER_KEYS", "") or "").split(",") if k.strip()]
    quest = db.scalar(select(M.AppConfig).where(M.AppConfig.key == "quest_steps"))
    formd = db.scalar(select(M.AppConfig).where(M.AppConfig.key == "form_defaults"))

    ops = LiveEngineOps(
        cfg, account=_live_account(ident, isubs[0]), posto_id=str(bk.posto_id),
        nationality=bk.nationality, residence=bk.residence,
        quest_steps=(quest.value if quest else {}), form_defaults=(formd.value if formd else {}),
        capsolver_keys=caps, warm_proxy=url, headless=headless, sub_account_id=isubs[0].id,
        accounts=[dict(_live_account(ident, s), sub_account_id=s.id) for s in isubs],
        identity_id=ident.id)
    ops._log = lambda t: None
    log(f"  login attempt on {provider} #{session_id(url)} geo={geo} ({ident.full_name[:24]})")
    if not ops.do_login():
        try:
            if ops.session: ops.session.close_browser()
        except Exception:
            pass
        return None
    # ops.warm_proxy may have been rolled during login -- report the exit actually in use.
    return {"ops": ops, "session": ops.session, "provider": provider,
            "session_id": session_id(ops.warm_proxy), "identity_id": ident.id, "t0": time.time()}


def reclassify(r: dict) -> str:
    """Re-derive a probe's verdict from the RAW page evidence recorded at the time.

    Exists because the live classifier shipped a bug (any-marker instead of all-markers, see
    probe_auth) that mislabelled logged-out pages as AUTHENTICATED. Because every probe persists
    its own evidence -- byte count, marker list, password field, final url -- the recorded verdicts
    could be corrected offline without re-running the experiment or re-spending logins. Reporting
    always goes through this rather than trusting `verdict`.
    """
    d = r.get("detail") or {}
    url = (d.get("final_url") or "").lower()
    if d.get("page_bytes") is None:
        return "TRANSPORT_FAIL"
    if "sessionlost" in url:
        return "SESSION_LOST"
    if "authentication.jsp" in url:
        return "AUTH_REDIRECT"
    marks = d.get("home_markers") or []
    if len(marks) >= 2:
        return "AUTHENTICATED"
    if marks or d.get("has_password_field"):
        return "LOGGED_OUT"
    return "INCONCLUSIVE"


def report(path: str) -> int:
    rows = [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]
    if not rows:
        print("no rows"); return 1
    for r in rows:
        r["verdict_recorded"] = r.get("verdict")
        r["verdict"] = reclassify(r)
    from collections import defaultdict
    by = defaultdict(list)
    for r in rows:
        by[r["arm"]].append(r)
    print(f"\n=== Q19/Q20 session lifetime -- {len(rows)} probes across {len(by)} arm(s) ===\n")
    for arm, rs in sorted(by.items()):
        rs.sort(key=lambda r: r["ts"])
        print(f"-- {arm}  ({rs[0]['provider']}, mode={rs[0]['mode']}, session #{rs[0]['session_id']})")
        last = rs[-1]
        print(f"   probes {len(rs)}, observed {last['elapsed_s']/60:.1f} min")
        changes = [r for r in rs if r.get("exit_changed_since_prev")]
        print(f"   involuntary exit changes: {len(changes)}")
        for c in changes:
            print(f"      t={c['elapsed_s']/60:6.1f}m  {c['prev_exit_ip']} -> {c['exit_ip']}"
                  f"   session verdict AT the change: {c['verdict']}")
        from collections import Counter
        print(f"   verdicts: {dict(Counter(r['verdict'] for r in rs))}")
        lost = [r for r in rs if r["verdict"] in ("SESSION_LOST", "AUTH_REDIRECT", "LOGGED_OUT")]
        if lost:
            f = lost[0]
            gap = f.get("gap_before_s")
            print(f"   *** SESSION LOST at t={f['elapsed_s']/60:.1f}m ({f['verdict']})"
                  + (f", after a {gap/60:.0f}m gap of no authenticated traffic" if gap else ""))
        else:
            print(f"   *** NOT OBSERVED to expire within {last['elapsed_s']/60:.1f} min")
        alive = [r for r in rs if r["verdict"] == "AUTHENTICATED"]
        if alive:
            print(f"   last authenticated probe: t={alive[-1]['elapsed_s']/60:.1f}m")
        print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=4.0)
    ap.add_argument("--interval", type=float, default=300.0, help="active-arm probe interval (s)")
    ap.add_argument("--geo", default="it")
    ap.add_argument("--booking", default="Test 2032")
    ap.add_argument("--max-login-attempts", type=int, default=14)
    ap.add_argument("--out", default="var/session_lifetime.jsonl")
    ap.add_argument("--report")
    ap.add_argument("--no-headless", action="store_true")
    args = ap.parse_args()
    if args.report:
        return report(args.report)

    from sqlalchemy import select
    from apps.hub.db import SessionLocal
    from libs.db import models as M

    def log(t):
        print(f"  {time.strftime('%H:%M:%S')} {t}", flush=True)

    db = SessionLocal()
    bk = db.scalar(select(M.Booking).where(M.Booking.name == args.booking))
    if bk is None:
        print("no such booking"); return 1

    # arm name -> (provider, mode). soax carries the Q19 vehicle: its sessionlength ceiling forces a
    # real involuntary exit change at ~60 min under the live browser (Q1, 20/20).
    wanted = [("q19_soax_involuntary", "soax", "active"),
              ("q20_active_ws", "webshare", "active"),
              ("q20_idle_ws", "webshare", "idle")]
    used: set = set()
    arms = []
    attempts = 0
    for name, provider, mode in wanted:
        while attempts < args.max_login_attempts:
            attempts += 1
            got = acquire_session(db, bk, provider, args.geo, not args.no_headless, log, used)
            if got:
                got.update(name=name, mode=mode, interval=args.interval)
                got["last_ip"] = None
                arms.append(got)
                log(f"  ACQUIRED {name} ({provider} #{got['session_id']}) "
                    f"after {attempts} total login attempt(s)")
                break
            log(f"  login refused ({attempts}/{args.max_login_attempts} attempts used)")
        if attempts >= args.max_login_attempts and len(arms) < len(wanted):
            log("  login attempt budget exhausted -- continuing with the arms acquired")
            break
    db.close()
    if not arms:
        print("\nNO AUTHENTICATED SESSION COULD BE ESTABLISHED -- experiment not run.")
        print("This is a login-wall result and says nothing about session lifetime.")
        return 2

    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + args.hours * 3600
    lock = threading.Lock()
    print(f"\nobserving {len(arms)} arm(s) for up to {args.hours}h "
          f"(active every {args.interval/60:.0f}m; idle gaps {IDLE_GAPS_MIN} min)\n")
    with out.open("w", encoding="utf-8") as sink:
        ts = [threading.Thread(target=run_arm, args=(a, deadline, sink, lock, log), daemon=True)
              for a in arms]
        for t in ts: t.start()
        for t in ts: t.join()
    for a in arms:
        try: a["session"].close_browser()
        except Exception: pass
    return report(str(out))


if __name__ == "__main__":
    raise SystemExit(main())

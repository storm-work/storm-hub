#!/usr/bin/env python3
"""Q21: does an AUTH-GATED GET refresh the portal authentication session?

Q20 measured a portal session lifetime of 33-45 min under a HOME_URL probe every 5 min, but could
not distinguish two explanations:
    A. an ABSOLUTE lifetime that no activity extends
    B. an IDLE timeout where our HOME_URL probe does not count as activity
This decides between them, and it is the highest-value unknown in the holding architecture: under A,
~40 min is a hard ceiling on any held session; under B, a keepalive that requests the RIGHT url can
hold a session open and only the url was ever wrong.

THE ENDPOINT, AND WHY IT IS TRUSTED. No auth-gated url existed anywhere in this codebase --
"consultar pedidos" appeared only as a marker string -- so it was discovered from a live
authenticated page (probes/authgate_discovery_probe.py) and then PROVEN gated by fetching it from a
cookie-less browser on the same proxy:

    GET /VistosOnline/Pedidos   authenticated -> 59,244B, "Terminar Sessao" present
    GET /VistosOnline/Pedidos   no cookies    -> 28,717B, "Terminar Sessao" ABSENT

Same url, no redirect in either case, decisively different content. The marker is the LOGOUT link:
it exists only when there is a session to end. That is a semantic gate, not a page-size heuristic --
which matters, because Q20's first classifier accepted a PUBLIC menu item as proof of authentication
and reported logged-out pages as authenticated for 25+ minutes.

ARMS -- the control must not be contaminated:
    test     GET /VistosOnline/Pedidos  every 5 min   (auth-gated traffic)
    control  GET /VistosOnline/          every 5 min   (exactly what production's pre-warm keepalive does)
The control NEVER touches the auth-gated endpoint, not even to check its own status: both arms
classify on the SAME marker ("terminar sess"), which is present on both pages while authenticated.
Giving the control an auth-gated status check would hand it the very traffic under test.

SAFETY: GETs only. Never navigates /VistosOnline/logout -- that link is on both pages and would
deliberately destroy the session under measurement. No ScheduleController, no warm, no submit, no
booking path. Raw evidence is persisted per probe so verdicts are re-derivable offline.

    .venv/bin/python probes/q21_auth_refresh_probe.py --hours 3
    .venv/bin/python probes/q21_auth_refresh_probe.py --report var/q21_auth_refresh.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PEDIDOS_URL = "https://pedidodevistos.mne.gov.pt/VistosOnline/Pedidos"
LOGOUT_URL_FRAGMENT = "/logout"          # never navigate here
AUTH_MARKER = "terminar sess"            # the logout link -- present only when a session exists
LOGGED_OUT_MARKER = "iniciar sess"

IP_ECHO = "https://api.ipify.org"


def _exit_ip(session):
    try:
        session.browser_nav(IP_ECHO, timeout=30)
        b = (session.browser_eval("document.body ? document.body.innerText : ''") or "").strip()
    except Exception:
        return None
    return b if b and len(b) <= 45 and " " not in b else None


def probe(session, url: str) -> tuple[str, dict]:
    """One GET with full raw evidence. Retries the nav/eval race that blinded the Q20 idle arm."""
    assert LOGOUT_URL_FRAGMENT not in url, "refusing to navigate to the logout endpoint"
    d: dict = {"requested": url}
    final = html = None
    has_pw = None
    for _ in range(3):
        try:
            final = session.browser_nav(url, timeout=45)
        except Exception as e:
            d["error"] = f"{type(e).__name__}: {e}"[:160]
            time.sleep(2); continue
        if "pedidodevistos.mne.gov.pt" not in (final or ""):
            d["off_origin_url"] = final
            time.sleep(2); continue
        try:
            html = session.browser_eval("document.documentElement.outerHTML") or ""
            has_pw = bool(session.browser_eval(
                "document.querySelector('input[type=password]') !== null"))
            break
        except Exception as e:
            d["error"] = f"{type(e).__name__}: {e}"[:160]
            html = None; time.sleep(2)
    d["final_url"] = final
    if html is None:
        return "TRANSPORT_FAIL", d
    low = html.lower()
    d["bytes"] = len(html)
    d["has_password_field"] = has_pw
    d["has_auth_marker"] = AUTH_MARKER in low          # "terminar sess" -> logged IN
    d["has_loggedout_marker"] = LOGGED_OUT_MARKER in low
    d["has_consultar"] = "consultar pedidos" in low
    d["sessionlost"] = "sessionlost" in (final or "").lower()
    d["auth_jsp"] = "authentication.jsp" in (final or "").lower()
    return classify(d), d


def classify(d: dict) -> str:
    """Verdict from raw evidence only -- never page size, never a public menu item."""
    if d.get("bytes") is None:
        return "TRANSPORT_FAIL"
    if d.get("sessionlost"):
        return "SESSION_LOST"
    if d.get("auth_jsp"):
        return "AUTH_REDIRECT"
    if d.get("has_auth_marker"):
        return "AUTHENTICATED"
    # A page must POSITIVELY look logged-out to be called logged-out. The original fallback here was
    # `bytes > 0`, which made ANY unrecognised response "LOGGED_OUT" -- and it fired once, on a
    # 1,920-byte body with no markers at all, no password field and no redirect, served at the home
    # url at t=180.1m. That is an unrecognised response, not evidence of logout, and calling it one
    # would have manufactured the very session-expiry this experiment was looking for.
    if d.get("has_loggedout_marker") or d.get("has_password_field"):
        return "LOGGED_OUT"
    # The known logged-out home page is ~58.2KB and carries the public menu item. A body this small
    # is something else entirely (error/interstitial) -- report it as unknown.
    return "INCONCLUSIVE"


def run_arm(arm, deadline, sink, lock, log):
    url = arm["url"]
    seq = 0
    fails = 0
    while time.time() < deadline:
        verdict, d = probe(arm["session"], url)
        ip = _exit_ip(arm["session"])
        now = time.time()
        row = {"arm": arm["name"], "kind": arm["kind"], "url": url, "provider": arm["provider"],
               "proxy_session_id": arm["session_id"], "identity_id": arm["identity_id"],
               "seq": seq, "t0": arm["t0"], "ts": now, "elapsed_s": round(now - arm["t0"], 1),
               "exit_ip": ip, "verdict": verdict, "detail": d,
               "vistos_sid_present": arm.get("sid_present")}
        with lock:
            sink.write(json.dumps(row) + "\n"); sink.flush()
        log(f"[{arm['name']}] t={row['elapsed_s']/60:6.1f}m {verdict:<14} "
            f"bytes={d.get('bytes')} auth_marker={d.get('has_auth_marker')} exit={ip}")
        seq += 1
        if verdict in ("LOGGED_OUT", "SESSION_LOST", "AUTH_REDIRECT"):
            log(f"[{arm['name']}] *** SESSION LOST at t={row['elapsed_s']/60:.1f}m ({verdict})")
            return
        if verdict == "TRANSPORT_FAIL":
            fails += 1
            if fails >= 8:
                log(f"[{arm['name']}] abandoned -- 8 consecutive transport failures")
                return
        else:
            fails = 0
        end = min(deadline, time.time() + arm["interval"])
        while time.time() < end:
            time.sleep(min(15.0, end - time.time()))


def report(path: str) -> int:
    rows = [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]
    if not rows:
        print("no rows"); return 1
    for r in rows:
        r["verdict"] = classify(r.get("detail") or {})
    by = defaultdict(list)
    for r in rows:
        by[r["arm"]].append(r)
    print(f"\n=== Q21: does an auth-gated GET refresh authentication? -- {len(rows)} probes ===\n")
    summary = {}
    for arm, rs in sorted(by.items()):
        rs.sort(key=lambda r: r["ts"])
        auth = [r for r in rs if r["verdict"] == "AUTHENTICATED"]
        lost = [r for r in rs if r["verdict"] in ("LOGGED_OUT", "SESSION_LOST", "AUTH_REDIRECT")]
        print(f"-- {arm}  [{rs[0]['kind']}]  {rs[0]['url']}")
        print(f"   provider={rs[0]['provider']} #{rs[0]['proxy_session_id']}  probes={len(rs)}")
        print(f"   verdicts: {dict(Counter(r['verdict'] for r in rs))}")
        if auth:
            print(f"   last AUTHENTICATED : t={auth[-1]['elapsed_s']/60:.1f} min")
        if lost:
            print(f"   *** FIRST LOGOUT   : t={lost[0]['elapsed_s']/60:.1f} min  ({lost[0]['verdict']})")
            summary[arm] = (rs[0]["kind"], lost[0]["elapsed_s"] / 60.0, auth[-1]["elapsed_s"]/60 if auth else 0)
        else:
            print(f"   *** NOT OBSERVED to expire within {rs[-1]['elapsed_s']/60:.1f} min")
            summary[arm] = (rs[0]["kind"], None, auth[-1]["elapsed_s"]/60 if auth else 0)
        print()
    tests = [v for v in summary.values() if v[0] == "test"]
    ctrls = [v for v in summary.values() if v[0] == "control"]
    print("=== VERDICT ===")
    print(f"  test arms   (auth-gated /Pedidos): "
          f"{[('lost @%.1fm' % v[1]) if v[1] else ('alive @%.1fm' % v[2]) for v in tests]}")
    print(f"  control arms (HOME_URL)          : "
          f"{[('lost @%.1fm' % v[1]) if v[1] else ('alive @%.1fm' % v[2]) for v in ctrls]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=3.0)
    ap.add_argument("--interval", type=float, default=300.0)
    ap.add_argument("--tests", type=int, default=2)
    ap.add_argument("--controls", type=int, default=1)
    ap.add_argument("--geo", default="it")
    ap.add_argument("--max-login-attempts", type=int, default=16)
    ap.add_argument("--out", default="var/q21_auth_refresh.jsonl")
    ap.add_argument("--report")
    args = ap.parse_args()
    if args.report:
        return report(args.report)

    from apps.hub.db import SessionLocal
    from libs.db import models as M
    from sqlalchemy import select
    from probes.session_lifetime_probe import acquire_session
    from apps.worker.ops_live import HOME_URL

    def log(t):
        print(f"  {time.strftime('%H:%M:%S')} {t}", flush=True)

    db = SessionLocal()
    bk = db.scalar(select(M.Booking).where(M.Booking.name == "Test 2032"))
    wanted = ([("q21_test_%d" % i, "test", PEDIDOS_URL) for i in range(1, args.tests + 1)]
              + [("q21_control_%d" % i, "control", HOME_URL) for i in range(1, args.controls + 1)])
    used, arms, attempts = set(), [], 0
    for name, kind, url in wanted:
        while attempts < args.max_login_attempts:
            attempts += 1
            got = acquire_session(db, bk, "webshare", args.geo, True, log, used)
            if got:
                try:
                    ck = got["session"].get_all_cookies()
                    got["sid_present"] = any(c.get("name") == "Vistos_sid" for c in ck)
                except Exception:
                    got["sid_present"] = None
                got.update(name=name, kind=kind, url=url, interval=args.interval)
                arms.append(got)
                log(f"  ACQUIRED {name} [{kind}] webshare #{got['session_id']} "
                    f"Vistos_sid={got['sid_present']} (attempt {attempts})")
                break
            log(f"  login refused ({attempts}/{args.max_login_attempts})")
        if attempts >= args.max_login_attempts:
            break
    db.close()
    if not arms:
        print("\nNO AUTHENTICATED SESSION -- login wall. Experiment not run.")
        return 2

    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + args.hours * 3600
    lock = threading.Lock()
    print(f"\nobserving {len(arms)} arm(s) for up to {args.hours}h, probe every "
          f"{args.interval/60:.0f} min\n")
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

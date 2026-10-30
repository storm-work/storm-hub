#!/usr/bin/env python3
"""Does the portal's POST-LOGIN flow need the browser, or can a DETACHED HTTP client do it?

RE-OPENS the detached-client question after the HAR finding (2026-10, data/new_booking.har): the
real booking's ONLY anti-bot is Google reCAPTCHA Enterprise (sitekey 6LdOB9cr..., verified
server-side from the token). There is NO DataDome/Akamai/bot-challenge anywhere in a real booking,
and the SubmeterVistoCriaPDF submit body carries NO token (`lang=PT&txtHuman=&back=&f_date_c=...&
cmbPeriodo=5`) -- it is purely session-cookie-gated. The vendored engine ALSO already uses a
detached primp client (synced cookies, same proxy) for its API calls. So a detached client with
(a) valid session cookies from a browser login, (b) IP continuity on the same sticky exit, (c) a
valid reCAPTCHA token for /slots, MIGHT do the post-login steps.

LOGIN IS A SHOT-LOTTERY, especially in stress -- so this probe does NOT give up after one login.
It auto-pulls VERIFIED sub-accounts from the DB (that are not in a live run), and KEEPS TRYING
(rolling exits + rotating accounts) until ONE logs in, THEN runs the detached test. That way a
login failure never robs us of the valuable post-login steps.

Once logged in, it replays from the SAME exit, vs the browser control:
    1. GET  Schedule.jsp?posto_id=X  -> authed form (markers) or a login redirect?
    2. POST /slots?posto_id=X        -> a slots array / ReCaptchaError / a WAF block?

NEVER SUBMITS. SubmeterVistoCriaPDF is NOT called under any client. Nothing is leased or booked.

Usage:
  cd /srv/projects/storm-hub
  .venv/bin/python probes/httpx_postlogin_probe.py <posto_id> [--pool soax|isp] [--accounts N] \
                                                   [--exit-tries K] [--headed] [--user U --pass P]
  # default: posto 3059, pool soax, 8 accounts, 6 exit rolls each. --user/--pass overrides the DB pull.
"""
import os
import random
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

BASE = "https://pedidodevistos.mne.gov.pt/VistosOnline"
SCHED = BASE + "/Schedule.jsp"
SLOTS = BASE + "/slots"
ORIGIN = "https://pedidodevistos.mne.gov.pt"
MARKERS = ("getSlots", "cmbPeriodo", "SubmeterVistoCriaPDF", "f_date_c", "captchaDiv")

# login-result classification
_THROTTLE = ("não foi possível de momento", "try again later", "serviço que pretende")
_TRANSIENT = ("err_timed_out", "err_connection", "err_tunnel", "err_proxy", "timed out",
              "timeout", "chromewebdata", "failed to fetch", "err_empty_response")


def _load_env():
    env = _ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def _arg(flag, default=None):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def _summarize(body: str) -> str:
    low = (body or "").lower()
    # Order matters: a full Schedule.jsp page CONTAINS the literal "ReCaptchaError" in its JS, so
    # the authed-form check (markers + size) must win before the error-substring checks.
    if any(m in (body or "") for m in MARKERS) and len(body or "") > 5000:
        return "AUTHED schedule form (markers present)"
    if "authentication.jsp" in low or "sessionlost" in low:
        return "LOGIN-REDIRECT (session not accepted)"
    if "recaptchaerror" in low:
        return "ReCaptchaError (token refused)"
    if "datadome" in low or "/ch/bd.js" in low:
        return "BOT-CHALLENGE (DataDome)"
    if any(m in (body or "") for m in MARKERS):
        return "AUTHED schedule form (markers present)"
    try:
        import json as _j
        d = _j.loads((body or "").strip())
        if isinstance(d, list):
            return f"JSON array ({len(d)} items) -- a real /slots answer"
        return f"JSON object: {str(d)[:80]}"
    except Exception:
        return f"unrecognised ({len(body or '')}B)"


def _pick_accounts(db, limit):
    from sqlalchemy import text
    # VERIFIED sub-accounts whose identity is NOT in a live (RUNNING) worker, proven-loginnable first.
    rows = db.execute(text("""
        select sa.username, sa.password
        from sub_accounts sa
        where sa.status = 'VERIFIED'
          and sa.password is not null and sa.password <> ''
          and sa.identity_id not in (select identity_id from workers where status = 'RUNNING')
          and (cast(:g as text) is null or sa.identity_id in (select id from identities where group_name = :g))
          and (cast(:iid as text) is null or sa.identity_id = :iid)
        order by (sa.last_login_ok_at is not null) desc, random()
        limit :n
    """), {"n": limit, "g": _arg("--group"), "iid": _arg("--identity-id")}).all()
    return [(r.username, r.password) for r in rows]


def _login_until_success(get_session, keys, proxy0, accounts, exit_tries, headed):
    """Keep trying (roll exits + rotate accounts) until one logs in. Returns (session, user, proxy)."""
    from libs.proxies.rotating import roll_rotating_url
    for user, pw in accounts:
        proxy = proxy0
        for k in range(1, exit_tries + 1):
            print(f"[login] {user}  exit-try {k}/{exit_tries}")
            s = None
            try:
                s = get_session(proxy, headless=not headed)
                res = s.browser_login(user, pw, lang="PT", capsolver_keys=keys, timeout=180)
                body = (res.get("body") or "") if isinstance(res, dict) else str(res)
                status = res.get("status") if isinstance(res, dict) else "?"
                low = body.lower()
                if status == 200 or "terminar" in low:
                    print(f"[login] OK ✓  ({user})")
                    return s, user, proxy
                if any(m in low for m in _THROTTLE) or any(m in low for m in _TRANSIENT):
                    print(f"[login]   throttle/transient -- rolling exit (status={status})")
                    try: s.close_browser()
                    except Exception: pass
                    proxy = roll_rotating_url(proxy)
                    continue
                # a real auth verdict (bad creds / already booked) -- next account
                print(f"[login]   auth verdict, next account: {body[:120]!r}")
                try: s.close_browser()
                except Exception: pass
                break
            except Exception as e:
                msg = str(e).lower()
                print(f"[login]   error: {type(e).__name__}: {str(e)[:120]}")
                try:
                    if s is not None: s.close_browser()
                except Exception: pass
                if any(m in msg for m in _TRANSIENT):
                    proxy = roll_rotating_url(proxy)
                    continue
                break
    return None, None, None


def main() -> int:
    posto = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "3059"
    pool = (_arg("--pool", "soax")).lower()
    n_acc = int(_arg("--accounts", "8"))
    exit_tries = int(_arg("--exit-tries", "6"))
    headed = "--headed" in sys.argv
    u_over, p_over = _arg("--user"), _arg("--pass")

    _load_env()
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from libs.db import models as M
    from libs.proxies import conn_url

    eng = create_engine(os.environ["DATABASE_URL"])
    with Session(eng) as db:
        keys = [k.strip() for k in
                (db.scalar(select(M.SettingRow.value).where(M.SettingRow.key == "CAPSOLVER_KEYS")) or "").split(",")
                if k.strip()]
        if not keys:
            print("ERROR: set CAPSOLVER_KEYS in the dashboard first"); return 1
        from apps.hub.settings_store import get_global
        _settings = get_global(db)
        geo = _arg("--geo")                      # e.g. DE/IT clear the login wall ~8x better than CZ
        if pool in ("soax", "rotating"):
            # ROTATING: self-MINT like the workers (no DB rows exist for rotating) -- D39/D40.
            from libs.proxies.rotating import mint_rotating_url
            proxy0 = mint_rotating_url("soax", "rotating", settings=_settings, geo=geo or None)
        else:
            cands = db.scalars(select(M.Proxy).where(M.Proxy.provider == pool,
                               M.Proxy.active.is_(True), M.Proxy.is_healthy.is_(True))).all()
            if not cands:
                print(f"ERROR: no healthy {pool.upper()} proxy"); return 1
            proxy0 = conn_url(random.choice(cands))
        accounts = [(u_over, p_over)] if (u_over and p_over) else _pick_accounts(db, n_acc)
    if not accounts:
        print("ERROR: no VERIFIED sub-accounts available (all in use?)"); return 1
    print(f"[probe] posto={posto} pool={pool} accounts={len(accounts)} exit_tries={exit_tries}")

    os.environ.setdefault("CAPSOLVER_KEYS", ",".join(keys))
    os.environ["HEADED"] = "1" if headed else "0"
    from libs.engine.vendor import get_session, race_all

    s, user, proxy = _login_until_success(get_session, keys, proxy0, accounts, exit_tries, headed)
    if s is None:
        print("[probe] NO LOGIN after all accounts/exits -- could not reach the detached test. "
              "Re-run later or raise --accounts/--exit-tries.")
        return 1
    try:
        # BROWSER control
        s.browser_nav(SCHED + "?posto_id=" + posto, timeout=30)
        bhtml = s.browser_eval("document.documentElement.outerHTML") or ""
        ua = (s.browser_eval("navigator.userAgent") or "").strip()
        try:
            exit_ip = s.browser_eval(
                "fetch('https://api.ipify.org?format=json').then(r=>r.json()).then(j=>j.ip)")
        except Exception:
            exit_ip = "?"
        print(f"\n[probe] BROWSER GET Schedule.jsp -> {_summarize(bhtml)} ({len(bhtml)}B)  exit_ip={exit_ip}")

        cookies = s.get_all_cookies()
        jar = {c["name"]: c["value"] for c in cookies
               if c.get("name") in ("Vistos_sid", "cookiesession1")
               or "mne.gov.pt" in (c.get("domain") or "")}
        print(f"[probe] detached clients get cookies: {list(jar)}")

        print("[probe] solving a SCHEDULE_EVISA token for /slots (ProxyLess) ...")
        tok = race_all(keys, [], [], [], "SCHEDULE_EVISA", proxy=None) or ""
        print(f"[probe] token len={len(tok)}")

        headers = {
            "User-Agent": ua or "Mozilla/5.0",
            "sec-ch-ua": '"Not;A=Brand";v="99", "Google Chrome";v="139", "Chromium";v="139"',
            "sec-fetch-site": "same-origin",
            "Referer": SCHED + "?posto_id=" + posto,
            "Origin": ORIGIN,
        }
        cookie_hdr = "; ".join(f"{k}={v}" for k, v in jar.items())
        slots_url = SLOTS + "?posto_id=" + posto
        sched_url = SCHED + "?posto_id=" + posto
        body = f"posto_id={posto}&captcha={tok}"

        # ---- httpx (plain Python TLS) ----
        print("\n=== httpx (plain Python TLS, SAME exit, synced cookies) ===")
        try:
            import httpx
            try:
                hc = httpx.Client(proxy=proxy, verify=False, timeout=15.0)
            except TypeError:
                hc = httpx.Client(proxies=proxy, verify=False, timeout=15.0)
            h = {**headers, "Cookie": cookie_hdr}
            r = hc.get(sched_url, headers=h)
            print(f"  GET  Schedule.jsp -> status={r.status_code}  {_summarize(r.text)}")
            r = hc.post(slots_url, content=body.encode(),
                        headers={**h, "Content-Type": "application/x-www-form-urlencoded"})
            print(f"  POST /slots       -> status={r.status_code}  {_summarize(r.text)}")
            hc.close()
        except Exception as e:
            print(f"  httpx ERROR: {type(e).__name__}: {str(e)[:160]}")

        # ---- primp (Chrome-impersonated TLS) ----
        print("\n=== primp (Chrome-impersonated TLS, SAME exit, synced cookies) ===")
        try:
            import primp
            pc = primp.Client(proxy=proxy, verify=False, timeout=15, follow_redirects=True)
            try: pc.set_cookies(ORIGIN + "/", jar)
            except Exception: pass
            r = pc.get(sched_url, headers={**headers, "Cookie": cookie_hdr})
            print(f"  GET  Schedule.jsp -> status={r.status_code}  {_summarize(r.text)}")
            r = pc.post(slots_url, data=body,
                        headers={**headers, "Cookie": cookie_hdr,
                                 "Content-Type": "application/x-www-form-urlencoded"})
            print(f"  POST /slots       -> status={r.status_code}  {_summarize(r.text)}")
        except Exception as e:
            print(f"  primp ERROR: {type(e).__name__}: {str(e)[:160]}")

        print("\n[probe] DONE (logged in as %s). NEVER SUBMITTED. If a detached client shows an "
              "AUTHED form + a real /slots answer, the post-login steps do NOT need the browser." % user)
        return 0
    finally:
        try: s.close_browser()
        except Exception: pass


if __name__ == "__main__":
    raise SystemExit(main())

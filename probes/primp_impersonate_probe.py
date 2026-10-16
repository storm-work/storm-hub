#!/usr/bin/env python3
"""STRICT-WINDOW A/B: does aligning primp's TLS fingerprint to the browser defeat the WAF?

MEASURED 2026-10-16 ([[portal-strict-window-blocks-primp]]): the portal flips to a strict anti-bot
posture ~75 min before slot-open and serves a ~44KB bot-challenge interstitial to our DETACHED primp
requests (quest steps, keepalive probe) while the real BROWSER passes. The Explore found primp was
running with NO impersonation (a generic/random fingerprint) while the browser version-aligns to the
real Chromium (149). This probe tests the fix: pin primp to `chrome_148` (closest valid primp target)
and see if the same session-cookie GETs that get the interstitial under the default come back as the
real form under chrome_148.

It logs in once (reusing httpx_postlogin_probe's shot-lottery login harness), extracts the FULL cookie
jar + UA, then for each impersonation variant replays the SAME GETs on the SAME exit -- so the ONLY
variable is the primp TLS/JA3 fingerprint. NEVER SUBMITS; nothing is leased or booked.

RUN IT DURING A STRICT WINDOW (e.g. ~60-75 min before a real slot-open) -- in the permissive window
BOTH variants pass and the test is inconclusive.

Usage:
  cd /srv/projects/storm-hub
  .venv/bin/python probes/primp_impersonate_probe.py <posto_id> [--pool soax|isp] [--accounts N] \
                                                      [--exit-tries K] [--geo DE] [--user U --pass P]
"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

# Reuse the proven login harness + env/account helpers rather than re-implementing them.
from probes.httpx_postlogin_probe import (          # noqa: E402
    _load_env, _pick_accounts, _login_until_success, _arg,
)

BASE = "https://pedidodevistos.mne.gov.pt/VistosOnline"
ORIGIN = "https://pedidodevistos.mne.gov.pt"
QUEST = BASE + "/Questionario"
SCHED = BASE + "/Schedule.jsp"

# The two variants under test. None = the OLD behaviour (primp's generic/random default).
VARIANTS = [("default (unset -> random)", None), ("chrome_148 (browser-aligned)", "chrome_148")]


def _classify(html: str) -> str:
    """form | CHALLENGE | other -- the same opinion the worker's _is_challenge_body/markers encode."""
    from apps.worker.ops_live import _is_challenge_body, _SCHED_PAGE_MARKERS
    low = (html or "").lower()
    if any(m.lower() in low for m in _SCHED_PAGE_MARKERS) or "pergunta" in low or "questionario" in low:
        return "form"
    if _is_challenge_body(html):
        return "CHALLENGE"
    if "authentication.jsp" in low:
        return "auth-redirect"
    return "other"


def _probe_variant(proxy: str, jar: dict, ua: str, impersonate):
    import primp
    kw = dict(proxy=proxy, verify=False, timeout=30, connect_timeout=10, read_timeout=25,
              follow_redirects=True)
    if impersonate is not None:
        kw["impersonate"] = impersonate
    pc = primp.Client(**kw)
    try:
        pc.set_cookies(ORIGIN + "/", jar)
    except Exception:
        pass
    H = {"User-Agent": ua or "Mozilla/5.0",
         "Cookie": "; ".join(f"{k}={v}" for k, v in jar.items())}
    out = {}
    for name, url in (("Questionario", QUEST), ("Schedule.jsp", SCHED + "?posto_id=" + POSTO)):
        try:
            r = pc.get(url, headers=H)
            body = r.text or ""
            out[name] = f"{_classify(body):9} ({len(body)}B, status={getattr(r,'status_code','?')})"
        except Exception as e:
            out[name] = f"ERROR {type(e).__name__}: {str(e)[:50]}"
    return out


POSTO = "3059"


def main() -> int:
    global POSTO
    POSTO = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "3059"
    pool = (_arg("--pool", "soax")).lower()
    n_acc = int(_arg("--accounts", "8"))
    exit_tries = int(_arg("--exit-tries", "6"))
    u_over, p_over = _arg("--user"), _arg("--pass")

    _load_env()
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from libs.db import models as M

    eng = create_engine(os.environ["DATABASE_URL"])
    with Session(eng) as db:
        keys = [k.strip() for k in
                (db.scalar(select(M.SettingRow.value).where(M.SettingRow.key == "CAPSOLVER_KEYS")) or "").split(",")
                if k.strip()]
        if not keys:
            print("ERROR: set CAPSOLVER_KEYS in the dashboard first"); return 1
        from apps.hub.settings_store import get_global
        _settings = get_global(db)
        geo = _arg("--geo")
        if pool in ("soax", "rotating"):
            from libs.proxies.rotating import mint_rotating_url
            proxy0 = mint_rotating_url("soax", "rotating", settings=_settings, geo=geo or None)
        else:
            from libs.proxies import conn_url
            import random
            cands = db.scalars(select(M.Proxy).where(M.Proxy.provider == pool,
                               M.Proxy.active.is_(True), M.Proxy.is_healthy.is_(True))).all()
            if not cands:
                print(f"ERROR: no healthy {pool.upper()} proxy"); return 1
            proxy0 = conn_url(random.choice(cands))
        accounts = [(u_over, p_over)] if (u_over and p_over) else _pick_accounts(db, n_acc)
    if not accounts:
        print("ERROR: no VERIFIED sub-accounts available"); return 1

    os.environ.setdefault("CAPSOLVER_KEYS", ",".join(keys))
    from libs.engine.vendor import get_session

    print(f"[probe] posto={POSTO} pool={pool} -- logging in (shot lottery) ...")
    s, user, proxy = _login_until_success(get_session, keys, proxy0, accounts, exit_tries, False)
    if s is None:
        print("[probe] NO LOGIN -- re-run later or raise --accounts/--exit-tries."); return 1
    try:
        ua = (s.browser_eval("navigator.userAgent") or "").strip()
        cookies = s.get_all_cookies() or []
        jar = {c["name"]: c.get("value", "") for c in cookies
               if c.get("name") and ("mne.gov.pt" in (c.get("domain") or "") or not c.get("domain"))}
        print(f"[probe] logged in as {user}; FULL jar carried to primp: {sorted(jar)}")
        print(f"[probe] exit={proxy}\n")
        print(f"{'variant':34} {'Questionario':34} {'Schedule.jsp'}")
        for label, imp in VARIANTS:
            res = _probe_variant(proxy, jar, ua, imp)
            print(f"{label:34} {res['Questionario']:34} {res['Schedule.jsp']}")
        print("\nINTERPRETATION: if 'default' shows CHALLENGE and 'chrome_148' shows form, the WAF is "
              "fingerprint-gated and the impersonation fix defeats it. If BOTH pass, you're in the "
              "permissive window (re-run closer to slot-open). If BOTH show CHALLENGE, the fingerprint "
              "is not the (only) gate -- consider the browser-submit fallback.")
    finally:
        try:
            s.close_browser()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

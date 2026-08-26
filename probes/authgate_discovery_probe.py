#!/usr/bin/env python3
"""Find and PROVE an authentication-gated portal endpoint (prerequisite for Q21).

Q21 must not use HOME_URL: the whole point is to test whether a genuinely authenticated request
refreshes the session, and the home page is public (it renders for anyone, merely with a different
menu). No auth-gated URL exists anywhere in this codebase -- `consultar pedidos` appears only as a
marker string -- so it has to be discovered from a live authenticated page and then VERIFIED.

Verification is the part that matters. An endpoint is only usable for Q21 if we can show it behaves
differently for an authenticated session than for a session-less browser. So this probe:

  1. logs in once
  2. dumps every link on the authenticated home page
  3. picks the "consultar pedidos" (consult requests) candidate
  4. GETs it WITH the session          -> records url, size, redirect target, markers
  5. GETs it from a CLEAN browser      -> same proxy, no cookies at all
  6. compares, and only accepts the endpoint if the two differ in a way that proves a gate

Output: probes/authgate_endpoint.json, consumed by the Q21 probe, so the endpoint and its verdict
markers are derived from evidence rather than assumed.

GET only. No POST anywhere -- no ScheduleController, no form submit, no booking path.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT = Path(__file__).resolve().parent / "authgate_endpoint.json"

_LINK_RE = re.compile(r'<a\b[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def links(html: str) -> list[tuple[str, str]]:
    out = []
    for href, inner in _LINK_RE.findall(html or ""):
        text = re.sub(r"\s+", " ", _TAG_RE.sub("", inner)).strip()
        if href and not href.lower().startswith(("javascript:", "#", "mailto:")):
            out.append((href, text))
    return out


def fetch(session, url: str, timeout: int = 45) -> dict:
    """One GET, with the raw evidence Q21 will need to re-derive verdicts."""
    d: dict = {"requested": url}
    try:
        final = session.browser_nav(url, timeout=timeout)
    except Exception as e:
        d["error"] = f"{type(e).__name__}: {e}"[:200]
        return d
    d["final_url"] = final
    try:
        html = session.browser_eval("document.documentElement.outerHTML") or ""
    except Exception as e:
        d["error"] = f"{type(e).__name__}: {e}"[:200]
        return d
    low = html.lower()
    d["bytes"] = len(html)
    d["title"] = (re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S).group(1).strip()[:120]
                  if re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S) else None)
    d["has_password_field"] = bool(session.browser_eval(
        "document.querySelector('input[type=password]') !== null"))
    for probe in ("consultar pedidos", "solicitar pedido de visto", "sessionlost",
                  "authentication.jsp", "iniciar sess", "terminar sess", "sair"):
        d[f"has::{probe}"] = probe in low
    return d


def main() -> int:
    from sqlalchemy import select
    from apps.hub.db import SessionLocal
    from libs.db import models as M
    from libs.domain.enums import SettingsLayer as L, SubAccountStatus
    from libs.domain.settings.resolver import resolve
    from libs.engine.config import EngineConfig
    from libs.engine import vendor_env
    from libs.proxies.lease import conn_url, session_id
    from apps.hub.settings_store import get_global
    from apps.hub.main import _live_account, _rank_live_sub_accounts
    import random as _r

    db = SessionLocal()
    bk = db.scalar(select(M.Booking).where(M.Booking.name == "Test 2032"))
    eff = resolve({L.GLOBAL: get_global(db), L.BOOKING: (bk.settings_json or {})})
    eff["ROLE"] = "apply"; eff["WARMUP_ONLY"] = True; eff["CAPTURE_ONLY"] = True
    eff["LOGIN_TRANSIENT_RETRY_MAX"] = 10
    cfg = EngineConfig.from_settings(eff, no_book=True)
    vendor_env.apply(cfg.raw)
    from apps.worker.ops_live import LiveEngineOps, HOME_URL
    from libs.engine.vendor import get_session

    cands = db.scalars(select(M.Identity).where(M.Identity.status == "active").limit(200)).all()
    _r.shuffle(cands)
    pool = db.scalars(select(M.Proxy).where(
        M.Proxy.provider == "webshare", M.Proxy.active.is_(True)).limit(2000)).all()
    _r.shuffle(pool)

    ops = None
    for attempt, ident in enumerate(cands[:8], 1):
        subs = [s for s in _rank_live_sub_accounts(db, ident.id)
                if s.status != SubAccountStatus.BURNED]
        if not subs:
            continue
        url = conn_url(pool[attempt], geo="it")
        print(f"  login attempt {attempt} on webshare #{session_id(url)} ({ident.full_name[:24]})",
              flush=True)
        o = LiveEngineOps(
            cfg, account=_live_account(ident, subs[0]), posto_id=str(bk.posto_id),
            nationality=bk.nationality, residence=bk.residence, quest_steps={}, form_defaults={},
            capsolver_keys=[k.strip() for k in
                            (get_global(db).get("CAPSOLVER_KEYS", "") or "").split(",") if k.strip()],
            warm_proxy=url, headless=True, sub_account_id=subs[0].id,
            accounts=[dict(_live_account(ident, s), sub_account_id=s.id) for s in subs],
            identity_id=ident.id)
        o._log = lambda t: None
        if o.do_login():
            ops = o
            proxy_used = o.warm_proxy
            break
        try:
            if o.session: o.session.close_browser()
        except Exception:
            pass
    db.close()
    if ops is None:
        print("could not authenticate -- login wall. Nothing discovered.")
        return 2

    result: dict = {"discovered_at": time.time(), "proxy_session_id": session_id(proxy_used)}
    try:
        ops.session.browser_nav(HOME_URL, timeout=45)
        html = ops.session.browser_eval("document.documentElement.outerHTML") or ""
        print(f"\n  authenticated home: {len(html)}B")
        all_links = links(html)
        result["home_bytes"] = len(html)
        result["links"] = [{"href": h, "text": t} for h, t in all_links][:60]
        print("  links on the authenticated home page:")
        for h, t in all_links:
            print(f"     {t[:44]:<46} -> {h[:80]}")

        cand = [(h, t) for h, t in all_links if "consultar" in t.lower()]
        if not cand:
            cand = [(h, t) for h, t in all_links
                    if any(k in h.lower() for k in ("consult", "pedido", "lista"))]
        if not cand:
            result["error"] = "no consultar-pedidos link found on the authenticated home page"
            print("\n  !! no candidate auth-gated link found")
            OUT.write_text(json.dumps(result, indent=2)); return 3

        href, text = cand[0]
        from urllib.parse import urljoin
        target = urljoin(HOME_URL, href)
        result["candidate"] = {"href": href, "text": text, "url": target}
        print(f"\n  candidate auth-gated endpoint: {text!r} -> {target}")

        result["authed"] = fetch(ops.session, target)
        print(f"  WITH session   : {json.dumps(result['authed'])[:300]}")
    finally:
        pass

    # A session-less browser on the SAME proxy. If the endpoint is truly gated, this must differ.
    clean = None
    try:
        clean = get_session(proxy=proxy_used, headless=True)
        result["anonymous"] = fetch(clean, result["candidate"]["url"])
        print(f"  NO session     : {json.dumps(result['anonymous'])[:300]}")
    except Exception as e:
        result["anonymous"] = {"error": f"{type(e).__name__}: {e}"[:200]}
    finally:
        for s in (clean, ops.session):
            try:
                if s: s.close_browser()
            except Exception:
                pass

    a, n = result.get("authed", {}), result.get("anonymous", {})
    gated = False
    reasons = []
    if a.get("final_url") and n.get("final_url"):
        if a["final_url"] != n["final_url"]:
            gated = True; reasons.append(f"different final_url ({a['final_url']} vs {n['final_url']})")
        if n.get("has_password_field") and not a.get("has_password_field"):
            gated = True; reasons.append("anonymous gets a password field, authenticated does not")
        if n.get("has::authentication.jsp") or "authentication" in (n.get("final_url") or "").lower():
            gated = True; reasons.append("anonymous is redirected to Authentication.jsp")
        if a.get("bytes") and n.get("bytes") and abs(a["bytes"] - n["bytes"]) > 500:
            gated = True; reasons.append(f"size differs ({a['bytes']} vs {n['bytes']})")
    result["is_auth_gated"] = gated
    result["gate_evidence"] = reasons
    OUT.write_text(json.dumps(result, indent=2))
    print(f"\n  AUTH-GATED: {gated}")
    for r in reasons:
        print(f"     - {r}")
    print(f"  written -> {OUT}")
    return 0 if gated else 4


if __name__ == "__main__":
    raise SystemExit(main())

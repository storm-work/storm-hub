"""Donut Browser probes for BROWSER_BACKEND=donut. Touch NO portal page (the only site visited is the
IP echo api.ipify.org).

STAGE 1 -- readiness (default). Checks, in order, and stops at the first failure with the reason:
  1. the Local API answers (/v1/openapi.json, no token needed);
  2. the token is accepted (GET /v1/profiles);
  3. a throwaway profile starts on the given exit and Playwright attaches over CDP -- i.e. the plan
     really includes automation (402 otherwise) and the launch path the engine uses works;
  4. what the profile presents: the fingerprint line the engine logs, and the exit IP (ipify);
  5. CDP throughput: --cdp-ops quick page.evaluate round-trips, timed. Donut documents a limit of
     "100 automation (CDP) requests per minute" on Wayfern without saying per profile or per
     account; a login alone is far more than 100 CDP messages, so this is the number that decides
     whether the mode can work at all.

STAGE 2 -- scale (--count N). Starts N throwaway profiles AT ONCE, each on its own fresh sticky exit,
attaches to each, opens the IP echo, holds them all for --hold seconds while sampling RSS/CPU of
every profile's process tree, then stops them. Reports start/attach latency (p50/p95/max), failures,
429s (Donut's hourly automation limit) and per-profile RAM/CPU. --backend chromium runs the SAME
test on plain Playwright Chromium (our launch flags) as the baseline to compare against.

Every profile/browser is stopped (and Donut's throwaway profiles deleted) at the end, whatever happened.

Usage (on the server, as `storm`, with the Donut app running and DONUT_API_TOKEN set):
  .venv/bin/python -m probes.donut_probe --soax                        # stage 1
  .venv/bin/python -m probes.donut_probe --soax --count 10             # stage 2: 10, then 25, 50
  .venv/bin/python -m probes.donut_probe --soax --count 10 --backend chromium   # baseline
  .venv/bin/python -m probes.donut_probe --proxy http://user:pass@host:port      # explicit exit
The token is read from Global settings (DONUT_API_TOKEN) unless already in the environment.
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import threading
import time

from probes import proc_stats

_FP_JS = """() => ({ua: navigator.userAgent, platform: navigator.platform,
    tz: Intl.DateTimeFormat().resolvedOptions().timeZone, langs: navigator.languages,
    webdriver: navigator.webdriver, cores: navigator.hardwareConcurrency,
    screen: screen.width + 'x' + screen.height})"""
_IP_ECHO = "https://api.ipify.org?format=json"


def _load_settings() -> None:
    """Export the GLOBAL Donut settings exactly as a worker gets them (resolved + vendor_env)."""
    from apps.hub.db import SessionLocal
    from apps.hub.settings_store import get_global
    from libs.domain.enums import SettingsLayer as L
    from libs.domain.settings import resolve
    from libs.engine import vendor_env
    db = SessionLocal()
    try:
        eff = resolve({L.GLOBAL: get_global(db)})
    finally:
        db.close()
    keys = ("DONUT_API_URL", "DONUT_API_TOKEN", "DONUT_PROFILE_OS", "DONUT_HEADLESS")
    print("settings:", vendor_env.apply({k: eff[k] for k in keys if not os.environ.get(k)}))


def _soax_url() -> str:
    from sqlalchemy import select
    from apps.hub.db import SessionLocal
    from libs.db import models as M
    from libs.proxies.lease import conn_url
    db = SessionLocal()
    try:
        row = db.scalars(select(M.Proxy).where(M.Proxy.provider == "soax", M.Proxy.active.is_(True))
                         .limit(1)).first()
        if row is None:
            sys.exit("no active soax proxy row")
        return conn_url(row)
    finally:
        db.close()


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def _api_checks(donut) -> bool:
    import httpx
    base = (os.environ.get("DONUT_API_URL") or donut.DEFAULT_API_URL).rstrip("/")
    try:
        r = httpx.get(base + "/v1/openapi.json", timeout=10)
        print(f"1. Local API at {base}: HTTP {r.status_code}")
    except httpx.HTTPError as e:
        print(f"1. FAIL Local API unreachable at {base}: {e}")
        return False
    try:
        n = len(donut._items(donut._Api().call("GET", "/v1/profiles"), "profiles"))
        print(f"2. token accepted ({n} profiles)")
    except donut.DonutError as e:
        print(f"2. FAIL {e}")
        return False
    return True


def readiness(proxy: str | None, cdp_ops: int) -> int:
    import donut
    from playwright.sync_api import sync_playwright
    if not _api_checks(donut):
        return 1
    lease = None
    try:
        t0 = time.monotonic()
        lease = donut.launch(proxy, fp_seed=None)
        with sync_playwright() as pw:
            browser = donut.connect(pw, lease)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            print(f"3. profile started + attached in {time.monotonic() - t0:.1f}s ({lease.cdp_url})")
            print(f"4. fingerprint: {page.evaluate(_FP_JS)}")
            try:
                page.goto(_IP_ECHO, timeout=45_000)
                print(f"   exit: {page.inner_text('body')[:80]}")
            except Exception as e:
                print(f"   exit check failed: {e}")
            t1, errors = time.monotonic(), []
            for i in range(cdp_ops):
                try:
                    page.evaluate("1 + 1")
                except Exception as e:
                    errors.append(f"#{i}: {e}")
                    if len(errors) >= 3:
                        break
            dt = time.monotonic() - t1
            print(f"5. {cdp_ops} evaluate round-trips in {dt:.1f}s "
                  f"({cdp_ops / dt * 60:.0f}/min), errors={len(errors)}")
            for e in errors:
                print(f"   {e[:200]}")
            browser.close()
    except donut.DonutError as e:
        print(f"3. FAIL {e}")
        return 1
    finally:
        donut.release(lease)
        print("profile stopped and deleted")
    return 0


def _chromium_args() -> list[str]:
    # Our production launch flags (session.py: headless=new + WebRTC block), so the baseline is the
    # browser the fleet actually runs.
    return ["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage",
            "--no-sandbox", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--headless=new"]


def scale(n: int, base_proxy: str | None, backend: str, hold_s: float) -> int:
    from playwright.sync_api import sync_playwright
    from libs.engine.vendor.session import _parse_proxy
    from libs.proxies.lease import roll_sticky_session
    import donut
    if backend == "donut" and not _api_checks(donut):
        return 1

    results = [dict(i=i) for i in range(n)]
    up = [threading.Event() for _ in range(n)]
    release = threading.Event()

    def one(i: int) -> None:
        r = results[i]
        proxy = roll_sticky_session(base_proxy, provider="soax") if base_proxy else None
        lease = None
        t0 = time.monotonic()
        try:
            with sync_playwright() as pw:
                if backend == "donut":
                    lease = donut.laun
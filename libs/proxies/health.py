"""Real-portal proxy health probe.

The ONE way this codebase can actually verify a proxy works against pedidodevistos.mne.gov.pt: a raw
HTTP/TCP check can't, because the portal's WAF (DataDome) blocks raw fetches outright regardless of
proxy/session health (confirmed repeatedly elsewhere in this codebase -- do_discover()'s and
LiveEngineOps's own keepalive comments). A real browser nav to the portal home page is the SAME probe
contract LiveEngineOps's own keepalive already uses -- success/exception is the verdict -- reused here
rather than inventing a second, unproven check.

Design (operator-specified, 2026-08-13): screening happens ON DEMAND, at lease time -- NOT on a
periodic sweep. `lease_proxy()` (libs/proxies/lease.py) calls `check_proxy` per candidate before
handing one back, retrying the next candidate on failure. This matters most for ROTATING proxy pools:
the actual IP a provider hands out for "the next connection" can be completely different from
whatever was checked historically, so a periodic background sweep's result is stale by the time a
worker actually needs a proxy -- only a check done AT the moment of leasing proves anything.

Used by:
  - lease_proxy()'s optional `health_check` parameter (the primary, on-demand path).
  - scripts/proxy_health_check.py, a manual/on-demand whole-pool diagnostic tool (NOT scheduled --
    the pool is screened lazily as workers actually request proxies, not swept periodically).
"""
from __future__ import annotations

HOME_URL = "https://pedidodevistos.mne.gov.pt/VistosOnline/"

SCORE_STEP_UP = 10.0
SCORE_STEP_DOWN = 25.0
SCORE_MAX = 100.0
SCORE_MIN = 0.0


def check_proxy(conn_url: str, timeout: float = 20.0) -> tuple[bool, str]:
    """One throwaway headless browser, one nav to the real portal home page. Never raises -- a probe
    failure IS the result, not an error the caller has to handle. Blocking/synchronous and can take
    several seconds (browser launch + nav); callers on a shared single thread (the EventHub
    consumer, the Scheduler tick) must dispatch this to a background thread/pool rather than call it
    inline, or every other booking's event/tick handling stalls behind it."""
    from libs.engine.vendor import get_session

    session = None
    try:
        session = get_session(proxy=conn_url, headless=True)
        session.browser_nav(HOME_URL, timeout=timeout)
        return True, "ok"
    except Exception as e:
        return False, f"{e.__class__.__name__}: {e}"[:200]
    finally:
        if session is not None:
            try:
                session.close_browser()
            except Exception:
                pass


def apply_result(proxy, ok: bool, detail: str) -> None:
    """Pure mutation of one Proxy row's health bookkeeping given a probe outcome -- separated from
    check_proxy so the scoring/classification logic is unit-testable without a browser. On success,
    score climbs (capped) and is_healthy=True; on failure it drops (floored) and is_healthy=False --
    a flaky proxy trends down over repeated failures, a recovering one climbs back up, and
    lease_proxy()'s own `order_by(score.desc(), ...)` then naturally prefers the more-reliable proxy
    among healthy ones with zero further changes needed there."""
    proxy.is_healthy = ok
    # 160, matching the column (migration cc000healthdetail). At 24 the net::ERR_* code -- the
    # only part that says WHICH failure this is -- was always the part cut off.
    proxy.health = "ok" if ok else detail[:160]
    if ok:
        proxy.score = min(SCORE_MAX, proxy.score + SCORE_STEP_UP)
    else:
        proxy.score = max(SCORE_MIN, proxy.score - SCORE_STEP_DOWN)

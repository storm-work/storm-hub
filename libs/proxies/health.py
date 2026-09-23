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

# Tier-1's target, and it is deliberately NOT the portal. This file already records what probing the
# portal costs: on 2026-08-28, while the portal throttled, 4,489 webshare rows flipped to
# is_healthy=False and 73 of 120 workers never launched -- re-checked against a neutral url the
# condemned rows connected 20/20, identical to the healthy control. "The flag was recording the
# portal, not the proxy."
#
# That was survivable when the verdict only re-ordered a pool of regenerable strings. It is NOT
# survivable now: tier-1 is what the dashboard's DELETE action keys off, and an ISP proxy is a
# purchased, individually-owned IP. A portal outage must never be able to present a healthy pool as
# deletable garbage.
NEUTRAL_URL = "https://api.ipify.org/?format=json"

SCORE_STEP_UP = 10.0
SCORE_STEP_DOWN = 25.0
SCORE_MAX = 100.0
SCORE_MIN = 0.0


# Connection-level failures: the exit could not carry a request AT ALL. A browser cannot fix any of
# these, which is what makes a cheap raw probe a valid NEGATIVE filter even though DataDome makes it
# useless as a positive one.
_DEAD_MARKERS = (
    "connect", "timed out", "timeout", "connection", "tunnel", "proxy", "socks",
    "unreachable", "reset", "refused", "eof", "handshake", "ssl",
)


def quick_screen(conn_url: str, timeout: float = 6.0) -> tuple[bool, str]:
    """Cheap raw reachability check -- NO browser. `(alive, reason)`.

    This is the tier storm-auto has and storm-hub did not, and its absence is expensive. Our own
    docstring below says a raw check "can't" verify a proxy, which is half right and cost us the
    other half:

      * a raw check CANNOT prove an exit is GOOD -- DataDome 403s raw fetches regardless. True.
      * a raw check CAN prove an exit is DEAD -- if TCP/TLS will not establish through the proxy,
        no browser is going to change that.

    So this is a one-way filter: a connection-level failure is decisive and rejects the candidate;
    ANY HTTP response (200, 403, anything) means the exit is alive and the caller must fall through
    to the authoritative browser check.

    MEASURED 2026-08-23: of 228 transient login failures, ~173 were connectivity-level (timeouts,
    session-init timeouts, connection closed, tunnel failed) -- ~76% of them never needed a browser
    at all. At ~20-25s per Chromium probe against ~4s here, screening in this order is most of the
    "40% of login work is retrying dead infrastructure" figure, and it removes the fleet-launch
    browser storm that made those probes fail each other in the first place.
    """
    try:
        import primp
    except ImportError:
        return True, "primp unavailable -- falling through to the browser check"
    try:
        c = primp.Client(proxy=conn_url, verify=False, follow_redirects=True, timeout=timeout)
        r = c.get(HOME_URL)
        # Any status at all means the tunnel carried a request end to end.
        return True, f"reachable (status={r.status_code})"
    except Exception as e:
        detail = f"{e.__class__.__name__}: {e}"
        low = detail.lower()
        if any(m in low for m in _DEAD_MARKERS):
            return False, f"dead exit -- {detail[:150]}"
        # Something we do not recognise: do NOT reject on it. Failing open costs one browser probe;
        # failing closed on an unfamiliar error would quietly shrink the pool the way the 639-row
        # false-condemnation incident did.
        return True, f"unrecognised raw error, deferring to browser -- {detail[:120]}"


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


def connectivity_probe(conn_url: str, timeout: float = 8.0) -> tuple[bool, str]:
    """TIER 1 -- "is this proxy alive at all". Raw request to a NEUTRAL url. `(alive, reason)`.

    Distinct from quick_screen(), which asks the same question against the PORTAL as a pre-lease
    negative filter. The difference matters exactly here: this verdict is operator-facing and is
    what the delete action keys off, so it must describe the PROXY and nothing else. A throttling
    portal produces timeouts, "timed out" is a dead-marker, and quick_screen would therefore report
    a perfectly good ISP proxy as dead -- which is how 4,489 rows were condemned in one afternoon.

    Answers the question a raw probe can actually answer. Per this module's own rule: a raw check
    cannot prove an exit is GOOD, but it can prove one is DEAD -- if TCP/TLS will not establish
    through the proxy, no browser will change that.
    """
    try:
        import primp
    except ImportError:
        return True, "primp unavailable -- cannot run tier 1"
    try:
        c = primp.Client(proxy=conn_url, verify=False, follow_redirects=True, timeout=timeout)
        r = c.get(NEUTRAL_URL)
    except Exception as e:
        detail = f"{e.__class__.__name__}: {e}"
        low = detail.lower()
        if any(m in low for m in _DEAD_MARKERS):
            return False, f"dead: {detail[:150]}"
        # Unrecognised errors fail OPEN, same rule as quick_screen. Failing closed on an unfamiliar
        # error is what the 639-row false-condemnation incident was, and here it would additionally
        # offer a working proxy up for deletion.
        return True, f"unrecognised error, not condemning: {detail[:120]}"
    if r.status_code >= 500:
        # The neutral endpoint itself is having a bad day -- that is not a verdict on the proxy.
        return True, f"neutral endpoint returned {r.status_code}; inconclusive"
    return True, f"alive (status={r.status_code})"


def probe_tiers(conn_url: str, *, want_portal: bool = True,
                connectivity=None, portal=None) -> dict:
    """Both tiers for one proxy, in the only order that is safe.

    Returns {"tier1_ok", "tier1_detail", "tier2_ok", "tier2_detail", "ok"}; tier2_* are None when
    the portal tier did not run.

    TIER 2 IS SKIPPED WHEN TIER 1 FAILS -- not to save time (though it saves ~25s of Chromium per
    dead proxy), but because a browser probe through a dead tunnel produces a portal-shaped error
    message for a connectivity problem, which is precisely the misattribution that has repeatedly
    sent investigations here in the wrong direction.

    `ok` is TIER 1 ONLY. Tier 2 is diagnostic -- "can this IP reach the portal right now" -- and is
    deliberately not part of the liveness verdict, because the portal's availability is not a
    property of the proxy (see NEUTRAL_URL). Callers that persist health, and the delete action in
    particular, must use `ok`.
    """
    _conn = connectivity or connectivity_probe
    _portal = portal or check_proxy

    t1_ok, t1_detail = _conn(conn_url)
    if not t1_ok:
        return {"tier1_ok": False, "tier1_detail": t1_detail,
                "tier2_ok": None, "tier2_detail": None, "ok": False}
    if not want_portal:
        return {"tier1_ok": True, "tier1_detail": t1_detail,
                "tier2_ok": None, "tier2_detail": None, "ok": True}
    t2_ok, t2_detail = _portal(conn_url)
    return {"tier1_ok": True, "tier1_detail": t1_detail,
            "tier2_ok": t2_ok, "tier2_detail": t2_detail, "ok": True}


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


# ── Provider-level availability (2026-08-31) ─────────────────────────────────────────────────
# check_proxy above screens ONE ROW against the real portal. That was wrong twice over, and both
# were measured:
#
#   1. There is no row-level subject to screen. All 32,600 rows are resource_type='rotating' and
#      resolve to TWO credentials on TWO endpoints (soax 20,000 rows -> proxy.soax.com:5000,
#      webshare 9,001 -> p.webshare.io:80). A row is a credential template; the exit is chosen by a
#      session id we generate, and CONFIRMED_FACTS §17 measured those exits at 100% survival to
#      60 min and 0% at 75. Scoring a row records the quality of an exit that no longer exists.
#   2. It probed the PORTAL, so it measured portal availability. On 2026-08-28, while the portal
#      throttled (§35), 4,489 webshare rows flipped to is_healthy=False and 73 of 120 workers never
#      launched ("no proxy leased after 5 probe(s): 5 failed health"). Re-checked 2026-08-31 against
#      a NEUTRAL url: condemned rows connected 20/20 (100%), identical to the healthy control, 20
#      distinct exits. The flag was recording the portal, not the proxy.
#
# So availability is checked where a real subject exists -- the CREDENTIAL and ENDPOINT -- once per
# provider, cached, against a neutral url, on a THROWAWAY session id. Never the portal (that is what
# condemned the pool) and never the session id being leased out (tier-1 poisoning: browser-only 5/6
# vs primp-then-browser 0/6).
_PROVIDER_CACHE: dict[str, tuple[float, bool, str]] = {}
PROVIDER_CHECK_TTL = 300.0


def provider_reachable(provider: str, conn_url: str, *, ttl: float = PROVIDER_CHECK_TTL,
                       now: float | None = None) -> tuple[bool, str]:
    """Is this provider's gateway usable at all? Cached per provider for `ttl` seconds.

    Catches the failures that are genuinely per-credential -- dead account, suspended plan, wrong
    endpoint/port, auth rejection -- which are the only proxy-side failures a pre-lease check can
    honestly detect. It deliberately says nothing about any individual exit: that is unknowable
    before use and is handled after use by the worker's own _roll_exit.
    """
    import time as _t
    now = _t.time() if now is None else now
    hit = _PROVIDER_CACHE.get(provider)
    if hit and now - hit[0] < ttl:
        return hit[1], hit[2]
    from .exit_ip import observe_exit_ip
    ip = observe_exit_ip(conn_url, timeout=8.0)
    ok = ip is not None
    detail = f"exit {ip}" if ok else "no response from the provider gateway"
    _PROVIDER_CACHE[provider] = (now, ok, detail)
    return ok, detail


def reset_provider_cache() -> None:
    """Test seam / operator escape hatch -- forget every cached provider verdict."""
    _PROVIDER_CACHE.clear()

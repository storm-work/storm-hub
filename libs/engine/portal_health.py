"""Telling a SLOW portal apart from a BROKEN one, and noticing when slow becomes systemic.

During a real booking window the portal degrades: responses that normally take seconds take tens of
seconds. The engine treated that the same as a dead one -- a timeout raised, the failure classifier
called it transient infrastructure, and the recovery ladder eventually escalated to a re-login. That
is the worst possible response to slowness, because a re-login costs a CAPTCHA solve and burns the
sub-account rate limit, which is already the binding constraint. Patience was the right answer and
we spent credit instead.

Two distinctions this module exists to draw:

  slow         the request reached the portal and it is taking its time. Wait longer; never rebuild
               the session over it, and never charge it to the account.
  unreachable  the request never landed -- proxy died, tunnel dropped, chrome-error page. The
               session/proxy really is the problem and rebuilding is correct.

and separately, whether slowness has stopped being one worker's bad luck and become a property of
the window. A booking whose workers are all crawling should back off TOGETHER (the shared
RedisBreaker) rather than each independently deciding to re-login into the same wall.

Pure and dependency-free so the decision logic is unit-testable without a browser, a portal, or a
clock; the caller supplies durations and outcomes.
"""
from __future__ import annotations

from collections import deque

# Markers that mean "the request never reached the portal". A proxy that dropped the tunnel, a
# navigation that landed on Chrome's own error page: the session/proxy is genuinely at fault and
# rebuilding it is the correct response.
_UNREACHABLE_MARKERS = (
    # net::ERR_TIMED_OUT lives HERE, not in the slow set, and the order below depends on it: it
    # contains the substring "timed out" but is a Chrome NETWORK error, meaning the request never
    # reached the portal. Waiting longer cannot help; rotating the proxy can.
    "err_timed_out",
    "err_connection", "err_tunnel", "err_proxy", "err_network", "err_empty_response",
    "err_name_not_resolved", "err_socks", "err_address", "err_aborted",
    "chromewebdata", "chrome-error",
    # Solver/proxy-provider phrasings for the same thing (CapSolver returns
    # ERROR_PROXY_CONNECT_REFUSED, which matches none of the net::ERR_* forms above).
    "connect_refused", "connection refused", "connection reset", "proxy_connect",
    # IN-PAGE forms. Everything above is what Playwright reports when a NAVIGATION fails, but a
    # large part of the warm-up runs as fetch() inside the page, and a dead tunnel surfaces there as
    # a bare "TypeError: Failed to fetch" with no net::ERR_* code attached. Likewise do_apply reads
    # the submit response and recognises Chrome's own network-error interstitial, saying in its own
    # words that "the request never reached the portal".
    #
    # Both were classified "unknown" until 2026-08-22, so the recovery ladder treated a DEAD PROXY
    # as a session/portal problem. Traced live on worker SOFIA FILOMENA COSTA (Test 2032): the
    # submit hit a network-error page, then two full re-warms (34s + 41s) and a re-login and a
    # checkpoint restore all failed with "Failed to fetch" -- every one of them spent on a tunnel
    # that was already provably dead, instead of rotating the exit.
    #
    # The knowledge existed; it was in a CODE COMMENT in _run_warmup_steps ("a transient network
    # failure inside the page (dead proxy, dropped tunnel)") rather than in the classifier that
    # actually decides what to do about it.
    "failed to fetch", "network-error page", "networkerror", "err_internet_disconnected",
)

# Markers that mean "it reached the portal and the portal is being slow". Deliberately checked AFTER
# the unreachable set: `err_timed_out` is a Chrome network error and belongs to unreachable, while a
# bare "timed out" from our own queue wait means we ran out of patience, not that the portal died.
_SLOW_MARKERS = (
    "timed out", "timeout", "deadline", "504", "503", "502", "gateway",
)

# The portal's own verdict -- nothing to do with speed or reachability.
_PORTAL_MARKERS = (
    "login_failed", "auth_redirect", "unexpected_redirect", "csrf", "questionario",
)


def classify_failure(msg: str) -> str:
    """One of "unreachable" | "slow" | "portal" | "unknown".

    Order matters. `err_timed_out` contains "timed out" but is a Chrome NETWORK error -- the request
    never landed -- so the unreachable set is checked first and wins.
    """
    low = (msg or "").lower()
    if any(m in low for m in _UNREACHABLE_MARKERS):
        return "unreachable"
    if any(m in low for m in _SLOW_MARKERS):
        return "slow"
    if any(m in low for m in _PORTAL_MARKERS):
        return "portal"
    return "unknown"


class PortalHealth:
    """A rolling view of how the portal is behaving for ONE worker.

    Keeps the last `window` observations per phase so a single unlucky call can't declare a storm,
    and counts CONSECUTIVE slow failures so "the portal is slow right now" can be distinguished from
    "one request was slow an hour ago".

    `degraded_after` is deliberately more than a couple: the point is to catch a sustained window,
    and tripping a booking-wide breaker on two slow calls would do more harm than the slowness.
    """

    def __init__(self, *, window: int = 20, degraded_after: int = 4, slow_factor: float = 2.5):
        self.window = window
        self.degraded_after = degraded_after
        self.slow_factor = slow_factor
        self._samples: dict[str, deque] = {}
        self.slow_streak = 0
        self.unreachable_streak = 0

    # ── recording ─────────────────────────────────────────────────────────────
    def observe(self, phase: str, seconds: float, *, ok: bool = True) -> None:
        """Record how long a phase took. Successful durations are what a baseline is built from."""
        if ok:
            self.slow_streak = 0
            self.unreachable_streak = 0
        d = self._samples.setdefault(phase, deque(maxlen=self.window))
        d.append(float(seconds))

    def observe_failure(self, kind: str) -> None:
        """Record a classified failure (see classify_failure)."""
        if kind == "slow":
            self.slow_streak += 1
            self.unreachable_streak = 0
        elif kind == "unreachable":
            self.unreachable_streak += 1
            self.slow_streak = 0
        else:
            self.slow_streak = 0
            self.unreachable_streak = 0

    # ── reading ───────────────────────────────────────────────────────────────
    def median(self, phase: str) -> float | None:
        d = self._samples.get(phase)
        if not d:
            return None
        s = sorted(d)
        return s[len(s) // 2]

    def is_degraded(self) -> bool:
        """Has slowness become sustained enough to be the window's property, not this call's?"""
        return self.slow_streak >= self.degraded_after

    def is_slow_against(self, phase: str, baseline: float) -> bool:
        """Is this phase's recent median materially worse than the baseline it usually runs at?

        Used to set NAV_TIMEOUT_SCALE from data rather than by feel -- the operator currently picks
        4 and hopes. Needs a few samples first: a median over one observation is that observation.
        """
        med = self.median(phase)
        if med is None or baseline <= 0:
            return False
        d = self._samples.get(phase)
        if d is None or len(d) < 3:
            return False
        return med > baseline * self.slow_factor

    def snapshot(self) -> dict:
        """Per-phase medians plus the current streaks -- emitted for observability."""
        return {
            "phases": {p: round(self.median(p) or 0.0, 1) for p in self._samples},
            "slow_streak": self.slow_streak,
            "unreachable_streak": self.unreachable_streak,
            "degraded": self.is_degraded(),
        }

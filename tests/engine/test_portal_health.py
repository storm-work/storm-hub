"""Slow is not broken, and one slow call is not a slow window.

During a real booking window the portal degrades rather than dies. The engine treated the two the
same: a timeout raised, got classified as transient infrastructure, and the recovery ladder
eventually escalated to a re-login -- the worst possible response, since a re-login costs a CAPTCHA
solve and burns the sub-account rate limit that is already the binding constraint.
"""
from __future__ import annotations

import pytest

from libs.engine.portal_health import PortalHealth, classify_failure


# ── classification ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("msg", [
    "Page.goto: net::ERR_CONNECTION_CLOSED at https://pedidodevistos.mne.gov.pt/",
    "unexpected_redirect_chrome-error://chromewebdata/",
    "net::ERR_TUNNEL_CONNECTION_FAILED",
    "ERROR_PROXY_CONNECT_REFUSED",
])
def test_a_request_that_never_landed_is_unreachable(msg):
    assert classify_failure(msg) == "unreachable"


@pytest.mark.parametrize("msg", [
    "browser_nav timed out after 120s",
    "TimeoutError: Page.goto exceeded",
    "504 Gateway Timeout",
])
def test_a_request_that_landed_but_dragged_is_slow(msg):
    assert classify_failure(msg) == "slow"


def test_err_timed_out_is_unreachable_not_slow():
    """The ordering trap: net::ERR_TIMED_OUT contains "timed out" but is a Chrome NETWORK error --
    the request never reached the portal, so rebuilding the session is right and waiting is not."""
    assert classify_failure("Page.goto: net::ERR_TIMED_OUT") == "unreachable"


@pytest.mark.parametrize("msg", [
    "status=200 login_failed_auth_redirect",
    "unexpected_redirect_https://pedidodevistos.mne.gov.pt/VistosOnline/login",
    "do_warmup: csrf_missing after Formulario POST",
])
def test_the_portals_own_verdict_is_neither(msg):
    """Speed and reachability say nothing here -- the portal answered, and said no."""
    assert classify_failure(msg) == "portal"


def test_an_unrecognised_failure_is_not_silently_called_slow():
    assert classify_failure("something nobody has seen before") == "unknown"
    assert classify_failure("") == "unknown"
    assert classify_failure(None) == "unknown"


# ── sustained slowness ───────────────────────────────────────────────────────
def test_one_slow_call_is_not_a_degraded_window():
    """Tripping a booking-wide breaker on a single slow call would do more harm than the slowness."""
    h = PortalHealth(degraded_after=4)
    h.observe_failure("slow")
    assert h.is_degraded() is False


def test_sustained_slowness_is_a_property_of_the_window():
    h = PortalHealth(degraded_after=4)
    for _ in range(4):
        h.observe_failure("slow")
    assert h.is_degraded() is True


def test_a_success_clears_the_streak():
    """"Slow an hour ago" must not count toward "slow right now"."""
    h = PortalHealth(degraded_after=3)
    h.observe_failure("slow"); h.observe_failure("slow")
    h.observe("warmup", 12.0, ok=True)
    h.observe_failure("slow")
    assert h.is_degraded() is False


def test_slow_and_unreachable_streaks_do_not_pollute_each_other():
    """They call for opposite responses -- wait vs rebuild -- so mixing them would pick the wrong one."""
    h = PortalHealth(degraded_after=3)
    h.observe_failure("slow"); h.observe_failure("slow")
    h.observe_failure("unreachable")
    assert h.slow_streak == 0
    assert h.unreachable_streak == 1
    assert h.is_degraded() is False


# ── latency baseline ─────────────────────────────────────────────────────────
def test_median_needs_samples_before_it_judges():
    """A median over one observation is that observation -- not evidence of a trend."""
    h = PortalHealth()
    h.observe("warmup", 200.0)
    assert h.is_slow_against("warmup", baseline=38.0) is False


def test_a_sustained_high_median_reads_as_slow_against_baseline():
    h = PortalHealth(slow_factor=2.5)
    for _ in range(5):
        h.observe("warmup", 150.0)          # measured healthy warm-up is ~38s
    assert h.is_slow_against("warmup", baseline=38.0) is True


def test_normal_timings_are_not_slow():
    h = PortalHealth(slow_factor=2.5)
    for s in (36.0, 40.0, 38.0, 41.0, 37.0):
        h.observe("warmup", s)
    assert h.is_slow_against("warmup", baseline=38.0) is False


def test_unknown_phase_or_zero_baseline_never_claims_slow():
    h = PortalHealth()
    assert h.is_slow_against("never-seen", baseline=10.0) is False
    for _ in range(5):
        h.observe("apply", 90.0)
    assert h.is_slow_against("apply", baseline=0.0) is False


def test_the_window_bounds_what_counts_as_recent():
    h = PortalHealth(window=3, slow_factor=2.0)
    for _ in range(3):
        h.observe("apply", 300.0)           # a slow patch...
    for _ in range(3):
        h.observe("apply", 40.0)            # ...that has since recovered
    assert h.is_slow_against("apply", baseline=47.0) is False


def test_snapshot_reports_medians_and_streaks():
    h = PortalHealth(degraded_after=2)
    h.observe("warmup", 40.0); h.observe("warmup", 60.0)
    h.observe_failure("slow"); h.observe_failure("slow")
    snap = h.snapshot()
    assert snap["degraded"] is True
    assert snap["slow_streak"] == 2
    assert "warmup" in snap["phases"]


# ── in-page forms of "the request never landed" (2026-08-22) ─────────────────
@pytest.mark.parametrize("msg", [
    "browser_eval failed: Page.evaluate: TypeError: Failed to fetch",
    "home-lang POST failed: browser_eval failed: Page.evaluate: TypeError: Failed to fetch",
    "do_apply: submit response is a Chrome network-error page (187732B) -- the request never "
    "reached the portal",
])
def test_an_in_page_fetch_failure_is_unreachable_not_unknown(msg):
    """Everything in the original unreachable set was a Playwright NAVIGATION error, but much of the
    warm-up runs as fetch() inside the page, where a dead tunnel surfaces as a bare "Failed to
    fetch" with no net::ERR_* code. Both of these classified "unknown" until 2026-08-22, so the
    ladder treated a dead proxy as a session problem: worker SOFIA FILOMENA COSTA spent two full
    re-warms (34s + 41s), a re-login and a checkpoint restore on a tunnel already proven dead."""
    assert classify_failure(msg) == "unreachable"


def test_the_portals_own_rejection_is_still_not_unreachable():
    """Guard rail: the new markers must not swallow a genuine portal verdict."""
    assert classify_failure("status=200 login_failed_auth_redirect") == "portal"
    assert classify_failure("do_warmup: csrf_missing after Formulario POST") == "portal"

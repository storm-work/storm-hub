"""The opaque-origin guard.

Regression for the failure confirmed live 2026-08-25: a page left on chrome-error by a failed
navigation is an OPAQUE origin, so every subsequent in-page fetch throws "TypeError: Failed to
fetch" -- and nothing ever put the page back, so one bad navigation disabled applying (but not
login/warm-up/discovery, which each begin with a navigation and therefore self-heal) permanently.
"""
import json
import pytest

from apps.worker.ops_live import LiveEngineOps, _PORTAL_ORIGIN, BASE


class _Sess:
    def __init__(self, origins):
        self._origins = list(origins)      # consumed one per _on_portal_origin call
        self.navs = []

    def browser_eval(self, js, timeout=30):
        o = self._origins.pop(0)
        if isinstance(o, Exception):
            raise o
        href = BASE + "/Schedule.jsp" if o == _PORTAL_ORIGIN else "chrome-error://chromewebdata/"
        return json.dumps({"o": o, "h": href})

    def browser_nav(self, url, timeout=30, **_kw):
        self.navs.append(url)
        return url


def _ops(sess):
    o = object.__new__(LiveEngineOps)
    o.session = sess
    o.posto_pdf = "2032"
    o.posto_id = "2032"
    o._log = lambda *a, **k: None
    return o


def test_live_origin_is_accepted_without_navigating():
    s = _Sess([_PORTAL_ORIGIN])
    assert _ops(s)._on_portal_origin() is True
    assert s.navs == []          # the cheap path costs no portal request


@pytest.mark.parametrize("bad", ["null", RuntimeError("Execution context was destroyed")])
def test_opaque_origin_is_rejected(bad):
    # "null" is what a chrome-error page reports -- the exact condition that makes fetch throw.
    assert _ops(_Sess([bad]))._on_portal_origin() is False


def test_ensure_re_anchors_a_poisoned_page():
    s = _Sess(["null", _PORTAL_ORIGIN])          # poisoned, then healthy after the nav
    assert _ops(s)._ensure_portal_origin("the submit") is True
    assert len(s.navs) == 1 and s.navs[0].startswith(BASE + "/Schedule.jsp")


def test_ensure_reports_failure_when_the_tunnel_is_really_down():
    # Re-anchoring must not paper over a genuinely dead tunnel: still False, and the caller
    # returns "session" rather than charging the slot for it.
    s = _Sess(["null", "null"])
    assert _ops(s)._ensure_portal_origin("the submit") is False

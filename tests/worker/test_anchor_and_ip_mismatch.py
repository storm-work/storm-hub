"""Two fixes from the 2026-08-25 end-to-end trace.

1. The booking anchor has PROVENANCE and AGE. `posto_pdf` alone answers neither, and it means two
   different things depending on which of its writers set it -- the chain (fresh) or a checkpoint
   (unverified, arbitrarily old).
2. A "Por favor, confirma o reCAPTCHA" that coincides with OUR exit moving is a token/IP mismatch,
   not the account failing a challenge, and must not be charged to its two-strike budget.
"""
import pytest

from apps.worker.ops_live import LiveEngineOps


def _ops(**attrs):
    o = object.__new__(LiveEngineOps)
    o.logs = []
    o._log = o.logs.append
    o._anchor_src = None
    o._anchor_at = None
    o.posto_pdf = None
    for k, v in attrs.items():
        setattr(o, k, v)
    return o


# ── anchor provenance ────────────────────────────────────────────────────────
def test_a_chain_built_anchor_carries_an_age():
    o = _ops()
    o._set_anchor("chain")
    assert o._anchor_src == "chain"
    assert o._anchor_age() is not None and o._anchor_age() < 5
    assert "chain/" in o._anchor_desc()


def test_a_checkpoint_anchor_has_no_age_and_says_so():
    # The distinction that matters: we did NOT build this, so we cannot claim it is fresh.
    o = _ops()
    o._set_anchor("checkpoint")
    assert o._anchor_age() is None
    assert o._anchor_desc() == "checkpoint/age-unknown"


def test_clearing_the_anchor_clears_all_three():
    o = _ops(posto_pdf="2032")
    o._set_anchor("chain")
    o._clear_anchor()
    assert (o.posto_pdf, o._anchor_src, o._anchor_at) == (None, None, None)


def test_a_checkpoint_anchor_does_not_suppress_the_keepalive_rewarm():
    # REGRESSION: the guard was `if posto_pdf is None`, and a checkpoint restore SETS posto_pdf
    # without rebuilding anything -- so an unverified anchor silently skipped the re-warm.
    o = _ops(posto_pdf="2032")
    o._set_anchor("checkpoint")
    assert o._anchor_src != "chain", "must still re-warm: nothing on this session verified it"
    o._set_anchor("chain")
    assert o._anchor_src == "chain", "the chain just ran; re-warming again would be waste"


# ── the login's IP-mismatch excuse ───────────────────────────────────────────
def test_exit_ip_reports_movement_rather_than_only_logging():
    class _S:
        def __init__(self, ip): self.ip = ip; self.navs = []
        def browser_nav(self, url, *a, **k): self.navs.append(url); return url
        def browser_eval(self, *a, **k): return self.ip
    o = _ops(session=_S("1.1.1.1"), warm_proxy="", _emit=None)
    assert o._note_exit_ip("first") is False           # a baseline is not a change
    o.session.ip = "2.2.2.2"
    assert o._note_exit_ip("later") is True            # moved -> the caller can act
    assert o._note_exit_ip("again") is False           # stable -> no excuse available
    # AND IT MUST NOT NAVIGATE. It used to browser_nav() to the echo, which took the browser off
    # the portal at "warmed" -- immediately after the chain built the anchor -- so every in-page
    # fetch after it was cross-origin and threw "Failed to fetch". Confirmed live 2026-08-25.
    assert o.session.navs == [], f"the exit probe must never navigate: {o.session.navs}"


def test_an_unknown_exit_is_never_reported_as_moved():
    # Biased to False on purpose: a flaky echo must not manufacture an excuse that keeps a genuinely
    # failing account in rotation.
    class _S:
        def browser_nav(self, *a, **k): raise RuntimeError("down")
        def browser_eval(self, *a, **k): raise RuntimeError("down")
    o = _ops(session=_S(), warm_proxy="", _emit=None)
    assert o._note_exit_ip("x") is False

"""RECAPTCHA_SOLVE_PROXY must be settable per BOOKING and actually REACH the vendored engine.

This is the "delivery gap" class of bug in one test: a setting that is correct in the catalog, correct
in the booking row, and correct in the resolved config, but never arrives at the code that reads it.
The vendored engine reads this knob from os.environ ONLY -- never from EngineConfig -- so resolving it
correctly proves nothing on its own. Every hop is asserted here:

    catalog (legal at BOOKING) -> resolve() -> vendor_env.apply() -> os.environ -> session reader

Plus the precedence trap that made the booking switch inert in the first place: a TEMPLATE that pins
the same key outranks BOOKING (WORKER > TEMPLATE > BOOKING > GLOBAL).
"""
from __future__ import annotations

import os

import pytest

from libs.domain.enums import SettingsLayer as L
from libs.domain.settings.resolver import resolve, validate_layer
from libs.engine import vendor_env

KEY = "RECAPTCHA_SOLVE_PROXY"


@pytest.fixture(autouse=True)
def _clean_env():
    """The session reader latches a module-global 'already warned' flag and reads os.environ, so a
    leaked value from one test would silently decide another's outcome."""
    import libs.engine.vendor.session as S
    before = os.environ.get(KEY)
    S._RC_PROXY_WARNED = False
    yield
    os.environ.pop(KEY, None)
    if before is not None:
        os.environ[KEY] = before


def test_settable_at_booking_layer():
    assert validate_layer(L.BOOKING, {KEY: True}) == {KEY: True}


def test_still_settable_at_template_for_ab_testing():
    assert validate_layer(L.TEMPLATE, {KEY: True}) == {KEY: True}


def test_settable_at_every_operator_facing_layer():
    """There are only four layers and this key is now legal at all of them, so there is no
    negative case to assert -- pin the exact set instead, so narrowing it later is a test failure
    rather than a silently inert switch in the UI."""
    from libs.domain.settings.catalog import CATALOG
    assert CATALOG[KEY].settable_layers() == frozenset(
        {L.GLOBAL, L.BOOKING, L.TEMPLATE, L.WORKER}
    )


@pytest.mark.parametrize("want", [True, False])
def test_booking_value_reaches_the_vendored_reader(want):
    """The whole point: flipping it on the booking must change what session.py actually sees."""
    import libs.engine.vendor.session as S

    eff = resolve({L.GLOBAL: {}, L.BOOKING: {KEY: want}})
    assert eff[KEY] is want, "resolve() dropped the booking value"

    os.environ.pop(KEY, None)
    exported = vendor_env.apply(eff)
    assert KEY in exported, "vendor_env did not export the knob"
    assert os.environ[KEY] == ("1" if want else "0")
    assert S._recaptcha_solve_proxy() is want, "the engine does not see the booking's value"


def test_booking_value_survives_an_unrelated_template():
    """An armed template must not clobber the switch just by existing -- only by pinning the key."""
    tpl = {"ROLE": "apply", "STICKY_RETRY_MAX": 3}
    eff = resolve({L.GLOBAL: {}, L.BOOKING: {KEY: True}, L.TEMPLATE: tpl})
    assert eff[KEY] is True


def test_template_pin_outranks_booking():
    """Documents the precedence trap, so nobody 'fixes' the booking switch by re-pinning templates.

    This is not a bug -- it is why scripts/unpin_template_setting.py exists. If this assertion ever
    flips, the Proxy-aware templates have silently become the real control point again.
    """
    eff = resolve({L.GLOBAL: {}, L.BOOKING: {KEY: False},
                   L.TEMPLATE: {"ROLE": "apply", KEY: True}})
    assert eff[KEY] is True


def test_default_is_proxyless():
    """Measured 2026-08-20: proxied scores identically and loses ~60% of solve attempts."""
    assert resolve({L.GLOBAL: {}})[KEY] is False

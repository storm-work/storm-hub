import pytest

from libs.domain.enums import SettingsLayer as L
from libs.domain.settings import (
    CATALOG,
    UnknownSetting,
    WrongLayer,
    resolve,
    settings_for_layer,
    validate_layer,
)
from libs.domain.settings.resolver import SettingsError, effective_source


def test_catalog_every_setting_has_single_home_and_valid_overrides():
    for key, s in CATALOG.items():
        assert s.home in (L.GLOBAL, L.BOOKING, L.TEMPLATE, L.WORKER)
        # override layers must be strictly higher precedence than home, never the home itself
        assert s.home not in s.override_layers, f"{key}: home listed in override_layers"


def test_defaults_populate_effective_config():
    eff = resolve({})
    assert eff["NAV_TIMEOUT_SCALE"] == 1.0
    assert eff["SCOUT_POOL"] is True
    assert eff["DISCOVERY_MODE"] == "parallel"
    assert eff["auto_ops"] is False


def test_precedence_worker_beats_template_beats_booking_beats_global():
    # HEADLESS_MODE is a GLOBAL default, override-ok at template & worker.
    eff = resolve({
        L.GLOBAL: {"HEADLESS_MODE": "old"},
        L.TEMPLATE: {"HEADLESS_MODE": "new"},
        L.WORKER: {"HEADLESS_MODE": "headed"},
    })
    assert eff["HEADLESS_MODE"] == "headed"
    src = effective_source({
        L.GLOBAL: {"HEADLESS_MODE": "old"},
        L.TEMPLATE: {"HEADLESS_MODE": "new"},
        L.WORKER: {"HEADLESS_MODE": "headed"},
    })
    assert src["HEADLESS_MODE"] == L.WORKER


def test_template_knob_overridden_at_worker():
    eff = resolve({
        L.TEMPLATE: {"NAV_TIMEOUT_SCALE": 3.0},
        L.WORKER: {"NAV_TIMEOUT_SCALE": 5.0},
    })
    assert eff["NAV_TIMEOUT_SCALE"] == 5.0


def test_no_overlap_wrong_layer_rejected():
    # NAV_TIMEOUT_SCALE is a TEMPLATE knob; setting it at BOOKING is illegal (no overlap).
    with pytest.raises(WrongLayer):
        validate_layer(L.BOOKING, {"NAV_TIMEOUT_SCALE": 2.0})
    # SCOUT_POOL is a BOOKING knob; not settable at GLOBAL.
    with pytest.raises(WrongLayer):
        validate_layer(L.GLOBAL, {"SCOUT_POOL": True})


def test_unknown_setting_rejected():
    with pytest.raises(UnknownSetting):
        validate_layer(L.GLOBAL, {"NOPE_NOT_A_KNOB": 1})


def test_booking_knob_not_overridable_below_or_above():
    # BOOKING knobs have no override layers -> only settable at BOOKING.
    with pytest.raises(WrongLayer):
        validate_layer(L.TEMPLATE, {"no_book": True})
    with pytest.raises(WrongLayer):
        validate_layer(L.WORKER, {"SCOUT_POOL": False})


def test_type_coercion_from_strings():
    eff = resolve({
        L.BOOKING: {"no_book": "true", "CAL_NUM_DAYS": "7", "WARMUP_START_JITTER": "120"},
        L.TEMPLATE: {"KEEPALIVE_NO_RESTORE": "1", "NAV_TIMEOUT_SCALE": "4"},
    })
    assert eff["no_book"] is True
    assert eff["CAL_NUM_DAYS"] == 7 and isinstance(eff["CAL_NUM_DAYS"], int)
    assert eff["WARMUP_START_JITTER"] == 120.0
    assert eff["KEEPALIVE_NO_RESTORE"] is True
    assert eff["NAV_TIMEOUT_SCALE"] == 4.0


def test_bad_type_raises():
    with pytest.raises(SettingsError):
        validate_layer(L.TEMPLATE, {"LOGIN_FIELD_RETRIES": "not-an-int"})


def test_settings_for_layer_lists_only_home():
    booking_keys = {s.key for s in settings_for_layer(L.BOOKING)}
    assert "SCOUT_POOL" in booking_keys
    assert "NAV_TIMEOUT_SCALE" not in booking_keys  # that's a template knob

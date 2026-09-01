

# ── enumerated settings render as a dropdown and are refused on write (2026-09-01) ───────────
# DISCOVERY_SOURCE rendered as a free text box because the framework had no way to say "one of
# these three". `slot` instead of `slots` was accepted by the UI and then silently ignored at
# runtime -- the operator sets a value, sees no error, and measures the wrong path.

def test_an_enumerated_setting_rejects_a_value_outside_its_set():
    from libs.domain.settings.resolver import validate_layer, NotAChoice
    from libs.domain.enums import SettingsLayer as L
    import pytest as _pytest
    with _pytest.raises(NotAChoice) as ei:
        validate_layer(L.BOOKING, {"DISCOVERY_SOURCE": "slot"})
    msg = str(ei.value)
    assert "calendar" in msg and "slots" in msg and "both" in msg, \
        f"the error must name the allowed set: {msg}"


def test_an_enumerated_value_is_canonicalised_on_write():
    from libs.domain.settings.resolver import validate_layer
    from libs.domain.enums import SettingsLayer as L
    assert validate_layer(L.BOOKING, {"DISCOVERY_SOURCE": "  SLOTS "}) == {"DISCOVERY_SOURCE": "slots"}


def test_every_valid_choice_is_accepted():
    from libs.domain.settings.resolver import validate_layer
    from libs.domain.enums import SettingsLayer as L
    for v in ("calendar", "slots", "both"):
        assert validate_layer(L.BOOKING, {"DISCOVERY_SOURCE": v})["DISCOVERY_SOURCE"] == v


def test_free_form_settings_are_unaffected():
    """choices=() must stay the default -- this must not turn every string into an enum."""
    from libs.domain.settings.resolver import validate_layer
    from libs.domain.enums import SettingsLayer as L
    from libs.domain.settings.catalog import CATALOG
    assert CATALOG["CAL_START_DATE"].choices == ()
    assert validate_layer(L.BOOKING, {"CAL_START_DATE": "2026-09-07"})["CAL_START_DATE"] == "2026-09-07"


def test_the_catalog_endpoint_publishes_choices():
    """The dashboard renders a <select> from this field, so it has to reach the wire."""
    from libs.domain.settings.catalog import CATALOG
    assert list(CATALOG["DISCOVERY_SOURCE"].choices) == ["calendar", "slots", "both"]

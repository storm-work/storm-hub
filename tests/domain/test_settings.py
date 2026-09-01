

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


# ── catalog hygiene: a setting must have a consumer, or a reason (2026-09-01) ────────────────
# 36 of 144 settings had no read site anywhere. A setting that looks configurable and is silently
# ignored is this project's most-repeated defect -- DISCOVERY_SOURCE, QUEST_STEP_GAP,
# PARTIAL_RENDER_RETRIES and nav_retries were each found that way, after runs were measured under
# a config nobody was reading.

_ALLOWED_UNREAD = {
    # Stored in the DB. Deleting the catalog entry makes resolve() reject the stored value and
    # breaks EVERY worker launch, not just the feature -- the whole GLOBAL layer is validated.
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    # Deliberately kept and labelled NOT IMPLEMENTED (cb87f37) so the dashboard says so out loud
    # rather than the setting quietly vanishing.
    "auto_ops", "persist_events",
}


def _read_sites(key: str) -> list[str]:
    import subprocess
    blob = subprocess.run(["grep", "-rn", "--include=*.py", key, "apps", "libs", "scripts"],
                          capture_output=True, text=True).stdout
    return [ln.split(":", 1)[0] for ln in blob.splitlines()
            if not ln.startswith("libs/domain/settings/")
            and not ln.startswith("libs/engine/vendor_env.py")
            and not ln.startswith("tests/")]


def test_every_setting_has_a_consumer_or_an_explicit_exemption():
    from libs.domain.settings.catalog import CATALOG
    orphans = sorted(k for k in CATALOG if k not in _ALLOWED_UNREAD and not _read_sites(k))
    assert not orphans, (
        "these settings are rendered to the operator and read by nothing -- wire them, delete "
        f"them, or add them to _ALLOWED_UNREAD with the reason: {orphans}")


def test_the_exemptions_are_still_justified():
    """Stop the exemption list becoming a dumping ground: if one grows a consumer, drop it."""
    from libs.domain.settings.catalog import CATALOG
    assert _ALLOWED_UNREAD <= set(CATALOG), "an exemption names a setting that no longer exists"
    now_read = sorted(k for k in _ALLOWED_UNREAD if _read_sites(k))
    assert not now_read, f"these are consumed now -- remove them from _ALLOWED_UNREAD: {now_read}"

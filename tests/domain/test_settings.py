import pytest



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


# ── proxy settings are template-overridable (2026-09-09) ─────────────────────────────────────
# A WorkerTemplate can now carry PROXY_PROVIDER/PROXY_KIND/PROXY_GEOS/HOLD_PROXY_PROVIDER/
# HOLD_PROXY_KIND/HOLD_PROXY_GEOS, so different templates attached within the same booking can
# draw from different proxy pools instead of needing a separate booking per pool. Previously
# these were override=_WRK (worker-only) -- a template settings_json carrying any of them was
# rejected outright by validate_layer.

def test_proxy_settings_are_now_settable_at_the_template_layer():
    from libs.domain.settings.resolver import validate_layer
    from libs.domain.enums import SettingsLayer as L
    for key, value in (("PROXY_PROVIDER", "webshare"), ("PROXY_KIND", "rotating"),
                       ("PROXY_GEOS", "DE"), ("HOLD_PROXY_PROVIDER", "soax"),
                       ("HOLD_PROXY_KIND", "rotating"), ("HOLD_PROXY_GEOS", "DE")):
        assert validate_layer(L.TEMPLATE, {key: value}) == {key: value}, key


def test_proxy_settings_still_settable_at_booking_and_worker_layers():
    """Widening the override set must not narrow it -- BOOKING (home) and WORKER (pre-existing
    override) must keep working exactly as before."""
    from libs.domain.settings.resolver import validate_layer
    from libs.domain.enums import SettingsLayer as L
    for key in ("PROXY_PROVIDER", "PROXY_KIND", "PROXY_GEOS", "HOLD_PROXY_PROVIDER",
               "HOLD_PROXY_KIND", "HOLD_PROXY_GEOS"):
        assert validate_layer(L.BOOKING, {key: "soax" if "PROVIDER" in key else "x"})
        assert validate_layer(L.WORKER, {key: "soax" if "PROVIDER" in key else "x"})


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


# ── every setting carries operator guidance (2026-09-01) ─────────────────────────────────────
# 62 of 112 had no note at all, so the dashboard rendered a bare box with a key name and nothing
# about what the value does. The tooltip composes the mechanical facts (type, default, owning
# layer, legal values) from metadata, so this only has to guarantee the half metadata cannot
# supply: what it DOES and what to pick.

def test_every_setting_has_a_note():
    from libs.domain.settings.catalog import CATALOG
    bare = sorted(k for k, s in CATALOG.items() if not (s.note or "").strip())
    assert not bare, f"these render to the operator with no guidance at all: {bare}"


def test_notes_are_substantive_not_a_restatement_of_the_key():
    """A note that just spells the key back is worse than none -- it looks documented and isn't."""
    from libs.domain.settings.catalog import CATALOG
    weak = []
    for k, s in CATALOG.items():
        note = (s.note or "").strip()
        if len(note) < 25 or note.strip(". ").lower() == k.replace("_", " ").lower():
            weak.append(k)
    assert not weak, f"these notes say nothing an operator could act on: {weak}"


# NOT TESTED, deliberately: an earlier version of this file tried to catch the space-loss bug from
# wrapping notes into adjacent string literals ("rationale" + "measured" -> "rationalemeasured") by
# regexing for a lowercase->uppercase transition. In a codebase whose notes legitimately name
# getPeriodosOcupados, onCaptchaSuccess, getSlots, EngineConfig, LiveEngineOps, RedisBreaker and
# SubmeterVistoCriaPDF, that heuristic is mostly false positives, and padding an allow-list until it
# passes would make it look like protection while testing nothing. All 71 joins were repaired and
# read back; test_notes_are_substantive is the check that carries real weight here.


# ── retired settings (production outage 2026-09-16) ──────────────────────────────────────────
def test_a_retired_setting_is_ignored_not_raised():
    """REPRODUCES THE OUTAGE. The six rotating-credential keys were removed from the catalog when
    credentials moved to the environment (D40) -- but a backfill had already written them into the
    live `settings` table. resolve() then raised UnknownSetting on EVERY call, and resolve() is on
    the scheduler's per-tick path: the scheduler crash-looped for ~2h45m, booking "Test 0916" never
    left SCHEDULED, and no worker on the host could launch.

    One stale row must never be able to do that. A leftover value for a deliberately-retired key is
    skipped; anything genuinely unknown still raises, because that is a real misconfiguration."""
    from libs.domain.settings import validate_layer
    from libs.domain.settings.catalog import RETIRED_SETTINGS
    from libs.domain.settings.resolver import UnknownSetting
    from libs.domain.enums import SettingsLayer as L

    stored = {k: "leftover" for k in RETIRED_SETTINGS}
    stored["CAPSOLVER_KEYS"] = "still-real"

    out = validate_layer(L.GLOBAL, stored)
    assert out == {"CAPSOLVER_KEYS": "still-real"}, "a retired key must be dropped, not carried"

    with pytest.raises(UnknownSetting):
        validate_layer(L.GLOBAL, {"TOTALLY_MADE_UP_KEY": "x"})


def test_resolve_survives_a_settings_table_full_of_retired_keys():
    """The end-to-end shape of the outage: resolve() is what the scheduler calls every tick, so it
    is the function that must not raise."""
    from libs.domain.settings import resolve
    from libs.domain.settings.catalog import RETIRED_SETTINGS
    from libs.domain.enums import SettingsLayer as L

    eff = resolve({L.GLOBAL: {k: "leftover" for k in RETIRED_SETTINGS},
                   L.BOOKING: {"CAL_NUM_DAYS": 20}})
    assert eff["CAL_NUM_DAYS"] == 20
    for k in RETIRED_SETTINGS:
        assert k not in eff


def test_no_retired_key_is_also_a_live_catalog_entry():
    """A key cannot be both retired and current -- that would silently drop a setting that something
    still reads, which is a worse failure than the one this mechanism fixes."""
    from libs.domain.settings.catalog import CATALOG, RETIRED_SETTINGS

    assert not (set(CATALOG) & RETIRED_SETTINGS)

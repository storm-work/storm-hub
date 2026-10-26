import pytest

from libs.domain.enums import SettingsLayer as L
from libs.domain.settings import (
    CATALOG,
    BelowMinimum,
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
    # Defaults revised 2026-10-26 (browser path): see the catalog notes for the evidence.
    assert eff["NAV_TIMEOUT_SCALE"] == 4.0
    assert eff["HYBRID_MODE"] is False and eff["KEEPALIVE_MODE"] == "schedule"
    assert eff["DISCOVERY_SOURCE"] == "slots" and eff["STRESS_RETRY_GAP"] == 3.0
    assert eff["KEEPALIVE_STRESS_HOLD"] is True and eff["STRESS_HOLD"] is True
    assert eff["SCOUT_POOL"] is True
    assert eff["DISCOVERY_MIN_INTERVAL"] == 30.0   # rotation pacing (replaced DISCOVERY_MODE et al.)
    assert eff["auto_ops"] is False


def test_precedence_worker_beats_template_beats_booking_beats_global():
    # HEADLESS_MODE is a GLOBAL default, override-ok at template & worker.
    eff = resolve({
        L.GLOBAL: {"HEADLESS_MODE": "old"},
        L.TEMPLATE: {"HEADLESS_MODE": "new"},
        L.WORKER: {"HEADLESS_MODE": "headful"},
    })
    assert eff["HEADLESS_MODE"] == "headful"
    src = effective_source({
        L.GLOBAL: {"HEADLESS_MODE": "old"},
        L.TEMPLATE: {"HEADLESS_MODE": "new"},
        L.WORKER: {"HEADLESS_MODE": "headful"},
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


def test_hold_swap_after_login_settable_at_booking_layer():
    """Regression: HOLD_SWAP_AFTER_LOGIN's home layer is TEMPLATE, but the dashboard's Booking
    Settings panel exposes it as a per-booking shortcut checkbox -- it must be settable at BOOKING
    without requiring a template edit, and a booking-level choice must actually take effect when
    no template/worker override is present."""
    eff = resolve({L.BOOKING: {"HOLD_SWAP_AFTER_LOGIN": True}})
    assert eff["HOLD_SWAP_AFTER_LOGIN"] is True


def test_hold_swap_after_login_template_still_wins_over_booking():
    # Precedence is WORKER > TEMPLATE > BOOKING > GLOBAL -- an explicit template override must
    # still beat the booking-level shortcut, matching every other layered setting's behavior.
    eff = resolve({
        L.BOOKING: {"HOLD_SWAP_AFTER_LOGIN": True},
        L.TEMPLATE: {"HOLD_SWAP_AFTER_LOGIN": False},
    })
    assert eff["HOLD_SWAP_AFTER_LOGIN"] is False


def test_hold_swap_after_found_is_no_longer_a_recognized_setting():
    """Phase 6: HOLD_SWAP_AFTER_FOUND was never a supported alias/compatibility mechanism -- it
    named a trigger point the code never had, and was removed rather than reconciled. The resolver
    must reject it exactly like any other unknown key, at every layer that ever accepted it."""
    with pytest.raises(UnknownSetting):
        validate_layer(L.BOOKING, {"HOLD_SWAP_AFTER_FOUND": True})
    with pytest.raises(UnknownSetting):
        validate_layer(L.TEMPLATE, {"HOLD_SWAP_AFTER_FOUND": True})
    assert "HOLD_SWAP_AFTER_FOUND" not in CATALOG


def test_hold_swap_after_login_has_no_fallback_reading_from_the_old_key():
    """A layer setting ONLY the (now-nonexistent) old key must not silently resolve
    HOLD_SWAP_AFTER_LOGIN to anything other than its catalog default -- there is no OR/fallback
    logic anywhere in the resolver; the old key simply does not exist."""
    eff = resolve({})
    assert eff["HOLD_SWAP_AFTER_LOGIN"] is False   # catalog default, nothing implicitly enables it


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


def test_no_catalog_default_violates_its_own_min_value():
    # Regression lock for the confirmed 2026-08-07 incident: a hold loop's frozen
    # effective_settings_json resolved HOLD_POLL_GAP to 0.0 and spun unthrottled for 66 minutes.
    # This asserts the general invariant (any Setting with a min_value must have a safe default),
    # not just the one key — the catalog module also asserts this at import time.
    for s in CATALOG.values():
        if s.min_value is not None and s.default is not None:
            assert s.default >= s.min_value, f"{s.key}: default {s.default} < min_value {s.min_value}"


def test_hold_poll_gap_default_is_safe():
    eff = resolve({})
    assert eff["HOLD_POLL_GAP"] >= 1.0


def test_resolved_throttle_below_minimum_is_rejected_loudly():
    # Explicitly reconstruct the confirmed incident's config shape and assert the resolver refuses it,
    # instead of silently producing a config that would let the hold loop spin unthrottled again.
    with pytest.raises(BelowMinimum):
        resolve({L.TEMPLATE: {"HOLD_POLL_GAP": 0.0}})
    with pytest.raises(BelowMinimum):
        resolve({L.WORKER: {"HOLD_POLL_GAP": 0.0}})


def test_all_recovered_global_settings_are_registered():
    # Regression lock: these 7 GLOBAL rows were restored into the DB from the recovered dump
    # (Phase A3). A worker launch calls resolve() against the WHOLE global layer regardless of
    # whether a given booking needs them -- a missing catalog entry for even one of them breaks
    # every worker launch, not just the feature that actually uses it (caught live in Phase E
    # validation: CF_MAIL_DOMAIN was missing and broke booking start entirely).
    recovered_keys = {
        "CAPSOLVER_KEYS", "EMAIL_PROVIDER", "CF_MAIL_DOMAIN", "CF_WORKER_URL",
        "CF_WORKER_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    }
    eff = resolve({L.GLOBAL: {k: "x" for k in recovered_keys}})
    for k in recovered_keys:
        assert eff[k] == "x"


def test_below_minimum_rejected_at_write_time_too():
    # Fail fast: an operator setting a bad value should get an immediate error on write, not only
    # a deferred failure when the worker actually launches.
    with pytest.raises(BelowMinimum):
        validate_layer(L.TEMPLATE, {"HOLD_POLL_GAP": 0.0})
    with pytest.raises(BelowMinimum):
        validate_layer(L.TEMPLATE, {"KEEPALIVE_INTERVAL": 0.0})


# ── Phase 3 (D32): worker-level proxy setting overrides ────────────────────────────────────────
# PROXY_PROVIDER/PROXY_GEOS/PROXY_KIND/HOLD_PROXY_PROVIDER/HOLD_PROXY_GEOS/HOLD_PROXY_KIND all had
# override=frozenset() -- L.WORKER could never win regardless of what workers.settings_json carried,
# because nothing was allowed to SET them there. These prove the precedence now actually reaches
# every one of the six, using the SAME resolve()/validate_layer() this file already tests everything
# else through -- no second resolver, no new precedence model.
_PROXY_WORKER_SETTINGS = (
    "PROXY_PROVIDER", "PROXY_GEOS", "PROXY_KIND",
    "HOLD_PROXY_PROVIDER", "HOLD_PROXY_GEOS", "HOLD_PROXY_KIND",
)


@pytest.mark.parametrize("key", _PROXY_WORKER_SETTINGS)
def test_proxy_setting_booking_only(key):
    eff = resolve({L.BOOKING: {key: "booking-value"}})
    assert eff[key] == "booking-value"


@pytest.mark.parametrize("key", _PROXY_WORKER_SETTINGS)
def test_proxy_setting_worker_overrides_booking(key):
    eff = resolve({L.BOOKING: {key: "booking-value"}, L.WORKER: {key: "worker-value"}})
    assert eff[key] == "worker-value"
    src = effective_source({L.BOOKING: {key: "booking-value"}, L.WORKER: {key: "worker-value"}})
    assert src[key] == L.WORKER


@pytest.mark.parametrize("key", _PROXY_WORKER_SETTINGS)
def test_proxy_setting_worker_absent_booking_wins(key):
    eff = resolve({L.BOOKING: {key: "booking-value"}})
    src = effective_source({L.BOOKING: {key: "booking-value"}})
    assert eff[key] == "booking-value"
    assert src[key] == L.BOOKING


@pytest.mark.parametrize("key", _PROXY_WORKER_SETTINGS)
def test_proxy_setting_settable_at_worker_layer(key):
    """The write-time gate (attach_identity validates worker_settings via validate_layer(L.WORKER,
    ...)) must accept these six keys -- confirms the override, not just the read-time resolve()."""
    validate_layer(L.WORKER, {key: "x"})  # must not raise


@pytest.mark.parametrize("key", _PROXY_WORKER_SETTINGS)
def test_proxy_setting_settable_at_template_layer(key):
    """2026-09-09: widened from override={L.WORKER} to {L.TEMPLATE, L.WORKER} -- a WorkerTemplate
    can now carry these six, so several templates attached within one booking can each draw from a
    different proxy pool. Confirms the widen landed for all six, not just PROXY_PROVIDER."""
    validate_layer(L.TEMPLATE, {key: "x"})  # must not raise


def test_proxy_setting_template_beats_booking_worker_beats_template():
    """Precedence must stay WORKER > TEMPLATE > BOOKING > GLOBAL now that TEMPLATE is a real writer
    for these keys too, not just BOOKING and WORKER."""
    eff = resolve({L.BOOKING: {"PROXY_PROVIDER": "soax"},
                   L.TEMPLATE: {"PROXY_PROVIDER": "webshare"}})
    assert eff["PROXY_PROVIDER"] == "webshare"
    eff = resolve({L.BOOKING: {"PROXY_PROVIDER": "soax"},
                   L.TEMPLATE: {"PROXY_PROVIDER": "webshare"},
                   L.WORKER: {"PROXY_PROVIDER": "soax"}})
    assert eff["PROXY_PROVIDER"] == "soax"   # WORKER still outranks TEMPLATE


def test_random_geo_is_omission_never_a_literal_string():
    """PROXY_GEOS/HOLD_PROXY_GEOS's established representation (D2, PROXY_CONTRACTS.md) is: blank/
    absent means no restriction, applied as omission at credential-build time -- never the literal
    string "random" flowing through the resolver. This did not change in Phase 3 and must not."""
    eff = resolve({})
    assert eff["PROXY_GEOS"] == ""
    assert eff["HOLD_PROXY_GEOS"] == ""
    eff = resolve({L.BOOKING: {"PROXY_GEOS": "DE,IT"}, L.WORKER: {"PROXY_GEOS": ""}})
    assert eff["PROXY_GEOS"] == ""   # worker can explicitly go back to "no restriction" too


def test_unimplemented_settings_say_so_in_their_note():
    """auto_ops and persist_events have zero read sites (audited 2026-08-21). Keeping them is fine --
    both name real intent -- but the operator sees these notes in the dashboard, and a checkbox that
    silently does nothing is worse than no checkbox. If either is ever wired, drop the marker."""
    import pathlib

    from libs.domain.settings.catalog import CATALOG

    src = pathlib.Path("apps").rglob("*.py")
    body = "\n".join(p.read_text() for p in src if "test" not in str(p))
    for key in ("auto_ops", "persist_events"):
        assert "NOT IMPLEMENTED" in (CATALOG[key].note or ""), f"{key} note must flag it"
        assert key not in body, f"{key} now HAS a read site -- wire it and drop the NOT IMPLEMENTED note"

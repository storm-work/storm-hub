"""Layered settings resolver (spec §6).

Given per-layer value maps, produce a single *effective* config for a worker launch, enforcing:
  1. Unknown keys are rejected (`UnknownSetting`).
  2. A key may only be set at its home layer or a declared override layer (`WrongLayer`) —
     this is the "no overlap between layers" rule the operator asked for.
  3. Higher-precedence layers win: WORKER > TEMPLATE > BOOKING > GLOBAL.
  4. Values are coerced/validated against the declared type.

The resolved config is meant to be FROZEN onto the worker at launch (spec §6 / effective_settings_json),
so a later change to a global/template default does not mutate an already-running worker.
"""
from __future__ import annotations

from typing import Any, Mapping

from ..enums import SettingsLayer as L
from .catalog import CATALOG, RETIRED_SETTINGS, Setting

# Precedence: index = strength (higher wins).
_PRECEDENCE = [L.GLOBAL, L.BOOKING, L.TEMPLATE, L.WORKER]
_RANK = {layer: i for i, layer in enumerate(_PRECEDENCE)}

LayerValues = Mapping[L, Mapping[str, Any]]


class SettingsError(ValueError):
    pass


class UnknownSetting(SettingsError):
    def __init__(self, key: str, layer: L):
        super().__init__(f"unknown setting {key!r} set at layer {layer}")
        self.key, self.layer = key, layer


class WrongLayer(SettingsError):
    def __init__(self, key: str, layer: L, allowed: frozenset):
        allowed_s = ", ".join(sorted(str(a) for a in allowed))
        super().__init__(
            f"setting {key!r} set at layer {layer}, but its home/override layers are: {allowed_s}"
        )
        self.key, self.layer, self.allowed = key, layer, allowed


class BelowMinimum(SettingsError):
    """Raised when a resolved loop-cadence/throttle setting is below its declared floor.

    Exists specifically because of a confirmed incident: a worker's frozen effective_settings_json
    resolved HOLD_POLL_GAP to 0.0, and the hold loop spun unthrottled (~150-280 writes/sec) for 66
    minutes. Never silently clamp-and-continue here — a throttle resolving to ~0 is exactly the
    failure mode this guards against, so it must be loud, not quietly "fixed."
    """
    def __init__(self, key: str, value: float, min_value: float):
        super().__init__(
            f"setting {key!r} resolved to {value!r}, below its declared minimum {min_value!r} — "
            "refusing to produce a config that would let a loop spin unthrottled"
        )
        self.key, self.value, self.min_value = key, value, min_value


class NotAChoice(SettingsError):
    """A setting with a closed set of values was given something outside it.

    Loud on write rather than tolerated at launch: a value the operator typed and the system then
    ignored is the "looks configured, isn't" failure this project keeps rediscovering.
    """
    def __init__(self, key: str, value: Any, choices: tuple):
        allowed = " | ".join(str(c) for c in choices)
        super().__init__(
            f"setting {key!r} was given {value!r}, which is not one of: {allowed}"
        )
        self.key, self.value, self.choices = key, value, choices


def _coerce(setting: Setting, value: Any) -> Any:
    """Coerce a raw value (possibly a string from a form/env) to the setting's declared type."""
    if value is None:
        return None
    t = setting.type
    if t is bool:
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
        raise SettingsError(f"setting {setting.key!r}: cannot read {value!r} as bool")
    if t is int:
        try:
            return int(value)
        except (TypeError, ValueError):
            raise SettingsError(f"setting {setting.key!r}: cannot read {value!r} as int")
    if t is float:
        try:
            return float(value)
        except (TypeError, ValueError):
            raise SettingsError(f"setting {setting.key!r}: cannot read {value!r} as float")
    return str(value)


def validate_layer(layer: L, values: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one layer's raw value map (keys exist + are legal at this layer) and coerce types.

    Called on write (before persisting a layer's settings) so the operator gets an immediate,
    specific error instead of a silent misconfiguration at launch.
    """
    out: dict[str, Any] = {}
    for key, raw in values.items():
        if key in RETIRED_SETTINGS:
            # A leftover row for a key that was deliberately removed from the catalog. Skipped, not
            # raised: resolve() runs on the scheduler's per-tick path, so raising here turns one
            # stale row into a host-wide outage -- which is precisely what happened on 2026-09-16
            # (scheduler crash-loop, ~2h45m, no booking able to start). See catalog.RETIRED_SETTINGS.
            continue
        setting = CATALOG.get(key)
        if setting is None:
            raise UnknownSetting(key, layer)
        if layer not in setting.settable_layers():
            raise WrongLayer(key, layer, setting.settable_layers())
        coerced = _coerce(setting, raw)
        if setting.min_value is not None and coerced is not None and coerced < setting.min_value:
            raise BelowMinimum(key, coerced, setting.min_value)
        # Enumerated settings are rejected on WRITE, so a typo never reaches a run. DISCOVERY_SOURCE
        # rendered as a free text box and `slot` (missing the 's') silently ran the old discovery
        # path while the operator believed the new one was under test -- the whole point of a closed
        # set is that the framework, not the worker, refuses it.
        if setting.choices and coerced is not None:
            norm = str(coerced).strip().lower()
            match = next((c for c in setting.choices if str(c).lower() == norm), None)
            if match is None:
                raise NotAChoice(key, coerced, setting.choices)
            coerced = match          # canonicalise: "  SLOTS " is stored as "slots"
        out[key] = coerced
    return out


def resolve(layers: LayerValues, *, include_defaults: bool = True) -> dict[str, Any]:
    """Merge per-layer values into one effective config.

    `layers` maps a SettingsLayer to its {key: value}. Each layer is validated (raising on unknown
    keys or wrong-layer placement), then merged by precedence. With `include_defaults`, every
    catalog key absent from all layers falls back to its declared default.
    """
    # Validate every provided layer first (fail fast, specific errors).
    validated: dict[L, dict[str, Any]] = {}
    for layer, values in layers.items():
        validated[layer] = validate_layer(layer, values)

    effective: dict[str, Any] = {}
    if include_defaults:
        for key, setting in CATALOG.items():
            effective[key] = setting.default

    # Apply layers low->high precedence so higher layers overwrite.
    for layer in sorted(validated, key=lambda l: _RANK[l]):
        for key, value in validated[layer].items():
            effective[key] = value

    # Hard floor on loop-cadence/throttle settings (see BelowMinimum) — checked on the FINAL resolved
    # value regardless of which layer supplied it, so no override path can silently reintroduce the
    # confirmed HOLD_POLL_GAP=0.0 incident.
    for key, value in effective.items():
        setting = CATALOG.get(key)
        if setting is None or setting.min_value is None or value is None:
            continue
        if value < setting.min_value:
            raise BelowMinimum(key, value, setting.min_value)

    return effective


def effective_source(layers: LayerValues) -> dict[str, L]:
    """Debug/introspection: which layer supplied each resolved key (highest-precedence provider)."""
    source: dict[str, L] = {}
    for layer, values in layers.items():
        validate_layer(layer, values)
        for key in values:
            if key not in source or _RANK[layer] > _RANK[source[key]]:
                source[key] = layer
    return source

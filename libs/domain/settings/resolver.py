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
from .catalog import CATALOG, Setting

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
        setting = CATALOG.get(key)
        if setting is None:
            raise UnknownSetting(key, layer)
        if layer not in setting.settable_layers():
            raise WrongLayer(key, layer, setting.settable_layers())
        out[key] = _coerce(setting, raw)
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

"""Persisted layered settings (the `settings` table) — read/write helpers.

Global settings (CAPSOLVER_KEYS, browser/solver defaults, HOST_SESSION_CAP…) are stored here so the
operator sets them once via the dashboard instead of env files. Values are validated against the
catalog on write (unknown/wrong-layer keys rejected — the no-overlap rule), stored as strings, and
re-coerced by the resolver on read.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from libs.db import models as M
from libs.domain.enums import SettingsLayer as L
from libs.domain.settings import validate_layer


def get_layer(db: Session, layer: L, scope_key: str = "") -> dict[str, str]:
    rows = db.scalars(
        select(M.SettingRow).where(M.SettingRow.layer == layer, M.SettingRow.scope_key == scope_key)
    ).all()
    return {r.key: r.value for r in rows}


def set_layer(db: Session, layer: L, values: dict, scope_key: str = "") -> dict[str, str]:
    """Validate `values` against the catalog for `layer`, then upsert. Returns the stored raw map."""
    validate_layer(layer, values)   # raises on unknown / wrong-layer keys
    existing = {
        r.key: r
        for r in db.scalars(
            select(M.SettingRow).where(M.SettingRow.layer == layer, M.SettingRow.scope_key == scope_key)
        ).all()
    }
    for key, val in values.items():
        sval = "" if val is None else str(val)
        if key in existing:
            existing[key].value = sval
        else:
            db.add(M.SettingRow(layer=layer, scope_key=scope_key, key=key, value=sval))
    db.commit()
    return get_layer(db, layer, scope_key)


def get_global(db: Session) -> dict[str, str]:
    return get_layer(db, L.GLOBAL)

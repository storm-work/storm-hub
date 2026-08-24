"""Editable config store (form_defaults, quest_steps) + the dynamic-date resolver.

Configs are seeded from config/*.json on first run, then edited via the dashboard. The engine reads
them at apply/register time. `resolve_dynamic` (re-exported here for backward compatibility; the real
definition lives in libs/engine/payload.py since libs/ must not depend on apps/) ports storm-auto's
date-expression resolver so the frequently-updated travel dates (f30/f31 literal, f16/f17 today±yr)
behave identically.
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from libs.db import models as M
from libs.engine.payload import resolve_dynamic  # noqa: F401 (re-exported for existing call sites)

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
SEED_KEYS = ("form_defaults", "quest_steps")


def get_config(db: Session, key: str, default=None):
    row = db.scalar(select(M.AppConfig).where(M.AppConfig.key == key))
    return row.value if row else default


def set_config(db: Session, key: str, value: dict) -> dict:
    row = db.scalar(select(M.AppConfig).where(M.AppConfig.key == key))
    if row:
        row.value = value
    else:
        db.add(M.AppConfig(key=key, value=value))
    db.commit()
    return value


def seed_templates(db: Session) -> list[str]:
    """Create the two default worker templates (scout, apply) if none exist."""
    from libs.domain.enums import WorkerRole
    if db.scalar(select(M.WorkerTemplate)):
        return []
    scout = M.WorkerTemplate(
        name="Default Scout", role=WorkerRole.SCOUT,
        settings_json={"ROLE": "scout", "CAPTURE_ONLY": True, "KEEPALIVE_NO_RESTORE": True,
                       "NAV_TIMEOUT_SCALE": 4, "LOGIN_FIELD_RETRIES": 4},
    )
    apply = M.WorkerTemplate(
        name="Default Apply", role=WorkerRole.APPLY,
        settings_json={"ROLE": "apply", "NAV_TIMEOUT_SCALE": 4, "LOGIN_FIELD_RETRIES": 4,
                       # No STICKY_RETRY_MAX here: the catalog default is 2, and pinning 3 in the
                       # template silently overrode it. Reduced to 2 (operator, 2026-08-21) because
                       # every failed login attempt spends a CAPTCHA the portal counts against us --
                       # it warns it blocks access after repeated reCAPTCHA failures, and the
                       # 2026-08-21 07:29 run watched captcha rejections turn into outright
                       # "Nao foi possivel ... tente mais tarde" throttling within six minutes.
                       "KEEPALIVE_NO_RESTORE": True},
    )
    db.add_all([scout, apply])
    db.commit()
    return ["Default Scout", "Default Apply"]


def seed_defaults(db: Session) -> list[str]:
    """Insert form_defaults/quest_steps from config/*.json if the key is absent. Idempotent."""
    seeded = []
    for key in SEED_KEYS:
        if db.scalar(select(M.AppConfig).where(M.AppConfig.key == key)):
            continue
        path = _CONFIG_DIR / f"{key}.json"
        if path.exists():
            db.add(M.AppConfig(key=key, value=json.loads(path.read_text(encoding="utf-8"))))
            seeded.append(key)
    if seeded:
        db.commit()
    return seeded

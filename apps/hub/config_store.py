"""Editable config store (form_defaults, quest_steps) + the dynamic-date resolver.

Configs are seeded from config/*.json on first run, then edited via the dashboard. The engine reads
them at apply/register time. `resolve_dynamic` ports storm-auto's date-expression resolver so the
frequently-updated travel dates (f30/f31 literal, f16/f17 today±yr) behave identically.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from libs.db import models as M

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
                       "KEEPALIVE_NO_RESTORE": True, "STICKY_RETRY_MAX": 3},
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


# ── dynamic-date resolver (ported from storm-auto batch_apply._resolve_dynamic) ──
def resolve_dynamic(expr: str, *, today: date | None = None) -> str:
    """Resolve a form_defaults dynamic value.
    - literal `YYYY/MM/DD` or `YYYY-MM-DD` -> normalized `YYYY/MM/DD` (pins a fixed travel date)
    - `today±Nd` / `today±Nyr` -> rolling offset in the portal's `YYYY/MM/DD` format
    - anything else -> "" """
    expr = (expr or "").strip()
    m_lit = re.fullmatch(r"(\d{4})[/-](\d{2})[/-](\d{2})", expr)
    if m_lit:
        return f"{m_lit.group(1)}/{m_lit.group(2)}/{m_lit.group(3)}"
    today = today or date.today()
    m = re.fullmatch(r"today([+-])(\d+)(d|yr)", expr)
    if not m:
        return ""
    sign, n, unit = m.group(1), int(m.group(2)), m.group(3)
    delta = timedelta(days=n) if unit == "d" else timedelta(days=n * 365)
    result = today + delta if sign == "+" else today - delta
    return result.strftime("%Y/%m/%d")

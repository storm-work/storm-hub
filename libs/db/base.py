"""SQLAlchemy declarative base + shared mixins.

Types are kept portable (String UUIDs, generic JSON, tz-aware DateTime) so `metadata.create_all`
runs on SQLite for a fast offline smoke test, while the real deploy targets Postgres via Alembic
(where JSON becomes JSONB in the migration). The domain enums (libs.domain.enums) are the single
source of truth for every status column.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class PKMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

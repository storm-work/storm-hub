"""Persistence layer — SQLAlchemy models + engine wiring (Postgres in prod, SQLite for smoke tests).

Kept separate from `libs.domain` (which stays framework-free) so the domain rules/state-machines have
zero ORM dependency. `models` mirrors spec §3 exactly; the domain enums drive every status column.
"""
from __future__ import annotations

from .base import Base
from .models import metadata

__all__ = ["Base", "metadata"]

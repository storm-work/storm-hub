"""Alembic environment for storm-hub.

Target metadata is the SQLAlchemy models in libs.db (spec §3). The DB URL comes from the
DATABASE_URL env var (Postgres in prod). For bootstrapping the very first migration offline,
a throwaway SQLite URL is used if DATABASE_URL is unset — the models use portable types, so the
generated ops apply to Postgres too.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

# make the repo root importable (libs.*)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from libs.db import metadata  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata


def _load_dotenv() -> None:
    """Minimal .env loader (no dependency) — populates os.environ from storm-hub/.env if present,
    without overriding already-set vars. Lets `alembic upgrade head` work with zero exports."""
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def get_url() -> str:
    _load_dotenv()
    return os.environ.get("DATABASE_URL", "sqlite:///./_bootstrap.db")


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(get_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

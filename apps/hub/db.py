"""SQLAlchemy engine + session dependency for the Hub."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

# Explicit pool sizing (Phase F10) rather than SQLAlchemy's defaults -- the Hub is the only DB
# client (workers never touch it, by design), but it serves FastAPI request handlers, the scheduler
# tick thread, and the event-consumer thread concurrently, so an unbounded/under-sized default pool
# is worth pinning down explicitly rather than leaving to whatever SQLAlchemy's default happens to be.
# pool_timeout is EXPLICIT (2026-09-13). It was relying on SQLAlchemy's 30s default, which is a
# reasonable value -- but the Hub wedged for hours on 2026-09-11 with every thread parked in
# QueuePool._do_get and nothing in the logs, because the one caller that could have surfaced it
# (_audit_middleware) wrapped its checkout in a bare `except Exception: pass`. Pinning the value
# here makes the bound reviewable instead of inherited, and the timeout is what turns "wedged
# forever, silently" into "raises, gets logged, and the thread moves on".
engine = create_engine(
    settings.database_url, pool_pre_ping=True, pool_size=10, max_overflow=20,
    pool_timeout=30, future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

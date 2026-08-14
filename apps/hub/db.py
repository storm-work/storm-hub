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
engine = create_engine(
    settings.database_url, pool_pre_ping=True, pool_size=10, max_overflow=20, future=True
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

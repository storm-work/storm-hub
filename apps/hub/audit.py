"""Background audit writer — audit rows must never sit on a request's critical path.

CONFIRMED LIVE 2026-09-11/13 (py-spy dump of a wedged Hub, PID 364357): every thread in the
process was parked in `QueuePool._do_get` and NOT ONE held a connection and did work. The Hub was
alive, systemd said `active (running)`, and it served nothing at all for hours.

The cause was `_audit_middleware` doing two things it must not do:

  1. **A second DB checkout per mutating request.** FastAPI only tears down the handler's
     `Depends(get_db)` session AFTER the response is fully sent, so the handler's connection (A) is
     still checked out when the middleware asks for its own (B). Under concurrency that alone
     exhausts a 10+20 pool.
  2. **Blocking the asyncio event loop.** The middleware is `async def` but called SYNCHRONOUS
     SQLAlchemy (`db.get`, `db.commit`) inline. Once the pool was drained, the event loop itself
     parked in `_do_get` -- so no response could be sent, so connection A was never released, so
     the pool could never recover. A self-sustaining deadlock: pool pressure that should have
     degraded to "slow" instead became a permanent, unrecoverable outage with no path back except
     a restart.

This writer removes both. The middleware does a non-blocking `submit()` (pure in-memory queue put,
no DB, no I/O, cannot block the loop); one background thread owns the writes. Audit is
fire-and-forget by design -- it must never be able to hold up, or take down, a real request.
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable, Iterator

from sqlalchemy.orm import Session

logger = logging.getLogger("hub.audit")

# Bounded on purpose: an unbounded queue turns a DB outage into unbounded memory growth, which is
# how a logging path takes down the process it was meant to observe. Audit is the cheapest thing in
# the system to lose, so overflow DROPS and counts rather than blocking a request or growing.
_MAX_QUEUE = 10_000
# Rows per session. Batching keeps one short checkout per burst instead of one per row.
_BATCH = 100
# How long the writer waits for the first row of a batch before looping (lets stop() be responsive).
_POLL = 0.5


class AuditWriter:
    """Owns all audit persistence. `resolve_session_factory` is a callable returning a
    context-manager-ish generator factory (the same `get_db`-shaped thing the app uses), resolved
    at DRAIN time rather than construction so `app.dependency_overrides` still applies -- that is
    what keeps tests writing into their own isolated SQLite instead of the production DB."""

    def __init__(self, resolve_session_factory: Callable[[], Callable[[], Iterator[Session]]]):
        self._resolve = resolve_session_factory
        self._q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=_MAX_QUEUE)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._user_names: dict[str, str] = {}   # user_id -> username, resolved once in the writer
        self.dropped = 0

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="audit-writer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
        self._drain_once(block=False)   # best-effort flush of whatever is still queued

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── producer side (called from the request path -- MUST NOT BLOCK) ───────
    def submit(self, row: dict[str, Any]) -> None:
        """Non-blocking. No DB, no I/O -- safe to call from the asyncio event loop.

        Falls back to an inline write ONLY when no writer thread is running. That is the case in
        tests (the fixtures construct TestClient without entering its lifespan, so startup never
        runs) and it preserves the pre-2026-09-13 behaviour those tests assert on -- they check the
        audit row immediately after the request returns, with no concurrency to deadlock against.
        In production the thread is started by the app lifespan, so this path is not taken; if it
        ever is, that is a startup bug worth seeing rather than silently losing every audit row."""
        if not self.is_running():
            self._write([row])
            return
        try:
            self._q.put_nowait(row)
        except queue.Full:
            self.dropped += 1
            if self.dropped % 100 == 1:      # log the first, then every 100th -- never per-row
                logger.warning("audit queue full (%d dropped); the writer is not keeping up",
                               self.dropped)

    # ── consumer side ────────────────────────────────────────────────────────
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._drain_once(block=True)
            except Exception:
                # A writer that dies takes audit with it silently. Log and keep going -- this
                # thread must outlive any single bad row or transient DB blip.
                logger.exception("audit writer drain failed; continuing")

    def _drain_once(self, *, block: bool) -> None:
        rows: list[dict[str, Any]] = []
        if block:
            try:
                rows.append(self._q.get(timeout=_POLL))
            except queue.Empty:
                return
        while len(rows) < _BATCH:
            try:
                rows.append(self._q.get_nowait())
            except queue.Empty:
                break
        if rows:
            self._write(rows)

    def _write(self, rows: list[dict[str, Any]]) -> None:
        from libs.db import models as M

        factory = self._resolve()
        gen = factory()
        db = next(gen)
        try:
            for row in rows:
                db.add(M.AuditRow(
                    actor=self._actor_name(db, row.pop("_actor_sub", None), row.pop("_actor", None)),
                    **row,
                ))
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            logger.exception("audit write failed for %d row(s)", len(rows))
        finally:
            gen.close()

    def _actor_name(self, db: Session, sub: str | None, explicit: str | None) -> str | None:
        """Resolve a token subject to a username INSIDE the writer, so the request path never
        spends a query on it. Cached: user ids are stable, and this used to be a `db.get(User, ...)`
        on the event loop for every mutating request."""
        if explicit is not None:
            return explicit
        if not sub:
            return None
        cached = self._user_names.get(sub)
        if cached is not None:
            return cached
        from libs.db import models as M
        try:
            u = db.get(M.User, sub)
        except Exception:
            return sub
        name = u.username if u else sub
        self._user_names[sub] = name
        return name

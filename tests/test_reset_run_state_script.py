"""scripts/reset_run_state.py -- scoped recovery for state corrupted by a known-bad run.

The dry-run default is the whole point (house rule F8: recovery is a reviewed, scoped, repeatable
script, never ad-hoc SQL against production), so that is what these pin hardest.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from libs.db import metadata
from libs.db import models as M
from libs.domain import enums as E

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A FILE-backed SQLite the script's own subprocess can open too.

    The script runs as a subprocess, so an in-process monkeypatch of SessionLocal does not reach it
    -- an earlier version of this fixture left the subprocess pointed at the REAL DATABASE_URL from
    .env, i.e. production, and was saved only by the script's running-workers guard. The URL is
    handed over through the environment, which is exactly how apps/hub/config.py resolves it."""
    url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_engine(url)
    metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = Session()
    yield s
    s.close()


def _seed(db, *, worker_status=E.WorkerStatus.STOPPED):
    bk = M.Booking(name="B", type="real", posto_id="5084", status=E.BookingStatus.RUNNING)
    ident = M.Identity(full_name="X", type="real")
    db.add_all([bk, ident]); db.commit()
    w = M.Worker(booking_id=bk.id, identity_id=ident.id, role=E.WorkerRole.APPLY, status=worker_status)
    slot = M.Slot(booking_id=bk.id, posto_id="5084", date="2026-09-01", period=1,
                  status=E.SlotStatus.LEASED)
    proxy = M.Proxy(provider="soax", kind="rotating", endpoint="1.1.1.1:1", credentials_ref="u:p",
                    active=True, is_healthy=False, health="TimeoutError", score=0.0, max_sessions=2)
    db.add_all([w, slot, proxy]); db.commit()
    db.add(M.SlotLease(slot_id=slot.id, worker_id=w.id)); db.commit()
    return bk, w, slot, proxy


def _run(*args):
    env = {**os.environ, "DATABASE_URL": os.environ["DATABASE_URL"]}
    return subprocess.run([sys.executable, "scripts/reset_run_state.py", *args],
                          cwd=ROOT, capture_output=True, text=True, env=env)


def test_requires_something_to_do(db):
    assert _run().returncode == 2


def test_refuses_while_workers_are_running(db):
    _seed(db, worker_status=E.WorkerStatus.RUNNING)
    r = _run("--proxy-health", "--confirm")
    assert r.returncode == 2 and "REFUSING" in r.stderr


def test_dry_run_writes_nothing(db):
    _bk, _w, slot, proxy = _seed(db)
    r = _run("--proxy-health", "--slot-leases", "all")
    assert r.returncode == 0 and "DRY RUN" in r.stdout
    db.expire_all()
    assert proxy.is_healthy is False and proxy.health == "TimeoutError"   # untouched
    assert db.scalars(select(M.SlotLease).where(M.SlotLease.released_at.is_(None))).all()
    assert slot.status == E.SlotStatus.LEASED


def test_confirm_resets_proxy_health_and_releases_leases(db):
    _bk, _w, slot, proxy = _seed(db)
    r = _run("--proxy-health", "--slot-leases", "all", "--confirm")
    assert r.returncode == 0 and "WROTE" in r.stdout
    db.expire_all()
    assert proxy.is_healthy is True and proxy.health is None
    assert not db.scalars(select(M.SlotLease).where(M.SlotLease.released_at.is_(None))).all()
    assert slot.status == E.SlotStatus.AVAILABLE       # reopened, needs the explicit flush


def test_never_reopens_a_genuinely_taken_slot(db):
    _bk, _w, slot, _p = _seed(db)
    slot.status = E.SlotStatus.TAKEN
    db.commit()

    _run("--slot-leases", "all", "--confirm")

    db.expire_all()
    assert slot.status == E.SlotStatus.TAKEN           # a real booking is never handed back out


def test_unknown_booking_name_is_an_error(db):
    _seed(db)
    r = _run("--slot-leases", "nope", "--confirm")
    assert r.returncode == 2 and "no booking named" in r.stderr

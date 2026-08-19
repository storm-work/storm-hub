"""cleanup_worker_logs.py -- selection logic, exercised end to end against a scratch dir + DB.

Runs the real script in a subprocess (as the operator does) rather than importing its internals,
because the guarantees that matter are behavioural: dry-run writes nothing, a RUNNING worker's log
is never touched, and selection is scoped to the one directory.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from libs.db import metadata
from libs.db import models as M
from libs.domain.enums import WorkerRole, WorkerStatus

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "cleanup_worker_logs.py"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    eng = create_engine(f"sqlite:///{db_path}")
    metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    db = S()
    bk = M.Booking(name="b", type="test", posto_id="2032")
    i1 = M.Identity(full_name="ANA KEEPER", type="real")
    i2 = M.Identity(full_name="BOB TARGET", type="real")
    db.add_all([bk, i1, i2]); db.commit()
    live = M.Worker(booking_id=bk.id, identity_id=i1.id, role=WorkerRole.APPLY,
                    status=WorkerStatus.RUNNING)
    done = M.Worker(booking_id=bk.id, identity_id=i2.id, role=WorkerRole.APPLY,
                    status=WorkerStatus.STOPPED)
    db.add_all([live, done]); db.commit()
    ids = (live.id, done.id)
    db.close()

    logs = tmp_path / "var" / "worker-logs"
    logs.mkdir(parents=True)
    for wid in (*ids, "deadbeef-gone-0000-0000-000000000000"):
        (logs / f"worker-{wid}.log").write_text("x" * 100)
    return db_path, logs, ids


def _run(db_path, logs, *args):
    """The script resolves its dir from __file__, so point it at the scratch tree via a copy."""
    script = logs.parent.parent / "cleanup_worker_logs.py"
    script.write_text(_SCRIPT.read_text().replace(
        '_ROOT = Path(__file__).resolve().parent.parent',
        f'_ROOT = Path({str(logs.parent.parent)!r})'))
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path}",
           "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    return subprocess.run([sys.executable, str(script), *args],
                          capture_output=True, text=True, env=env)


def test_dry_run_deletes_nothing(env):
    db_path, logs, _ = env
    r = _run(db_path, logs, "--all")
    assert r.returncode == 0, r.stderr
    assert "DRY RUN" in r.stdout
    assert len(list(logs.glob("*.log"))) == 3


def test_selecting_one_identity_touches_only_that_identitys_log(env):
    db_path, logs, (live_id, done_id) = env
    r = _run(db_path, logs, "--identity", "BOB", "--confirm")
    assert r.returncode == 0, r.stderr
    left = {f.name for f in logs.glob("*.log")}
    assert f"worker-{done_id}.log" not in left
    assert f"worker-{live_id}.log" in left        # a different identity, untouched
    assert len(left) == 2


def test_a_running_workers_log_is_never_deleted(env):
    """It is open and being written to, and reading it is how you diagnose the run in progress."""
    db_path, logs, (live_id, _) = env
    r = _run(db_path, logs, "--all", "--confirm")
    assert r.returncode == 0, r.stderr
    assert (logs / f"worker-{live_id}.log").exists()
    assert "skipped 1 log(s) of RUNNING workers" in r.stdout


def test_orphaned_selects_only_logs_with_no_worker_row(env):
    db_path, logs, (live_id, done_id) = env
    r = _run(db_path, logs, "--orphaned", "--confirm")
    assert r.returncode == 0, r.stderr
    left = {f.name for f in logs.glob("*.log")}
    assert left == {f"worker-{live_id}.log", f"worker-{done_id}.log"}


def test_no_selector_selects_nothing(env):
    db_path, logs, _ = env
    r = _run(db_path, logs)
    assert r.returncode == 0 and "nothing selected" in r.stdout
    assert len(list(logs.glob("*.log"))) == 3

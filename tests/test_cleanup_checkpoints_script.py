"""scripts/cleanup_checkpoints.py -- the sanctioned replacement for manual checkpoint deletion
(Phase F8). Invoked as a real subprocess (how it's actually used operationally) against the real
project's var/checkpoints/ directory, cleaned up after each test."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "cleanup_checkpoints.py"
_CKPT_DIR = _ROOT / "var" / "checkpoints"


@pytest.fixture()
def old_checkpoint():
    _CKPT_DIR.mkdir(parents=True, exist_ok=True)
    f = _CKPT_DIR / "_test_cleanup_script_identity.json"
    f.write_text('{"identity_id": "_test_cleanup_script_identity", "cookies": {}}')
    old_time = time.time() - 10 * 86400
    os.utime(f, (old_time, old_time))
    yield f
    f.unlink(missing_ok=True)


def _run(*args):
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args], capture_output=True, text=True, cwd=str(_ROOT)
    )


def test_dry_run_by_default_deletes_nothing(old_checkpoint):
    r = _run("--max-age-days", "7")
    assert r.returncode == 0
    assert "DRY RUN" in r.stdout
    assert old_checkpoint.exists()   # never deleted without --confirm


def test_confirm_flag_actually_deletes(old_checkpoint):
    r = _run("--max-age-days", "7", "--confirm")
    assert r.returncode == 0
    assert "Deleted 1/1" in r.stdout
    assert not old_checkpoint.exists()


def test_fresh_checkpoint_not_matched_by_age(old_checkpoint):
    fresh = _CKPT_DIR / "_test_cleanup_script_fresh.json"
    fresh.write_text('{"identity_id": "_test_cleanup_script_fresh", "cookies": {}}')
    try:
        r = _run("--max-age-days", "7", "--confirm")
        assert "_test_cleanup_script_fresh.json" not in r.stdout
        assert fresh.exists()   # untouched -- not old enough
    finally:
        fresh.unlink(missing_ok=True)


def test_help_documents_the_safety_contract():
    r = _run("--help")
    assert r.returncode == 0
    assert "--confirm" in r.stdout
    assert "dry" in r.stdout.lower()


def test_no_arbitrary_path_argument_accepted():
    # The scoping guarantee: this script must never accept a caller-supplied path -- it only ever
    # touches libs/engine/checkpoint.py's own var/checkpoints/.
    r = _run("--path", "/tmp/whatever")
    assert r.returncode != 0   # argparse rejects the unknown flag

"""scripts/cleanup_checkpoints.py -- the sanctioned replacement for manual checkpoint deletion
(Phase F8). Invoked as a real subprocess (how it's actually used operationally), but pointed at an
isolated `tmp_path` via CHECKPOINTS_DIR_TEST_OVERRIDE so this suite can never touch or delete real
operational data in var/checkpoints/ -- see docs/engineering/DECISIONS.md D15 and the safety-incident
note in FAILURE_MODES.md (a pre-fix run of this file deleted 3 real checkpoints, one of them live).

The script itself still accepts no `--path`/arbitrary-directory argument (test_no_arbitrary_path_
argument_accepted, below) -- that guarantee is untouched. The override lives one layer down, in
libs/engine/checkpoint.py, which the script imports its directory from; it is unset in every real
deployment, so production resolves to the exact same hardcoded path it always has."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "cleanup_checkpoints.py"


def _run(ckpt_dir, *args):
    env = {**os.environ, "CHECKPOINTS_DIR_TEST_OVERRIDE": str(ckpt_dir)}
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args], capture_output=True, text=True, cwd=str(_ROOT),
        env=env,
    )


@pytest.fixture()
def old_checkpoint(tmp_path):
    f = tmp_path / "_test_cleanup_script_identity.json"
    f.write_text('{"identity_id": "_test_cleanup_script_identity", "cookies": {}}')
    old_time = time.time() - 10 * 86400
    os.utime(f, (old_time, old_time))
    return f    # tmp_path is pytest's own isolated per-test directory -- no manual teardown needed


def test_dry_run_by_default_deletes_nothing(tmp_path, old_checkpoint):
    r = _run(tmp_path, "--max-age-days", "7")
    assert r.returncode == 0
    assert "DRY RUN" in r.stdout
    assert old_checkpoint.exists()   # never deleted without --confirm


def test_confirm_flag_actually_deletes(tmp_path, old_checkpoint):
    r = _run(tmp_path, "--max-age-days", "7", "--confirm")
    assert r.returncode == 0
    assert "Deleted 1/1" in r.stdout
    assert not old_checkpoint.exists()


def test_fresh_checkpoint_not_matched_by_age(tmp_path, old_checkpoint):
    fresh = tmp_path / "_test_cleanup_script_fresh.json"
    fresh.write_text('{"identity_id": "_test_cleanup_script_fresh", "cookies": {}}')
    r = _run(tmp_path, "--max-age-days", "7", "--confirm")
    assert "_test_cleanup_script_fresh.json" not in r.stdout
    assert fresh.exists()   # untouched -- not old enough


def test_help_documents_the_safety_contract(tmp_path):
    r = _run(tmp_path, "--help")
    assert r.returncode == 0
    assert "--confirm" in r.stdout
    assert "dry" in r.stdout.lower()


def test_no_arbitrary_path_argument_accepted(tmp_path):
    # The scoping guarantee: this script must never accept a caller-supplied path -- it only ever
    # touches whatever libs/engine/checkpoint.py resolves _CHECKPOINTS_DIR to (production: its
    # hardcoded var/checkpoints/; here: tmp_path, via the test-only env override). No CLI flag can
    # redirect it.
    r = _run(tmp_path, "--path", "/tmp/whatever")
    assert r.returncode != 0   # argparse rejects the unknown flag

"""Tests for the worker-control plumbing: event->status mapping (pure) + supervisor tree-kill."""
import subprocess
import time

from apps.hub.events import map_event_to_update
from apps.hub.supervisor import _alive, kill_tree
from libs.domain.enums import WorkerStatus, WorkerSubstate


# ── event -> worker-status mapping ────────────────────────────────────────────
def test_started_sets_running_idle():
    assert map_event_to_update("started", {}) == {
        "status": WorkerStatus.RUNNING, "substate": WorkerSubstate.IDLE}


def test_status_change_sets_substate_and_detail():
    upd = map_event_to_update("status_change", {"substate": "warming_up", "detail": "attempt 2"})
    assert upd == {"substate": "warming_up", "substate_detail": "attempt 2"}


def test_terminal_booked_completes():
    upd = map_event_to_update("terminal", {"outcome": "booked", "substate": "booked"})
    assert upd["status"] == WorkerStatus.COMPLETED and upd["substate"] == "booked"


def test_terminal_failed():
    assert map_event_to_update("terminal", {"outcome": "failed"})["status"] == WorkerStatus.FAILED


def test_terminal_stopped():
    assert map_event_to_update("terminal", {"outcome": "stopped"})["status"] == WorkerStatus.STOPPED


def test_log_line_no_db_change():
    assert map_event_to_update("log_line", {"text": "hi"}) == {}


# ── supervisor kill_tree ──────────────────────────────────────────────────────
def test_kill_tree_reaps_process_and_children():
    # parent shell spawns a child sleep; killing the tree must reap both
    p = subprocess.Popen(["bash", "-c", "sleep 30 & sleep 30"], start_new_session=True)
    time.sleep(0.3)
    assert _alive(p.pid)
    kill_tree(p.pid, grace=2.0)
    p.wait(timeout=3)   # reap the killed parent so it isn't a lingering zombie
    assert p.poll() is not None

"""Tests for the worker-control plumbing: event->status mapping (pure) + supervisor tree-kill."""
import subprocess
import time

from apps.hub.events import EventHub, map_event_to_update
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


def test_terminal_held_is_its_own_status_not_a_failure():
    """Confirmed bug 2026-08-14: `held` (emitted by _run_apply for "no slot assigned" and "apply
    bounced") had no entry in _TERMINAL_STATUS, so it hit the FAILED default -- a worker that did
    nothing wrong was shown red, counted as failed, and burned MAX_RESPAWN_ATTEMPTS."""
    assert map_event_to_update("terminal", {"outcome": "held"})["status"] == WorkerStatus.HELD


def test_every_phase_outcome_has_an_explicit_terminal_status():
    """Drift guard -- this is the test that would have caught the `held` bug. Any outcome
    libs/engine/phases.py can emit MUST be an explicit key in _TERMINAL_STATUS; falling through to
    the FAILED default silently mislabels a worker."""
    import re
    from pathlib import Path

    from apps.hub.events import _TERMINAL_STATUS

    src = Path("libs/engine/phases.py").read_text()
    emitted = set(re.findall(r'PhaseResult\(\s*"(\w+)"', src))
    assert emitted, "failed to parse any PhaseResult outcomes -- fix this test's regex"
    missing = emitted - set(_TERMINAL_STATUS)
    assert not missing, f"outcomes with no explicit terminal status (would silently become FAILED): {missing}"


def test_log_line_no_db_change():
    assert map_event_to_update("log_line", {"text": "hi"}) == {}


# ── EventHub.dates_reported buffer (D6) ────────────────────────────────────────
def test_pop_dates_reported_returns_and_clears_buffered_reports():
    hub = EventHub.__new__(EventHub)   # bypass __init__ -- no Redis needed for this
    hub.dates_reported = {}

    assert hub.pop_dates_reported("s1") == []   # nothing buffered yet

    hub.dates_reported.setdefault("s1", []).append([{"date": "d1", "outcome": "empty"}])
    hub.dates_reported.setdefault("s1", []).append([{"date": "d2", "outcome": "found"}])
    hub.dates_reported.setdefault("s2", []).append([{"date": "d3", "outcome": "failed"}])

    popped = hub.pop_dates_reported("s1")
    assert popped == [[{"date": "d1", "outcome": "empty"}], [{"date": "d2", "outcome": "found"}]]
    assert hub.pop_dates_reported("s1") == []          # drained
    assert hub.pop_dates_reported("s2") == [[{"date": "d3", "outcome": "failed"}]]   # untouched by s1's pop


# ── supervisor kill_tree ──────────────────────────────────────────────────────
def test_kill_tree_reaps_process_and_children():
    # parent shell spawns a child sleep; killing the tree must reap both
    p = subprocess.Popen(["bash", "-c", "sleep 30 & sleep 30"], start_new_session=True)
    time.sleep(0.3)
    assert _alive(p.pid)
    kill_tree(p.pid, grace=2.0)
    p.wait(timeout=3)   # reap the killed parent so it isn't a lingering zombie
    assert p.poll() is not None

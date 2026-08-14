"""Shared hub test fixtures.

Autouse: replace the real Supervisor.spawn/stop with no-ops for every test in this directory by
default. Without this, any test that hits an endpoint calling _do_launch_worker (start_booking,
launch_worker, attach+start flows) spawns a REAL `python -m apps.worker <id>` subprocess via
subprocess.Popen -- and since apps.hub.main's `supervisor` is a module-level singleton never torn
down between test runs, those processes are never reaped. They sit blocked forever waiting for a
`launch` command against a Redis stream/DB state that vanished when the test's in-memory SQLite
session ended. Found by direct observation during Phase E live validation: repeated pytest runs
across a single working session had left over a dozen orphaned worker processes accumulated on the
box. A test that genuinely needs to exercise real subprocess spawning (e.g. Supervisor.kill_tree
itself) should monkeypatch its own narrower fixture instead of disabling this one.
"""
import pytest


@pytest.fixture(autouse=True)
def _no_real_worker_subprocesses(monkeypatch):
    import apps.hub.main as hub_main

    monkeypatch.setattr(hub_main.supervisor, "spawn", lambda worker_id: 0)
    monkeypatch.setattr(hub_main.supervisor, "spawn_registrar", lambda run_id: 0)
    monkeypatch.setattr(hub_main.supervisor, "stop", lambda worker_id: True)


@pytest.fixture(autouse=True)
def _launch_fleet_synchronously(monkeypatch):
    """Run the fleet launch INLINE for hub tests instead of on a background thread.

    Production backgrounds it (apps/hub/main.py's _begin_fleet_launch) because a live launch is
    minutes of real per-proxy browser probes and blocking the HTTP request made the Start button
    look dead. But these tests run against in-memory SQLite with StaticPool, which hands the SAME
    physical connection to every session -- a real thread writing through it concurrently with the
    test thread produces "cannot rollback - no transaction is active" and similar, for reasons that
    have nothing to do with the behavior under test. Same reasoning already documented in
    tests/hub/test_live_launch.py's _launch_worker_fleet test.

    The backgrounding and its idempotence are covered directly in
    tests/hub/test_worker_status_semantics.py, which opts out of this fixture.
    """
    import apps.hub.main as hub_main

    def _inline(booking_id, worker_ids, *, jitter_secs=0.0):
        if not worker_ids:
            return False
        hub_main._launch_worker_fleet(worker_ids, jitter_secs=jitter_secs)
        return True

    monkeypatch.setattr(hub_main, "_begin_fleet_launch", _inline)

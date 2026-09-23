"""Supervisor -- process tracking must survive a Hub restart (confirmed bug, 2026-08-13): the old
Popen-only tracking meant a fresh Supervisor() instance had no record of processes a PREVIOUS Hub
instance spawned, so stop() silently no-oped for them and running_ids()/is_running() reported them
as dead even though they were perfectly healthy. Uses a real subprocess whose argv matches the
`python -m <module> <key>` shape Supervisor._spawn_module uses (stand-in module: the stdlib's own
http.server, so this needs no apps.worker/Redis machinery) to exercise the actual /proc-scan fallback
against a real, "foreign" (not-spawned-by-this-instance) process.
"""
import os
import subprocess
import sys
import time

from apps.hub.supervisor import Supervisor, _alive, _find_pid_by_cmdline


def _spawn_foreign(key: str) -> subprocess.Popen:
    """A real process with the exact argv shape Supervisor looks for, started OUTSIDE any
    Supervisor instance -- standing in for "a process a previous Hub instance spawned"."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", key],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    time.sleep(0.3)   # let it actually start and register its cmdline
    return proc


def test_find_pid_by_cmdline_locates_a_real_foreign_process():
    key = "18231"   # a port number doubling as this test's unique "key"
    proc = _spawn_foreign(key)
    try:
        found = _find_pid_by_cmdline("http.server", key)
        assert found == proc.pid
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_find_pid_by_cmdline_returns_none_for_unknown_key():
    assert _find_pid_by_cmdline("http.server", "no-such-key-ever-18232") is None


def test_stop_kills_a_reconciled_pid_it_never_spawned_itself():
    """The core regression: Supervisor.stop() must kill a process it never spawned itself (e.g.
    spawned by a previous Hub instance before a restart, now known only via reconcile()'s pid
    registry), not silently return False."""
    key = "18233"
    proc = _spawn_foreign(key)
    sup = Supervisor("redis://localhost:6379/0")   # fresh instance -- never spawned this pid
    sup.reconcile({key: "http.server"})
    assert sup._pids[key] == proc.pid

    try:
        ok = sup.stop(key)
        assert ok is True
        # proc is a real child of THIS test process (unlike in production, where Supervisor's
        # process is the real parent) -- os.kill(pid, 0) reports a killed-but-unreaped child as
        # still "alive" (zombie) until something wait()s on it, so reap it here rather than relying
        # on _alive() to observe the kill.
        assert proc.wait(timeout=5) is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_stop_falls_back_to_direct_proc_scan_for_worker_module():
    """Even with NO reconcile() call at all, stop() itself does a last-resort /proc scan for the
    two real module names it spawns (apps.worker / apps.registrar) -- covers a worker whose row
    predates this Hub instance in some edge case reconcile() didn't get to."""
    key = "18235"
    proc = subprocess.Popen(
        [sys.executable, "-m", "apps.worker", key],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    time.sleep(0.3)
    sup = Supervisor("redis://localhost:6379/0")
    assert sup._pids == {}
    try:
        ok = sup.stop(key)
        assert ok is True
        assert proc.wait(timeout=5) is not None   # reaped -- see the sibling test's note on zombies
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_stop_returns_false_when_nothing_matches():
    sup = Supervisor("redis://localhost:6379/0")
    assert sup.stop("no-such-worker-id-at-all") is False


def test_reconcile_picks_up_a_preexisting_process_and_is_running_reports_it():
    """reconcile() is what Hub startup calls with the DB's list of RUNNING workers -- after it,
    running_ids()/is_running() must correctly see a process this instance never spawned."""
    key = "18234"
    proc = _spawn_foreign(key)
    sup = Supervisor("redis://localhost:6379/0")
    try:
        assert sup.is_running(key) is False   # not yet reconciled

        sup.reconcile({key: "http.server"})

        assert sup.is_running(key) is True
        assert key in sup.running_ids()
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_reconcile_does_not_overwrite_an_already_tracked_pid():
    sup = Supervisor("redis://localhost:6379/0")
    sup._pids["w1"] = 999999   # pretend already tracked (e.g. spawned by this instance)
    sup.reconcile({"w1": "apps.worker"})
    assert sup._pids["w1"] == 999999   # untouched -- reconcile only fills in gaps


def test_reconcile_leaves_a_genuinely_dead_worker_untracked():
    sup = Supervisor("redis://localhost:6379/0")
    sup.reconcile({"nonexistent-worker-id": "apps.worker"})
    assert sup.is_running("nonexistent-worker-id") is False
    assert "nonexistent-worker-id" not in sup._pids


def test_worker_output_is_captured_to_a_log_file(tmp_path, monkeypatch):
    """Worker stdout/stderr went to DEVNULL, discarding every diagnostic the VENDORED engine prints
    -- and those are the only place some of the portal's own words appear. When browser_login
    bounces, session.py prints `[session] auth-redirect error: <the portal's message>` plus the page
    snippet; the worker's own log_line events never carry it, so "login_failed_auth_redirect" was as
    much as anyone could learn about 160 failures in one day."""
    import apps.hub.supervisor as sup_mod

    monkeypatch.setattr(sup_mod, "_LOG_DIR", tmp_path)
    s = sup_mod.Supervisor("redis://localhost:6379/0")
    monkeypatch.setattr(sup_mod.sys, "executable", "/bin/echo")

    pid = s._spawn_module("w-abc", "apps.worker")
    assert pid > 0
    for _ in range(50):
        f = tmp_path / "worker-w-abc.log"
        if f.exists() and f.stat().st_size:
            break
        time.sleep(0.05)
    body = (tmp_path / "worker-w-abc.log").read_text()
    assert "apps.worker" in body and "w-abc" in body   # echo wrote the argv we passed


# ── zombie reaping (confirmed live 2026-09-10: 13 accumulated) ────────────────────────────────
# A worker that finishes ON ITS OWN (books, fails, crashes -- anything other than an explicit
# stop() call) never went through the only code path that reaped a child (stop()'s proc.wait()).
# These spawn a REAL child via Supervisor._spawn_module, unlike the reconcile-based tests above --
# that makes THIS test process the real OS parent, exactly like production where the Hub process
# is the real parent, so os.kill(pid, 0)'s zombie blind spot actually reproduces here.
def _spawn_own_fast_exit(monkeypatch, key="z1"):
    import apps.hub.supervisor as sup_mod
    monkeypatch.setattr(sup_mod.sys, "executable", "/bin/echo")
    s = sup_mod.Supervisor("redis://localhost:6379/0")
    pid = s._spawn_module(key, "apps.worker")
    return s, pid


def _is_zombie(pid: int) -> bool:
    with open(f"/proc/{pid}/status") as f:
        return any(ln.startswith("State:") and "Z" in ln.split()[1] for ln in f)


def test_alive_returns_false_for_an_unreaped_zombie(monkeypatch, tmp_path):
    import apps.hub.supervisor as sup_mod
    monkeypatch.setattr(sup_mod, "_LOG_DIR", tmp_path)
    s, pid = _spawn_own_fast_exit(monkeypatch)
    deadline = time.time() + 5   # /bin/echo exits almost instantly; wait for it to become Z
    while not _is_zombie(pid) and time.time() < deadline:
        time.sleep(0.05)
    assert _is_zombie(pid), "child never reached zombie state -- test setup is unreliable"
    assert _alive(pid) is False
    os.waitpid(pid, 0)   # clean up -- this test process is the real parent


def test_running_ids_no_longer_reports_a_naturally_finished_worker(monkeypatch, tmp_path):
    """The actual regression pin: before this fix, a worker that exited on its own (never
    stop()-ed) stayed in running_ids() forever, so the scheduler's dead-worker reaper could never
    see it was gone."""
    import apps.hub.supervisor as sup_mod
    monkeypatch.setattr(sup_mod, "_LOG_DIR", tmp_path)
    s, pid = _spawn_own_fast_exit(monkeypatch, key="z2")
    deadline = time.time() + 5
    while "z2" in s.running_ids() and time.time() < deadline:
        time.sleep(0.05)
    assert "z2" not in s.running_ids()
    assert s.is_running("z2") is False


def test_reap_finished_removes_the_popen_reference_after_natural_exit(monkeypatch, tmp_path):
    import apps.hub.supervisor as sup_mod
    monkeypatch.setattr(sup_mod, "_LOG_DIR", tmp_path)
    s, pid = _spawn_own_fast_exit(monkeypatch, key="z3")
    assert "z3" in s._procs
    deadline = time.time() + 5
    while "z3" in s._procs and time.time() < deadline:
        s._reap_finished()
        time.sleep(0.05)
    assert "z3" not in s._procs   # reaped -- no lingering Popen/zombie reference


def test_spawned_workers_get_unbuffered_stdout():
    """A worker's log file is its ONLY diagnostic channel for the vendored engine's output -- the
    portal's own words appear nowhere else (see _spawn_module's comment). Block-buffered stdout
    silently withheld that for exactly the window in which it mattered.

    MEASURED 2026-09-16, mid-run, while diagnosing five workers parked at LOGGED_IN: four of the
    five log files held only the 204-byte launch banner after 12 minutes. The fifth was readable
    only because it had overflowed the ~8KB buffer -- and that one carried the actual answer (the
    portal's 'try again later' refusal). Direct A/B of the two spawn environments: 0 bytes on disk
    after 5s buffered, 108 bytes unbuffered.

    The sink is opened with buffering=0, but that covers only the PARENT's writes; the child's
    stdout is a pipe-to-file, which CPython block-buffers whenever it is not a tty.

    Asserts on the env actually handed to Popen, not on the source text -- a source-string check
    passes just as happily against a comment that merely mentions the variable.
    """
    from apps.hub import supervisor as sup

    captured = {}

    class _FakeProc:
        pid = 4242

        def poll(self):
            return None

    def _spy(argv, **kw):
        captured.update(kw)
        return _FakeProc()

    orig = sup.subprocess.Popen
    sup.subprocess.Popen = _spy
    try:
        s = sup.Supervisor.__new__(sup.Supervisor)
        s._procs, s._pids = {}, {}
        s.redis_url = "redis://localhost:6379/0"
        s._reap_finished = lambda: None
        s._spawn_module("w-test-unbuffered", "apps.worker")
    finally:
        sup.subprocess.Popen = orig

    assert captured["env"].get("PYTHONUNBUFFERED") == "1", (
        "worker stdout must be unbuffered or its diagnostics are invisible while a run is live")

"""Supervisor — spawns and reaps worker subprocesses on this host (single-host, spec §2).

One worker = one OS process (`python -m apps.worker <id>`). On stop we terminate the whole process
tree and force-kill on timeout — a leaked Chromium (once live ops land) can't run away and exhaust
inotify/FDs the way it did in storm-auto. Tracks pids in-memory; the durable source of truth is the
DB + the bus events. A Hub restart does NOT kill already-running workers (they're spawned with their
own session via start_new_session=True) -- but a fresh Supervisor() instance has no memory of them
either, which is exactly why `reconcile()` exists (called once at Hub startup, apps/hub/main.py's
lifespan): without it, a restarted Hub can neither stop nor correctly detect-as-alive any worker a
PREVIOUS instance spawned, which was a confirmed bug (the booking Stop button silently did nothing
for pre-restart workers, and the dead-worker reaper would eventually mark them FAILED despite being
perfectly healthy).
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_LOG_DIR = _ROOT / "var" / "worker-logs"


def _descendant_pids(pid: int) -> list[int]:
    """Collect pid + all descendants via /proc, leaves last (best-effort, Linux)."""
    out = [pid]
    try:
        kids = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
        for k in kids:
            out.extend(_descendant_pids(int(k)))
    except (FileNotFoundError, ProcessLookupError, ValueError):
        pass
    return out


def kill_tree(pid: int, *, grace: float = 5.0) -> None:
    """SIGTERM the tree, then SIGKILL whatever survives `grace` seconds (leaves-first)."""
    pids = _descendant_pids(pid)
    for p in reversed(pids):
        try:
            os.kill(p, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.time() + grace
    while time.time() < deadline:
        if not any(_alive(p) for p in pids):
            return
        time.sleep(0.1)
    for p in reversed(_descendant_pids(pid)):
        try:
            os.kill(p, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    # os.kill(pid, 0) succeeds for a ZOMBIE too -- the PID slot exists until something wait()s on
    # it. CONFIRMED LIVE 2026-09-10: 13 worker zombies had accumulated under the Hub's own PID,
    # every one still reporting "alive" here, which made them invisible to running_ids()/
    # is_running() and therefore to the scheduler's dead-worker reaper (_reap_dead_workers skips
    # anything running_ids() claims is alive). Read /proc directly to tell a genuinely running
    # process from an unreaped corpse.
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("State:"):
                return "Z" not in line.split()[1]
    except OSError:
        pass   # /proc unavailable or raced with the process exiting -- fail open, matches kill(0)
    return True


def _find_pid_by_cmdline(module: str, key: str) -> int | None:
    """Fallback for Supervisor.stop() when `key` isn't in this instance's own self._procs -- e.g.
    spawned by a PREVIOUS Hub process, before the current one restarted. self._procs is in-memory
    only and a fresh Supervisor() starts with it empty (confirmed bug: the module docstring claims
    "a Hub restart re-attaches without killing live workers", but no re-attachment logic actually
    existed anywhere -- every worker/registrar process spawned before the most recent Hub restart
    was silently unstoppable via the API from then on, since Supervisor.stop() just returned False
    for a `key` it had no record of and never even tried to find the real OS process). Scans /proc
    for a process whose cmdline matches the exact `python -m <module> <key>` invocation
    Supervisor._spawn_module uses, since that's a stable, predictable signature."""
    try:
        pids = os.listdir("/proc")
    except FileNotFoundError:
        return None
    for entry in pids:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        parts = [p for p in raw.split(b"\x00") if p]
        if len(parts) < 3:
            continue
        tail = [p.decode(errors="replace") for p in parts[-3:]]
        if tail == ["-m", module, key]:
            return pid
    return None


class Supervisor:
    """Authoritative process tracking is by PID (`self._pids`), not by `subprocess.Popen` object --
    `Popen` can only ever represent a process THIS Python process actually started, which breaks the
    moment the Hub itself restarts: a fresh Supervisor() has no Popen handle for any worker/registrar
    a PREVIOUS Hub instance spawned, even though those processes (own session, own process group)
    are still very much alive. Confirmed bug (2026-08-13): the old `self._procs: dict[str, Popen]`
    design meant `stop()` silently no-oped for anything not spawned by the CURRENT instance -- the
    booking "Stop" button did nothing for any worker that predated the last Hub restart -- and
    `running_ids()`/`is_running()` (which `_reap_dead_workers` trusts) reported EVERY such worker as
    not-running, so the very next scheduler tick past the dead-grace window would mark genuinely
    healthy, holding workers FAILED and release their leases. `self._procs` is kept only for
    processes spawned by THIS instance, purely so `stop()` can `Popen.wait()` and reap its own
    child's zombie; `self._pids` is the real source of truth for is_running/running_ids/stop,
    checked via a plain `os.kill(pid, 0)` liveness probe that works for any PID regardless of
    parentage."""

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._procs: dict[str, subprocess.Popen] = {}
        self._pids: dict[str, int] = {}
        # _pids / _procs are touched from several threads: the scheduler tick (running_ids/
        # is_running via _reap_dead_workers), FastAPI request handlers (spawn/stop on start/stop/
        # restart/detach), and the background fleet-launch tasks. Without this lock, running_ids()
        # iterating self._pids raced a concurrent spawn/stop mutating it and raised
        # "RuntimeError: dictionary changed size during iteration" -- which aborted the WHOLE
        # scheduler tick (CONFIRMED LIVE 2026-10-01, 1234 crashes in a day under worker churn),
        # so timed actions like slot-open fired late/erratically (a scheduled 09:48 open fired at
        # 10:08). Reentrant so a lock-holding method can call another.
        self._lock = threading.RLock()

    def _reap_finished(self) -> None:
        """Non-blocking reap of any of THIS instance's own children that have already exited on
        their own (never went through stop()) -- Popen.poll() calls waitpid(WNOHANG) internally
        and permanently records the exit code once it succeeds, so this costs nothing when
        nothing has exited and is safe to call as often as we like. Without it, a worker that
        finishes naturally (books, fails, crashes -- anything other than an explicit stop() call)
        leaked as a zombie forever; CONFIRMED LIVE 2026-09-10, 13 had accumulated. A pid known
        only via reconcile() (self._pids but no self._procs entry) has no Popen here and isn't
        this instance's to reap -- its real parent (or init) owns that, matching stop()'s own
        existing distinction between owned and merely-tracked processes."""
        with self._lock:
            for key, proc in list(self._procs.items()):
                if proc.poll() is not None:
                    self._procs.pop(key, None)

    def spawn(self, worker_id: str) -> int:
        """Start the worker subprocess (idempotent — a live one is left running)."""
        return self._spawn_module(worker_id, "apps.worker")

    def spawn_registrar(self, run_id: str) -> int:
        """Start a registration-run subprocess (apps/registrar) -- same idempotent/process-tree-owned
        treatment as a booking worker, just a different entrypoint module."""
        return self._spawn_module(run_id, "apps.registrar")

    def _spawn_module(self, key: str, module: str) -> int:
        self._reap_finished()
        with self._lock:
            existing_pid = self._pids.get(key)
        if existing_pid is not None and _alive(existing_pid):
            return existing_pid
        # PYTHONUNBUFFERED: the sink below is opened with buffering=0, but that only makes the
        # PARENT's writes unbuffered -- the CHILD's stdout is a pipe-to-file, which CPython
        # block-buffers at 4-8KB when it is not a tty. So a worker's diagnostics stayed invisible
        # until it happened to produce 8KB of them.
        #
        # Measured on the 2026-09-16 run while diagnosing why five workers were parked at
        # LOGGED_IN: four of the five logs held only the 204-byte launch banner after 12 minutes,
        # and the fifth was readable ONLY because it had overflowed the buffer at ~12KB -- and that
        # one turned out to carry the actual answer (the portal's own "try again later" refusal).
        # The diagnostic value of these files is exactly the point of writing them (see the comment
        # below), and buffering silently withheld it for the whole window in which it was useful.
        env = {**os.environ, "REDIS_URL": self.redis_url, "PYTHONUNBUFFERED": "1"}
        # Worker stdout/stderr used to go to DEVNULL, which threw away every diagnostic the VENDORED
        # engine prints -- and those are the only place some of the portal's own words appear. When
        # browser_login bounces, session.py prints `[session] auth-redirect error: <the portal's own
        # message>` and the surrounding page snippet; the worker's own log_line events never carry
        # it, so "login_failed_auth_redirect" was as much as anyone could ever learn. Same for
        # `[session] ANTIDETECT flags`, the browser-mode line, and the solver's per-provider trace.
        # Kept per worker so a single run's file can be read straight through.
        log_path = _LOG_DIR / f"{module.rsplit('.', 1)[-1]}-{key}.log"
        try:
            _LOG_DIR.mkdir(parents=True, exist_ok=True)
            sink = open(log_path, "ab", buffering=0)
            # A run boundary. The file is APPENDED to across every launch of this worker id, and
            # its contents carry no timestamps, so a file spanning several runs is impossible to
            # attribute -- reading one cost a wrong conclusion about which proxy pool a run had
            # used (2026-08-19). One dated marker per launch makes the boundaries findable.
            sink.write(f"\n===== launch {datetime.now(timezone.utc).isoformat()} "
                       f"{module} {key} =====\n".encode())
        except OSError:
            sink = subprocess.DEVNULL   # never let logging failure stop a launch
        proc = subprocess.Popen(
            [sys.executable, "-m", module, key],
            cwd=str(_ROOT),
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
            start_new_session=True,   # own process group so kill_tree is clean
        )
        if sink is not subprocess.DEVNULL:
            sink.close()   # the child holds its own dup; the parent must not keep this fd open
        with self._lock:
            self._procs[key] = proc
            self._pids[key] = proc.pid
        return proc.pid

    def reconcile(self, keys_by_module: dict[str, str]) -> None:
        """Re-discover processes a PREVIOUS Hub instance spawned, so a restart doesn't strand them
        as untrackable (unstoppable via stop(), invisible/false-dead to running_ids()/is_running()).
        `keys_by_module`: {worker_or_run_id: "apps.worker" | "apps.registrar"}, normally every
        DB-RUNNING Worker's id at Hub startup (apps/hub/main.py's lifespan, which has DB access this
        module deliberately doesn't). Safe to call repeatedly; a key already tracked is left alone,
        and a key whose process is genuinely gone (crashed while the Hub was down) is simply not
        added -- the existing dead/stall reap paths already handle a DB-RUNNING row with no live
        process behind it, once running_ids() can actually see the truth."""
        for key, module in keys_by_module.items():
            with self._lock:
                already = key in self._pids
            if already:
                continue
            pid = _find_pid_by_cmdline(module, key)   # /proc scan -- deliberately NOT under the lock
            if pid is not None:
                with self._lock:
                    self._pids[key] = pid

    def stop(self, worker_id: str) -> bool:
        with self._lock:
            pid = self._pids.get(worker_id)
        if pid is None:
            # Not reconciled/tracked yet -- last-resort direct lookup (covers a key that arrived
            # after the one-time startup reconcile(), e.g. a worker whose row predates this Hub
            # instance in some edge case reconcile() didn't cover).
            for module in ("apps.worker", "apps.registrar"):
                pid = _find_pid_by_cmdline(module, worker_id)
                if pid is not None:
                    break
        if pid is None:
            return False
        if _alive(pid):
            kill_tree(pid)
        with self._lock:
            proc = self._procs.pop(worker_id, None)
        if proc is not None:
            try:
                proc.wait(timeout=5)   # reap so it doesn't linger as a zombie (only valid for OUR
            except subprocess.TimeoutExpired:  # own child -- a foreign/reconciled pid isn't ours to
                pass                            # wait() on; its real parent or init(1) reaps it)
        with self._lock:
            self._pids.pop(worker_id, None)
        return True

    def is_running(self, worker_id: str) -> bool:
        self._reap_finished()
        with self._lock:
            pid = self._pids.get(worker_id)
        return pid is not None and _alive(pid)

    def running_ids(self) -> list[str]:
        # Called every scheduler tick (Scheduler._reap_dead_workers) -- this is where an own child
        # that finished naturally actually gets reaped in steady-state production use, piggybacking
        # on a cadence that already exists rather than adding a new one.
        self._reap_finished()
        # SNAPSHOT under the lock, then probe liveness outside it: iterating self._pids directly
        # raced a concurrent spawn/stop and crashed the whole scheduler tick (see __init__).
        with self._lock:
            items = list(self._pids.items())
        return [wid for wid, pid in items if _alive(pid)]

    def shutdown_all(self) -> None:
        with self._lock:
            wids = list(self._pids)
        for wid in wids:
            self.stop(wid)

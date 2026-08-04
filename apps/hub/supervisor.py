"""Supervisor — spawns and reaps worker subprocesses on this host (single-host, spec §2).

One worker = one OS process (`python -m apps.worker <id>`). On stop we terminate the whole process
tree and force-kill on timeout — a leaked Chromium (once live ops land) can't run away and exhaust
inotify/FDs the way it did in storm-auto. Tracks pids in-memory; the durable source of truth is the
DB + the bus events, so a Hub restart re-attaches without killing live workers.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


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
        return True
    except (ProcessLookupError, PermissionError):
        return False


class Supervisor:
    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._procs: dict[str, subprocess.Popen] = {}

    def spawn(self, worker_id: str) -> int:
        """Start the worker subprocess (idempotent — a live one is left running)."""
        existing = self._procs.get(worker_id)
        if existing and existing.poll() is None:
            return existing.pid
        env = {**os.environ, "REDIS_URL": self.redis_url}
        proc = subprocess.Popen(
            [sys.executable, "-m", "apps.worker", worker_id],
            cwd=str(_ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,   # own process group so kill_tree is clean
        )
        self._procs[worker_id] = proc
        return proc.pid

    def stop(self, worker_id: str) -> bool:
        proc = self._procs.get(worker_id)
        if not proc:
            return False
        if proc.poll() is None:
            kill_tree(proc.pid)
        try:
            proc.wait(timeout=5)   # reap so it doesn't linger as a zombie
        except subprocess.TimeoutExpired:
            pass
        self._procs.pop(worker_id, None)
        return True

    def is_running(self, worker_id: str) -> bool:
        proc = self._procs.get(worker_id)
        return bool(proc and proc.poll() is None)

    def running_ids(self) -> list[str]:
        return [wid for wid, p in self._procs.items() if p.poll() is None]

    def shutdown_all(self) -> None:
        for wid in list(self._procs):
            self.stop(wid)

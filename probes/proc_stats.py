"""Tiny /proc readers for the backend experiments (psutil is not installed): process trees, RSS and
CPU ticks, so a browser's cost can be measured whether Playwright launched it (a worker's subtree)
or Donut did (found by its --remote-debugging-port)."""
from __future__ import annotations

import os
import time

_CLK_TCK = os.sysconf("SC_CLK_TCK")


def _stat_fields(pid: int) -> list[str] | None:
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
    except OSError:
        return None
    # comm may contain spaces/parens; everything after the LAST ')' is space-separated
    return data[data.rfind(")") + 2:].split()


def _children_map() -> dict[int, list[int]]:
    kids: dict[int, list[int]] = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        f = _stat_fields(int(name))
        if f:
            kids.setdefault(int(f[1]), []).append(int(name))
    return kids


def tree(root: int, kids: dict[int, list[int]] | None = None) -> list[int]:
    kids = kids if kids is not None else _children_map()
    out, stack = [], [root]
    while stack:
        p = stack.pop()
        out.append(p)
        stack.extend(kids.get(p, []))
    return out


def cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def find(substr: str) -> list[int]:
    return [int(n) for n in os.listdir("/proc") if n.isdigit() and substr in cmdline(int(n))]


def donut_profile_root(port: int) -> int | None:
    """The browser process Donut started with this CDP port (its renderers are its children)."""
    pids = find(f"--remote-debugging-port={port}")
    if not pids:
        return None
    # the root is the one whose parent is not itself in the matching set
    ppid = {p: int((_stat_fields(p) or [0, 0])[1]) for p in pids}
    roots = [p for p in pids if ppid[p] not in pids]
    return roots[0] if roots else pids[0]


def rss_mb(pids: list[int]) -> float:
    total = 0
    for p in pids:
        try:
            with open(f"/proc/{p}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        total += int(line.split()[1])
                        break
        except OSError:
            pass
    return total / 1024.0


def cpu_ticks(pids: list[int]) -> int:
    t = 0
    for p in pids:
        f = _stat_fields(p)
        if f:
            t += int(f[11]) + int(f[12])     # utime + stime
    return t


class CpuMeter:
    """CPU% of a process set between successive calls (100 = one full core)."""

    def __init__(self):
        self._last: tuple[float, int] | None = None

    def percent(self, pids: list[int]) -> float | None:
        now, ticks = time.monotonic(), cpu_ticks(pids)
        last, self._last = self._last, (now, ticks)
        if last is None or now <= last[0]:
            return None
        return max(0.0, (ticks - last[1]) / _CLK_TCK / (now - last[0]) * 100.0)

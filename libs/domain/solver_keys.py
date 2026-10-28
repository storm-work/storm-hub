"""Which reCAPTCHA-solver key a SOLVE uses: spread evenly across the keys, and step past a key that is out of
balance -- for every consumer (booking workers, the registrar, the login-token factory).

WHY. Keys used to be chosen BEFORE the solve and frozen: a registration account got key[account index], a
booking worker got key[hash(worker id)], and the vendored race_all then used only the first key of whatever
list it was handed. So a one- or two-account registration run always leaned on key #1 while #2 sat unused; a
fleet was balanced only on average; and a key that ran dry (2026-10-27: ERROR_ZERO_BALANCE) failed every
attempt of every consumer assigned to it, forever, with working keys idle beside it. The login-token factory
passed the whole list but race_all used [0], so it only ever spent key #1.

WHAT. The vendored race_all now asks this book, per solve:
  order(solver, keys)   the keys to TRY, in order: live keys rotated by a fleet-wide round-robin counter, then
                        keys currently marked dead (soonest-to-recover first, so a top-up is noticed);
  report_ok / report_fail  feed per-key counters, and a key-level failure (empty balance, wrong key) marks that
                        key dead for a while so nobody wastes a solve or a browser session on it.
State is shared through Redis (counter, dead marks, counters) so every process spreads over the same
rotation; with no Redis it falls back to in-process state -- still balanced and still failing over per process.

The book never decides WHICH SOLVER (that is solver_select) and never touches a key's secret beyond a short
fingerprint (sha1[:10]) in Redis and the last 4 characters in logs.
"""
from __future__ import annotations

import hashlib
import threading
import time

# Marks of a failure that belongs to the KEY (the account behind it), not to this solve: trying the same key
# again will fail the same way. Matched case-insensitively against the solver's error text.
_KEY_FAILURE_MARKS = (
    "ERROR_ZERO_BALANCE", "ZERO_BALANCE",
    "ERROR_KEY_DOES_NOT_EXIST", "KEY_DOES_NOT_EXIST",
    "ERROR_WRONG_USER_KEY", "WRONG_USER_KEY",
    "ERROR_KEY_DENIED_ACCESS", "KEY_DENIED_ACCESS",
    "INVALID_KEY", "ERROR_ACCOUNT_SUSPENDED", "ACCOUNT_SUSPENDED",
)
_BALANCE_MARKS = ("ZERO_BALANCE",)

# How long a dead key is skipped before it is tried again. An empty balance is retried sooner (a top-up
# should be noticed without anyone touching a setting); a key the provider does not recognise waits longer.
DEAD_TTL_BALANCE = 900.0
DEAD_TTL_OTHER = 3600.0

_PREFIX = "solverkeys"


def is_key_failure(err) -> bool:
    text = str(err or "").upper()
    return any(m in text for m in _KEY_FAILURE_MARKS)


def _dead_ttl(err) -> float:
    text = str(err or "").upper()
    return DEAD_TTL_BALANCE if any(m in text for m in _BALANCE_MARKS) else DEAD_TTL_OTHER


def fingerprint(key: str) -> str:
    return hashlib.sha1(str(key).encode()).hexdigest()[:10]


def tail(key: str) -> str:
    """What a log may say about a key: its last 4 characters."""
    return f"...{str(key)[-4:]}"


class KeyBook:
    def __init__(self, redis=None, clock=time.time):
        self._r = redis
        self._now = clock
        self._lock = threading.Lock()
        self._rr: dict[str, int] = {}
        self._dead: dict[str, float] = {}                 # "solver:fp" -> dead-until epoch
        self._stats: dict[str, dict[str, int]] = {}       # "solver:fp" -> {tries, ok, fail}

    # ── storage (Redis first, in-process when it is missing or erroring) ─────────────────────────────
    def _next(self, solver: str) -> int:
        if self._r is not None:
            try:
                return int(self._r.incr(f"{_PREFIX}:rr:{solver}"))
            except Exception:
                pass
        with self._lock:
            self._rr[solver] = self._rr.get(solver, 0) + 1
            return self._rr[solver]

    def dead_until(self, solver: str, key: str) -> float:
        """Epoch until which `key` is skipped, or 0.0 when it is live."""
        fp = f"{solver}:{fingerprint(key)}"
        now = self._now()
        until = 0.0
        if self._r is not None:
            try:
                raw = self._r.get(f"{_PREFIX}:dead:{fp}")
                until = float(raw) if raw else 0.0
            except Exception:
                until = 0.0
        with self._lock:
            until = max(until, self._dead.get(fp, 0.0))
        return until if until > now else 0.0

    def _bump(self, solver: str, key: str, field: str) -> None:
        fp = f"{solver}:{fingerprint(key)}"
        if self._r is not None:
            try:
                self._r.hincrby(f"{_PREFIX}:stats:{fp}", field, 1)
            except Exception:
                pass
        with self._lock:
            s = self._stats.setdefault(fp, {})
            s[field] = s.get(field, 0) + 1

    # ── the interface race_all uses ──────────────────────────────────────────────────────────────────
    def order(self, solver: str, keys: list[str]) -> list[str]:
        """The keys to try for ONE solve, best first. Never empty when `keys` is not."""
        keys = [k for k in (keys or []) if k]
        if len(keys) <= 1:
            return keys
        # CANONICAL order, whatever order the caller handed over: consumers rotate their list (per account,
        # per worker) so list[0] differs, and rotating that again by the shared counter lets the two rotations
        # cancel -- 2026-10-28, two accounts both landed on the same key while the other sat unused.
        keys = sorted(dict.fromkeys(keys), key=fingerprint)
        live, dead = [], []
        for k in keys:
            until = self.dead_until(solver, k)
            (dead if until else live).append((until, k))
        live_keys = [k for _, k in live]
        if live_keys:
            start = self._next(solver) % len(live_keys)
            live_keys = live_keys[start:] + live_keys[:start]
        return live_keys + [k for _, k in sorted(dead)]

    def report_ok(self, solver: str, key: str) -> None:
        self._bump(solver, key, "tries")
        self._bump(solver, key, "ok")

    def report_fail(self, solver: str, key: str, err) -> bool:
        """Record a failed solve. Returns True when it was a KEY-level failure (the key is now marked dead)."""
        self._bump(solver, key, "tries")
        self._bump(solver, key, "fail")
        if not is_key_failure(err):
            return False
        ttl = _dead_ttl(err)
        fp = f"{solver}:{fingerprint(key)}"
        until = self._now() + ttl
        if self._r is not None:
            try:
                self._r.set(f"{_PREFIX}:dead:{fp}", f"{until:.0f}", ex=int(ttl))
            except Exception:
                pass
        with self._lock:
            self._dead[fp] = until
        return True

    # ── visibility ───────────────────────────────────────────────────────────────────────────────────
    def stats(self, solver: str, keys: list[str]) -> list[dict]:
        out = []
        for k in keys:
            fp = f"{solver}:{fingerprint(k)}"
            counts: dict[str, int] = {}
            if self._r is not None:
                try:
                    counts = {f: int(v) for f, v in (self._r.hgetall(f"{_PREFIX}:stats:{fp}") or {}).items()}
                except Exception:
                    counts = {}
            with self._lock:
                for f, v in self._stats.get(fp, {}).items():
                    counts[f] = max(counts.get(f, 0), v)
            until = self.dead_until(solver, k)
            out.append({"solver": solver, "key": tail(k), "tries": counts.get("tries", 0),
                        "ok": counts.get("ok", 0), "fail": counts.get("fail", 0),
                        "dead_for_s": round(until - self._now()) if until else 0})
        return out


def install(redis_client=None) -> KeyBook:
    """Put a KeyBook behind the vendored race_all in THIS process. Idempotent; the newest Redis wins."""
    import libs.engine.vendor  # noqa: F401  (puts the vendored modules on sys.path)
    import solver as _solver
    book = KeyBook(redis_client)
    _solver.KEY_BOOK = book
    return book

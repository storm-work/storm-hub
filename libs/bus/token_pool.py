"""Shared pre-solved reCAPTCHA token pool (Redis), for the login throughput lever.

WHY THIS IS SAFE AND SHARED. The login reCAPTCHA token is sitekey/action-based and solved ProxyLess
(solver-cloud IPs), so it is NOT bound to any worker's browser, session or exit IP -- a token solved
out-of-band by one process is usable by ANY worker's login POST. The vendored engine already accepts
one: session.py's browser_login takes a `pre_token` holder and injects it, skipping its own solver
race. This pool is the producer side of that.

WHY IT MATTERS. Login against the portal is a global throttle lottery where bookings scale with SHOTS
TAKEN, and ~1% of byte-identical requests are served (login-is-a-shot-lottery). Each shot otherwise
pays ~15-25s of INLINE captcha solve. A factory keeps this pool full so a login pops a READY token
instantly and fires at browser-launch speed instead of solve speed -- multiplying shots/minute.

PERISHABILITY. reCAPTCHA Enterprise tokens live ~110s, so every entry carries its solved-at time:
acquire() discards anything past max_age, prune() drops the stale front, and the factory keeps
re-filling. The list is FIFO (rpush tail / lpop head), so the OLDEST token is consumed first -- use
it before it expires.
"""
from __future__ import annotations

import concurrent.futures as _cf
import json
import time
from typing import Callable

from .names import token_pool_key

# reCAPTCHA Enterprise tokens are accepted for ~110s. Keep the usable window a touch under that so a
# token still has life left after it is popped and injected.
DEFAULT_MAX_AGE = 100.0


class TokenPool:
    """A Redis list of {"t": token, "ts": solved_at} entries for one action (default LOGIN_EVISA)."""

    def __init__(self, redis_client, action: str = "LOGIN_EVISA"):
        self.r = redis_client
        self.action = action
        self.key = token_pool_key(action)

    def push(self, token: str, *, solved_at: float | None = None) -> bool:
        if not token:
            return False
        self.r.rpush(self.key, json.dumps({"t": token, "ts": solved_at if solved_at is not None else time.time()}))
        return True

    def acquire(self, max_age: float = DEFAULT_MAX_AGE) -> str | None:
        """Pop the oldest STILL-FRESH token, discarding any expired ones encountered. Returns None if
        the pool is empty or holds only stale tokens. lpop is atomic, so two workers never get the
        same token."""
        now = time.time()
        while True:
            raw = self.r.lpop(self.key)
            if raw is None:
                return None
            try:
                d = json.loads(raw)
            except Exception:
                continue   # corrupt entry -- drop and keep going
            if now - float(d.get("ts", 0)) <= max_age and d.get("t"):
                return d["t"]
            # else stale: discard and try the next

    def depth(self) -> int:
        try:
            return int(self.r.llen(self.key))
        except Exception:
            return 0

    def prune(self, max_age: float = DEFAULT_MAX_AGE) -> int:
        """Drop stale tokens from the FRONT (oldest). FIFO means once the front is fresh the rest are
        fresher, so this stops at the first fresh entry. Returns how many were pruned."""
        now = time.time()
        pruned = 0
        while True:
            raw = self.r.lindex(self.key, 0)
            if raw is None:
                break
            try:
                d = json.loads(raw)
            except Exception:
                self.r.lpop(self.key)
                pruned += 1
                continue
            if now - float(d.get("ts", 0)) > max_age:
                self.r.lpop(self.key)
                pruned += 1
            else:
                break
        return pruned


def fill_pool(pool: TokenPool, solve_fn: Callable[[], str], *, target: int,
              max_age: float = DEFAULT_MAX_AGE, batch: int = 4,
              log: Callable[[str], None] = lambda _m: None) -> int:
    """Top `pool` up toward `target` fresh tokens. Prunes stale first, then solves the deficit (up to
    `batch` concurrently per call -- the factory loop calls this repeatedly) and pushes each.

    `solve_fn() -> token|""` is injected so this is testable without a real solver and so the caller
    owns the keys/action. Returns the number of tokens added. Never raises -- a solver failure just
    adds fewer tokens this cycle."""
    pool.prune(max_age)
    deficit = target - pool.depth()
    if deficit <= 0:
        return 0
    n = min(deficit, max(1, batch))
    added = 0
    try:
        with _cf.ThreadPoolExecutor(max_workers=n) as ex:
            for fut in _cf.as_completed([ex.submit(solve_fn) for _ in range(n)]):
                try:
                    tok = fut.result()
                except Exception as e:   # one solver failure must not sink the batch
                    log(f"token_factory: solve failed ({str(e)[:80]})")
                    tok = ""
                if tok and pool.push(tok):
                    added += 1
    except Exception as e:
        log(f"token_factory: fill cycle failed ({str(e)[:80]})")
    if added:
        log(f"token_factory: +{added} token(s) for {pool.action} (depth now {pool.depth()}/{target})")
    return added


def run_token_factory(redis_client, solve_fn: Callable[[], str], *, stop_event,
                      action: str = "LOGIN_EVISA", target: int = 8, interval: float = 5.0,
                      max_age: float = DEFAULT_MAX_AGE, batch: int = 4,
                      log: Callable[[str], None] = lambda _m: None) -> None:
    """Background loop: keep `action`'s pool topped up to `target` fresh tokens. Runs until
    `stop_event` is set. Each cycle prunes stale and solves the deficit; a quiet pool (full) solves
    nothing, so cost tracks demand."""
    pool = TokenPool(redis_client, action)
    log(f"token_factory: started for {action} (target={target}, interval={interval}s)")
    while not stop_event.is_set():
        try:
            fill_pool(pool, solve_fn, target=target, max_age=max_age, batch=batch, log=log)
        except Exception as e:
            log(f"token_factory: cycle error ({str(e)[:100]})")
        stop_event.wait(interval)
    log(f"token_factory: stopped for {action}")

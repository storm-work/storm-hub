"""Shared pre-solved reCAPTCHA token pool -- the login throughput lever (2026-10-01)."""
import time

import fakeredis
import pytest

from libs.bus.token_pool import TokenPool, fill_pool, run_token_factory


def _r():
    return fakeredis.FakeStrictRedis(decode_responses=True)


def test_push_acquire_is_fifo_and_atomic():
    p = TokenPool(_r())
    p.push("A"); p.push("B"); p.push("C")
    assert p.depth() == 3
    assert [p.acquire(), p.acquire(), p.acquire()] == ["A", "B", "C"]   # oldest first
    assert p.acquire() is None and p.depth() == 0                        # empty -> None


def test_acquire_discards_stale_tokens():
    p = TokenPool(_r())
    p.push("old", solved_at=time.time() - 500)   # expired
    p.push("fresh")
    assert p.acquire(max_age=100) == "fresh"      # the stale one is dropped, not returned
    assert p.depth() == 0


def test_acquire_returns_none_when_only_stale():
    p = TokenPool(_r())
    p.push("x", solved_at=time.time() - 999)
    assert p.acquire(max_age=100) is None


def test_prune_drops_stale_front_and_stops_at_fresh():
    p = TokenPool(_r())
    p.push("s1", solved_at=time.time() - 500)
    p.push("s2", solved_at=time.time() - 400)
    p.push("fresh")
    assert p.prune(max_age=100) == 2
    assert p.depth() == 1 and p.acquire() == "fresh"


def test_push_ignores_empty_token():
    p = TokenPool(_r())
    assert p.push("") is False and p.depth() == 0


def test_fill_pool_solves_only_the_deficit():
    p = TokenPool(_r())
    p.push("have1"); p.push("have2")
    solves = {"n": 0}
    def solve():
        solves["n"] += 1
        return f"tok{solves['n']}"
    added = fill_pool(p, solve, target=5, batch=10)   # deficit 3
    assert added == 3 and solves["n"] == 3 and p.depth() == 5


def test_fill_pool_noop_when_full():
    p = TokenPool(_r())
    for i in range(5):
        p.push(f"t{i}")
    calls = {"n": 0}
    def solve():
        calls["n"] += 1; return "x"
    assert fill_pool(p, solve, target=5) == 0 and calls["n"] == 0


def test_fill_pool_tolerates_solver_failures():
    p = TokenPool(_r())
    seq = iter(["", "good", None])
    def solve():
        v = next(seq)
        if v is None:
            raise RuntimeError("solver down")
        return v
    added = fill_pool(p, solve, target=3, batch=3)
    assert added == 1 and p.acquire() == "good"   # only the one good token landed


def test_run_token_factory_fills_then_stops():
    import threading
    r = _r()
    stop = threading.Event()
    def solve():
        return "T"
    t = threading.Thread(target=run_token_factory,
                         kwargs=dict(redis_client=r, solve_fn=solve, stop_event=stop,
                                     target=3, interval=0.01))
    t.start()
    try:
        deadline = time.time() + 3
        while TokenPool(r).depth() < 3 and time.time() < deadline:
            time.sleep(0.02)
        assert TokenPool(r).depth() >= 3
    finally:
        stop.set(); t.join(timeout=2)

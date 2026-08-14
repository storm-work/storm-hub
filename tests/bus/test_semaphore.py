import threading
import time

import fakeredis
import pytest

from libs.bus.names import semaphore_key
from libs.bus.semaphore import RedisSemaphore, make_gate


@pytest.fixture()
def r():
    return fakeredis.FakeStrictRedis(decode_responses=True)


def test_acquire_up_to_limit_then_rejects(r):
    sem = RedisSemaphore(r, "sem:test", limit=2, ttl=5.0)
    assert sem.acquire("w1") is True
    assert sem.acquire("w2") is True
    assert sem.acquire("w3") is False
    assert sem.current_count() == 2


def test_acquire_is_idempotent_for_existing_holder(r):
    sem = RedisSemaphore(r, "sem:test", limit=1, ttl=5.0)
    assert sem.acquire("w1") is True
    assert sem.acquire("w1") is True   # re-acquiring the same holder never overshoots the limit
    assert sem.current_count() == 1


def test_release_frees_a_slot(r):
    sem = RedisSemaphore(r, "sem:test", limit=1, ttl=5.0)
    sem.acquire("w1")
    assert sem.acquire("w2") is False
    sem.release("w1")
    assert sem.acquire("w2") is True


def test_expired_holder_self_heals_without_release(r):
    sem = RedisSemaphore(r, "sem:test", limit=1, ttl=0.05)
    sem.acquire("w1")
    assert sem.acquire("w2") is False
    time.sleep(0.1)   # w1's TTL elapses -- no release() call, just time passing
    assert sem.acquire("w2") is True   # self-healed, no explicit cleanup needed


def test_refresh_extends_holder_and_returns_false_if_pruned(r):
    sem = RedisSemaphore(r, "sem:test", limit=1, ttl=0.2)
    sem.acquire("w1")
    assert sem.refresh("w1") is True
    time.sleep(0.3)
    assert sem.refresh("w1") is False   # already pruned -- caller must re-acquire


def test_make_gate_uses_expected_key_pattern(r):
    gate = make_gate(r, booking_id="bk1", name="login", limit=5)
    assert gate.key == semaphore_key("login", "bk1")


def test_backstop_ttl_set_on_the_key(r):
    sem = RedisSemaphore(r, "sem:test", limit=1, ttl=5.0, backstop_ttl=100.0)
    sem.acquire("w1")
    ttl = r.ttl("sem:test")
    assert 0 < ttl <= 100


def test_concurrent_acquire_never_exceeds_limit(r):
    # The core correctness property the Lua script exists for: a naive ZADD+ZCARD pair has a TOCTOU
    # race under concurrent acquire; this must never overshoot the limit regardless of thread timing.
    sem = RedisSemaphore(r, "sem:race", limit=5, ttl=30.0)
    results: list[bool] = []
    lock = threading.Lock()

    def worker(i):
        ok = sem.acquire(f"w{i}")
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 5   # exactly 5 of the 50 concurrent attempts succeed
    assert sem.current_count() == 5

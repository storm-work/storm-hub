"""A reply must reach the call that asked for it, and no other (risk F17).

The vendored session is a threaded command queue: the caller pushes a command and blocks on the
result queue. Page operations inside the worker thread scale with NAV_TIMEOUT_SCALE; the caller-side
waits mostly did not -- measured 2026-08-20, 2 of 12 entry points scaled, while the worker templates
run NAV_TIMEOUT_SCALE=4.

So under a slow portal the caller abandoned a call that was still running, the thread finished, and
its reply was handed to whatever asked next. Untagged, that is undetectable. It is why portal
slowness produced state corruption rather than merely waiting, and it is the documented root cause
of several "inexplicable" JS/state errors in the predecessor codebase.

These exercise the queue/correlation machinery directly, with no browser: PlaywrightSession's
__init__ launches Playwright, so the helpers are driven on a bare instance.
"""
from __future__ import annotations

import queue
import threading
import time

import pytest

from libs.engine.vendor.session import PlaywrightSession, _QUEUE_MARGIN, _StampedResultQueue


class _Bare(PlaywrightSession):
    """A session with only the queue plumbing -- no Playwright, no browser."""

    def __init__(self):
        self._cmd_q = queue.Queue()
        self._seq_lock = threading.Lock()
        self._next_seq = 0
        self._current_seq = 0
        self._res_q = _StampedResultQueue(self)

    def serve(self, payload, *, seq=None):
        """Stand in for the worker thread: reply to a command as it would."""
        cmd = self._cmd_q.get_nowait()
        self._current_seq = seq if seq is not None else cmd.get("_seq", 0)
        self._res_q.put(payload)
        return cmd


def test_a_reply_reaches_the_call_that_asked_for_it():
    s = _Bare()
    seq = s._send({"action": "nav"})
    s.serve(("ok", "https://example/final"))
    assert s._await(seq, 1) == ("ok", "https://example/final")


def test_sequence_numbers_are_unique_and_travel_with_the_command():
    s = _Bare()
    a = s._send({"action": "one"})
    b = s._send({"action": "two"})
    assert a != b
    assert s._cmd_q.get_nowait()["_seq"] == a
    assert s._cmd_q.get_nowait()["_seq"] == b


def test_a_late_reply_is_never_handed_to_the_next_caller():
    """The exact F17 failure: call A times out, its reply lands, then call B asks."""
    s = _Bare()

    seq_a = s._send({"action": "slow"})
    with pytest.raises(queue.Empty):
        s._await(seq_a, -_QUEUE_MARGIN)          # A gives up before any reply

    s.serve(("ok", "A's LATE result"), seq=seq_a)  # the abandoned thread finishes

    seq_b = s._send({"action": "next"})
    with pytest.raises(queue.Empty):
        # B must NOT receive A's result. With nothing of its own to collect, it times out.
        s._await(seq_b, 0.15)


def test_the_stale_reply_is_discarded_so_the_right_one_still_arrives():
    """Discarding must not also lose the correct reply queued behind it."""
    s = _Bare()
    seq_a = s._send({"action": "abandoned"})
    seq_b = s._send({"action": "wanted"})

    s._cmd_q.get_nowait(); s._cmd_q.get_nowait()
    s._current_seq = seq_a; s._res_q.put(("ok", "stale"))
    s._current_seq = seq_b; s._res_q.put(("ok", "wanted"))

    assert s._await(seq_b, 1) == ("ok", "wanted")


def test_the_caller_outlives_the_worker_by_the_queue_margin():
    """A reply arriving just past the work's own budget must still be collected, not abandoned --
    that margin is what stops the desync in the first place."""
    s = _Bare()
    seq = s._send({"action": "slow"})

    def reply_late():
        time.sleep(0.4)
        s._cmd_q.get_nowait()
        s._current_seq = seq
        s._res_q.put(("ok", "just in time"))

    t = threading.Thread(target=reply_late, daemon=True); t.start()
    started = time.monotonic()
    assert s._await(seq, 0.05) == ("ok", "just in time")   # 0.05 budget + 5s margin
    assert time.monotonic() - started < _QUEUE_MARGIN + 1
    t.join(timeout=2)


def test_timeout_still_raises_queue_Empty():
    """Every call site keeps its own `except queue.Empty` message -- the contract must not change."""
    s = _Bare()
    seq = s._send({"action": "never answered"})
    with pytest.raises(queue.Empty):
        s._await(seq, -_QUEUE_MARGIN)


def test_drain_empties_the_queue_without_raising():
    s = _Bare()
    s._current_seq = 99
    s._res_q.put(("ok", 1)); s._res_q.put(("ok", 2))
    s._drain_replies()
    with pytest.raises(queue.Empty):
        s._res_q.get_nowait()
    s._drain_replies()      # already empty: must be a no-op, not an error

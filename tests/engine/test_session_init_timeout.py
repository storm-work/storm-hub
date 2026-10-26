"""F49 (run 1023): a PlaywrightSession whose init times out must not orphan its Chromium.

The caller never receives the object, so nothing would ever close() it. On run 1023 that was x200
inits, 210 orphaned browsers and ~1,065 chrome processes -- the host-overload amplifier."""
import threading

import pytest

import libs.engine.vendor.session as session


class _NeverReady(threading.Event):
    def wait(self, timeout=None):
        # Simulate the 120 s init deadline passing -- after a beat, so the (fake) worker thread has
        # recorded its driver pid, as a real one does right after Playwright starts.
        super().wait(0.2)
        return False


def test_init_timeout_force_kills_the_driver_tree(monkeypatch):
    killed = []
    monkeypatch.setattr(session, "_kill_proc_tree", lambda pid: killed.append(pid))
    monkeypatch.setattr(session.threading, "Event", _NeverReady)

    def _hung_worker(self):
        self._driver_pid = 4242          # Playwright started, then launch/bootstrap hung
    monkeypatch.setattr(session.PlaywrightSession, "_worker", _hung_worker)

    with pytest.raises(RuntimeError, match="init timed out"):
        session.PlaywrightSession(proxy=None)
    assert killed == [4242]


def test_init_timeout_before_playwright_started_kills_nothing(monkeypatch):
    killed = []
    monkeypatch.setattr(session, "_kill_proc_tree", lambda pid: killed.append(pid))
    monkeypatch.setattr(session.threading, "Event", _NeverReady)
    monkeypatch.setattr(session.PlaywrightSession, "_worker", lambda self: None)

    with pytest.raises(RuntimeError, match="init timed out"):
        session.PlaywrightSession(proxy=None)
    assert killed == [None]              # _kill_proc_tree(None) is a no-op by contract


def test_the_init_deadline_covers_the_bootstrap_at_the_configured_nav_scale(monkeypatch):
    """F4 rule: a caller deadline below the callee's worst case abandons live work. At production's
    NAV_TIMEOUT_SCALE=4 the bootstrap navs alone can take ~290 s; the old fixed 120 s killed 12
    browser starts on 2026-10-26."""
    seen = []

    class _Rec(threading.Event):
        def wait(self, timeout=None):
            if timeout is not None:          # only the init deadline waits with a timeout
                seen.append(timeout)
            return False
    monkeypatch.setattr(session, "_kill_proc_tree", lambda pid: None)
    monkeypatch.setattr(session.threading, "Event", _Rec)
    monkeypatch.setattr(session.PlaywrightSession, "_worker", lambda self: None)
    monkeypatch.setenv("NAV_TIMEOUT_SCALE", "4")
    with pytest.raises(RuntimeError, match=r"init timed out \(>310s\)"):
        session.PlaywrightSession(proxy=None)
    assert seen == [310.0]
    monkeypatch.setenv("NAV_TIMEOUT_SCALE", "1")
    seen.clear()
    with pytest.raises(RuntimeError, match=r"\(>120s\)"):
        session.PlaywrightSession(proxy=None)
    assert seen == [120.0], "scale 1 keeps the old 120 s floor"

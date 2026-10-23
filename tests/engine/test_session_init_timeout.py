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

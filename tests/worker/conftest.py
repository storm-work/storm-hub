import pytest

from apps.worker.ops_live import LiveEngineOps


@pytest.fixture(autouse=True)
def _no_warm_restart_pause(monkeypatch):
    """A failing warm-up now rolls back to the Questionario up to WARMUP_CHAIN_RESTARTS times with a
    growing real-time pause between passes (5 s, 10 s, 15 s). The page fakes fail warm-ups on
    purpose, so without this every such test would sleep ~30 s. The pause itself is not under test."""
    monkeypatch.setattr(LiveEngineOps, "_restart_pause", lambda self, n: 0.0)


@pytest.fixture(autouse=True)
def _no_real_browser(monkeypatch):
    """Rebinding a login to a rolled exit opens a NEW browser (ops_live.get_session). A unit test
    must never launch real Chromium/Donut; tests that model a rebind patch get_session themselves."""
    import apps.worker.ops_live as ol

    def _refuse(**kw):
        raise RuntimeError("unit tests do not launch real browsers")
    monkeypatch.setattr(ol, "get_session", _refuse)

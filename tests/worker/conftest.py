import pytest

from apps.worker.ops_live import LiveEngineOps


@pytest.fixture(autouse=True)
def _no_warm_restart_pause(monkeypatch):
    """A failing warm-up now rolls back to the Questionario up to WARMUP_CHAIN_RESTARTS times with a
    growing real-time pause between passes (5 s, 10 s, 15 s). The page fakes fail warm-ups on
    purpose, so without this every such test would sleep ~30 s. The pause itself is not under test."""
    monkeypatch.setattr(LiveEngineOps, "_restart_pause", lambda self, n: 0.0)

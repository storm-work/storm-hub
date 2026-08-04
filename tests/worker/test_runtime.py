"""Worker runtime tests — the phase machine driven through the bus (fakeredis), no browser."""
import fakeredis
import pytest

from apps.worker.ops_sim import SimEngineOps
from apps.worker.runtime import WorkerRuntime
from libs.bus.bus import Bus
from libs.domain.enums import SettingsLayer as L
from libs.domain.settings import resolve
from libs.engine.config import EngineConfig


def _cfg(booking=None, template=None):
    return EngineConfig.from_settings(resolve({L.BOOKING: booking or {}, L.TEMPLATE: template or {}}))


@pytest.fixture()
def bus():
    return Bus(fakeredis.FakeStrictRedis(decode_responses=True))


def _events(bus):
    return bus.read_events(last_id="0", block_ms=0)


def test_apply_worker_emits_started_status_and_booked(bus):
    rt = WorkerRuntime(bus, "w1")
    res = rt.run(_cfg(template={"ROLE": "apply"}), SimEngineOps())
    assert res.outcome == "booked"
    evs = _events(bus)
    types = [e.type for e in evs]
    assert types[0] == "started"
    assert types[-1] == "terminal"
    assert evs[-1].payload["outcome"] == "booked"
    # a status_change reached 'booked'
    substates = [e.payload.get("substate") for e in evs if e.type == "status_change"]
    assert "logging_in" in substates and "booked" in substates
    assert all(e.payload["worker_id"] == "w1" for e in evs)


def test_scout_worker_pushes_and_terminal_discovered(bus):
    ops = SimEngineOps()
    res = WorkerRuntime(bus, "s1").run(_cfg(template={"ROLE": "scout"}), ops)
    assert res.outcome == "discovered"
    assert ops.pushed == [{"date": "2026-08-18", "period": 4}]
    assert _events(bus)[-1].payload["outcome"] == "discovered"


def test_no_book_terminal_dry_run(bus):
    res = WorkerRuntime(bus, "w2").run(
        _cfg(booking={"no_book": True}, template={"ROLE": "apply"}), SimEngineOps()
    )
    assert res.outcome == "dry_run"
    assert _events(bus)[-1].payload["outcome"] == "dry_run"


def test_login_failure_terminal_failed(bus):
    res = WorkerRuntime(bus, "w3").run(_cfg(template={"ROLE": "apply"}), SimEngineOps(login_ok=False))
    assert res.outcome == "failed"
    term = _events(bus)[-1]
    assert term.type == "terminal" and term.payload["outcome"] == "failed"

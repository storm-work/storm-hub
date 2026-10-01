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
    res = rt.run(_cfg(template={"ROLE": "apply"}), SimEngineOps(assignment={"date": "d", "period": 3}))
    assert res.outcome == "booked"
    evs = _events(bus)
    assert evs[0].type == "started" and evs[-1].type == "terminal"
    assert evs[-1].payload["outcome"] == "booked"
    substates = [e.payload.get("substate") for e in evs if e.type == "status_change"]
    assert "logging_in" in substates and "booked" in substates
    assert all(e.payload["worker_id"] == "w1" for e in evs)


def test_scout_worker_pushes_and_terminal_discovered(bus):
    ops = SimEngineOps()   # defaults: single task ["2026-08-18"] -> found period 4, then stops
    res = WorkerRuntime(bus, "s1").run(_cfg(template={"ROLE": "scout"}), ops)
    assert res.outcome == "discovered"
    assert ops.pushed == [{"date": "2026-08-18", "period": 4}]
    assert _events(bus)[-1].payload["outcome"] == "discovered"


def test_no_book_terminal_dry_run(bus):
    res = WorkerRuntime(bus, "w2").run(
        _cfg(booking={"no_book": True}, template={"ROLE": "apply"}),
        SimEngineOps(assignment={"date": "d", "period": 1}),
    )
    assert res.outcome == "dry_run"
    assert _events(bus)[-1].payload["outcome"] == "dry_run"


def test_login_failure_terminal_failed(bus):
    res = WorkerRuntime(bus, "w3").run(_cfg(template={"ROLE": "apply"}), SimEngineOps(login_ok=False))
    assert res.outcome == "failed"
    term = _events(bus)[-1]
    assert term.type == "terminal" and term.payload["outcome"] == "failed"


class _CheckpointingOps(SimEngineOps):
    """SimEngineOps + a save_checkpoint_now spy -- mirrors LiveEngineOps having that method while
    SimEngineOps (used above) deliberately doesn't, so those tests double as proof the hook is a
    no-op for ops that don't support checkpointing."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self.checkpoint_calls = 0

    def save_checkpoint_now(self):
        self.checkpoint_calls += 1


def test_checkpoint_saved_on_reaching_holding(bus):
    """Regression (C1): save_checkpoint_now() previously had zero callers anywhere -- the entire
    restore chain was dead code. It must fire at least once when a worker first reaches a healthy
    holding state, so a later respawn can restore instead of paying a full fresh login."""
    ops = _CheckpointingOps(assignment={"date": "d", "period": 1})
    WorkerRuntime(bus, "w4").run(_cfg(template={"ROLE": "apply"}), ops)
    assert ops.checkpoint_calls >= 1


def test_checkpoint_not_saved_for_ops_without_support(bus):
    ops = SimEngineOps(assignment={"date": "d", "period": 1})
    # must not raise even though SimEngineOps has no save_checkpoint_now
    res = WorkerRuntime(bus, "w5").run(_cfg(template={"ROLE": "apply"}), ops)
    assert res.outcome == "booked"


def test_checkpoint_saved_repeatedly_during_scout_polling(bus):
    ops = _CheckpointingOps()   # default scout plan: one discover round then stop
    WorkerRuntime(bus, "s2").run(_cfg(template={"ROLE": "scout"}), ops)
    # HOLDING (pre-discover) + POLLING (post-discover) -> at least 2 saves for one round
    assert ops.checkpoint_calls >= 2


def test_timestamped_stream_prefixes_each_line():
    """Worker log lines (incl. the vendored [session] prints) are prefixed with a UTC timestamp so a
    log file is timeline-readable (2026-09-26). Partial writes stay on one line."""
    import io
    import re

    from apps.worker.__main__ import _TimestampedStream

    buf = io.StringIO()
    s = _TimestampedStream(buf)
    s.write("[session] one\n")
    s.write("part "); s.write("two\n")          # one logical line across two writes
    s.write("a\nb\n")                            # two lines in one write
    lines = [l for l in buf.getvalue().split("\n") if l]

    assert all(re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d\d\dZ ", l) for l in lines), lines
    assert any("part two" in l for l in lines), "a partial write must stay on one line"
    assert len(lines) == 4

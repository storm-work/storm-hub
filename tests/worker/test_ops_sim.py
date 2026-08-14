"""SimEngineOps -- the bus-wired wait loop must survive multiple timeout windows and heartbeat
while it waits, same contract as LiveEngineOps (see test_ops_live.py's keepalive tests), just with
no real portal session to ping."""
from apps.worker.bus_signals import TIMEOUT
from apps.worker.ops_sim import SimEngineOps


class _FakeSignals:
    """wait_slot_open/wait_assignment return TIMEOUT/"timeout" `timeouts` times, then `final`. See
    test_ops_live.py's twin of this class for why `stop_on_final` is explicit rather than inferred."""
    def __init__(self, timeouts, final, stop_on_final=False):
        self._left = timeouts
        self._final = final
        self._stop_on_final = stop_on_final
        self.stopped = False
        self.calls = 0

    def wait_slot_open(self, timeout=900):
        self.calls += 1
        if self._left > 0:
            self._left -= 1
            return "timeout"
        if self._stop_on_final:
            self.stopped = True
        return self._final

    def wait_assignment(self, timeout=900):
        self.calls += 1
        if self._left > 0:
            self._left -= 1
            return TIMEOUT
        if self._stop_on_final:
            self.stopped = True
        return self._final


def test_await_slot_open_survives_multiple_timeout_windows_and_heartbeats():
    events = []
    ops = SimEngineOps(signals=_FakeSignals(timeouts=3, final="slot_open"), emit=lambda t, **p: events.append(t))
    assert ops.await_slot_open() == "slot_open"
    assert ops._signals.calls == 4
    assert events.count("log_line") == 3   # one heartbeat per timeout window, none after the real result


def test_await_slot_open_stop_returns_immediately():
    ops = SimEngineOps(signals=_FakeSignals(timeouts=0, final="stop", stop_on_final=True))
    assert ops.await_slot_open() == "stop"
    assert ops.is_stopped() is True


def test_await_assignment_survives_multiple_timeout_windows():
    ops = SimEngineOps(signals=_FakeSignals(timeouts=2, final={"date": "d", "period": 1}))
    assert ops.await_assignment() == {"date": "d", "period": 1}
    assert ops._signals.calls == 3


def test_await_assignment_null_slot_not_swallowed_as_timeout():
    """Regression (N1): see test_ops_live.py's twin test -- the self-scout fallback signal
    (`assign slot=None, scouts_invalid=True`) is a real command, not a timeout."""
    ops = SimEngineOps(signals=_FakeSignals(timeouts=0, final=None))
    assert ops.await_assignment() is None
    assert ops._signals.calls == 1


def test_await_slot_open_without_signals_returns_immediately():
    ops = SimEngineOps()   # no bus wired -- unit-test mode
    assert ops.await_slot_open() == "slot_open"
    assert ops.is_stopped() is False

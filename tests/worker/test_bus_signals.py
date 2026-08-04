import fakeredis

from apps.worker.bus_signals import BusSignals
from libs.bus.bus import Bus


def _bus():
    return Bus(fakeredis.FakeStrictRedis(decode_responses=True))


def test_wait_launch_returns_cfg():
    bus = _bus()
    bus.send_command("w1", "launch", cfg={"ROLE": "apply"})
    sig = BusSignals(bus, "w1", "c1")
    assert sig.wait_launch(timeout=2) == {"ROLE": "apply"}


def test_wait_slot_open_blocks_until_signal():
    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    # nothing yet -> short timeout returns 'timeout'
    assert sig.wait_slot_open(timeout=0.2) == "timeout"
    bus.send_command("w1", "signal", name="slot_open")
    assert sig.wait_slot_open(timeout=2) == "slot_open"


def test_wait_assignment_returns_slot():
    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    bus.send_command("w1", "assign", slot={"id": "s1", "date": "2026-08-18", "period": 4})
    assert sig.wait_assignment(timeout=2) == {"id": "s1", "date": "2026-08-18", "period": 4}


def test_stop_aborts_waits():
    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    bus.send_command("w1", "stop")
    assert sig.wait_slot_open(timeout=2) == "stop"
    assert sig.stopped is True

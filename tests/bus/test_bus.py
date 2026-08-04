"""Bus tests over fakeredis (no server needed) — commands, events, and signals."""
import json

import fakeredis
import pytest

from libs.bus.bus import Bus


@pytest.fixture()
def bus():
    return Bus(fakeredis.FakeStrictRedis(decode_responses=True))


def test_command_roundtrip_and_ack(bus):
    bus.send_command("w1", "launch", cfg={"role": "apply"})
    msgs = bus.read_commands("w1", "consumer-a", block_ms=0)
    assert len(msgs) == 1
    m = msgs[0]
    assert m.type == "launch"
    assert m.payload["cfg"]["role"] == "apply"
    bus.ack_command("w1", m.id)
    # after ack + already-delivered, a fresh ">" read returns nothing new
    assert bus.read_commands("w1", "consumer-a", block_ms=0) == []


def test_commands_are_per_worker(bus):
    bus.send_command("w1", "stop")
    bus.send_command("w2", "pause")
    assert [m.type for m in bus.read_commands("w1", "c", block_ms=0)] == ["stop"]
    assert [m.type for m in bus.read_commands("w2", "c", block_ms=0)] == ["pause"]


def test_event_stream_roundtrip(bus):
    bus.emit_event("w1", "status_change", substate="warming_up")
    bus.emit_event("w1", "terminal", outcome="booked")
    evs = bus.read_events(last_id="0", block_ms=0)
    assert [e.type for e in evs] == ["status_change", "terminal"]
    assert evs[0].payload["worker_id"] == "w1"
    assert evs[0].payload["substate"] == "warming_up"
    assert evs[1].payload["outcome"] == "booked"


def test_signal_pubsub(bus):
    ps = bus.subscribe_signal("bk1")
    # drain the subscribe confirmation
    ps.get_message(timeout=0.1)
    bus.publish_signal("bk1", "slot_open", at="11:00")
    msg = None
    for _ in range(5):
        msg = ps.get_message(timeout=0.1)
        if msg and msg.get("type") == "message":
            break
    assert msg and msg["type"] == "message"
    body = json.loads(msg["data"])
    assert body["name"] == "slot_open" and body["at"] == "11:00"

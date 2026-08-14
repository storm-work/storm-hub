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


def test_command_stream_is_capped_maxlen(bus):
    # Phase F1: the confirmed incident's mitigation -- a runaway loop pushing hundreds of commands
    # must not be able to grow a single worker's stream without bound.
    from libs.bus.bus import CMD_STREAM_MAXLEN
    from libs.bus.names import CMD_STREAM
    for i in range(CMD_STREAM_MAXLEN + 500):
        bus.send_command("w1", "signal", name=f"n{i}")
    length = bus.r.xlen(CMD_STREAM("w1"))
    # approximate trimming (~) may leave a small margin above the exact cap, but must never approach
    # the full 700 writes that were actually sent.
    assert length < CMD_STREAM_MAXLEN * 2


def test_event_stream_is_capped_maxlen(bus):
    from libs.bus.bus import EVENT_STREAM_MAXLEN
    from libs.bus.names import EVENT_STREAM
    for i in range(200):
        bus.emit_event("w1", "log_line", text=f"line {i}")
    length = bus.r.xlen(EVENT_STREAM)
    assert length <= 200   # well under EVENT_STREAM_MAXLEN, just confirms no growth-prevention bug


def test_has_pending_commands_lifecycle(bus):
    # Phase F2: never send a worker another command of the same kind while one is still
    # outstanding. Caught live during Phase E validation: _drive_scouts was resending `discover`
    # every tick to a worker still blocked on await_slot_open, piling up unconsumed commands --
    # exactly the confirmed 2026-08-07 bug's shape, in new code, until this existed.
    assert bus.has_pending_commands("w1") is False   # no group/stream yet
    bus.send_command("w1", "discover", dates=["d1"])
    bus.ensure_command_group("w1")
    assert bus.has_pending_commands("w1") is False   # sent, but not yet delivered to a consumer
    msgs = bus.read_commands("w1", "c1", block_ms=0)
    assert bus.has_pending_commands("w1") is True    # delivered, not yet acked
    bus.ack_command("w1", msgs[0].id)
    assert bus.has_pending_commands("w1") is False   # acked -- clear to send another


def test_delete_worker_stream_removes_it(bus):
    bus.send_command("w1", "launch", cfg={})
    assert bus.r.exists("cmd:w1")
    bus.delete_worker_stream("w1")
    assert not bus.r.exists("cmd:w1")


def test_delete_worker_stream_missing_is_noop(bus):
    bus.delete_worker_stream("never-existed")   # must not raise


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

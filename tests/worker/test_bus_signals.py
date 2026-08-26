import fakeredis

from apps.worker.bus_signals import TIMEOUT, BusSignals
from libs.bus.bus import Bus


def _bus():
    return Bus(fakeredis.FakeStrictRedis(decode_responses=True))


def test_wait_launch_returns_cfg():
    bus = _bus()
    bus.send_command("w1", "launch", cfg={"ROLE": "apply"})
    sig = BusSignals(bus, "w1", "c1")
    assert sig.wait_launch(timeout=2) == {"ROLE": "apply"}


def test_wait_launch_poisoned_by_stale_stop_ahead_of_launch_in_the_same_batch():
    """Confirmed root cause (2026-08-13) of "I click start, no workers start": stop_worker/
    stop_booking/restart_worker used to leave a worker's just-sent "stop" command sitting
    unconsumed in its stream. On the very next relaunch, the freshly-spawned process's very first
    wait_launch() call drains the WHOLE batch and returns on the FIRST match in stream order -- a
    stale "stop" ahead of "launch" makes it treat itself as told-to-stop and exit immediately,
    never even looking at its own launch payload. This test documents/locks in that exact
    mechanism; the actual fix is that apps/hub/main.py now calls Bus.delete_worker_stream() before
    ever sending a fresh "launch" to a worker that was previously stopped (see
    test_dashboard_gaps.py's stop_worker/stop_booking/restart_worker/start_booking tests)."""
    bus = _bus()
    bus.send_command("w1", "stop")             # never consumed by the (now-dead) previous process
    bus.send_command("w1", "launch", cfg={"ROLE": "apply"})   # the NEXT relaunch's command
    sig = BusSignals(bus, "w1", "c1")

    assert sig.wait_launch(timeout=2) is None   # poisoned: never reaches the launch payload
    assert sig.stopped is True


def test_wait_launch_clean_after_delete_worker_stream():
    """The actual fix: clearing the stream before the next launch removes the stale stop entirely,
    so the relaunch's wait_launch() sees only the fresh launch command."""
    bus = _bus()
    bus.send_command("w1", "stop")
    bus.delete_worker_stream("w1")
    bus.send_command("w1", "launch", cfg={"ROLE": "apply"})
    sig = BusSignals(bus, "w1", "c1")

    assert sig.wait_launch(timeout=2) == {"ROLE": "apply"}
    assert sig.stopped is False


def test_wait_slot_open_blocks_until_signal():
    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    bus.send_command("w1", "signal", name="slot_open")
    assert sig.wait_slot_open(timeout=2) == "slot_open"


def test_wait_slot_open_returns_timeout_after_one_window():
    """BusSignals itself is bounded per-window again -- looping-until-stop moved up to the ops layer
    (LiveEngineOps/SimEngineOps's await_slot_open), which has a real keepalive action to perform
    between windows instead of blocking silently forever. See test_ops_live.py/test_ops_sim.py for
    the "never gives up, and pings the portal while waiting" behavior this enables."""
    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    assert sig.wait_slot_open(timeout=0.05) == "timeout"


def test_wait_assignment_returns_slot():
    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    bus.send_command("w1", "assign", slot={"id": "s1", "date": "2026-08-18", "period": 4})
    assert sig.wait_assignment(timeout=2) == {"id": "s1", "date": "2026-08-18", "period": 4}


def test_wait_assignment_returns_timeout_sentinel_after_one_window():
    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    assert sig.wait_assignment(timeout=0.05) is TIMEOUT


def test_wait_assignment_null_slot_is_a_real_result_not_a_timeout():
    """The self-scout fallback signal is `assign slot=None, scouts_invalid=True` -- a real command
    whose slot happens to be None. Must return immediately, not be confused with TIMEOUT."""
    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    bus.send_command("w1", "assign", slot=None, scouts_invalid=True)
    assert sig.wait_assignment(timeout=2) is None
    assert sig.scouts_invalid() is True


def test_drain_acks_entire_batch_even_when_caller_returns_early():
    """Regression (X1): a command batched behind the one the caller is looking for must still be
    acked -- otherwise it's lost forever (a fresh XREADGROUP '>' never redelivers an
    already-delivered entry) and sits in the consumer group's PEL permanently, poisoning
    Bus.has_pending_commands() for that worker from then on (which in turn made the scheduler's
    dedup-before-send guard permanently skip sending it any future scout/slot assignment)."""
    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    # Both land in the same fetch: an irrelevant "discover" first, then the "signal" this call
    # actually wants. wait_slot_open returns as soon as it sees the signal, without ever reaching
    # the discover message in the loop.
    bus.send_command("w1", "discover", dates=["2026-08-18"])
    bus.send_command("w1", "signal", name="slot_open")
    assert sig.wait_slot_open(timeout=2) == "slot_open"
    assert bus.has_pending_commands("w1") is False


def test_stop_aborts_waits():
    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    bus.send_command("w1", "stop")
    assert sig.wait_slot_open(timeout=2) == "stop"
    assert sig.stopped is True


def test_wait_discover_task_returns_dates():
    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    bus.send_command("w1", "discover", dates=["2026-08-18", "2026-08-19"])
    assert sig.wait_discover_task(timeout=2) == ["2026-08-18", "2026-08-19"]


def test_wait_discover_task_stop_returns_none():
    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    bus.send_command("w1", "stop")
    assert sig.wait_discover_task(timeout=2) is None
    assert sig.stopped is True


def test_wait_discover_task_returns_inactivate_sentinel_on_inactivate_signal():
    """False-slot-open revert (operator spec 2026-08-14): distinct from stop (None) -- the scout
    must go back to holding, not terminate."""
    from libs.engine.phases import INACTIVATE

    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    bus.send_command("w1", "signal", name="inactivate")
    assert sig.wait_discover_task(timeout=2) is INACTIVATE
    assert sig.stopped is False   # NOT a stop


def test_inactivate_clears_a_stale_slot_found_seen():
    """A re-fired slot-open is a genuinely new window -- a slot_found from the PREVIOUS (now
    inactivated) window must not carry over and skip loose-hold protection in the new one."""
    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    bus.send_command("w1", "signal", name="slot_found")
    sig.wait_discover_task(timeout=1)
    assert sig.slot_found_seen is True

    bus.send_command("w1", "signal", name="inactivate")
    sig.wait_discover_task(timeout=2)
    assert sig.slot_found_seen is False


def test_take_inactivated_consumes_the_flag():
    """One revert = one transition back to holding; a stale flag must not immediately kick the scout
    out of the NEXT slot-open window too."""
    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    sig.inactivated = True
    assert sig.take_inactivated() is True
    assert sig.take_inactivated() is False


def test_wait_discover_task_stashes_special_days_when_present():
    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    cached = {"2026": {"8": [15, 16]}}
    bus.send_command("w1", "discover", dates=["2026-08-18"], special_days=cached)
    assert sig.wait_discover_task(timeout=2) == ["2026-08-18"]
    assert sig.last_discover_special_days == cached


def test_wait_discover_task_special_days_none_when_absent():
    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    bus.send_command("w1", "discover", dates=["2026-08-18"])
    assert sig.wait_discover_task(timeout=2) == ["2026-08-18"]
    assert sig.last_discover_special_days is None


def test_wait_proxy_returns_assigned_reply():
    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    bus.send_command("w1", "proxy_assigned", proxy="http://u:p@newhost:1234")
    # resource_type (Phase 5): rides alongside the proxy URL so a caller can tell a static hold
    # assignment apart from a rotating one without any DB access; None when the sender omits it
    # (e.g. an older/warmup-phase reply that never carried the field).
    assert sig.wait_proxy(timeout=2) == {
        "ok": True, "proxy": "http://u:p@newhost:1234", "resource_type": None,
    }


def test_wait_proxy_returns_unavailable_reply():
    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    bus.send_command("w1", "proxy_unavailable", reason="pool exhausted")
    assert sig.wait_proxy(timeout=2) == {"ok": False, "reason": "pool exhausted"}


def test_wait_proxy_returns_timeout_sentinel_after_one_window():
    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    assert sig.wait_proxy(timeout=0.05) is TIMEOUT


def test_wait_proxy_stop_returns_none():
    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    bus.send_command("w1", "stop")
    assert sig.wait_proxy(timeout=2) is None
    assert sig.stopped is True


def test_scouts_invalid_rides_along_on_assign():
    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    assert sig.scouts_invalid() is False   # default before any assign seen
    bus.send_command("w1", "assign", slot={"id": "s1"}, scouts_invalid=True)
    sig.wait_assignment(timeout=2)
    assert sig.scouts_invalid() is True


def test_scouts_invalid_defaults_false_when_absent():
    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    bus.send_command("w1", "assign", slot={"id": "s1"})
    sig.wait_assignment(timeout=2)
    assert sig.scouts_invalid() is False


# ── a signal delivered outside its own wait must not be lost (2026-08-14) ─────
def test_slot_open_delivered_during_warmup_is_not_lost():
    """A signal is sent to each worker ONCE: the Hub marks it notified after a confirmed send and
    _latch_signals skips it forever after. But _drain acks the whole batch regardless of which
    wait_* is running, so a slot_open arriving while the worker was still logging in used to be
    acked and dropped -- and the worker then waited in await_slot_open for a signal that had
    already been delivered and would never be re-sent. Confirmed live: scouts stuck HOLDING while
    the booking was already past slot-found."""
    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    bus.send_command("w1", "signal", name="slot_open")

    sig.wait_launch(timeout=1)              # some OTHER wait consumes the batch first
    assert sig.slot_open_seen is True

    assert sig.wait_slot_open(timeout=1) == "slot_open"   # still honoured, no re-send needed


def test_inactivate_clears_the_sticky_slot_open():
    """The false-slot-open revert ends the window; a genuinely new one must be signalled again."""
    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    bus.send_command("w1", "signal", name="slot_open")
    sig.wait_slot_open(timeout=1)
    assert sig.slot_open_seen is True

    bus.send_command("w1", "signal", name="inactivate")
    sig.wait_discover_task(timeout=1)
    assert sig.slot_open_seen is False
    assert sig.wait_slot_open(timeout=1) == "timeout"     # waits for a real new window


def test_every_signal_the_hub_sends_survives_arriving_out_of_band():
    """Guard for the whole class. _drain acks everything it reads, so ANY command type the Hub
    sends must either be recorded stickily or stashed -- otherwise it is silently destroyed when it
    arrives while a different wait_* is running. Three separate live bugs came from this
    (assign, slot_open, and the discover special_days payload)."""
    for name, check in (("slot_open", lambda s: s.slot_open_seen),
                        ("slot_found", lambda s: s.slot_found_seen),
                        ("inactivate", lambda s: s.inactivated)):
        bus = _bus()
        sig = BusSignals(bus, "w1", "c1")
        bus.send_command("w1", "signal", name=name)
        sig.wait_launch(timeout=1)          # deliberately the WRONG wait for this signal
        assert check(sig), f"{name} was destroyed by an unrelated wait_*"

    bus = _bus()
    sig = BusSignals(bus, "w1", "c1")
    bus.send_command("w1", "assign", slot={"date": "2026-09-01", "period": 3})
    sig.wait_launch(timeout=1)
    assert sig.pending_assignment == {"date": "2026-09-01", "period": 3}

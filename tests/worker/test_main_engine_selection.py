"""apps.worker.__main__._build_ops -- engine selection. _engine defaults to 'sim' (never live by
accident); 'live' constructs LiveEngineOps only when the Hub explicitly opted in via the launch
payload."""
import fakeredis
import pytest

from apps.worker.__main__ import _build_ops
from apps.worker.bus_signals import BusSignals
from apps.worker.ops_live import LiveEngineOps
from apps.worker.ops_sim import SimEngineOps
from libs.bus.bus import Bus
from libs.domain.enums import SettingsLayer as L
from libs.domain.settings import resolve
from libs.engine.config import EngineConfig


def _cfg():
    return EngineConfig.from_settings(resolve({L.TEMPLATE: {"ROLE": "apply"}}))


def _bus():
    return Bus(fakeredis.FakeStrictRedis(decode_responses=True))


def test_sim_engine_selected_by_default():
    bus = _bus()
    signals = BusSignals(bus, "w1", "c1")
    ops = _build_ops("sim", _cfg(), {}, bus, "w1", signals)
    assert isinstance(ops, SimEngineOps)


def test_live_engine_constructed_from_launch_payload():
    bus = _bus()
    signals = BusSignals(bus, "w1", "c1")
    payload = {
        "account": {"username": "u", "password": "p"},
        "posto_id": "5084",
        "nationality": "CPV",
        "residence": "IRL",
        "quest_steps": {"IRL": []},
        "form_defaults": {"static": {}, "dynamic": {}, "from_account": {}, "from_nationality": {}},
        "capsolver_keys": ["k1"],
        "warm_proxy": "http://p:1234",
        "sub_account_id": "sub-1",
        "identity_id": "identity-1",
        "booking_id": "bk-1",
    }
    ops = _build_ops("live", _cfg(), payload, bus, "w1", signals)
    assert isinstance(ops, LiveEngineOps)
    assert ops.identity_id == "identity-1"
    assert ops.booking_id == "bk-1"


def test_live_engine_receives_the_sub_account_rotation_candidates():
    """Confirmed live 2026-08-14: the Hub sent `accounts` (the full ranked sub-account list) and
    _build_ops silently dropped it, so LiveEngineOps fell back to its single-candidate default and
    do_login()'s sticky-then-rotate loop could never rotate. A worker with 3 usable sub-accounts
    terminated failed/login after 3 attempts on ONE account instead of 3x3=9."""
    bus = _bus()
    signals = BusSignals(bus, "w1", "c1")
    accounts = [
        {"username": "u1", "password": "p1", "sub_account_id": "sub-1"},
        {"username": "u2", "password": "p2", "sub_account_id": "sub-2"},
        {"username": "u3", "password": "p3", "sub_account_id": "sub-3"},
    ]
    payload = {
        "account": accounts[0], "accounts": accounts,
        "posto_id": "5084", "nationality": "CPV", "residence": "IRL",
        "quest_steps": {"IRL": []},
        "form_defaults": {"static": {}, "dynamic": {}, "from_account": {}, "from_nationality": {}},
        "capsolver_keys": ["k1"], "warm_proxy": "http://p:1234",
        "sub_account_id": "sub-1", "identity_id": "identity-1", "booking_id": "bk-1",
    }
    ops = _build_ops("live", _cfg(), payload, bus, "w1", signals)
    assert [a["sub_account_id"] for a in ops.accounts] == ["sub-1", "sub-2", "sub-3"]


def test_every_launch_payload_key_the_hub_sends_is_consumed_by_build_ops():
    """The seam guard. Both ends of sub-account rotation were unit-tested -- the Hub's
    _do_launch_worker built `accounts`, and LiveEngineOps._fresh_login rotated through it -- but
    NOTHING tested that the value actually crossed the bus into the constructor, and it didn't.
    Parses the Hub's launch_kwargs keys out of main.py and asserts _build_ops reads each one."""
    import re
    from pathlib import Path

    main_src = Path("apps/hub/main.py").read_text()
    block = re.search(r"launch_kwargs = dict\(\s*\n\s*_engine=\"live\",(.*?)\n\s*\)", main_src, re.S)
    assert block, "could not find the live launch_kwargs block in apps/hub/main.py"
    sent = set(re.findall(r"^\s*(\w+)=", block.group(1), re.M))

    build_src = Path("apps/worker/__main__.py").read_text()
    consumed = set(re.findall(r'payload(?:\.get)?[\[\(]"(\w+)"', build_src))

    missing = sent - consumed
    assert not missing, (
        f"launch payload keys the Hub sends but apps/worker/__main__.py never reads: {missing}")


def test_live_engine_log_wired_to_log_line_event():
    """Regression, confirmed live 2026-08-13: LiveEngineOps._log defaulted to a no-op and nothing
    in _build_ops ever overrode it, so every internal self._log(...) diagnostic (do_swap's own
    failure reasons among them) was silently discarded -- there was no way to find out, from any
    log the Hub retained, why one worker swapped to its hold proxy and another in the same booking
    didn't. ops._log must emit a real `log_line` event (the same type WorkerRuntime's own
    transition logging already uses, so the dashboard's per-worker log view picks it up for free)."""
    bus = _bus()
    signals = BusSignals(bus, "w1", "c1")
    payload = {
        "account": {"username": "u", "password": "p"},
        "posto_id": "5084",
        "nationality": "CPV",
        "residence": "IRL",
        "quest_steps": {"IRL": []},
        "form_defaults": {"static": {}, "dynamic": {}, "from_account": {}, "from_nationality": {}},
        "capsolver_keys": ["k1"],
        "warm_proxy": "http://p:1234",
        "sub_account_id": "sub-1",
        "identity_id": "identity-1",
        "booking_id": "bk-1",
    }
    ops = _build_ops("live", _cfg(), payload, bus, "w1", signals)
    ops._log("do_swap: new session failed health check -- reverting to phase-1 proxy")

    msgs = bus.read_events(last_id="0", block_ms=10, count=10)
    log_lines = [m for m in msgs if m.type == "log_line"]
    assert len(log_lines) == 1
    assert log_lines[0].payload["text"] == \
        "do_swap: new session failed health check -- reverting to phase-1 proxy"
    assert log_lines[0].payload["worker_id"] == "w1"


def test_live_engine_missing_required_field_raises():
    bus = _bus()
    signals = BusSignals(bus, "w1", "c1")
    with pytest.raises(KeyError):
        _build_ops("live", _cfg(), {"account": {}}, bus, "w1", signals)

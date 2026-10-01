"""apps.worker.__main__._build_ops -- engine selection. _engine defaults to 'sim' (never live by
accident); 'live' constructs LiveEngineOps only when the Hub explicitly opted in via the launch
payload."""
import re

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
    # _build_ops also emits its own "vendor env applied: {...}" line and a "worker context: ..."
    # correlation line on a live build -- match on the one under test rather than a total count.
    log_lines = [m for m in msgs if m.type == "log_line"
                 and not m.payload["text"].startswith("vendor env applied:")
                 and "worker context:" not in m.payload["text"]]
    assert len(log_lines) == 1
    # ops._log prefixes a UTC ISO-8601 timestamp onto every line (Phase-K, 2026-10-01) so a worker
    # log is timeline-readable AND machine-parseable -- assert after that prefix, not raw equality.
    text = log_lines[0].payload["text"]
    assert text.endswith(
        "do_swap: new session failed health check -- reverting to phase-1 proxy")
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z ", text), \
        f"missing timestamp prefix: {text!r}"
    assert log_lines[0].payload["worker_id"] == "w1"


def test_live_engine_missing_required_field_raises():
    bus = _bus()
    signals = BusSignals(bus, "w1", "c1")
    with pytest.raises(KeyError):
        _build_ops("live", _cfg(), {"account": {}}, bus, "w1", signals)


# ── vendor env knobs (confirmed live 2026-08-14) ──────────────────────────────
def test_vendor_env_knobs_are_exported_from_frozen_settings():
    """The whole vendor tuning surface (13 keys) is read from os.environ, never from EngineConfig,
    while every key is ALSO a catalog setting the Hub freezes into effective_settings_json. The two
    halves never met: workers ran with NAV_TIMEOUT_SCALE=1.0 while their frozen settings said 4.0,
    and the entire anti-detect surface was off regardless of the template."""
    import os

    from apps.worker.__main__ import _export_vendor_env

    cfg = EngineConfig.from_settings(resolve({L.TEMPLATE: {
        "ROLE": "apply", "NAV_TIMEOUT_SCALE": 4.0, "LOGIN_FIELD_RETRIES": 4,
        "STEALTH_HARDEN": True, "HUMAN_BEHAVIOR": False,
    }}))
    for k in ("NAV_TIMEOUT_SCALE", "LOGIN_FIELD_RETRIES", "STEALTH_HARDEN", "HUMAN_BEHAVIOR"):
        os.environ.pop(k, None)

    exported = _export_vendor_env(cfg)

    assert exported["NAV_TIMEOUT_SCALE"] == "4"
    assert exported["STEALTH_HARDEN"] == "1"      # vendor tests `== "1"`, not truthiness
    assert exported["HUMAN_BEHAVIOR"] == "0"
    # int() on the vendor side raises on "4.0" -- an integral float must serialise without the .0
    assert exported["LOGIN_FIELD_RETRIES"] == "4"
    assert int(os.environ["LOGIN_FIELD_RETRIES"]) == 4
    assert float(os.environ["NAV_TIMEOUT_SCALE"]) == 4.0


def test_every_vendor_env_knob_is_a_real_catalog_setting():
    """Guard both directions: a knob the engine reads from the environment is only reachable by an
    operator if it also exists in the settings catalog."""
    import re
    from pathlib import Path

    from apps.worker.__main__ import _VENDOR_ENV_KNOBS

    catalog = Path("libs/domain/settings/catalog.py").read_text()
    missing = [k for k in _VENDOR_ENV_KNOBS if f'"{k}"' not in catalog]
    assert not missing, f"vendor env knobs with no catalog setting (unreachable): {missing}"

    vendor_src = "".join(
        p.read_text() for p in Path("libs/engine/vendor").glob("*.py"))
    read_from_env = set(re.findall(r'os\.environ\.get\("([A-Z_]+)"', vendor_src))
    # every env knob the engine reads should be exported by the worker (minus infra-only ones)
    unexported = read_from_env - set(_VENDOR_ENV_KNOBS) - {"REDIS_URL", "DATABASE_URL", "DISPLAY"}
    assert not unexported, f"vendor reads these from env but the worker never exports them: {unexported}"


def test_solver_race_defaults_match_the_proven_vendor_values():
    """Regression guard for a near-miss (2026-08-14): these two were inert catalog numbers (4.0 /
    120.0) for as long as nothing exported them. Wiring _export_vendor_env would have made them
    real and CUT the solver's grace from the proven 25s to 4s. storm-auto ships no .env and runs on
    the vendored solver's own 25/180, so those are ground truth -- if the vendor ever changes them,
    this fails rather than letting the catalog drift into silently degrading CAPTCHA solving."""
    import re
    from pathlib import Path

    src = Path("libs/engine/vendor/solver.py").read_text()
    vendor = dict(re.findall(r'os\.environ\.get\("(SOLVER_RACE_\w+)",\s*"([\d.]+)"\)', src))
    cfg = EngineConfig.from_settings(resolve({L.TEMPLATE: {"ROLE": "apply"}}))
    for key, proven in vendor.items():
        assert float(cfg.raw[key]) == float(proven), (
            f"{key}: catalog default {cfg.raw[key]} != proven vendor default {proven}")

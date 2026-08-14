"""Worker process entrypoint:  python -m apps.worker <worker_id>

Connects to the bus, waits for its `launch` command (carrying the frozen effective settings), runs
the phase machine via WorkerRuntime, and exits when it reaches a terminal state or is told to stop.

Engine selection is driven by `_engine` ("sim" | "live") in the launch payload -- a Hub-controlled,
per-worker, explicit decision (Phase C6/B3), never an env var or a default-to-live: live execution
must always be an auditable choice made by whoever launched the worker, not an accident of how the
process happened to start.

The worker needs only the bus (REDIS_URL) — no DB — matching the decoupled architecture (§2). For a
live-engine launch, every input LiveEngineOps needs (account, proxy, quest_steps, form_defaults,
capsolver_keys) travels through the launch command payload for the same reason: the worker never
reaches into the DB itself.
"""
from __future__ import annotations

import os
import sys

from libs.bus import Bus, connect
from libs.engine.config import EngineConfig

from .bus_signals import BusSignals
from .ops_sim import SimEngineOps
from .runtime import WorkerRuntime


# The vendored engine (libs/engine/vendor/session.py + solver) reads ALL of its tuning knobs from
# os.environ -- never from EngineConfig. Every one of them is ALSO a catalog setting that the Hub
# faithfully resolves and freezes into effective_settings_json, so the two halves never met:
# operators set them, the Hub stored them, and the engine read the environment and got its defaults.
#
# Confirmed live 2026-08-14 on booking "Test 0814": every worker's frozen settings said
# NAV_TIMEOUT_SCALE=4.0 (from the seeded templates, see apps/hub/config_store.py) while the engine
# ran at 1.0 -- i.e. quarter-length nav timeouts -- and the run failed with a pile of
# "Page.goto: Timeout 30000ms/25000ms exceeded". The same logs showed
# "ANTIDETECT flags: stealth_harden=0 det_fp=0 webrtc_block=0 human=0", i.e. the whole anti-detect
# surface was off regardless of what the template asked for.
#
# ORDERING MATTERS: STEALTH_HARDEN / DETERMINISTIC_FP / HUMAN_BEHAVIOR / WEBRTC_BLOCK are read into
# module-level constants at IMPORT time, so this must run before the vendor module is first
# imported -- i.e. before `from .ops_live import LiveEngineOps` below, which is exactly why that
# import is function-local rather than at the top of this file.
_VENDOR_ENV_KNOBS = (
    "NAV_TIMEOUT_SCALE", "LOGIN_FIELD_RETRIES", "STEALTH_HARDEN", "DETERMINISTIC_FP",
    "HUMAN_BEHAVIOR", "WEBRTC_BLOCK", "HEADLESS_MODE", "DRIVER", "LOGIN_CAPSOLVER_ONLY",
    "LOGIN_ANTICAPTCHA_ONLY", "RECAPTCHA_SOLVE_PROXY", "SOLVER_RACE_GRACE",
    "SOLVER_RACE_HARD_TIMEOUT",
)


def _export_vendor_env(cfg: EngineConfig) -> dict[str, str]:
    """Push the frozen settings the vendored engine expects into os.environ. Returns what was set,
    for logging (this was invisible for the whole reconstruction -- worth saying out loud).

    Encoding matters: the vendor tests booleans as `== "1"` and parses LOGIN_FIELD_RETRIES with
    int(), which raises on "4.0" -- so an integral float must be written as "4", not "4.0"."""
    exported: dict[str, str] = {}
    for key in _VENDOR_ENV_KNOBS:
        if key not in cfg.raw:
            continue
        value = cfg.raw[key]
        if isinstance(value, bool):
            text = "1" if value else "0"
        elif isinstance(value, float) and value.is_integer():
            text = str(int(value))
        else:
            text = str(value)
        os.environ[key] = text
        exported[key] = text
    return exported


def _build_ops(engine_kind: str, cfg: EngineConfig, payload: dict, bus: Bus, worker_id: str,
               signals: BusSignals):
    emit = lambda t, **p: bus.emit_event(worker_id, t, **p)  # noqa: E731
    if engine_kind == "live":
        exported = _export_vendor_env(cfg)   # MUST precede the vendor import -- see above
        emit("log_line", text=f"vendor env applied: {exported}")
        from .ops_live import LiveEngineOps
        redis_client = bus.r if hasattr(bus, "r") else None
        ops = LiveEngineOps(
            cfg,
            account=payload["account"],
            # Confirmed live 2026-08-14: the Hub has always SENT `accounts` (the full ranked
            # sub-account candidate list, _rank_live_sub_accounts) but this call never read it, so
            # LiveEngineOps fell back to its single-candidate default and do_login()'s
            # sticky-then-rotate loop could never rotate. A worker with 3 usable sub-accounts gave
            # up after 3 sticky attempts on ONE of them (terminal failed/login) instead of 3x3=9.
            # Both ends were unit-tested; the seam between them was not -- see
            # tests/worker/test_main_engine_selection.py's payload-key coverage test.
            accounts=payload.get("accounts"),
            posto_id=payload["posto_id"],
            nationality=payload["nationality"],
            residence=payload["residence"],
            quest_steps=payload["quest_steps"],
            form_defaults=payload["form_defaults"],
            capsolver_keys=payload["capsolver_keys"],
            warm_proxy=payload["warm_proxy"],
            hold_proxy=payload.get("hold_proxy"),
            sub_account_id=payload["sub_account_id"],
            identity_id=payload.get("identity_id"),
            booking_id=payload.get("booking_id"),
            headless=bool(payload.get("headless", True)),
            signals=signals,
            emit=emit,
            redis_client=redis_client,
        )
        # Regression (confirmed live, 2026-08-13): LiveEngineOps._log defaults to a no-op
        # (`lambda t: None`) and nothing here ever overrode it, so every self._log(...) call inside
        # ops_live.py -- do_swap()'s own failure diagnostics ("no hold_proxy provided", "get_all_
        # cookies failed", "new session launch failed", "new session failed health check"), do_apply,
        # do_discover, keepalive, etc. -- was silently discarded. Confirmed the hard way: two apply
        # workers in the same booking/template diverged (one swapped to its hold proxy, one didn't),
        # and there was NO way to find out why from any log the Hub or journalctl retained. Wired to
        # the same `log_line` event WorkerRuntime's own transition logging already uses, so the
        # dashboard's existing per-worker log view picks these up with no further changes.
        ops._log = lambda t: emit("log_line", text=t)
        return ops
    step_delay = float(os.environ.get("SIM_STEP_DELAY", "1.2"))
    return SimEngineOps(step_delay=step_delay, signals=signals, emit=emit)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m apps.worker <worker_id>", file=sys.stderr)
        return 2
    worker_id = sys.argv[1]
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    bus = Bus(connect(redis_url))
    consumer = f"worker-{os.getpid()}"
    signals = BusSignals(bus, worker_id, consumer)
    bus.emit_event(worker_id, "process_up", pid=os.getpid())

    cfg_payload = signals.wait_launch()
    if cfg_payload is None:
        bus.emit_event(worker_id, "terminal", outcome="stopped", detail="stopped before launch")
        return 0

    cfg = EngineConfig.from_settings(cfg_payload, no_book=bool(cfg_payload.get("no_book", False)))
    engine_kind = signals.launch_payload.get("_engine", "sim")
    ops = _build_ops(engine_kind, cfg, signals.launch_payload, bus, worker_id, signals)
    try:
        WorkerRuntime(bus, worker_id).run(cfg, ops)
    finally:
        if hasattr(ops, "close"):
            ops.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

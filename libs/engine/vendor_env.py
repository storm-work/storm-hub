"""Deliver settings to the vendored engine, which reads them from os.environ.

libs/engine/vendor/* takes ALL of its tuning from environment variables -- never from EngineConfig --
while every one of those variables is also a settings-catalog key the Hub resolves and freezes. The
two halves only meet here.

Lives in libs/ (not apps/worker/) because BOTH processes need it:
  - the worker, before it builds a LiveEngineOps session;
  - the Hub, before libs/proxies/health.py's check_proxy opens its own probe browser.

That second one was the miss (2026-08-14). check_proxy runs inside the HUB, so it was screening
proxies with NAV_TIMEOUT_SCALE=1.0 and the entire anti-detect surface off -- strictly harsher
conditions than the workers that would actually use those proxies. Every false negative it produced
set is_healthy=False, and at the time lease_proxy's _candidate_query filtered on is_healthy, so a
good proxy was permanently removed from the pool by a probe that never had the settings the operator
configured. (That filter is gone as of the same fix -- see lease_proxy's NOTE -- so this class of
false negative now costs ordering rather than eligibility. The env-export bug itself was still real.)
"""
from __future__ import annotations

import os

# Read at MODULE IMPORT time by the vendored session (STEALTH_HARDEN, DETERMINISTIC_FP,
# HUMAN_BEHAVIOR, WEBRTC_BLOCK become module-level constants), so apply() must run before the vendor
# is first imported -- which is why both call sites import it lazily/locally.
VENDOR_ENV_KNOBS = (
    "NAV_TIMEOUT_SCALE", "LOGIN_FIELD_RETRIES", "STEALTH_HARDEN", "DETERMINISTIC_FP",
    "HUMAN_BEHAVIOR", "WEBRTC_BLOCK", "PLAYWRIGHT_STEALTH", "HEADLESS_MODE", "DRIVER", "LOGIN_CAPSOLVER_ONLY",
    "LOGIN_ANTICAPTCHA_ONLY", "RECAPTCHA_SOLVE_PROXY", "SOLVER_RACE_GRACE",
    "SOLVER_RACE_HARD_TIMEOUT", "PRIMP_IMPERSONATE",
    "BROWSER_BACKEND", "DONUT_API_URL", "DONUT_API_TOKEN", "DONUT_HEADLESS", "DONUT_PROFILE_OS",
    "DONUT_PROFILE_SCOPE", "DONUT_PROFILE_KEEP_STATE", "DONUT_CDP_THROTTLE", "DONUT_CDP_PER_MIN",
    "DONUT_CDP_BURST",
)

# Exported like any other knob, but never echoed: apply()'s return value goes into the worker's event
# log ("vendor env applied: ...").
_SECRET_KNOBS = frozenset({"DONUT_API_TOKEN"})


def apply(settings: dict) -> dict[str, str]:
    """Push `settings` (a resolved settings dict, e.g. EngineConfig.raw or the GLOBAL layer) into
    os.environ for the vendored engine. Returns what was set, for logging -- this was invisible for
    the whole reconstruction and is worth saying out loud.

    Encoding matters: the vendor tests booleans as `== "1"`, and parses LOGIN_FIELD_RETRIES with
    int(), which raises on "4.0" -- so an integral float must serialise as "4", not "4.0".
    """
    exported: dict[str, str] = {}
    for key in VENDOR_ENV_KNOBS:
        if key not in settings:
            continue
        value = settings[key]
        if isinstance(value, bool):
            text = "1" if value else "0"
        elif isinstance(value, float) and value.is_integer():
            text = str(int(value))
        else:
            text = str(value)
        os.environ[key] = text
        exported[key] = ("•••set" if text else "") if key in _SECRET_KNOBS else text
    return exported

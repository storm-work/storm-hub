"""The settings catalog — every setting mapped to exactly ONE home layer (spec §6).

This is the single source of truth the operator's comment demanded: "list down all possible
settings and their belongings ... we will not accept overlap of settings between layer - but
in required situation overrides can be done in the proposed priority order."

Each `Setting` declares:
  - `home`   : the layer that owns it (where it is normally set).
  - `override_layers` : the *higher-precedence* layers additionally allowed to override it
                        (empty for most; used only for the few (override-ok) knobs in §6).
  - `type` / `default` : for validation + the effective-config fallback.

Precedence (resolver): WORKER > TEMPLATE > BOOKING > GLOBAL.
Anything set at a layer that is neither its home nor an allowed override layer is REJECTED
by the resolver (no silent overlap).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..enums import SettingsLayer as L

# Shorthands for the two documented override policies (§6):
_TPL = frozenset({L.TEMPLATE, L.WORKER})   # global default, override-ok at template (and worker)
_WRK = frozenset({L.WORKER})               # template knob, rare per-worker instance override


@dataclass(frozen=True)
class Setting:
    key: str
    home: L
    type: type = str
    default: Any = None
    override_layers: frozenset = field(default_factory=frozenset)
    group: str = ""
    note: str = ""

    def settable_layers(self) -> frozenset:
        return frozenset({self.home}) | self.override_layers


def _s(key, home, type=str, default=None, override=frozenset(), group="", note=""):
    return Setting(key, home, type, default, override, group, note)


# ─────────────────────────────────────────────────────────────────────────────
# 6.1 GLOBAL — system, infra, credentials, provider config
# ─────────────────────────────────────────────────────────────────────────────
_GLOBAL = [
    # Infra
    _s("DATABASE_URL", L.GLOBAL, str, group="infra"),
    _s("REDIS_URL", L.GLOBAL, str, group="infra"),
    _s("HOST_SESSION_CAP", L.GLOBAL, int, 175, group="infra",
       note="max concurrent worker sessions on this host"),
    # Credentials
    _s("CAPSOLVER_KEYS", L.GLOBAL, str, group="credentials"),
    _s("TELEGRAM_BOT_TOKEN", L.GLOBAL, str, group="credentials"),
    _s("TELEGRAM_CHAT_ID", L.GLOBAL, str, group="credentials"),
    _s("SIGNAL_TOKEN", L.GLOBAL, str, group="credentials"),
    # Browser / stealth defaults (override-ok at template)
    _s("HEADLESS_MODE", L.GLOBAL, str, "new", _TPL, "browser"),
    _s("DRIVER", L.GLOBAL, str, "playwright", _TPL, "browser"),
    _s("DETERMINISTIC_FP", L.GLOBAL, bool, True, _TPL, "browser"),
    _s("WEBRTC_BLOCK", L.GLOBAL, bool, True, _TPL, "browser"),
    _s("STEALTH_HARDEN", L.GLOBAL, bool, True, _TPL, "browser"),
    _s("EPHEMERAL_PROFILE", L.GLOBAL, bool, True, _TPL, "browser"),
    _s("HUMAN_BEHAVIOR", L.GLOBAL, bool, True, _TPL, "browser"),
    _s("BROWSER_INJECT_ROTATIONS", L.GLOBAL, bool, False, _TPL, "browser"),
    # Solver defaults (override-ok at template for A/B)
    _s("LOGIN_CAPSOLVER_ONLY", L.GLOBAL, bool, False, _TPL, "solver"),
    _s("LOGIN_ANTICAPTCHA_ONLY", L.GLOBAL, bool, False, _TPL, "solver"),
    _s("RECAPTCHA_SOLVE_PROXY", L.GLOBAL, bool, False, _TPL, "solver"),
    _s("NATIVE_SOLVE", L.GLOBAL, bool, False, _TPL, "solver"),
    _s("SOLVER_RACE_GRACE", L.GLOBAL, float, 4.0, _TPL, "solver"),
    _s("SOLVER_RACE_HARD_TIMEOUT", L.GLOBAL, float, 120.0, _TPL, "solver"),
]

# ─────────────────────────────────────────────────────────────────────────────
# 6.2 BOOKING — the run/event
# (Core targets type/posto/nationality/residence live as BOOKING COLUMNS, not settings.)
# ─────────────────────────────────────────────────────────────────────────────
_BOOKING = [
    # Test-only / run mode
    _s("no_book", L.BOOKING, bool, False, group="mode", note="test dry-run: apply workers never submit"),
    _s("auto_ops", L.BOOKING, bool, False, group="mode", note="R2-C1: auto-drive the full command flow"),
    _s("persist_events", L.BOOKING, bool, False, group="mode",
       note="R2-C2: persist raw logs/events to PG (heavy); TTL auto-release"),
    _s("TEST_POSTO_ID", L.BOOKING, str, group="mode"),
    # Timing (warmup_start_at / slot_open_at are booking timestamp columns)
    _s("WARMUP_START_JITTER", L.BOOKING, float, 300.0, group="timing"),
    # Sim / test hooks
    _s("SIM_SLOTS_OPEN_AT", L.BOOKING, str, group="sim"),
    _s("SIMULATE_RELEASE_AT", L.BOOKING, str, group="sim"),
    _s("SIGNAL_TRIGGER_FILE", L.BOOKING, str, group="sim"),
    # Date window
    _s("CAL_START_DATE", L.BOOKING, str, group="date_window"),
    _s("CAL_END_DATE", L.BOOKING, str, group="date_window"),
    _s("CAL_NUM_DAYS", L.BOOKING, int, 0, group="date_window"),
    # Discovery / pool policy
    _s("DISCOVERY_MODE", L.BOOKING, str, "parallel", group="discovery"),
    _s("DISCOVERY_SOURCE", L.BOOKING, str, "calendar", group="discovery"),
    _s("DISCOVERY_MAX_PARALLEL", L.BOOKING, int, 10, group="discovery"),
    _s("SLOT_CAPACITY_PER_PERIOD", L.BOOKING, str, "auto", group="discovery"),
    _s("PER_PERIOD_FANOUT", L.BOOKING, int, 4, group="discovery"),
    _s("SLOT_MAX_PERIOD", L.BOOKING, int, 14, group="discovery"),
    _s("SLOT_PERIOD_FILTER", L.BOOKING, str, group="discovery"),
    # Scout pool
    _s("SCOUT_POOL", L.BOOKING, bool, True, group="scout_pool"),
    _s("SCOUT_POOL_MULTI_CAPACITY", L.BOOKING, bool, True, group="scout_pool"),
    _s("SCOUT_POOL_KEEP", L.BOOKING, bool, False, group="scout_pool"),
    _s("SCOUT_CLOSE_AFTER_MISSES", L.BOOKING, int, 2, group="scout_pool"),
    _s("SCOUT_LEASE_TIMEOUT", L.BOOKING, float, 120.0, group="scout_pool"),
    _s("SCOUT_HEARTBEAT_TTL", L.BOOKING, float, 60.0, group="scout_pool"),
    _s("SCOUT_WAIT_TIMEOUT", L.BOOKING, float, 30.0, group="scout_pool"),
    # Guards
    _s("MAX_BOOKINGS", L.BOOKING, int, 0, group="guards"),
    _s("BOOKING_CIRCUIT_BREAKER", L.BOOKING, bool, False, group="guards"),
    _s("BREAKER_HOLD_GAP", L.BOOKING, float, 0.0, group="guards"),
]

# ─────────────────────────────────────────────────────────────────────────────
# 6.3 WORKER TEMPLATE — role behavior bundle (override-ok at worker instance)
# ─────────────────────────────────────────────────────────────────────────────
_TEMPLATE = [
    # Login / warmup resilience
    _s("NAV_TIMEOUT_SCALE", L.TEMPLATE, float, 1.0, _WRK, "login_resilience"),
    _s("LOGIN_FIELD_RETRIES", L.TEMPLATE, int, 3, _WRK, "login_resilience"),
    _s("LOGIN_STORM_RETRIES", L.TEMPLATE, int, 8, _WRK, "login_resilience"),
    _s("STICKY_RETRY_MAX", L.TEMPLATE, int, 2, _WRK, "login_resilience"),
    _s("QUEST_STEP_RETRIES", L.TEMPLATE, int, 10, _WRK, "login_resilience"),
    _s("QUEST_STEP_GAP", L.TEMPLATE, float, 0.0, _WRK, "login_resilience"),
    _s("WARMUP_MAX_ATTEMPTS", L.TEMPLATE, int, 6, _WRK, "login_resilience"),
    _s("PARTIAL_RENDER_RETRIES", L.TEMPLATE, int, 3, _WRK, "login_resilience"),
    _s("REWARM_MAX_ATTEMPTS", L.TEMPLATE, int, 3, _WRK, "login_resilience"),
    _s("REWARM_EXPIRED_CAP", L.TEMPLATE, int, 0, _WRK, "login_resilience"),
    # Keepalive
    _s("KEEPALIVE_INTERVAL", L.TEMPLATE, float, 900.0, _WRK, "keepalive"),
    _s("KEEPALIVE_DECAY_MIN", L.TEMPLATE, float, 30.0, _WRK, "keepalive"),
    _s("KEEPALIVE_DECAY_MAX", L.TEMPLATE, float, 60.0, _WRK, "keepalive"),
    _s("KEEPALIVE_NO_RESTORE", L.TEMPLATE, bool, False, _WRK, "keepalive"),
    _s("KEEPALIVE_BROWSER_CONFIRM", L.TEMPLATE, bool, True, _WRK, "keepalive"),
    _s("KEEPALIVE_BROWSER_FROM", L.TEMPLATE, str, "", _WRK, "keepalive"),
    _s("KEEPALIVE_DEAD_TRIP", L.TEMPLATE, int, 3, _WRK, "keepalive"),
    _s("KEEPALIVE_PROBE", L.TEMPLATE, str, "home", _WRK, "keepalive"),
    _s("KEEPALIVE_OFF", L.TEMPLATE, bool, False, _WRK, "keepalive"),
    # Hold / ISP swap
    _s("HOLD_SWAP_AFTER_FOUND", L.TEMPLATE, bool, False, _WRK, "hold_swap"),
    _s("HOLD_SWAP_RETRIES", L.TEMPLATE, int, 3, _WRK, "hold_swap"),
    _s("HOLD_VERIFY", L.TEMPLATE, bool, True, _WRK, "hold_swap"),
    _s("HOLD_VERIFY_MINS", L.TEMPLATE, float, 0.0, _WRK, "hold_swap"),
    _s("HOLD_POLL_GAP", L.TEMPLATE, float, 0.0, _WRK, "hold_swap"),
    _s("HOLD_CKPT_SECS", L.TEMPLATE, float, 0.0, _WRK, "hold_swap"),
    _s("SWAP_DIRECT_BOOTSTRAP", L.TEMPLATE, bool, False, _WRK, "hold_swap"),
    _s("SESSION_PRESERVING_FANOUT", L.TEMPLATE, bool, True, _WRK, "hold_swap"),
    # Discovery / poll cadence
    _s("DISCOVERY_NAV_RETRIES", L.TEMPLATE, int, 0, _WRK, "poll_cadence"),
    _s("DISCOVERY_NAV_BACKOFF", L.TEMPLATE, float, 0.75, _WRK, "poll_cadence"),
    _s("DETECT_INTERVAL", L.TEMPLATE, float, 0.0, _WRK, "poll_cadence"),
    _s("DETECT_PROBE_DAYS", L.TEMPLATE, int, 0, _WRK, "poll_cadence"),
    _s("DETECT_RECHECK", L.TEMPLATE, bool, False, _WRK, "poll_cadence"),
    _s("POLL_GAP_LIGHT", L.TEMPLATE, float, 8.0, _WRK, "poll_cadence"),
    _s("POLL_GAP_HARDENED", L.TEMPLATE, float, 2.0, _WRK, "poll_cadence"),
    _s("POLL_GAP_MAX", L.TEMPLATE, float, 45.0, _WRK, "poll_cadence"),
    _s("POLL_BACKOFF_AFTER", L.TEMPLATE, int, 0, _WRK, "poll_cadence"),
    _s("GETTIME_CACHE_SECS", L.TEMPLATE, float, 300.0, _WRK, "poll_cadence"),
    # Concurrency
    _s("LOGIN_CONCURRENCY", L.TEMPLATE, int, 10, _WRK, "concurrency"),
    _s("APPLY_CONCURRENCY", L.TEMPLATE, int, 100, _WRK, "concurrency"),
    _s("POLL_CONCURRENCY", L.TEMPLATE, int, 1, _WRK, "concurrency"),
    _s("EXECUTOR_THREADS", L.TEMPLATE, int, 300, _WRK, "concurrency"),
    # Backoff / respawn
    _s("PROXY_FAULT_BACKOFF", L.TEMPLATE, float, 1.5, _WRK, "backoff"),
    _s("PROXY_FAULT_BACKOFF_CAP", L.TEMPLATE, float, 8.0, _WRK, "backoff"),
    _s("RESPAWN_BACKOFF_BASE", L.TEMPLATE, float, 0.0, _WRK, "backoff"),
    _s("RESPAWN_BACKOFF_CAP", L.TEMPLATE, float, 0.0, _WRK, "backoff"),
    _s("PRESERVE_BACKOFF", L.TEMPLATE, float, 0.0, _WRK, "backoff"),
    _s("EMPTY_HOLD_DEAD_CAP", L.TEMPLATE, int, 0, _WRK, "backoff"),
    # Role behavior
    _s("ROLE", L.TEMPLATE, str, "apply", _WRK, "role"),
    _s("CAPTURE_ONLY", L.TEMPLATE, bool, False, _WRK, "role", note="scout=True"),
    _s("WARMUP_ONLY", L.TEMPLATE, bool, False, _WRK, "role"),
    _s("FORMSTATE_CHECK_BROWSER", L.TEMPLATE, bool, False, _WRK, "role"),
    _s("NO_RETIRE_LOGIN", L.TEMPLATE, bool, False, _WRK, "role"),
]

# WORKER-instance settings are runtime bindings (proxy/identity/subaccount order) resolved by
# the Hub, not free-form knobs — so they are not in the catalog; the worker layer's catalog role
# is to override TEMPLATE/GLOBAL knobs (via override_layers above).

CATALOG: dict[str, Setting] = {s.key: s for s in (_GLOBAL + _BOOKING + _TEMPLATE)}

assert len(CATALOG) == len(_GLOBAL) + len(_BOOKING) + len(_TEMPLATE), "duplicate setting key in catalog"


def settings_for_layer(layer: L) -> list[Setting]:
    """Settings whose HOME is `layer` (what a layer's editor shows), sorted by group then key."""
    return sorted(
        (s for s in CATALOG.values() if s.home == layer),
        key=lambda s: (s.group, s.key),
    )

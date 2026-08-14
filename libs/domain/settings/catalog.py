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

# Shorthands for the documented override policies (§6):
_TPL = frozenset({L.TEMPLATE, L.WORKER})   # global default, override-ok at template (and worker)
_WRK = frozenset({L.WORKER})               # template knob, rare per-worker instance override
_TPL_BK = frozenset({L.BOOKING, L.WORKER}) # template knob exposed as a per-booking operator shortcut


@dataclass(frozen=True)
class Setting:
    key: str
    home: L
    type: type = str
    default: Any = None
    override_layers: frozenset = field(default_factory=frozenset)
    group: str = ""
    note: str = ""
    min_value: float | None = None

    def settable_layers(self) -> frozenset:
        return frozenset({self.home}) | self.override_layers


def _s(key, home, type=str, default=None, override=frozenset(), group="", note="", min_value=None):
    return Setting(key, home, type, default, override, group, note, min_value)


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
    # Registration/mail-verification credentials (the registration UI itself is deferred, but these
    # are legitimate stored GLOBAL config recovered from the dump -- without a catalog entry,
    # resolve() rejects them and breaks EVERY worker launch, not just registration, since it
    # validates the whole GLOBAL layer regardless of what a given launch actually needs).
    _s("EMAIL_PROVIDER", L.GLOBAL, str, group="credentials"),
    _s("CF_MAIL_DOMAIN", L.GLOBAL, str, group="credentials"),
    _s("CF_WORKER_URL", L.GLOBAL, str, group="credentials"),
    _s("CF_WORKER_SECRET", L.GLOBAL, str, group="credentials"),
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
    # Defaults corrected 2026-08-14 to match the PROVEN values. Both are read by the vendored solver
    # from os.environ (libs/engine/vendor/solver.py), which until now nothing ever set -- so these
    # catalog numbers were inert and the engine silently used its own 25/180. The moment the worker
    # started actually exporting them (apps/worker/__main__.py's _export_vendor_env), the old 4.0/
    # 120.0 would have taken effect and CUT the solver's grace period from 25s to 4s. storm-auto,
    # which has booked successfully for months, ships no .env at all and therefore runs on exactly
    # these 25/180 code defaults -- treat those as ground truth, not the reconstructed guesses.
    _s("SOLVER_RACE_GRACE", L.GLOBAL, float, 25.0, _TPL, "solver",
       note="seconds to keep racing for a better CAPTCHA score after the first acceptable solve"),
    _s("SOLVER_RACE_HARD_TIMEOUT", L.GLOBAL, float, 180.0, _TPL, "solver",
       note="absolute ceiling on one solve race"),
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
    _s("ENGINE_MODE", L.BOOKING, str, "sim", group="mode",
       note="'sim' | 'live' -- explicit per-booking opt-in; live is never inferred/defaulted"),
    _s("PROXY_PROVIDER", L.BOOKING, str, "soax", group="mode",
       note="phase-1 (login/warmup) leased-proxy provider: 'soax' | 'webshare'"),
    _s("PROXY_KIND", L.BOOKING, str, group="mode",
       note="optional phase-1 proxy kind filter (Proxy.kind): 'rotating' | 'static'; blank = no filter"),
    _s("HOLD_PROXY_PROVIDER", L.BOOKING, str, "webshare", group="mode",
       note="phase-2 (hold/apply/discovery) leased-proxy provider, used only when HOLD_SWAP_AFTER_FOUND "
            "is on -- default 'webshare' (long, ~24h session lifetime) vs phase-1's 'soax' default "
            "(~1h session lifetime): a long hold needs to outlive soax's own session cap, which a "
            "same-provider swap can't fix -- see libs.domain.settings.catalog's hold_swap group"),
    _s("HOLD_PROXY_KIND", L.BOOKING, str, group="mode",
       note="optional phase-2 proxy kind filter; blank = no filter"),
    # Timing (warmup_start_at / slot_open_at are booking timestamp columns)
    _s("WARMUP_START_JITTER", L.BOOKING, float, 0.0, group="timing",
       note="anti-herd (operator spec 2026-08-14: 'hub spawns worker processors -- concurrency "
            "window + jitter do matter'): each worker's actual spawn moment is delayed by a random "
            "[0, jitter) seconds BEFORE it does anything (including its proxy-lease health probe), "
            "so a fleet launch doesn't hit the portal/proxy pool in one instant. Applied in "
            "_launch_worker_fleet, both for a scheduled auto-start and a manual Start Instantly. "
            "Off (0) by default -- unlike KEEPALIVE_JITTER/RESTORE_STAMPEDE_JITTER (which jitter a "
            "background loop's own timing), this one delays _launch_worker_fleet ITSELF, which the "
            "/bookings/{id}/start HTTP handler calls synchronously and waits on -- a nonzero value "
            "makes that request take up to ~jitter_secs to return for a fleet start, so this is an "
            "explicit operator opt-in per booking, never a silent default. The concurrency window "
            "(_LAUNCH_FLEET_CONCURRENCY, currently a fixed constant, not a setting) separately "
            "bounds how many launches run at once; jitter only spreads out WHEN each one starts "
            "within that window."),
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
    _s("DISCOVERY_MAX_PARALLEL", L.BOOKING, int, 10, group="discovery",
       note="SINGLE-mode (pre slot-found) 'awaken scout limit' -- only up to this many scouts are "
            "ever in the single-mode probe rotation at once, the rest stay parked in hold; a dead "
            "awakened scout is replaced from the parked pool. Renamed in meaning 2026-08-14 (was "
            "briefly wired as a SPLIT-mode cap on 2026-08-13, which was wrong -- split mode never "
            "benches a scout, see DISCOVERY_CONCURRENCY)"),
    _s("DISCOVERY_CONCURRENCY", L.BOOKING, int, 5, group="discovery",
       note="SPLIT-mode (post slot-found) concurrency window: every scout gets a persistent date "
            "slice (full fleet coverage, nobody benched), but only this many may have a discover "
            "round actively in flight at once -- the window rotates across the fleet as rounds "
            "finish, so 'more scouts' still means faster full-range coverage without hammering the "
            "portal with everyone at once"),
    _s("SINGLE_MODE_INTERVAL", L.BOOKING, float, 30.0, group="discovery", min_value=1.0,
       note="minimum seconds between successive SINGLE-mode probes fleet-wide (not per scheduler "
            "tick, which is much hotter at 5s) -- 'if there are 10 scouts, 1 scout retries "
            "discovery per 300s'"),
    _s("DATE_RETIRE_ENABLED", L.BOOKING, bool, True, group="discovery",
       note="on/off switch for date retirement -- when off, a date is NEVER pruned from a scout's "
            "discovery slice no matter how many empty reports it gets"),
    _s("DATE_RETIRE_AFTER_MISSES", L.BOOKING, int, 3, group="discovery", min_value=1.0,
       note="consecutive empty (never 'failed'/unknown) reports before a date is retired from "
            "active scanning; ignored when DATE_RETIRE_ENABLED is off"),
    _s("SLOT_OPEN_INACTIVATE_AFTER", L.BOOKING, float, 1200.0, group="discovery",
       note="false-slot-open guard (operator spec 2026-08-14): if slot-open has been fired for this "
            "many seconds and NOT ONE slot has been found in that whole window, acknowledge the "
            "slot-open was false -- clear slot_open_fired_at, send every worker an `inactivate` "
            "signal, and revert the booking to WARMING so scouts fall back to plain holding instead "
            "of grinding discovery (which risks portal rate-limiting/detection for nothing). "
            "Slot-open can then be re-fired, manually or on schedule. 0 disables the guard entirely; "
            "never fires once slot_found has fired (there ARE slots -- the open was real)."),
    _s("SLOT_LEASE_TTL", L.BOOKING, float, 180.0, group="discovery",
       note="seconds a worker may hold a slot lease without converting it into an apply before the "
            "scheduler reclaims it and returns the slot to the pool. A lease is a promise to attempt "
            "a booking within seconds; a worker that dies, hangs, or bounces without emitting a "
            "terminal event would otherwise sterilise those units forever. A worker actually in "
            "APPLYING/BOOKED is never reclaimed, however long it takes. 0 disables reclaim."),
    _s("SLOT_CAPACITY_PER_PERIOD", L.BOOKING, str, "auto", group="discovery",
       note="how many concurrent apply-worker leases one (date, period) slot can absorb before "
            "it's exhausted -- 'auto' falls back to a per-slot Slot.capacity_estimate if the "
            "discovering scout supplied one, else the DEFAULT_SLOT_CAPACITY constant (5); set a "
            "literal integer here to force the same cap fleet-wide regardless of per-slot estimates"),
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
    _s("BOOKING_TIMEOUT_HOURS", L.BOOKING, float, 48.0, group="guards",
       note="hard stop for the whole booking; replaces the removed per-worker hold timeout"),
    _s("BREAKER_TRIP_THRESHOLD", L.BOOKING, int, 5, group="guards",
       note="consecutive portal-down signals before RedisBreaker trips"),
    _s("BREAKER_COOLDOWN", L.BOOKING, float, 120.0, group="guards", min_value=1.0),
    _s("APPLY_MAX_ATTEMPTS", L.BOOKING, int, 5, group="guards"),
    _s("WORKER_STALL_TIMEOUT", L.BOOKING, float, 1500.0, group="guards", min_value=1.0,
       note="no events from a worker longer than this -> reaped as stalled"),
    _s("REDIS_STREAM_MAXLEN", L.BOOKING, int, 200, group="guards",
       note="approximate XADD maxlen cap on each per-worker cmd stream"),
    _s("REDIS_EVENT_STREAM_MAXLEN", L.BOOKING, int, 5000, group="guards",
       note="approximate XADD maxlen cap on the shared evt stream"),
    _s("MAX_RESPAWN_ATTEMPTS", L.BOOKING, int, 5, group="guards",
       note="hold-phase respawn hard cap; beyond this the worker is left FAILED, not retried forever"),
    _s("WORKER_EVENT_RATE_LIMIT", L.BOOKING, float, 5.0, group="guards", min_value=0.5,
       note="events/sec sustained beyond this auto-kills the worker (runaway-loop circuit breaker)"),
]

# ─────────────────────────────────────────────────────────────────────────────
# 6.3 WORKER TEMPLATE — role behavior bundle (override-ok at worker instance)
# ─────────────────────────────────────────────────────────────────────────────
_TEMPLATE = [
    # Login / warmup resilience
    _s("NAV_TIMEOUT_SCALE", L.TEMPLATE, float, 1.0, _WRK, "login_resilience"),
    _s("LOGIN_FIELD_RETRIES", L.TEMPLATE, int, 3, _WRK, "login_resilience"),
    _s("LOGIN_STORM_RETRIES", L.TEMPLATE, int, 8, _WRK, "login_resilience"),
    _s("STICKY_RETRY_MAX", L.TEMPLATE, int, 2, _WRK, "login_resilience",
       note="login attempts spent on ONE sub-account before rotating to the next candidate. Only "
            "charged for failures that are actually a verdict on the credentials -- see "
            "LOGIN_TRANSIENT_RETRY_MAX"),
    _s("LOGIN_TRANSIENT_RETRY_MAX", L.TEMPLATE, int, 6, _WRK, "login_resilience",
       note="separate budget for INFRASTRUCTURE login failures (proxy connection errors, DataDome "
            "challenges, timeouts, navigations that never landed). These say nothing about the "
            "account, so charging them to STICKY_RETRY_MAX burns through every sub-account and "
            "fails the worker over proxy weather -- confirmed live 2026-08-14, six workers "
            "permanently FAILED that way. 0 makes every failure count against the sub-account, "
            "i.e. the old (broken) behaviour."),
    _s("QUEST_STEP_RETRIES", L.TEMPLATE, int, 10, _WRK, "login_resilience"),
    _s("QUEST_STEP_GAP", L.TEMPLATE, float, 0.0, _WRK, "login_resilience"),
    _s("WARMUP_MAX_ATTEMPTS", L.TEMPLATE, int, 6, _WRK, "login_resilience"),
    _s("PARTIAL_RENDER_RETRIES", L.TEMPLATE, int, 3, _WRK, "login_resilience"),
    _s("REWARM_MAX_ATTEMPTS", L.TEMPLATE, int, 3, _WRK, "login_resilience"),
    _s("REWARM_EXPIRED_CAP", L.TEMPLATE, int, 0, _WRK, "login_resilience"),
    # Keepalive
    _s("KEEPALIVE_INTERVAL", L.TEMPLATE, float, 900.0, _WRK, "keepalive", min_value=1.0),
    _s("KEEPALIVE_DECAY_MIN", L.TEMPLATE, float, 30.0, _WRK, "keepalive"),
    _s("KEEPALIVE_DECAY_MAX", L.TEMPLATE, float, 60.0, _WRK, "keepalive"),
    _s("KEEPALIVE_NO_RESTORE", L.TEMPLATE, bool, False, _WRK, "keepalive"),
    _s("KEEPALIVE_BROWSER_CONFIRM", L.TEMPLATE, bool, True, _WRK, "keepalive"),
    _s("KEEPALIVE_BROWSER_FROM", L.TEMPLATE, str, "", _WRK, "keepalive"),
    _s("KEEPALIVE_DEAD_TRIP", L.TEMPLATE, int, 3, _WRK, "keepalive"),
    _s("KEEPALIVE_PROBE", L.TEMPLATE, str, "home", _WRK, "keepalive"),
    _s("KEEPALIVE_OFF", L.TEMPLATE, bool, False, _WRK, "keepalive"),
    _s("KEEPALIVE_JITTER", L.TEMPLATE, float, 30.0, _WRK, "keepalive",
       note="anti-herd: randomize keepalive timing so many workers don't probe in the same instant"),
    _s("CHECKPOINT_RESTORE_BUDGET", L.TEMPLATE, float, 90.0, _WRK, "keepalive",
       note="wall-clock seconds the checkpoint restore chain (rebuild -> health probe -> re-warm) "
            "may spend before abandoning and going straight to a full login. Every tier is a real "
            "browser navigation, so with NAV_TIMEOUT_SCALE=4 a doomed restore burned up to 384s "
            "before the worker even started logging in (confirmed live 2026-08-14). 0 disables the "
            "budget and lets the chain run to completion."),
    _s("RESTORE_STAMPEDE_JITTER", L.TEMPLATE, float, 30.0, _WRK, "keepalive",
       note="anti-herd: stagger checkpoint-restore attempts after a mass relaunch"),
    _s("SESSION_REFRESH_STAGGER", L.TEMPLATE, float, 120.0, _WRK, "keepalive"),
    # Hold / ISP swap
    _s("HOLD_SWAP_AFTER_FOUND", L.TEMPLATE, bool, False, _TPL_BK, "hold_swap",
       note="on/off switch for the phase-1->phase-2 proxy swap (LiveEngineOps.do_swap); off by "
            "default -- a worker just keeps its phase-1 (PROXY_PROVIDER) proxy for the whole run. "
            "NAME IS HISTORICAL, NOT LITERAL: despite the name, the swap fires right after warmup "
            "finishes -- i.e. the instant a worker is about to start its (potentially many-hour) "
            "hold -- NOT after a slot is actually found (PhaseMachine._warmup() calls do_swap() "
            "before await_slot_open() even begins). This is intentional: the operator's design is "
            "soax (short, ~1h session) for the brief login/warmup, then a swap to webshare (long, "
            "~24h session) BEFORE the long hold starts, so the hold never outlives its proxy's own "
            "session cap. When on, a second proxy is leased per HOLD_PROXY_PROVIDER/HOLD_PROXY_KIND. "
            "Booking-layer override added as the dashboard's booking-settings shortcut checkbox -- a "
            "per-booking operator choice, not something that should require editing a worker template"),
    _s("HOLD_SWAP_RETRIES", L.TEMPLATE, int, 3, _WRK, "hold_swap",
       note="swap retry protocol (operator spec 2026-08-14): if the FIRST swap attempt fails (new "
            "session doesn't pass its post-swap health check, or launch itself fails), do_swap() "
            "holds a bit longer on the CURRENT (phase-1) proxy -- HOLD_SWAP_RETRY_HOLD_SECS -- then "
            "requests a FRESH hold-pool proxy and tries again. Bounded by this many total extra "
            "attempts (set to 1 for the operator's literal example: one initial try, one final "
            "chance). Exhausting the budget gives up on swapping PERMANENTLY for this worker -- it "
            "keeps holding on its original phase-1 proxy and is never stopped."),
    _s("HOLD_SWAP_RETRY_HOLD_SECS", L.TEMPLATE, float, 600.0, _WRK, "hold_swap",
       note="how long do_swap() holds on the current proxy after a failed swap attempt before "
            "requesting a fresh hold-pool proxy and retrying (operator's example: 10 minutes)."),
    _s("HOLD_VERIFY", L.TEMPLATE, bool, True, _WRK, "hold_swap"),
    _s("HOLD_VERIFY_MINS", L.TEMPLATE, float, 0.0, _WRK, "hold_swap"),
    # HOLD_POLL_GAP: the hold-loop's own sleep between iterations. A resolved value of 0 means the
    # loop spins unthrottled — this is the CONFIRMED root cause of the 2026-08-07 incident (a worker's
    # frozen effective_settings_json showed HOLD_POLL_GAP=0.0, and it wrote ~150-280 events/sec for 66
    # minutes straight). Never leave this at a 0-is-the-placeholder default; min_value is enforced by
    # the resolver (see resolver.py) as a hard backstop regardless of what any layer sets it to.
    _s("HOLD_POLL_GAP", L.TEMPLATE, float, 5.0, _WRK, "hold_swap", min_value=1.0),
    _s("HOLD_CKPT_SECS", L.TEMPLATE, float, 0.0, _WRK, "hold_swap"),
    _s("SWAP_DIRECT_BOOTSTRAP", L.TEMPLATE, bool, False, _WRK, "hold_swap"),
    _s("SESSION_PRESERVING_FANOUT", L.TEMPLATE, bool, True, _WRK, "hold_swap"),
    # Discovery / poll cadence
    _s("DISCOVERY_NAV_RETRIES", L.TEMPLATE, int, 0, _WRK, "poll_cadence"),
    _s("DISCOVERY_NAV_BACKOFF", L.TEMPLATE, float, 0.75, _WRK, "poll_cadence"),
    _s("DETECT_INTERVAL", L.TEMPLATE, float, 0.0, _WRK, "poll_cadence"),
    _s("DETECT_PROBE_DAYS", L.TEMPLATE, int, 0, _WRK, "poll_cadence"),
    _s("DETECT_RECHECK", L.TEMPLATE, bool, False, _WRK, "poll_cadence"),
    _s("POLL_GAP_LIGHT", L.TEMPLATE, float, 8.0, _WRK, "poll_cadence", min_value=0.5),
    _s("POLL_GAP_HARDENED", L.TEMPLATE, float, 2.0, _WRK, "poll_cadence", min_value=0.5),
    _s("POLL_GAP_MAX", L.TEMPLATE, float, 45.0, _WRK, "poll_cadence", min_value=0.5),
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
    _s("RESPAWN_BACKOFF_BASE", L.TEMPLATE, float, 2.0, _WRK, "backoff"),
    _s("RESPAWN_BACKOFF_CAP", L.TEMPLATE, float, 120.0, _WRK, "backoff"),
    _s("PRESERVE_BACKOFF", L.TEMPLATE, float, 0.0, _WRK, "backoff"),
    _s("EMPTY_HOLD_DEAD_CAP", L.TEMPLATE, int, 0, _WRK, "backoff"),
    _s("HOLD_RESPAWN", L.TEMPLATE, bool, True, _WRK, "backoff",
       note="feature flag for the hold-phase respawn mechanism"),
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
assert all(
    s.default is None or s.min_value is None or s.default >= s.min_value for s in CATALOG.values()
), "a setting's own default violates its declared min_value"


def settings_for_layer(layer: L) -> list[Setting]:
    """Settings whose HOME is `layer` (what a layer's editor shows), sorted by group then key."""
    return sorted(
        (s for s in CATALOG.values() if s.home == layer),
        key=lambda s: (s.group, s.key),
    )

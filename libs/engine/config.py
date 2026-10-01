"""EngineConfig — the typed bridge from resolved settings (control plane) to the engine (data plane).

`libs.domain.settings.resolve()` produces a flat effective-config dict (frozen onto a worker at
launch). The engine shouldn't read that dict by stringly-typed keys everywhere, so this adapter
maps it once into a structured, typed config grouped by concern. Unknown/extra keys are ignored;
missing keys fall back to the catalog defaults already baked into the resolved dict.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from libs.domain.enums import WorkerRole


@dataclass(frozen=True)
class LoginConfig:
    nav_timeout_scale: float = 1.0
    field_retries: int = 3
    storm_retries: int = 8
    sticky_retry_max: int = 2
    transient_retry_max: int = 6
    quest_step_retries: int = 10
    warmup_max_attempts: int = 6
    partial_render_retries: int = 3


@dataclass(frozen=True)
class KeepaliveConfig:
    interval: float = 900.0
    decay_min: float = 30.0
    decay_max: float = 60.0
    no_restore: bool = False
    browser_confirm: bool = True
    browser_from: str = ""
    off: bool = False
    # dead_trip/probe/jitter exist in the settings catalog (group "keepalive") but were missing from
    # this bridge -- added when the hold loop's keepalive was actually wired up (it was previously
    # parsed nowhere: a "holding" worker sat with zero portal traffic for up to 900s per wait, and
    # the server-side session/form-state can lapse in that gap).
    dead_trip: int = 3
    probe: str = "home"
    jitter: float = 30.0
    # KEEPALIVE_MODE (2026-10-01): "auth_only" (default, proven) | "schedule" (non-destructive
    # cursor-survival measurement -- probes Schedule.jsp and records whether the booking cursor
    # survived, never re-warms). See ops_live._do_keepalive and the hot-period report.
    mode: str = "auth_only"


@dataclass(frozen=True)
class HoldSwapConfig:
    # Named for WHEN it happens: the swap fires immediately after warmup completes (phases.py runs
    # it between WARMED and HOLDING), NOT after slot-found. The old name HOLD_SWAP_AFTER_FOUND
    # described a trigger point that was never the code's.
    swap_after_login: bool = False
    swap_retries: int = 3
    retry_hold_secs: float = 600.0
    verify: bool = True
    poll_gap: float = 0.0
    direct_bootstrap: bool = False
    session_preserving_fanout: bool = True


@dataclass(frozen=True)
class DiscoveryConfig:
    source: str = "calendar"
    nav_retries: int = 0
    nav_backoff: float = 0.75
    gettime_cache_secs: float = 300.0
    num_days: int = 0
    start_date: str = ""
    end_date: str = ""
    max_period: int = 14
    period_filter: str = ""


@dataclass(frozen=True)
class PoolConfig:
    enabled: bool = True
    multi_capacity: bool = True
    keep: bool = False
    per_period_fanout: int = 4
    close_after_misses: int = 2
    lease_timeout: float = 120.0
    capacity_per_period: str = "auto"


@dataclass(frozen=True)
class SolverConfig:
    capsolver_only: bool = False
    anticaptcha_only: bool = False
    solve_proxy: bool = False
    native: bool = False
    race_grace: float = 4.0
    race_hard_timeout: float = 120.0


@dataclass(frozen=True)
class EngineConfig:
    role: WorkerRole
    capture_only: bool
    no_book: bool
    warmup_only: bool
    login: LoginConfig
    keepalive: KeepaliveConfig
    hold_swap: HoldSwapConfig
    discovery: DiscoveryConfig
    pool: PoolConfig
    solver: SolverConfig
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_settings(cls, eff: dict[str, Any], *, no_book: bool = False) -> "EngineConfig":
        g = eff.get
        role = WorkerRole(g("ROLE", "apply"))
        return cls(
            role=role,
            # A scout is always capture-only; an apply worker follows the flag (default False).
            capture_only=(role == WorkerRole.SCOUT) or bool(g("CAPTURE_ONLY", False)),
            no_book=bool(no_book or g("no_book", False)),
            warmup_only=bool(g("WARMUP_ONLY", False)),
            login=LoginConfig(
                nav_timeout_scale=float(g("NAV_TIMEOUT_SCALE", 1.0)),
                field_retries=int(g("LOGIN_FIELD_RETRIES", 3)),
                storm_retries=int(g("LOGIN_STORM_RETRIES", 8)),
                sticky_retry_max=int(g("STICKY_RETRY_MAX", 2)),
                transient_retry_max=int(g("LOGIN_TRANSIENT_RETRY_MAX", 6)),
                quest_step_retries=int(g("QUEST_STEP_RETRIES", 10)),
                warmup_max_attempts=int(g("WARMUP_MAX_ATTEMPTS", 6)),
                partial_render_retries=int(g("PARTIAL_RENDER_RETRIES", 3)),
            ),
            keepalive=KeepaliveConfig(
                interval=float(g("KEEPALIVE_INTERVAL", 900.0)),
                decay_min=float(g("KEEPALIVE_DECAY_MIN", 30.0)),
                decay_max=float(g("KEEPALIVE_DECAY_MAX", 60.0)),
                no_restore=bool(g("KEEPALIVE_NO_RESTORE", False)),
                browser_confirm=bool(g("KEEPALIVE_BROWSER_CONFIRM", True)),
                browser_from=str(g("KEEPALIVE_BROWSER_FROM", "") or ""),
                off=bool(g("KEEPALIVE_OFF", False)),
                dead_trip=int(g("KEEPALIVE_DEAD_TRIP", 3)),
                probe=str(g("KEEPALIVE_PROBE", "home") or "home"),
                jitter=float(g("KEEPALIVE_JITTER", 30.0)),
                mode=str(g("KEEPALIVE_MODE", "auth_only") or "auth_only").strip().lower(),
            ),
            hold_swap=HoldSwapConfig(
                swap_after_login=bool(g("HOLD_SWAP_AFTER_LOGIN", False)),
                swap_retries=int(g("HOLD_SWAP_RETRIES", 3)),
                retry_hold_secs=float(g("HOLD_SWAP_RETRY_HOLD_SECS", 600.0)),
                verify=bool(g("HOLD_VERIFY", True)),
                poll_gap=float(g("HOLD_POLL_GAP", 0.0)),
                direct_bootstrap=bool(g("SWAP_DIRECT_BOOTSTRAP", False)),
                session_preserving_fanout=bool(g("SESSION_PRESERVING_FANOUT", True)),
            ),
            discovery=DiscoveryConfig(
                source=str(g("DISCOVERY_SOURCE", "calendar")),
                nav_retries=int(g("DISCOVERY_NAV_RETRIES", 0)),
                nav_backoff=float(g("DISCOVERY_NAV_BACKOFF", 0.75)),
                gettime_cache_secs=float(g("GETTIME_CACHE_SECS", 300.0)),
                num_days=int(g("CAL_NUM_DAYS", 0)),
                start_date=str(g("CAL_START_DATE", "") or ""),
                end_date=str(g("CAL_END_DATE", "") or ""),
                max_period=int(g("SLOT_MAX_PERIOD", 14)),
                period_filter=str(g("SLOT_PERIOD_FILTER", "") or ""),
            ),
            pool=PoolConfig(
                enabled=bool(g("SCOUT_POOL", True)),
                multi_capacity=bool(g("SCOUT_POOL_MULTI_CAPACITY", True)),
                keep=bool(g("SCOUT_POOL_KEEP", False)),
                per_period_fanout=int(g("PER_PERIOD_FANOUT", 4)),
                close_after_misses=int(g("SCOUT_CLOSE_AFTER_MISSES", 2)),
                lease_timeout=float(g("SCOUT_LEASE_TIMEOUT", 120.0)),
                capacity_per_period=str(g("SLOT_CAPACITY_PER_PERIOD", "auto")),
            ),
            solver=SolverConfig(
                capsolver_only=bool(g("LOGIN_CAPSOLVER_ONLY", False)),
                anticaptcha_only=bool(g("LOGIN_ANTICAPTCHA_ONLY", False)),
                solve_proxy=bool(g("RECAPTCHA_SOLVE_PROXY", False)),
                native=bool(g("NATIVE_SOLVE", False)),
                race_grace=float(g("SOLVER_RACE_GRACE", 4.0)),
                race_hard_timeout=float(g("SOLVER_RACE_HARD_TIMEOUT", 120.0)),
            ),
            raw=dict(eff),
        )

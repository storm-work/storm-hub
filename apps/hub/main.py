"""storm-hub Manager Hub — FastAPI application (Phase 3, first launchable slice).

DB-backed endpoints that exercise the domain layer over HTTP: settings catalog, identities,
bookings, and attach-identity → materialize-worker (enforcing the role rules + C2 identity
exclusivity). Redis/worker-control and WS log streaming are added later in Phase 3.
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from libs.db import models as M
from libs.domain.enums import (
    BOOKING_ACTIVE_STATUSES,
    BookingStatus,
    IdentityStatus,
    SessionPurpose,
    SessionStatus,
    SlotLeaseOutcome,
    SlotStatus,
    UserRole,
)
from libs.domain.enums import SettingsLayer as L
from libs.domain.rules import RuleViolation, resolve_worker_role
from libs.domain.settings import CATALOG, settings_for_layer, validate_layer
from libs.domain.state_machines import IllegalTransition, booking_transition

from libs.domain.enums import HandoverStatus

from .auth import (
    require_admin,
    Principal,
    active_operator,
    current_principal,
    hash_password,
    make_token,
    open_session,
    require_operator,
    try_decode,
    verify_password,
)
from libs.bus import Bus, connect
from libs.engine import vendor_env
from libs.bus.names import EVENT_STREAM, notified_key, respawn_key
from libs.domain.enums import WorkerStatus, WorkerSubstate
from libs.domain.settings import resolve

from libs.domain.enums import RegistrationMode, RegistrationStatus, SubAccountStatus
from libs.proxies.health import check_proxy
from libs.proxies.lease import ProxyLeaseError, lease_proxy, release_proxy, conn_url

from .config import settings
from .config_store import get_config, seed_defaults, seed_templates, set_config
from .db import SessionLocal, engine, get_db
from .events import EventHub
from .genaccounts import gen_fake_identities, gen_subaccounts
from .importers import parse_geo, parse_identities, parse_proxies
from .settings_store import get_global, set_layer
from .scheduler import (LAUNCH_FAILED_PREFIX, _LAUNCH_BLOCKED_BOOKING_STATUSES, _aware,
                        Scheduler)
from .supervisor import Supervisor
from .schemas import (
    BookedCredentialsRequest,
    BookedIdentityOut,
    ArtifactOut,
    AttachIdentity,
    AuditRowOut,
    BookedAppointmentOut,
    BookingCreate,
    BookingOut,
    BookingUpdate,
    BootstrapAdmin,
    GenSubAccounts,
    GenTestIdentities,
    GlobalSettingsPut,
    IdentityCreate,
    IdentityImport,
    IdentityOut,
    IdentityUpdate,
    ImportResult,
    LoginRequest,
    LoginResponse,
    ProxyCreate,
    ProxyImport,
    ProxyOut,
    RegistrationJobOut,
    RegistrationRunRequest,
    SlotOut,
    SubAccountOut,
    SubAccountResetRequest,
    TemplateCreate,
    TemplateOut,
    UserCreate,
    UserOut,
    WhoAmI,
    WorkerOut,
)

logger = logging.getLogger(__name__)

supervisor = Supervisor(settings.redis_url)
event_hub = EventHub(settings.redis_url)
_bus = Bus(connect(settings.redis_url))

# A live-mode launch now runs a real-portal health probe before leasing a proxy (see
# _do_launch_worker's lease_proxy(..., health_check=check_proxy) calls) -- launching a whole fleet
# serially through that would take minutes for a large booking and, worse, stall whichever thread
# runs the loop for its entire duration. Bounded concurrency here keeps a fleet launch's wall-clock
# time close to fleet_size/_LAUNCH_FLEET_CONCURRENCY instead of fleet_size, and keeps the DB
# connection pool (pool_size=10, max_overflow=20, see db.py) comfortably headroomed against the
# Hub's other concurrent DB users (request handlers, the scheduler tick, the event consumer).
_LAUNCH_FLEET_CONCURRENCY = 8


def _resolve_booking_setting(db: Session, bk: M.Booking, key: str, default):
    """One-off booking-level setting lookup (global<booking, no template layer) for call sites that
    aren't inside the Scheduler class (which has its own _effective_booking_setting for the same
    global<booking<template resolution, but needs a template per-worker -- this is booking-only)."""
    eff = resolve({L.GLOBAL: get_global(db), L.BOOKING: (bk.settings_json or {})})
    return eff.get(key, default)


# Bookings with a fleet launch currently in flight. A live-mode launch is SLOW -- each worker's
# proxy lease runs real browser health probes against the portal (libs/proxies/health.py), up to
# max_probes candidates, twice over when HOLD_SWAP_AFTER_FOUND is on -- so a six-worker fleet takes
# minutes of wall clock, not milliseconds.
#
# Confirmed live 2026-08-14 ("start button doesn't work"): /bookings/{id}/start ran that fleet
# launch INLINE, so the HTTP request simply never came back. The operator clicked Start eight more
# times; every click started ANOTHER full fleet launch, each spawning its own probe browsers
# (observed: 234 concurrent chrome processes, 8.5GB, for a 6-worker booking). The scheduler's own
# auto-start path had the same shape and was worse -- it blocked the whole tick thread, so the
# booking's status never advanced past SCHEDULED and plan_booking_actions re-fired "start" on the
# next tick, forever.
_launching: set[str] = set()
_launching_lock = threading.Lock()


def _begin_fleet_launch(booking_id: str, worker_ids: list[str], *, jitter_secs: float = 0.0) -> bool:
    """Kick off a fleet launch on a background thread. Returns False (and does nothing) if one is
    already in flight for this booking -- that idempotence is what makes the Start button safe to
    click twice, and what stops the scheduler stacking a launch per tick."""
    if not worker_ids:
        return False
    with _launching_lock:
        if booking_id in _launching:
            return False
        _launching.add(booking_id)

    def _run() -> None:
        try:
            _launch_worker_fleet(worker_ids, jitter_secs=jitter_secs)
        except Exception:
            # _launch_worker_fleet already isolates per-worker failures, so reaching here means
            # something broke around the whole fleet. Swallow it: an exception escaping a daemon
            # thread kills the thread silently and would leave `_launching` set forever without the
            # finally below -- and there is no caller left to hand it to.
            logger.exception("fleet launch failed for booking %s", booking_id)
        finally:
            with _launching_lock:
                _launching.discard(booking_id)

    threading.Thread(target=_run, name=f"launch-{booking_id[:8]}", daemon=True).start()
    return True


def _launch_worker_fleet(worker_ids: list[str], *, jitter_secs: float = 0.0) -> list[str]:
    """Launch many workers concurrently, each on its OWN DB session (SQLAlchemy Sessions aren't
    thread-safe to share) -- needed now that a live-mode launch's proxy lease can involve a live
    network probe per candidate (libs/proxies/health.py's check_proxy), which is too slow to run
    fleet_size times in a serial loop. Each worker is launched independently: one worker's failure
    (e.g. its proxy pool momentarily exhausted) does NOT prevent the others from being attempted,
    unlike the old serial `for w in workers: _do_launch_worker(db, w)` loop, where an exception
    raised mid-loop (_do_launch_worker re-raises after marking the worker FAILED) aborted every
    worker after it in iteration order. Returns the ids of workers whose launch raised, for the
    caller to log -- a failed worker's own row is already left in a terminal FAILED state by
    _do_launch_worker itself, so callers don't need to do anything else with this list.

    `jitter_secs` (WARMUP_START_JITTER, operator spec 2026-08-14: "hub spawns worker processors
    (concurrency window + jitter do matter)") staggers each worker's actual spawn moment by a random
    delay in [0, jitter_secs) BEFORE it does anything (including its proxy-lease health probe) --
    anti-herd, same principle as RESTORE_STAMPEDE_JITTER, so a fleet's warmup doesn't all hit the
    portal/proxy pool in the same instant. The concurrency window (_LAUNCH_FLEET_CONCURRENCY) still
    bounds how many run at once; jitter only spreads out WHEN each one starts within that window."""
    failed: list[str] = []

    def _one(worker_id: str) -> None:
        if jitter_secs > 0:
            time.sleep(random.uniform(0, jitter_secs))
        db = SessionLocal()
        try:
            w = db.get(M.Worker, worker_id)
            if w is not None:
                _do_launch_worker(db, w)
        finally:
            db.close()

    if not worker_ids:
        return failed
    with ThreadPoolExecutor(max_workers=_LAUNCH_FLEET_CONCURRENCY) as ex:
        futs = {ex.submit(_one, wid): wid for wid in worker_ids}
        for fut in as_completed(futs):
            wid = futs[fut]
            try:
                fut.result()
            except Exception as e:
                # Record WHY, don't just count it. Confirmed live 2026-08-14: two workers on a
                # 7-worker booking sat at READY forever while the other five ran. Their launches had
                # raised (proxy lease exhausting its health-probe budget, which raises BEFORE spawn
                # and so correctly leaves the worker READY and relaunchable) -- but the exception
                # was swallowed here and the `failed` list discarded by the caller, so there was no
                # log line, no event, and no field on the row saying anything at all. The operator
                # saw "ready" and had no way to learn more.
                failed.append(wid)
                reason = getattr(e, "detail", None) or f"{e.__class__.__name__}: {e}"
                logger.warning("worker %s failed to launch: %s", wid, reason)
                db = SessionLocal()
                try:
                    w = db.get(M.Worker, wid)
                    if w is not None:
                        w.substate_detail = f"{LAUNCH_FAILED_PREFIX} {str(reason)[:200]}"
                        db.commit()
                finally:
                    db.close()
    return failed


# ── auto-ops scheduler callbacks ──────────────────────────────────────────────
def _requeue_stale_workers(db: Session, booking_id: str) -> int:
    """Bring a previous run's workers back to READY so a start can actually launch them.

    Both start paths need this and only ONE had it. The manual /start requeues STOPPED/HELD/FAILED
    (fixed 2026-08-13: "Start Instantly did nothing, because every worker had drifted to
    STOPPED/FAILED and this endpoint only ever looked at READY ones"), while _sched_on_start
    selected READY workers directly -- so a SCHEDULED run fired on time, moved the booking to
    WARMING, and launched ZERO workers. Confirmed live 2026-08-19: warmup fired at 01:10:01 and all
    seven workers stayed `stopped`.

    Shared here so the next fix to this rule cannot reach one caller and miss the other."""
    stale = list(db.scalars(select(M.Worker).where(
        M.Worker.booking_id == booking_id,
        M.Worker.status.in_([WorkerStatus.STOPPED, WorkerStatus.HELD, WorkerStatus.FAILED]))).all())
    for w in stale:
        _reset_worker_bus_state(booking_id, w.id)
        _set_worker_status(w, WorkerStatus.READY)
    if stale:
        db.commit()
    return len(stale)


def _sched_on_start(db: Session, bk: M.Booking) -> None:
    from libs.domain.state_machines import IllegalTransition, booking_transition
    # Requeue FIRST -- a scheduled run is normally the SECOND or later run of a booking, so its
    # workers are sitting STOPPED from last time and a READY-only query returns nothing.
    requeued = _requeue_stale_workers(db, bk.id)
    worker_ids = [w.id for w in db.scalars(select(M.Worker).where(
        M.Worker.booking_id == bk.id, M.Worker.status == WorkerStatus.READY)).all()]
    logger.info("scheduled start for booking %s: requeued %d, launching %d worker(s)",
                bk.id, requeued, len(worker_ids))
    jitter = float(_resolve_booking_setting(db, bk, "WARMUP_START_JITTER", 0.0) or 0.0)
    # Backgrounded: this used to run inline and block the scheduler's whole tick thread for the
    # minutes a live fleet launch takes -- during which the status transition below never ran, so
    # plan_booking_actions saw SCHEDULED again next tick and re-fired "start" indefinitely.
    try:
        bk.status = booking_transition(bk.status, BookingStatus.WARMING)
    except IllegalTransition:
        pass
    db.commit()
    # After the commit, for the same reason as start_booking: the launch thread uses its own
    # session, so it must not be able to read the booking in its pre-start status.
    _begin_fleet_launch(bk.id, worker_ids, jitter_secs=jitter)


def _sched_on_slot_open(db: Session, bk: M.Booking) -> None:
    """Publishes the broadcast + flips booking status. Per-worker command delivery is the
    scheduler's own _latch_signals' job (D5) -- called every tick, confirm-before-mark, so a worker
    that starts/respawns after this fires still gets notified (the confirmed 2026-08-07 bug's fix)."""
    from libs.domain.state_machines import IllegalTransition, booking_transition
    _bus.publish_signal(bk.id, "slot_open")
    try:
        bk.status = booking_transition(bk.status, BookingStatus.RUNNING)
    except IllegalTransition:
        pass
    db.commit()


def _sched_on_assign(db: Session, worker: M.Worker, slot: M.Slot) -> None:
    _bus.send_command(worker.id, "assign",
                      slot={"id": slot.id, "date": slot.date, "period": slot.period})


def _reset_worker_bus_state(booking_id: str, worker_id: str) -> None:
    """A worker about to get a fresh process incarnation (relaunch/requeue) must start with a clean
    slate on the bus:
      - its command stream -- a stale unconsumed command (most commonly an earlier "stop") would
        otherwise poison the very next "launch": BusSignals.wait_launch() drains its whole batch and
        returns on the FIRST match in stream order, so a stale stop ahead of the fresh launch makes
        the new process exit immediately without ever reaching its own launch payload (confirmed
        bug, 2026-08-13: "I click start, no workers start").
      - its hold-respawn backoff counter (`respawn:<id>`) -- an operator-initiated fresh start
        should reset an exhausted MAX_RESPAWN_ATTEMPTS count, not leave it permanently given up on.
      - its membership in the slot_open/slot_found "already notified" sets
        (libs.bus.names.notified_key). That set is keyed by the STABLE worker_id and is meant to
        survive a HUB restart (the Hub shouldn't forget who it already told) but must NOT survive
        the WORKER's own restart: a freshly-spawned process has an empty command stream and has
        never actually received the signal in ITS OWN lifetime. Confirmed bug (2026-08-13): a
        booking whose signals fired hours earlier, restarted after several workers died/were
        stopped, had no way to get slot_open/slot_found to the new processes -- apps/hub/
        scheduler.py's _latch_signals thought it had nothing left to do, and the manual
        fire-slot-open/fire-slot-found endpoints are correctly idempotent no-ops once the BOOKING
        itself has fired (that's the right, durable half) -- so "I can't fire any signal after
        restart" was actually "the relaunched worker was silently never told, and the booking
        correctly refused to re-fire something that already happened."
    """
    _bus.delete_worker_stream(worker_id)
    _bus.r.delete(respawn_key(worker_id))
    _bus.r.srem(notified_key(booking_id, "slot_open"), worker_id)
    _bus.r.srem(notified_key(booking_id, "slot_found"), worker_id)


def _require_worker_action(w: M.Worker, action: str) -> None:
    """409 unless `action` is legal from this worker's current status (libs.domain.state_machines'
    WORKER_ACTION_STATUSES -- the same table the dashboard mirrors for button enablement).

    Confirmed bug (2026-08-14): launch/restart had NO status guard at all. Launching an already
    RUNNING worker was a no-op at the process level (Supervisor.spawn is idempotent) but still ran
    _do_launch_worker's unconditional lease_proxy, and lease_proxy has no per-worker dedupe -- so
    every extra click added a permanently unreleased ProxyLease row (two, with swap on) and
    overwrote the frozen effective_settings_json. Worse, both actions were live on COMPLETED
    workers, i.e. ones that had already booked a real appointment."""
    from libs.domain.state_machines import can_worker_action
    if not can_worker_action(action, w.status):
        raise HTTPException(409, f"cannot {action} worker {w.id}: status is {w.status}")


def _set_worker_status(w: M.Worker, status: WorkerStatus, *, detail: str | None = None) -> None:
    """The ONLY sanctioned way to write Worker.status outside the event consumer, because it also
    clears `substate`.

    Confirmed bug (2026-08-14): apps/hub/events.py's map_event_to_update was the only writer of
    `substate` anywhere, so every endpoint/reaper that moved a worker out of RUNNING left the
    in-flight substate behind -- stop/restart/requeue produced rows reading `status=ready,
    substate=holding`, and the dashboard faithfully rendered a worker as "holding" when it wasn't
    running at all. `substate` describes what a RUNNING worker is doing right now; for any other
    status it must be NULL, and the last-known context lives in `substate_detail` + the logs.

    The worker's own `started`/`status_change` events re-populate it the moment a new process is
    actually alive, so clearing here never loses live information -- it only drops the stale corpse
    of a previous incarnation."""
    w.status = status
    w.substate = None
    # Clear the detail too unless a NEW one is supplied. It describes why the worker reached its
    # LAST state; carrying it forward means a relaunched worker still reads "no live process
    # (dead-worker reap)" while it is demonstrably alive and running -- observed 2026-08-14, and
    # doubly misleading now that the dashboard surfaces this as the status tooltip.
    w.substate_detail = detail


def _sched_on_relaunch(db: Session, worker: M.Worker) -> None:
    """Hold-phase respawn (D2) — relaunch a single FAILED worker via the same launch path a fresh
    start uses. The scheduler's _hold_respawn already stopped the old process first (F15) and gates
    this call with per-worker backoff + a hard attempt cap (F6) before ever calling it."""
    _reset_worker_bus_state(worker.booking_id, worker.id)
    _set_worker_status(worker, WorkerStatus.READY)
    db.commit()
    _do_launch_worker(db, worker)


scheduler = Scheduler(
    SessionLocal, on_start=_sched_on_start, on_slot_open=_sched_on_slot_open,
    on_assign=_sched_on_assign, on_relaunch=_sched_on_relaunch,
    supervisor=supervisor, event_hub=event_hub, redis_client=_bus.r, bus=_bus,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        db = SessionLocal()
        seed_defaults(db)      # form_defaults / quest_steps into the DB if absent
        seed_templates(db)     # default scout + apply worker templates
        # Re-discover worker processes a PREVIOUS Hub instance spawned (confirmed bug: without
        # this, a restarted Hub could neither stop nor correctly detect-as-alive any worker that
        # predated it -- the booking Stop button silently no-oped for them, and the dead-worker
        # reaper would eventually mark them FAILED despite being perfectly healthy). Needs DB
        # access for the "which workers does the DB say are RUNNING" list, which is why this lives
        # here rather than in Supervisor's own constructor.
        running_ids = [w.id for w in db.scalars(
            select(M.Worker).where(M.Worker.status == WorkerStatus.RUNNING)).all()]
        # The Hub runs the vendored browser too -- libs/proxies/health.py's check_proxy, called by
        # lease_proxy on every live launch and by EventHub's proxy_request handler. It read the
        # vendor's env defaults (NAV_TIMEOUT_SCALE=1.0, anti-detect entirely off) while the WORKERS
        # that would use those very proxies ran at the operator's configured settings -- so the Hub
        # screened with a harsher instrument than the one doing the real work, and every false
        # negative set is_healthy=False, which _candidate_query filters on, permanently removing a
        # good proxy from the pool. Applied from the GLOBAL layer: process-wide env can't be
        # per-booking, and global is the layer that isn't a per-run choice.
        # RESOLVED, not the raw stored rows. The settings table holds only what an operator
        # explicitly set (credentials, here), never the catalog's defaults -- so this passed a dict
        # containing none of the 13 vendor knobs and apply() exported {}, leaving the probe on the
        # vendor's own defaults: stealth_harden=0 det_fp=0 webrtc_block=0 human=0 headless=old
        # fp_seed=NONE. Exactly the harsher-instrument problem this block was written to prevent,
        # still happening. Confirmed live 2026-08-15 in the session banner of a Hub-side probe.
        exported = vendor_env.apply(resolve({L.GLOBAL: get_global(db)}))
        logger.info("vendor env applied for Hub-side proxy probes: %s", exported)
        db.close()
        supervisor.reconcile({wid: "apps.worker" for wid in running_ids})
    except Exception:
        pass                   # DB may be down at boot; endpoints still report degraded
    event_hub.start()          # background thread tailing the evt stream -> DB + WS
    scheduler.start()          # auto-ops: scheduled start + slot-open + slot assignment
    yield
    # Release open dashboard sockets FIRST: uvicorn will not complete shutdown while one is still
    # being served, and ws_events blocks forever on its queue by construction.
    event_hub.close_subscribers()
    scheduler.stop()
    event_hub.stop()
    supervisor.shutdown_all()


app = FastAPI(
    title="storm-hub",
    version="0.1.0",
    lifespan=lifespan,
    # /docs, /redoc, /openapi.json disclose the full endpoint/schema surface unauthenticated --
    # closed by default now the Hub is reachable over the public internet (ENABLE_API_DOCS=1 to
    # open them back up for local dev).
    docs_url="/docs" if settings.enable_api_docs else None,
    redoc_url="/redoc" if settings.enable_api_docs else None,
    openapi_url="/openapi.json" if settings.enable_api_docs else None,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _bday_for_register(raw: str | None) -> str:
    """Identity.birthdate is stored ISO (YYYY-MM-DD) -- the importer normalizes to this on purpose
    (apps/hub/importers.py's _normalize_dob), and libs/domain generators match it. The portal's
    /register endpoint's `bday` field expects YYYY/MM/DD (confirmed against storm-auto's proven
    batch_register.py -- gen_fake_accounts.py's _random_birthdate() emits slash-format directly and
    it's passed straight through, unconverted, into the payload there). Sending ISO dashes through
    unconverted is a real bug: found by comparing this implementation against storm-auto's after a
    live run failed 10/10 registrations. Converts here, at the single point of use, rather than
    changing the stored format (which importers.py and genaccounts.py both already agree on)."""
    if not raw:
        return "1985/01/01"
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw.strip())
    if m:
        y, mo, d = m.groups()
        return f"{y}/{mo}/{d}"
    return raw  # already slash-formatted (or unrecognized) -- pass through as-is


def _client_ip(request: Request) -> str:
    """Real client IP. Correct ONLY because storm-hub.service runs uvicorn with --proxy-headers
    --forwarded-allow-ips=127.0.0.1 -- uvicorn's ProxyHeadersMiddleware then trusts Caddy's
    X-Forwarded-For (which Caddy's reverse_proxy sets automatically) and rewrites request.client
    accordingly. Without that flag this always reads 127.0.0.1 (Caddy's own address), which would
    silently collapse rate limiting and audit-log IPs for every real visitor into one bucket."""
    return request.client.host if request.client else "unknown"


# ── Audit logging (Phase: production hardening) ───────────────────────────────
# A generic middleware, not per-endpoint calls: catches every mutating (state-changing) request —
# including any added later — without having to remember to thread an _audit() call through each
# new endpoint. Never logs request/response bodies (they can carry passwords/tokens); only
# method+path+status+actor+ip. /auth/login is excluded here and audited explicitly below instead,
# since only that endpoint knows the attempted username *before* the password is checked.
_AUDIT_SKIP_PATHS = {"/auth/login"}
_AUDIT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def _audit_middleware(request: Request, call_next):
    response = await call_next(request)
    if request.method in _AUDIT_METHODS and request.url.path not in _AUDIT_SKIP_PATHS:
        try:
            # Resolve the session the same way FastAPI's Depends(get_db) would -- respecting
            # app.dependency_overrides -- so tests (which override get_db onto an isolated
            # in-memory SQLite session) don't leak audit rows into the real production DB.
            db_factory = app.dependency_overrides.get(get_db, get_db)
            gen = db_factory()
            db = next(gen)
            try:
                actor = None
                auth_header = request.headers.get("authorization", "")
                if auth_header.lower().startswith("bearer "):
                    data = try_decode(auth_header[7:])
                    if data:
                        u = db.get(M.User, data.get("sub"))
                        actor = u.username if u else data.get("sub")
                # action stays just the HTTP method (AuditRow.action is VARCHAR(64)); several
                # routes' resolved paths (e.g. /bookings/{36-char-uuid}/fire-slot-open) exceed 64
                # chars once a real id is substituted in, which would silently fail to audit those
                # calls if the method were prefixed onto the path here. Full path -> target
                # (VARCHAR(128), comfortably fits every route in this app).
                db.add(M.AuditRow(
                    actor=actor,
                    action=request.method,
                    target=request.url.path,
                    ts=_now(),
                    payload={"status": response.status_code, "ip": _client_ip(request)},
                ))
                db.commit()
            finally:
                gen.close()
        except Exception:
            pass  # audit logging must never break the actual request
    return response


# ── Health & meta ─────────────────────────────────────────────────────────────
@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    from sqlalchemy import inspect as _inspect
    try:
        db.execute(text("SELECT 1"))
        n = len(_inspect(db.get_bind()).get_table_names())
        return {"status": "ok", "db": "ok", "tables": n}
    except Exception as e:  # infra failure path (e.g. Postgres down)
        return {"status": "degraded", "db": f"error: {e.__class__.__name__}"}


@app.get("/ops/status")
def ops_status(db: Session = Depends(get_db)) -> dict:
    """Minimum-viable operational observability (Phase F3) -- exists specifically because of the
    confirmed incident's root cause: a runaway hold loop drove Redis writes into the hundreds/sec
    for over an hour with nothing surfacing it until the operator noticed live, by hand. This
    endpoint is the cheap, always-on alternative: Redis memory + the hot streams' lengths (should
    stay bounded by Phase F1's maxlen caps), disk headroom, and live worker counts, polled by
    whoever/whatever is watching this deployment."""
    import shutil

    out: dict = {}

    try:
        info = _bus.r.info("memory")
        out["redis"] = {
            "used_memory_human": info.get("used_memory_human"),
            "used_memory_bytes": info.get("used_memory"),
            "maxmemory_bytes": info.get("maxmemory"),
            "evicted_keys": info.get("evicted_keys", 0),
        }
        out["evt_stream_len"] = _bus.r.xlen("evt")
    except Exception as e:
        out["redis"] = {"error": f"{e.__class__.__name__}: {e}"}

    try:
        disk = shutil.disk_usage("/")
        out["disk"] = {"free_bytes": disk.free, "total_bytes": disk.total,
                       "free_pct": round(100 * disk.free / disk.total, 1)}
    except Exception as e:
        out["disk"] = {"error": f"{e.__class__.__name__}: {e}"}

    try:
        running = db.scalars(select(func.count()).select_from(M.Worker)
                             .where(M.Worker.status == WorkerStatus.RUNNING)).one()
        by_role = dict(db.execute(
            select(M.Worker.role, func.count()).where(M.Worker.status == WorkerStatus.RUNNING)
            .group_by(M.Worker.role)
        ).all())
        out["workers"] = {"running": running, "by_role": {str(k): v for k, v in by_role.items()},
                          "supervisor_processes": len(supervisor.running_ids())}
    except Exception as e:
        out["workers"] = {"error": f"{e.__class__.__name__}: {e}"}

    return out


@app.get("/audit", response_model=list[AuditRowOut])
def list_audit(limit: int = 200, db: Session = Depends(get_db),
               me: Principal = Depends(current_principal)) -> list[M.AuditRow]:
    limit = max(1, min(limit, 1000))
    return list(db.scalars(select(M.AuditRow).order_by(M.AuditRow.ts.desc()).limit(limit)).all())


@app.get("/bookings/{booking_id}/appointments", response_model=list[BookedAppointmentOut])
def list_appointments(booking_id: str, since_started: bool = False,
                      db: Session = Depends(get_db),
                      me: Principal = Depends(current_principal)) -> list[BookedAppointmentOut]:
    """Appointments for a booking, with the identity name and the sub-account that booked resolved
    HERE rather than in the dashboard: it used to join against /identities, which no longer returns
    identities that have booked -- and every appointment is by definition one of those.

    `since_started` limits the list to the booking's CURRENT run (booked_at >= started_at), which is
    what the booking page wants: appointments from previous runs are history, not this run's result.
    """
    bk = db.get(M.Booking, booking_id)
    if not bk:
        raise HTTPException(404, "booking not found")
    # OUTER joins on purpose: these exist only to put a NAME on the row. An inner join would let a
    # missing identity or sub-account row HIDE a real appointment, which is the one thing this
    # endpoint must never do -- the appointment is the record, the name is decoration.
    q = (select(M.BookedAppointment, M.Identity.full_name, M.SubAccount.username)
         .outerjoin(M.Identity, M.Identity.id == M.BookedAppointment.identity_id)
         .outerjoin(M.SubAccount, M.SubAccount.id == M.BookedAppointment.sub_account_id)
         .where(M.BookedAppointment.booking_id == booking_id)
         .order_by(M.BookedAppointment.booked_at.desc()))
    if since_started and bk.started_at:
        q = q.where(M.BookedAppointment.booked_at >= bk.started_at)
    out = []
    for appt, name, username in db.execute(q).all():
        row = BookedAppointmentOut.model_validate(appt)
        row.identity_name, row.sub_account_username = name, username
        out.append(row)
    return out


@app.get("/bookings/{booking_id}/slots", response_model=list[SlotOut])
def list_slots(booking_id: str, db: Session = Depends(get_db),
               me: Principal = Depends(current_principal)) -> list[M.Slot]:
    """Slots a scout has found for this booking (libs.db.models.Slot, written by
    events.py's _persist_slots on every slots_found event) -- no read endpoint existed for these
    before; the dashboard's Slots tab is the first consumer."""
    return list(db.scalars(
        select(M.Slot).where(M.Slot.booking_id == booking_id)
        .order_by(M.Slot.date.asc(), M.Slot.period.asc())
    ).all())


@app.delete("/bookings/{booking_id}/slots")
def clear_slots(booking_id: str, db: Session = Depends(get_db),
                _op: Principal = Depends(require_operator)) -> dict:
    """Retire this booking's discovered slots (dashboard Slots tab "Clear" button).

    Same semantics as the retirement /start already performs, and for the same reason: slots are
    per-run OBSERVATIONS of a portal that has moved on, and a stale pool makes the scheduler fire
    slot_found on the very next tick and skip the whole discovery phase. Exposed as its own action
    so the operator can drop a pool that has gone phantom without restarting the run.

    CLOSED, not deleted: SlotStatus.CLOSED already means "no longer believed present" and reopens on
    rediscovery, so history (and any BookedAppointment FK) stays intact. TAKEN is left alone -- that
    is a real booking, not an observation, and is never something a cleanup click may erase."""
    bk = db.get(M.Booking, booking_id)
    if not bk:
        raise HTTPException(404, "booking not found")
    slots = db.scalars(select(M.Slot).where(
        M.Slot.booking_id == booking_id,
        M.Slot.status.notin_([SlotStatus.TAKEN, SlotStatus.CLOSED]))).all()
    for slot in slots:
        for lease in db.scalars(select(M.SlotLease).where(
                M.SlotLease.slot_id == slot.id, M.SlotLease.released_at.is_(None))).all():
            lease.released_at = _now()
            lease.outcome = SlotLeaseOutcome.EXPIRED
        slot.status = SlotStatus.CLOSED
    # Clearing the pool means the found-phase is over as far as this booking is concerned: leave
    # slot_found latched and the next tick just re-fires it off whatever is rediscovered.
    bk.slot_found_fired_at = None
    try:
        _bus.r.delete(notified_key(booking_id, "slot_found"))
    except Exception:
        pass
    db.commit()
    return {"cleared": len(slots)}


@app.get("/artifacts/{artifact_id}")
def get_artifact_meta(artifact_id: str, db: Session = Depends(get_db),
                      me: Principal = Depends(current_principal)) -> ArtifactOut:
    a = db.get(M.Artifact, artifact_id)
    if not a:
        raise HTTPException(404, "artifact not found")
    return a


@app.get("/artifacts/{artifact_id}/file")
def get_artifact_file(artifact_id: str, db: Session = Depends(get_db),
                      me: Principal = Depends(current_principal)) -> FileResponse:
    # PDFs contain passport/PII -- gated behind auth even though several other list endpoints in
    # this app are open-read, because this is a document download, not a status table.
    a = db.get(M.Artifact, artifact_id)
    if not a:
        raise HTTPException(404, "artifact not found")
    p = Path(a.path)
    if not p.is_file():
        raise HTTPException(404, "artifact file missing on disk")
    media = "application/pdf" if a.kind == "pdf" else "application/octet-stream"
    return FileResponse(p, media_type=media, filename=p.name)


@app.get("/settings/catalog")
def settings_catalog() -> dict:
    """The settings taxonomy (§6), grouped by home layer — powers the dashboard settings editors.

    The "booking" list is the one exception to "home layer only": it also includes settings whose
    home is elsewhere but that declare BOOKING as an override layer (currently just
    HOLD_SWAP_AFTER_FOUND, home=TEMPLATE) -- the Booking Settings tab needs the full set of what's
    actually legal to set at BOOKING, not just what's homed there."""
    out: dict[str, list[dict]] = {}
    for layer in (L.GLOBAL, L.BOOKING, L.TEMPLATE):
        settings = (
            sorted((s for s in CATALOG.values() if layer in s.settable_layers()), key=lambda s: (s.group, s.key))
            if layer == L.BOOKING else settings_for_layer(layer)
        )
        out[str(layer)] = [
            {
                "key": s.key,
                "type": s.type.__name__,
                "default": s.default,
                "group": s.group,
                "home": str(s.home),
                "override_layers": sorted(str(x) for x in s.override_layers),
                "note": s.note,
            }
            for s in settings
        ]
    out["_meta"] = {"total": len(CATALOG), "precedence": "worker > template > booking > global"}
    return out


# ── Auth & sessions (§12c / R2-C7) ────────────────────────────────────────────
@app.post("/auth/bootstrap", response_model=UserOut, status_code=201)
def bootstrap_admin(body: BootstrapAdmin, db: Session = Depends(get_db)) -> M.User:
    """Create the FIRST admin — only works while the users table is empty (headless bootstrap)."""
    if db.scalar(select(func.count()).select_from(M.User)):
        raise HTTPException(409, "already bootstrapped; ask an admin to create users")
    user = M.User(username=body.username, password_hash=hash_password(body.password), role=UserRole.ADMIN)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@app.post("/auth/users", response_model=UserOut, status_code=201)
def create_user(
    body: UserCreate, db: Session = Depends(get_db), me: Principal = Depends(current_principal)
) -> M.User:
    if me.user.role != UserRole.ADMIN:
        raise HTTPException(403, "only admin can create users")
    if db.scalar(select(M.User).where(M.User.username == body.username)):
        raise HTTPException(409, "username taken")
    user = M.User(username=body.username, password_hash=hash_password(body.password), role=body.role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_LOCKOUT_SECONDS = 900  # 15 min


@app.post("/auth/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)) -> LoginResponse:
    """Brute-force protection: a Redis-backed per-IP fail counter (defense in depth alongside the
    fail2ban jail watching Caddy's access log at infra/Caddyfile — that one bans at the firewall
    level; this one blocks at the app level even if fail2ban is down/misconfigured). Counter is
    per-IP, not per-username, so a single attacker can't spread guesses across many usernames to
    dodge it. Relies on _client_ip resolving the real visitor IP, not Caddy's own address."""
    ip = _client_ip(request)
    fail_key = f"loginfail:{ip}"
    r = _bus.r
    fails = int(r.get(fail_key) or 0)
    if fails >= _LOGIN_MAX_ATTEMPTS:
        ttl = r.ttl(fail_key)
        raise HTTPException(429, f"too many failed login attempts; try again in {max(int(ttl), 1)}s")

    user = db.scalar(select(M.User).where(M.User.username == body.username))
    if not user or not verify_password(body.password, user.password_hash):
        pipe = r.pipeline()
        pipe.incr(fail_key)
        pipe.expire(fail_key, _LOGIN_LOCKOUT_SECONDS)
        pipe.execute()
        db.add(M.AuditRow(actor=body.username, action="login_failed", target=ip, ts=_now(),
                          payload={"ip": ip}))
        db.commit()
        raise HTTPException(401, "bad credentials")

    r.delete(fail_key)
    sess, notice = open_session(db, user, body.purpose)
    user.last_login = _now()
    db.add(M.AuditRow(actor=user.username, action="login_ok", target=ip, ts=_now(),
                      payload={"ip": ip, "purpose": str(body.purpose)}))
    db.commit()
    db.refresh(sess)
    return LoginResponse(
        token=make_token(user.id, sess.id),
        user=UserOut.model_validate(user),
        purpose=sess.purpose,
        session_status=str(sess.status),
        notice=notice,
    )


@app.post("/auth/logout")
def logout(db: Session = Depends(get_db), me: Principal = Depends(current_principal)) -> dict:
    me.session.status = SessionStatus.ENDED
    db.commit()
    return {"status": "logged_out"}


@app.get("/auth/whoami", response_model=WhoAmI)
def whoami(me: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> WhoAmI:
    # The dashboard polls whoami, so this is where a PAUSED seat naturally notices that the holder
    # has gone quiet and takes over -- no manual admin force-release needed for the common case of
    # a closed browser tab. See _expire_stale_operate_seats.
    if me.session.purpose == SessionPurpose.OPERATE and me.session.status == SessionStatus.PAUSED:
        if _expire_stale_operate_seats(db):
            db.refresh(me.session)
            me = Principal(user=me.user, session=me.session)
    return WhoAmI(
        username=me.user.username,
        role=me.user.role,
        purpose=me.session.purpose,
        session_status=str(me.session.status),
        is_operating=me.is_operating,
    )


# An operate seat is held by an ACTIVE session, and only one may exist at a time
# (uq_single_active_operate). Nothing ever expired one, so a browser tab closed hours ago kept the
# lock indefinitely and every later login was PAUSED behind it -- confirmed live 2026-08-14: a
# session last seen at 03:36 was still holding the seat at 09:16, so the operator's own 08:47 login
# was read-only. Every settings field and Save button silently disabled, which reads as "saving is
# broken" rather than "you don't hold the seat".
OPERATE_SEAT_STALE_AFTER = 1800.0   # seconds since last_seen


def _expire_stale_operate_seats(db: Session) -> int:
    """End ACTIVE operate sessions that have gone quiet, and promote the longest-waiting PAUSED one.

    Deliberately conservative: only ACTIVE-and-silent seats are ended, and only when something is
    actually waiting or asking. A live operator is never bumped -- whoami refreshes last_seen."""
    now = _now()
    cutoff = now - timedelta(seconds=OPERATE_SEAT_STALE_AFTER)
    stale = db.scalars(select(M.UserSession).where(
        M.UserSession.purpose == SessionPurpose.OPERATE,
        M.UserSession.status == SessionStatus.ACTIVE,
        M.UserSession.last_seen < cutoff,
    )).all()
    for s in stale:
        s.status = SessionStatus.ENDED
    if not stale:
        return 0
    db.flush()
    # Promote the oldest waiting seat so the lock doesn't simply sit empty.
    waiting = db.scalars(select(M.UserSession).where(
        M.UserSession.purpose == SessionPurpose.OPERATE,
        M.UserSession.status == SessionStatus.PAUSED,
    ).order_by(M.UserSession.created_at.asc())).first()
    if waiting is not None:
        waiting.status = SessionStatus.ACTIVE
    db.commit()
    return len(stale)


@app.get("/sessions")
def list_active_sessions(db: Session = Depends(get_db), me: Principal = Depends(current_principal)) -> list[dict]:
    rows = db.scalars(
        select(M.UserSession).where(M.UserSession.status != SessionStatus.ENDED)
    ).all()
    out = []
    for s in rows:
        u = db.get(M.User, s.user_id)
        out.append({"session_id": s.id, "user": u.username if u else None,
                    "purpose": str(s.purpose), "status": str(s.status)})
    return out


# ── Operate-seat handover (§12c) ──────────────────────────────────────────────
@app.post("/sessions/handover/request")
def request_handover(db: Session = Depends(get_db), me: Principal = Depends(current_principal)) -> dict:
    """A paused Operate session asks for the seat. If the seat is free it's granted immediately;
    otherwise a pending request is created for the current holder to grant/deny."""
    if me.session.purpose != SessionPurpose.OPERATE:
        raise HTTPException(400, "log in with purpose=operate before requesting the seat")
    if me.is_operating:
        raise HTTPException(400, "you already hold the operate seat")
    holder = active_operator(db)
    if holder is None:
        me.session.status = SessionStatus.ACTIVE
        db.commit()
        return {"status": "granted", "note": "seat was free"}
    req = M.OpsHandoverRequest(operate_session_id=holder.id, requester_user_id=me.user.id)
    db.add(req)
    db.commit()
    db.refresh(req)
    holder_user = db.get(M.User, holder.user_id)
    return {"status": "pending", "request_id": req.id,
            "current_holder": holder_user.username if holder_user else None}


@app.post("/sessions/handover/{req_id}/grant")
def grant_handover(req_id: str, db: Session = Depends(get_db),
                   op: Principal = Depends(require_operator)) -> dict:
    req = db.get(M.OpsHandoverRequest, req_id)
    if not req:
        raise HTTPException(404, "handover request not found")
    if req.operate_session_id != op.session.id:
        raise HTTPException(403, "only the current holder can grant this request")
    if req.status != HandoverStatus.PENDING:
        raise HTTPException(409, f"request already {req.status}")
    # End my seat first, then promote the requester's paused operate session (keeps the single-active index happy).
    op.session.status = SessionStatus.ENDED
    db.flush()
    reqsess = db.scalars(
        select(M.UserSession).where(
            M.UserSession.user_id == req.requester_user_id,
            M.UserSession.purpose == SessionPurpose.OPERATE,
            M.UserSession.status == SessionStatus.PAUSED,
        ).order_by(M.UserSession.started_at.desc())
    ).first()
    if reqsess:
        reqsess.status = SessionStatus.ACTIVE
    req.status = HandoverStatus.GRANTED
    db.commit()
    return {"status": "granted", "promoted_session": reqsess.id if reqsess else None}


@app.post("/sessions/handover/{req_id}/deny")
def deny_handover(req_id: str, db: Session = Depends(get_db),
                  op: Principal = Depends(require_operator)) -> dict:
    req = db.get(M.OpsHandoverRequest, req_id)
    if not req:
        raise HTTPException(404, "handover request not found")
    if req.operate_session_id != op.session.id:
        raise HTTPException(403, "only the current holder can deny this request")
    req.status = HandoverStatus.DENIED
    db.commit()
    return {"status": "denied"}


@app.post("/sessions/{sid}/force-release")
def force_release(sid: str, db: Session = Depends(get_db),
                  me: Principal = Depends(current_principal)) -> dict:
    """Admin frees a stale operate seat."""
    if me.user.role != UserRole.ADMIN:
        raise HTTPException(403, "only admin can force-release")
    s = db.get(M.UserSession, sid)
    if not s:
        raise HTTPException(404, "session not found")
    s.status = SessionStatus.ENDED
    db.commit()
    return {"status": "released", "session_id": sid}


# ── Identities ────────────────────────────────────────────────────────────────
@app.post("/identities", response_model=IdentityOut, status_code=201)
def create_identity(
    body: IdentityCreate, db: Session = Depends(get_db), _op: Principal = Depends(require_operator)
) -> M.Identity:
    ident = M.Identity(
        full_name=body.full_name,
        type=body.type,
        birthdate=body.birthdate,
        passport_no=body.passport_no,
        gender=body.gender,
        nationality=body.nationality,
        comment=body.comment,
        target_posto=body.target_posto if body.type == "real" else None,
    )
    db.add(ident)
    db.commit()
    db.refresh(ident)
    return ident


@app.get("/identities", response_model=list[IdentityOut])
def list_identities(db: Session = Depends(get_db)) -> list[IdentityOut]:
    """Every identity, each annotated with the ACTIVE booking currently holding it (if any).

    C2 exclusivity is enforced by attach_identity (409 "already allocated to another active
    booking"), but nothing ever exposed the allocation, so the attach panel happily listed
    identities that were guaranteed to fail -- the operator only found out by picking one. One
    grouped query rather than per-identity lookups, so this stays a single round trip for the whole
    list."""
    identities = list(db.scalars(select(M.Identity).order_by(M.Identity.created_at.desc())).all())
    # Keyed on an UNRELEASED allocation, deliberately NOT on BOOKING_ACTIVE_STATUSES: the partial
    # unique index uq_identity_active_allocation is UNIQUE(identity_id) WHERE released_at IS NULL
    # and does not look at booking status at all, so an unreleased row blocks re-attachment no
    # matter what its booking is doing. attach_identity's own 409 check uses the narrower
    # active-status rule, which means a DRAFT/COMPLETED/ABORTED booking's stale allocation passes
    # that check and then hits the index instead -- see the KNOWN-GAP note on attach_identity.
    # Reporting the narrower rule here would make this annotation lie in exactly that case.
    held = {
        identity_id: (booking_id, booking_name)
        for identity_id, booking_id, booking_name in db.execute(
            select(M.IdentityAllocation.identity_id, M.Booking.id, M.Booking.name)
            .join(M.Booking, M.Booking.id == M.IdentityAllocation.booking_id)
            .where(M.IdentityAllocation.released_at.is_(None))
        ).all()
    }
    # Identities that already booked are excluded from this list entirely. The attach panel is its
    # only consumer, and offering a person who already HAS an appointment invites a second one --
    # "if any one of the sub-accounts booked, that identity is mission completed" (operator,
    # 2026-08-19). They remain visible in their own right via /identities/booked.
    booked_ids = {i for (i,) in db.execute(
        select(M.BookedAppointment.identity_id).distinct()).all()}
    out: list[IdentityOut] = []
    for ident in identities:
        if ident.id in booked_ids:
            continue
        row = IdentityOut.model_validate(ident)
        booking = held.get(ident.id)
        if booking:
            row.allocated_booking_id, row.allocated_booking_name = booking
        out.append(row)
    return out


@app.get("/identities/booked", response_model=list[BookedIdentityOut])
def list_booked_identities(type: str | None = None, posto_id: str | None = None,
                           date_from: str | None = None, date_to: str | None = None,
                           db: Session = Depends(get_db),
                           me: Principal = Depends(current_principal)) -> list[BookedIdentityOut]:
    """Identities that have completed their mission -- one row per identity, never per sub-account.

    "If any one of the sub-accounts booked, that identity is mission completed" (operator, 2026-08-19).
    An identity is a PERSON; the sub-accounts are just login attempts at the portal on their behalf,
    so a second appointment for the same person is a defect, not a bonus. This lists them once,
    naming the sub-account that actually got through.

    Filters mirror how the list is used: `type` (real vs test), `posto_id`, and a booked-at date
    range. Readable by any signed-in role -- it carries no credentials (see
    /identities/booked/credentials for those)."""
    q = (select(M.BookedAppointment, M.Identity, M.SubAccount, M.Booking, M.Slot)
         .join(M.Identity, M.Identity.id == M.BookedAppointment.identity_id)
         .join(M.SubAccount, M.SubAccount.id == M.BookedAppointment.sub_account_id)
         .join(M.Booking, M.Booking.id == M.BookedAppointment.booking_id)
         .outerjoin(M.Slot, M.Slot.id == M.BookedAppointment.slot_id)
         .order_by(M.BookedAppointment.booked_at.desc()))
    if type:
        q = q.where(M.Identity.type == type)
    if posto_id:
        q = q.where(M.Booking.posto_id == posto_id)
    if date_from:
        q = q.where(M.BookedAppointment.booked_at >= datetime.fromisoformat(date_from))
    if date_to:
        q = q.where(M.BookedAppointment.booked_at <= datetime.fromisoformat(date_to))

    out: list[BookedIdentityOut] = []
    seen: set[str] = set()
    for appt, ident, sub, bk, slot in db.execute(q).all():
        if ident.id in seen:
            continue      # one row per IDENTITY -- see the docstring
        seen.add(ident.id)
        out.append(BookedIdentityOut(
            identity_id=ident.id, full_name=ident.full_name, type=str(ident.type),
            nationality=ident.nationality, passport_no=ident.passport_no,
            sub_account_id=sub.id, sub_account_username=sub.username,
            booking_id=bk.id, booking_name=bk.name, posto_id=bk.posto_id,
            booked_at=appt.booked_at,
            slot_date=slot.date if slot else None, slot_period=slot.period if slot else None,
            pdf_artifact_id=appt.pdf_artifact_id,
        ))
    return out


@app.post("/identities/booked/credentials")
def export_booked_credentials(body: BookedCredentialsRequest, db: Session = Depends(get_db),
                              me: Principal = Depends(require_admin)) -> dict:
    """Username + password for the sub-accounts that actually booked. ADMIN ONLY.

    This is the one endpoint in the system that returns stored passwords, so it is deliberately
    narrow: admin role only, restricted to identities that HAVE a booking (you cannot use it to dump
    the account pool), and it logs who exported what. It exists because the appointment is useless
    without the credentials to log in and manage it."""
    if not body.identity_ids:
        return {"rows": [], "count": 0}
    rows = db.execute(
        select(M.Identity, M.SubAccount, M.Booking, M.BookedAppointment)
        .join(M.BookedAppointment, M.BookedAppointment.identity_id == M.Identity.id)
        .join(M.SubAccount, M.SubAccount.id == M.BookedAppointment.sub_account_id)
        .join(M.Booking, M.Booking.id == M.BookedAppointment.booking_id)
        .where(M.Identity.id.in_(body.identity_ids))
        .order_by(M.Identity.full_name)
    ).all()
    seen: set[str] = set()
    out = []
    for ident, sub, bk, appt in rows:
        if ident.id in seen:
            continue
        seen.add(ident.id)
        out.append({"full_name": ident.full_name, "username": sub.username,
                    "password": sub.password, "posto_id": bk.posto_id,
                    "booking": bk.name,
                    "booked_at": appt.booked_at.isoformat() if appt.booked_at else None})
    logger.warning("credentials export by admin %s: %d booked identity/identities",
                   me.user.username, len(out))
    return {"rows": out, "count": len(out)}


@app.patch("/identities/{identity_id}", response_model=IdentityOut)
def update_identity(identity_id: str, body: IdentityUpdate, db: Session = Depends(get_db),
                    _op: Principal = Depends(require_operator)) -> M.Identity:
    ident = db.get(M.Identity, identity_id)
    if not ident:
        raise HTTPException(404, "identity not found")
    if body.comment is not None:
        ident.comment = body.comment
    if body.target_posto is not None:
        if ident.type != "real":
            raise HTTPException(400, "target_posto only applies to real identities")
        ident.target_posto = body.target_posto
    db.commit()
    db.refresh(ident)
    return ident


@app.post("/identities/{identity_id}/archive", response_model=IdentityOut)
def archive_identity(identity_id: str, db: Session = Depends(get_db),
                     _op: Principal = Depends(require_operator)) -> M.Identity:
    ident = db.get(M.Identity, identity_id)
    if not ident:
        raise HTTPException(404, "identity not found")
    ident.status = IdentityStatus.ARCHIVED
    ident.archived_at = _now()
    db.commit()
    db.refresh(ident)
    return ident


@app.delete("/identities/{identity_id}", status_code=204)
def delete_identity(identity_id: str, db: Session = Depends(get_db),
                    _op: Principal = Depends(require_operator)) -> None:
    ident = db.get(M.Identity, identity_id)
    if not ident:
        return
    # Blocks on ANY existing worker/booking-identity link, not just an active-status booking --
    # simpler and correct regardless of DB backend (SQLite in tests doesn't enforce FK constraints
    # by default the way Postgres does, so this can't rely on IntegrityError alone as the guard).
    # /bookings/{id}/identities/{id} DELETE (detach) removes these rows first, so a detached
    # identity is always deletable afterward.
    still_attached = db.scalar(select(func.count()).select_from(M.Worker)
                               .where(M.Worker.identity_id == identity_id))
    if still_attached:
        raise HTTPException(409, "identity is attached to a booking -- detach it first")
    try:
        db.delete(ident)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "identity has related records (sub-accounts, bookings, appointments) "
                                 "-- archive it instead of deleting")


@app.post("/identities/import", response_model=ImportResult)
def import_identities(body: IdentityImport, db: Session = Depends(get_db),
                      _op: Principal = Depends(require_operator)) -> ImportResult:
    parsed, errors = parse_identities(body.csv, default_type=str(body.default_type))
    created = subs = 0
    identity_ids: list[str] = []
    sub_account_ids: list[str] = []
    existing = {u for (u,) in db.execute(select(M.SubAccount.username)).all()}
    known_passports = {p for (p,) in db.execute(
        select(M.Identity.passport_no).where(M.Identity.passport_no.isnot(None))).all()}
    for pi in parsed:
        if pi.passport_no and pi.passport_no in known_passports:
            errors.append(f"skip duplicate passport {pi.passport_no}")
            continue
        ident = M.Identity(full_name=pi.full_name, type=pi.type, birthdate=pi.birthdate,
                           passport_no=pi.passport_no, gender=pi.gender, nationality=pi.nationality,
                           status="active" if pi.sub_accounts else "empty")
        if pi.passport_no:
            known_passports.add(pi.passport_no)
        db.add(ident)
        db.flush()
        created += 1
        identity_ids.append(ident.id)
        for sa in pi.sub_accounts:
            if sa.username in existing:
                errors.append(f"skip duplicate username {sa.username}")
                continue
            new_sa = M.SubAccount(identity_id=ident.id, username=sa.username, password=sa.password,
                                  email=sa.email, email_pass=sa.email_pass, status=sa.status)
            db.add(new_sa)
            db.flush()
            existing.add(sa.username)
            subs += 1
            sub_account_ids.append(new_sa.id)
    db.commit()
    return ImportResult(created=created, sub_accounts=subs, errors=errors,
                        identity_ids=identity_ids, sub_account_ids=sub_account_ids)


# ── Sub-account generation ────────────────────────────────────────────────────
@app.post("/identities/gen-subaccounts", response_model=ImportResult)
def gen_subaccounts_bulk(body: GenSubAccounts, db: Session = Depends(get_db),
                         _op: Principal = Depends(require_operator)) -> ImportResult:
    """Generate `count` sub-accounts for each selected identity (multi-select in the dashboard)."""
    existing = {u for (u,) in db.execute(select(M.SubAccount.username)).all()}
    subs = 0
    errors: list[str] = []
    sub_account_ids: list[str] = []
    for iid in body.identity_ids:
        ident = db.get(M.Identity, iid)
        if not ident:
            errors.append(f"identity {iid} not found")
            continue
        first, _, last = (ident.full_name or "x x").partition(" ")
        for sa in gen_subaccounts(first, last or "x", body.count, existing):
            new_sa = M.SubAccount(identity_id=ident.id, username=sa.username, password=sa.password,
                                  status="new")
            db.add(new_sa)
            db.flush()
            subs += 1
            sub_account_ids.append(new_sa.id)
        if ident.status == "empty":
            ident.status = "active"
    db.commit()
    return ImportResult(created=len(body.identity_ids), sub_accounts=subs, errors=errors,
                        identity_ids=list(body.identity_ids), sub_account_ids=sub_account_ids)


@app.post("/identities/gen-test", response_model=ImportResult)
def gen_test_identities(body: GenTestIdentities, db: Session = Depends(get_db),
                        _op: Principal = Depends(require_operator)) -> ImportResult:
    """Generate N fake TEST identities, each with sub-accounts."""
    existing_u = {u for (u,) in db.execute(select(M.SubAccount.username)).all()}
    existing_p = {p for (p,) in db.execute(
        select(M.Identity.passport_no).where(M.Identity.passport_no.isnot(None))).all()}
    gen = gen_fake_identities(body.count, body.accounts_per_identity,
                              existing_usernames=existing_u, existing_passports=existing_p,
                              nationality=body.nationality)
    subs = 0
    for gi in gen:
        ident = M.Identity(full_name=gi.full_name, type="test", birthdate=gi.birthdate,
                           passport_no=gi.passport_no, gender=gi.gender, nationality=gi.nationality,
                           status="active" if gi.sub_accounts else "empty")
        db.add(ident)
        db.flush()
        for sa in gi.sub_accounts:
            db.add(M.SubAccount(identity_id=ident.id, username=sa.username, password=sa.password,
                                status="new"))
            subs += 1
    db.commit()
    return ImportResult(created=len(gen), sub_accounts=subs)


# ── Proxy pool (DB-backed) ────────────────────────────────────────────────────
@app.get("/proxies", response_model=list[ProxyOut])
def list_proxies(db: Session = Depends(get_db)) -> list[M.Proxy]:
    return list(db.scalars(select(M.Proxy).order_by(M.Proxy.created_at.desc())).all())


@app.post("/proxies", response_model=ProxyOut, status_code=201)
def create_proxy(body: ProxyCreate, db: Session = Depends(get_db),
                 _op: Principal = Depends(require_operator)) -> M.Proxy:
    p = M.Proxy(provider=body.provider, kind=body.kind, endpoint=body.endpoint,
                credentials_ref=body.credentials_ref, geo=parse_geo(body.credentials_ref),
                max_sessions=body.max_sessions, active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@app.post("/proxies/import", response_model=ImportResult)
def import_proxies(body: ProxyImport, db: Session = Depends(get_db),
                   _op: Principal = Depends(require_operator)) -> ImportResult:
    parsed, errors = parse_proxies(body.text)
    # dedup on (endpoint, credentials_ref): SOAX shares one endpoint (proxy.soax.com:5000) across
    # many distinct session strings, so endpoint alone would collapse the whole pool to one row.
    existing = {(e, c) for (e, c) in
                db.execute(select(M.Proxy.endpoint, M.Proxy.credentials_ref)).all()}
    created = 0
    for pp in parsed:
        key = (pp.endpoint, pp.credentials_ref)
        if key in existing:
            errors.append(f"skip duplicate {pp.endpoint}")
            continue
        db.add(M.Proxy(provider=body.provider, kind=body.kind, endpoint=pp.endpoint,
                       credentials_ref=pp.credentials_ref, geo=parse_geo(pp.credentials_ref),
                       max_sessions=body.max_sessions, active=True))
        existing.add(key)
        created += 1
    db.commit()
    return ImportResult(created=created, errors=errors)


@app.post("/proxies/{proxy_id}/toggle-active", response_model=ProxyOut)
def toggle_proxy_active(proxy_id: str, db: Session = Depends(get_db),
                        _op: Principal = Depends(require_operator)) -> M.Proxy:
    p = db.get(M.Proxy, proxy_id)
    if not p:
        raise HTTPException(404, "proxy not found")
    p.active = not p.active
    db.commit()
    db.refresh(p)
    return p


@app.post("/proxies/reset-health")
def reset_proxy_health(confirm: bool = False, force: bool = False,
                       db: Session = Depends(get_db),
                       _op: Principal = Depends(require_operator)) -> dict:
    """Clear every proxy's health verdict back to unprobed (dashboard Proxies tab "Reset pool").

    Same operation, same guards and same semantics as
    `scripts/reset_run_state.py --proxy-health` -- exposed here because the situation it exists for
    recurs and shelling into the box for it is friction the operator shouldn't need.

    WHY it is ever needed: lease_proxy filters on is_healthy, and health.apply_result sets
    is_healthy=False on ANY probe failure, so every false negative removes a good proxy from the
    pool PERMANENTLY. Confirmed live 2026-08-15: the Hub's own probe ran with anti-detection off
    (it exported an empty vendor env), screening proxies far more harshly than the workers that
    would use them, and condemned 639 of 2000. Fixing the probe does not un-condemn them; only this
    does.

    Resets to is_healthy=True / health=None / score=0.0 -- "unprobed", not "known good": the next
    lease re-probes, now with the same browser configuration a worker uses.

    Dry-run by default (`confirm=false`) so the operator sees the scope before writing, and refuses
    while workers are RUNNING because they would immediately re-dirty exactly these rows. `force`
    overrides that, matching the script's own escape hatch."""
    running = db.scalars(select(M.Worker).where(M.Worker.status == WorkerStatus.RUNNING)).all()
    if running and not force:
        raise HTTPException(409, f"{len(running)} worker(s) are RUNNING and would immediately "
                                 f"re-dirty this state -- stop the booking first, or pass force")
    total = db.scalar(select(func.count()).select_from(M.Proxy)) or 0
    probed = db.scalar(select(func.count()).select_from(M.Proxy)
                       .where(M.Proxy.health.isnot(None))) or 0
    leasable_before = db.scalar(select(func.count()).select_from(M.Proxy).where(
        M.Proxy.active.is_(True), M.Proxy.is_healthy.is_(True))) or 0
    if confirm:
        for pr in db.scalars(select(M.Proxy).where(M.Proxy.health.isnot(None))).all():
            pr.is_healthy, pr.health, pr.score = True, None, 0.0
        db.commit()
        logger.warning("proxy pool health reset by operator: %d probed row(s) cleared", probed)
    return {"confirmed": confirm, "total": total, "probed": probed,
            "leasable_before": leasable_before,
            "leasable_after": total if confirm else leasable_before,
            "running_workers": len(running)}


# ── Registration subsystem (apps/registrar) ────────────────────────────────────
@app.get("/sub-accounts", response_model=list[SubAccountOut])
def list_sub_accounts(db: Session = Depends(get_db),
                      me: Principal = Depends(current_principal)) -> list[SubAccountOut]:
    """Flattened for the dashboard's Registration page: identity name/type + this sub-account's
    latest registration_jobs row (status/attempts), so the table doesn't need a client-side join."""
    subs = list(db.scalars(select(M.SubAccount).order_by(M.SubAccount.created_at.desc())).all())
    if not subs:
        return []
    ident_ids = {s.identity_id for s in subs}
    idents = {i.id: i for i in db.scalars(select(M.Identity).where(M.Identity.id.in_(ident_ids))).all()}
    sub_ids = [s.id for s in subs]
    jobs = db.scalars(
        select(M.RegistrationJob).where(M.RegistrationJob.sub_account_id.in_(sub_ids))
        .order_by(M.RegistrationJob.created_at.desc())
    ).all()
    latest_job: dict[str, M.RegistrationJob] = {}
    for j in jobs:
        latest_job.setdefault(j.sub_account_id, j)  # first seen per id = most recent (query is DESC)

    out = []
    for s in subs:
        ident = idents.get(s.identity_id)
        job = latest_job.get(s.id)
        out.append(SubAccountOut(
            id=s.id, identity_id=s.identity_id,
            identity_name=ident.full_name if ident else "?",
            identity_type=str(ident.type) if ident else "?",
            username=s.username, status=str(s.status), email=s.email,
            registration_status=str(job.status) if job else None,
            attempts=job.attempts if job else 0,
            created_at=s.created_at,
        ))
    return out


@app.post("/sub-accounts/reset", response_model=ImportResult)
def reset_sub_accounts(body: SubAccountResetRequest, db: Session = Depends(get_db),
                       _op: Principal = Depends(require_operator)) -> ImportResult:
    """Regenerate username+password for selected sub-accounts and clear prior registration state --
    the dashboard's "Reset" button (requested in the recovered conversation history: regen
    credentials for selected accounts, which naturally requeues them -- status back to NEW -- for
    another registration run). Status is set directly, not via subaccount_transition(): this is a
    deliberate operator override, not a normal forward progression (mirrors force_release's same
    direct-assignment treatment of an admin override)."""
    from .genaccounts import make_password, make_username
    existing = {u for (u,) in db.execute(select(M.SubAccount.username)).all()}
    updated = 0
    errors: list[str] = []
    for sid in body.sub_account_ids:
        sa = db.get(M.SubAccount, sid)
        if not sa:
            errors.append(f"{sid}: not found")
            continue
        ident = db.get(M.Identity, sa.identity_id)
        first, _, last = (ident.full_name if ident else "x x").partition(" ")
        sa.username = make_username(first or "x", last or "x", existing)
        sa.password = make_password()
        sa.email = None
        sa.email_pass = None
        sa.notes = "reset"
        sa.status = SubAccountStatus.NEW
        updated += 1
    db.commit()
    return ImportResult(created=updated, errors=errors)


@app.post("/registration/run")
def run_registration(body: RegistrationRunRequest, db: Session = Depends(get_db),
                     _op: Principal = Depends(require_operator)) -> dict:
    subs = [db.get(M.SubAccount, sid) for sid in body.sub_account_ids]
    subs = [s for s in subs if s is not None]
    if not subs:
        raise HTTPException(400, "no valid sub_account_ids")

    # get-or-create + bump the RegistrationJob row for each selected sub-account (one row per
    # sub-account, mutated in place across retries -- the pre-existing schema's own design, see
    # RegistrationJob's attempts counter).
    for sa in subs:
        job = db.scalar(select(M.RegistrationJob).where(M.RegistrationJob.sub_account_id == sa.id)
                        .order_by(M.RegistrationJob.created_at.desc()))
        if job is None:
            job = M.RegistrationJob(sub_account_id=sa.id)
            db.add(job)
        job.mode = body.mode
        job.status = RegistrationStatus.QUEUED
        job.attempts = (job.attempts or 0) + 1
    db.commit()

    # Proxies: a read-only selection, not a per-account DB lease -- ProxyLease.worker_id is a real
    # FK to workers.id, which a registration run has none of, and registration cycles through many
    # proxies briefly rather than holding one for a long booking-style hold phase, so the capacity
    # bookkeeping ProxyLease exists for doesn't fit here. apps/registrar round-robins this list.
    q = select(M.Proxy).where(M.Proxy.provider == body.provider, M.Proxy.active.is_(True),
                              M.Proxy.is_healthy.is_(True))
    if body.kind:
        q = q.where(M.Proxy.kind == body.kind)
    geos = _geos(body.geos)
    if geos:
        # NULL geo is never excluded, exactly as in lease_proxy: a filter narrows the pool, it must
        # not silently empty it for a provider whose credentials carry no country.
        q = q.where(M.Proxy.geo.in_(geos) | M.Proxy.geo.is_(None))
    proxy_rows = list(db.scalars(q).all())
    if not proxy_rows:
        raise HTTPException(409, f"no active healthy {body.provider} proxies available"
                                 + (f" (kind={body.kind})" if body.kind else "")
                                 + (f" (geo={body.geos})" if body.geos else ""))
    proxies = [{"id": p.id, "url": conn_url(p)} for p in proxy_rows]

    g = get_global(db)
    capsolver_keys = [k.strip() for k in str(g.get("CAPSOLVER_KEYS", "")).split(",") if k.strip()]
    if not capsolver_keys:
        raise HTTPException(409, "CAPSOLVER_KEYS not set in global settings")
    email_provider = str(g.get("EMAIL_PROVIDER", "") or "mailtm")
    email_cfg = {
        "cf_mail_domain": g.get("CF_MAIL_DOMAIN"),
        "cf_worker_url": g.get("CF_WORKER_URL"),
        "cf_worker_secret": g.get("CF_WORKER_SECRET"),
    }

    accounts = []
    for sa in subs:
        ident = db.get(M.Identity, sa.identity_id)
        first, _, last = (ident.full_name if ident else "x x").partition(" ")
        accounts.append({
            "sub_account_id": sa.id, "username": sa.username, "password": sa.password,
            "first_name": first, "last_name": last or first,
            "birthdate": _bday_for_register(ident.birthdate if ident else None),
            "gender": (ident.gender if ident else None) or "M",
            "nationality": (ident.nationality if ident else None) or "PRT",
            "traveldoc": (ident.passport_no if ident else None) or "",
        })

    run_id = f"reg-{uuid.uuid4().hex[:12]}"
    supervisor.spawn_registrar(run_id)
    _bus.ensure_command_group(run_id)
    _bus.send_command(
        run_id, "launch", cfg={}, accounts=accounts, proxies=proxies,
        capsolver_keys=capsolver_keys, lang="PT", email_provider=email_provider,
        email_cfg=email_cfg, concurrency=body.concurrency,
        browser_concurrency=body.browser_concurrency or body.concurrency,
    )
    return {"run_id": run_id, "queued": len(accounts)}


@app.get("/registration/jobs", response_model=list[RegistrationJobOut])
def list_registration_jobs(limit: int = 100, db: Session = Depends(get_db),
                           me: Principal = Depends(current_principal)) -> list[M.RegistrationJob]:
    limit = max(1, min(limit, 500))
    return list(db.scalars(
        select(M.RegistrationJob).order_by(M.RegistrationJob.created_at.desc()).limit(limit)
    ).all())


@app.post("/registration/runs/{run_id}/stop")
def stop_registration_run(run_id: str, _op: Principal = Depends(require_operator)) -> dict:
    _bus.send_command(run_id, "stop")
    supervisor.stop(run_id)
    return {"stopped": True}


# ── Global settings (capsolver keys, browser/solver defaults) ─────────────────
_SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASS")


@app.get("/settings/global")
def read_global_settings(db: Session = Depends(get_db),
                         me: Principal = Depends(current_principal)) -> dict:
    """Return stored global settings; secret-looking values are masked."""
    out = {}
    for k, v in get_global(db).items():
        out[k] = "•••set" if v and any(h in k.upper() for h in _SECRET_HINTS) else v
    return out


@app.put("/settings/global")
def write_global_settings(body: GlobalSettingsPut, db: Session = Depends(get_db),
                          _op: Principal = Depends(require_operator)) -> dict:
    try:
        stored = set_layer(db, L.GLOBAL, body.values)
    except Exception as e:
        raise HTTPException(400, f"invalid global settings: {e}")
    return {"saved": sorted(body.values.keys()), "total_stored": len(stored)}


# ── Worker templates ──────────────────────────────────────────────────────────
@app.get("/templates", response_model=list[TemplateOut])
def list_templates(db: Session = Depends(get_db)) -> list[M.WorkerTemplate]:
    return list(db.scalars(select(M.WorkerTemplate).order_by(M.WorkerTemplate.created_at.asc())).all())


@app.post("/templates", response_model=TemplateOut, status_code=201)
def create_template(body: TemplateCreate, db: Session = Depends(get_db),
                    _op: Principal = Depends(require_operator)) -> M.WorkerTemplate:
    settings_json = dict(body.settings or {})
    settings_json.setdefault("ROLE", str(body.role))
    try:
        validate_layer(L.TEMPLATE, settings_json)
    except Exception as e:
        raise HTTPException(400, f"invalid template settings: {e}")
    tpl = M.WorkerTemplate(name=body.name, role=body.role, settings_json=settings_json)
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return tpl


@app.put("/templates/{template_id}", response_model=TemplateOut)
def update_template(template_id: str, body: TemplateCreate, db: Session = Depends(get_db),
                    _op: Principal = Depends(require_operator)) -> M.WorkerTemplate:
    tpl = db.get(M.WorkerTemplate, template_id)
    if not tpl:
        raise HTTPException(404, "template not found")
    settings_json = dict(body.settings or {})
    settings_json.setdefault("ROLE", str(body.role))
    try:
        validate_layer(L.TEMPLATE, settings_json)
    except Exception as e:
        raise HTTPException(400, f"invalid template settings: {e}")
    tpl.name, tpl.role, tpl.settings_json = body.name, body.role, settings_json
    db.commit()
    db.refresh(tpl)
    return tpl


@app.delete("/templates/{template_id}", status_code=204)
def delete_template(template_id: str, db: Session = Depends(get_db),
                    _op: Principal = Depends(require_operator)) -> None:
    tpl = db.get(M.WorkerTemplate, template_id)
    if tpl:
        db.delete(tpl)
        db.commit()


# ── Editable configs (form_defaults, quest_steps) ─────────────────────────────
@app.get("/configs")
def list_configs(db: Session = Depends(get_db), me: Principal = Depends(current_principal)) -> list[str]:
    return [r.key for r in db.scalars(select(M.AppConfig)).all()]


@app.get("/configs/{key}")
def read_config(key: str, db: Session = Depends(get_db),
                me: Principal = Depends(current_principal)) -> dict:
    val = get_config(db, key)
    if val is None:
        raise HTTPException(404, f"config {key} not found")
    return {"key": key, "value": val}


@app.put("/configs/{key}")
def write_config(key: str, body: dict, db: Session = Depends(get_db),
                 _op: Principal = Depends(require_operator)) -> dict:
    value = body.get("value", body)
    if not isinstance(value, (dict, list)):
        raise HTTPException(400, "config value must be a JSON object or array")
    set_config(db, key, value)
    return {"key": key, "saved": True}


# ── Bookings ──────────────────────────────────────────────────────────────────
@app.post("/bookings", response_model=BookingOut, status_code=201)
def create_booking(
    body: BookingCreate, db: Session = Depends(get_db), _op: Principal = Depends(require_operator)
) -> M.Booking:
    # Validate the booking-layer settings against the catalog (no unknown/wrong-layer keys).
    try:
        clean = validate_layer(L.BOOKING, body.settings)
    except Exception as e:
        raise HTTPException(400, f"invalid booking settings: {e}")
    bk = M.Booking(
        name=body.name,
        type=body.type,
        posto_id=body.posto_id,
        nationality=body.nationality,
        residence=body.residence,
        warmup_start_at=body.warmup_start_at,
        slot_open_at=body.slot_open_at,
        settings_json=clean,
    )
    db.add(bk)
    db.commit()
    db.refresh(bk)
    return bk


@app.put("/bookings/{booking_id}", response_model=BookingOut)
def update_booking(booking_id: str, body: BookingUpdate, db: Session = Depends(get_db),
                   _op: Principal = Depends(require_operator)) -> M.Booking:
    bk = db.get(M.Booking, booking_id)
    if not bk:
        raise HTTPException(404, "booking not found")
    if body.settings is not None:
        try:
            bk.settings_json = validate_layer(L.BOOKING, body.settings)
        except Exception as e:
            raise HTTPException(400, f"invalid booking settings: {e}")
    for field in ("name", "nationality", "residence", "warmup_start_at", "slot_open_at"):
        val = getattr(body, field)
        if val is not None:
            setattr(bk, field, val)
    db.commit()
    db.refresh(bk)
    return bk


@app.get("/bookings", response_model=list[BookingOut])
def list_bookings(db: Session = Depends(get_db)) -> list[M.Booking]:
    return list(db.scalars(select(M.Booking).order_by(M.Booking.created_at.desc())).all())


@app.get("/bookings/{booking_id}", response_model=BookingOut)
def get_booking(booking_id: str, db: Session = Depends(get_db)) -> M.Booking:
    bk = db.get(M.Booking, booking_id)
    if not bk:
        raise HTTPException(404, "booking not found")
    return bk


@app.delete("/bookings/{booking_id}", status_code=204)
def delete_booking(booking_id: str, db: Session = Depends(get_db),
                   _op: Principal = Depends(require_operator)) -> None:
    """Permanently remove a booking and everything scoped to it -- there was previously no way to
    delete a booking at all (only nested resources: identities-within-a-booking, templates). Refuses
    to touch a booking that ever actually booked something (a real BookedAppointment row) -- that's
    exactly the data this whole system exists to produce, never something to let a cleanup click
    wipe by accident. Everything else scoped to the booking (workers, their proxy/slot leases and
    artifacts, slots, the identity attachments/allocations) is deleted; each RUNNING worker is
    stopped first (bus stop + supervisor kill + the same bus-state reset stop_worker uses) so no
    orphaned process keeps running against rows that no longer exist. An identity's own
    identity_events history is preserved (booking_id nulled out, not deleted) -- that's the
    identity's audit trail, not the booking's."""
    bk = db.get(M.Booking, booking_id)
    if not bk:
        raise HTTPException(404, "booking not found")
    if db.scalar(select(M.BookedAppointment.id).where(
            M.BookedAppointment.booking_id == booking_id).limit(1)):
        raise HTTPException(409, "cannot delete a booking with a real booked appointment on it")

    workers = list(db.scalars(select(M.Worker).where(M.Worker.booking_id == booking_id)).all())
    for w in workers:
        if w.status == WorkerStatus.RUNNING:
            _bus.send_command(w.id, "stop")
            supervisor.stop(w.id)
        _reset_worker_bus_state(booking_id, w.id)
        db.execute(delete(M.ProxyLease).where(M.ProxyLease.worker_id == w.id))
        db.execute(delete(M.SlotLease).where(M.SlotLease.worker_id == w.id))
        db.execute(delete(M.Artifact).where(M.Artifact.worker_id == w.id))
        db.execute(delete(M.WorkerRun).where(M.WorkerRun.worker_id == w.id))
    db.execute(delete(M.Worker).where(M.Worker.booking_id == booking_id))
    db.execute(delete(M.Slot).where(M.Slot.booking_id == booking_id))
    db.execute(delete(M.BookingIdentity).where(M.BookingIdentity.booking_id == booking_id))
    db.execute(delete(M.IdentityAllocation).where(M.IdentityAllocation.booking_id == booking_id))
    db.execute(update(M.IdentityEvent).where(M.IdentityEvent.booking_id == booking_id)
              .values(booking_id=None))
    db.delete(bk)
    db.commit()
    scheduler._scout_pools.pop(booking_id, None)


# ── Attach identity → materialize worker (rules + C2 exclusivity) ─────────────
@app.patch("/bookings/{booking_id}/settings", response_model=BookingOut)
def patch_booking_settings(booking_id: str, body: dict, db: Session = Depends(get_db),
                           _op: Principal = Depends(require_operator)) -> M.Booking:
    """MERGE keys into a booking's settings, leaving every other key alone.

    PUT /bookings/{id} replaces settings_json wholesale, which the full Settings panel needs (an
    absent key there means "clear this override"). But the overview's shortcut panel only owns a
    handful of proxy/swap fields, and it was PUTting its own snapshot of the WHOLE object -- taken
    when the page loaded. Confirmed live 2026-08-14: a booking configured with
    CAL_START_DATE/CAL_END_DATE came back with only the three keys the shortcut panel knows about,
    because toggling a proxy pool wrote back a stale copy that had never heard of the calendar
    window. Two panels, both authoritative over the same object, last-write-wins.

    Partial updates go through here instead, so the two can't destroy each other's work."""
    bk = db.get(M.Booking, booking_id)
    if not bk:
        raise HTTPException(404, "booking not found")
    merged = {**(bk.settings_json or {}), **body}
    try:
        bk.settings_json = validate_layer(L.BOOKING, merged)
    except Exception as e:
        raise HTTPException(400, f"invalid booking settings: {e}")
    db.commit()
    db.refresh(bk)
    return bk


@app.post("/bookings/{booking_id}/identities", response_model=WorkerOut, status_code=201)
def attach_identity(
    booking_id: str, body: AttachIdentity, db: Session = Depends(get_db),
    _op: Principal = Depends(require_operator),
) -> M.Worker:
    bk = db.get(M.Booking, booking_id)
    if not bk:
        raise HTTPException(404, "booking not found")
    ident = db.get(M.Identity, body.identity_id)
    if not ident:
        raise HTTPException(404, "identity not found")

    # Role rule (spec §4): test booking assigns role; real booking derives it.
    try:
        role = resolve_worker_role(bk.type, ident.type, body.assigned_role)
    except RuleViolation as e:
        raise HTTPException(400, str(e))

    # C2 exclusivity: block if the identity is already allocated to another ACTIVE booking.
    other_active = db.scalars(
        select(M.Booking.status)
        .join(M.IdentityAllocation, M.IdentityAllocation.booking_id == M.Booking.id)
        .where(
            M.IdentityAllocation.identity_id == ident.id,
            M.IdentityAllocation.released_at.is_(None),
            M.IdentityAllocation.booking_id != bk.id,
        )
    ).all()
    if any(st in BOOKING_ACTIVE_STATUSES for st in other_active):
        raise HTTPException(409, "identity already allocated to another active booking")

    # KNOWN GAP (found 2026-08-14, not yet fixed): the check above uses BOOKING_ACTIVE_STATUSES,
    # which excludes DRAFT/COMPLETED/ABORTED -- but the DB's uq_identity_active_allocation index is
    # UNIQUE(identity_id) WHERE released_at IS NULL and ignores booking status entirely, and nothing
    # releases an allocation when a booking merely becomes terminal (only detach_identity and
    # booking delete do). So an identity left allocated to an aborted/completed/draft booking passes
    # this check and then trips the index on flush. Caught here and reported honestly rather than
    # surfacing as an opaque 500. The real fix is to release allocations when a booking reaches a
    # terminal state; GET /identities already reports the DB's stricter reality so the attach panel
    # doesn't offer these in the first place.
    stale = db.scalar(
        select(M.IdentityAllocation)
        .where(M.IdentityAllocation.identity_id == ident.id,
               M.IdentityAllocation.released_at.is_(None),
               M.IdentityAllocation.booking_id != bk.id)
    )
    if stale is not None:
        stale_bk = db.get(M.Booking, stale.booking_id)
        raise HTTPException(
            409,
            f"identity still holds an unreleased allocation to booking "
            f"'{stale_bk.name if stale_bk else stale.booking_id}' "
            f"(status {stale_bk.status if stale_bk else '?'}) -- detach it there first",
        )

    # Template: default to the first template matching the resolved role if none given.
    template_id = body.template_id
    if template_id:
        tpl = db.get(M.WorkerTemplate, template_id)
        if not tpl:
            raise HTTPException(404, "template not found")
        if tpl.role != role:
            raise HTTPException(400, f"template role {tpl.role} != worker role {role}")
    else:
        tpl = db.scalars(select(M.WorkerTemplate).where(M.WorkerTemplate.role == role)
                         .order_by(M.WorkerTemplate.created_at.asc())).first()
        template_id = tpl.id if tpl else None

    alloc = M.IdentityAllocation(identity_id=ident.id, booking_id=bk.id, allocated_at=_now())
    db.add(alloc)
    db.add(M.BookingIdentity(booking_id=bk.id, identity_id=ident.id, assigned_role=body.assigned_role))
    worker = M.Worker(booking_id=bk.id, identity_id=ident.id, role=role, template_id=template_id)
    db.add(worker)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "identity already attached / allocated (concurrent)")
    db.refresh(worker)
    return worker


@app.get("/bookings/{booking_id}/workers", response_model=list[WorkerOut])
def list_workers(booking_id: str, db: Session = Depends(get_db)) -> list[M.Worker]:
    return list(db.scalars(select(M.Worker).where(M.Worker.booking_id == booking_id)).all())


@app.delete("/bookings/{booking_id}/identities/{identity_id}")
def detach_identity(booking_id: str, identity_id: str, db: Session = Depends(get_db),
                    _op: Principal = Depends(require_operator)) -> dict:
    """Reverse of attach: stop+remove the worker, release its proxy/slot leases, release the C2
    allocation. Requested repeatedly in the recovered conversation history (a "remove identities"
    control was part of the original booking-page spec) but never had a UI or endpoint before.

    Confirmed bug (2026-08-14): `release_proxy` only sets `released_at` -- it does NOT delete the
    ProxyLease row -- so the row still held a live FK to `worker.id`, and the very next line
    (`db.delete(worker)`) always failed with a ForeignKeyViolation for any worker that had ever
    actually leased a proxy (i.e. any live-mode worker that got past launch). That 500 came back as
    Starlette's default PLAIN-TEXT "Internal Server Error" body (no JSON, since nothing here ever
    caught it), which is why the dashboard showed a raw JSON-parse error instead of a real message.
    Fixed by actually deleting the ProxyLease rows (matching how SlotLease rows are already
    correctly deleted just below) and WorkerRun rows (currently always empty in this codebase --
    nothing writes WorkerRun yet -- but defensive all the same). Artifact.worker_id is nulled, not
    deleted: an Artifact can be a booked PDF's record, worth keeping even once the worker row that
    produced it is gone."""
    worker = db.scalar(select(M.Worker).where(
        M.Worker.booking_id == booking_id, M.Worker.identity_id == identity_id))
    if worker:
        # Detach was the ONE action in WORKER_ACTION_STATUSES that no endpoint enforced (launch/
        # stop/restart all call this; detach did not). Both other layers already said detach is
        # illegal from RUNNING -- the table declares it and the dashboard disables the button -- so
        # the invariant state_machines.py states in its own comment ("a disabled button always means
        # 'not applicable', never 'will 409'") was only true for three of the four actions.
        #
        # Enforced rather than widened because detach is destructive in a way the others are not: it
        # DELETES the ProxyLease/SlotLease rows outright. Doing that to a live worker both rips the
        # leases out from under it and destroys the only record of which proxy/slot it was using --
        # which is exactly how a previous investigation lost its evidence. Stop it first, explicitly.
        _require_worker_action(worker, "detach")
        if worker.status == WorkerStatus.RUNNING:
            # Unreachable via the guard above; kept for the race where a worker reports RUNNING
            # between the guard and here, so we never delete a worker whose process is still live.
            _bus.send_command(worker.id, "stop")
            supervisor.stop(worker.id)
        db.execute(delete(M.ProxyLease).where(M.ProxyLease.worker_id == worker.id))
        for lease in db.scalars(select(M.SlotLease).where(M.SlotLease.worker_id == worker.id)).all():
            db.delete(lease)
        db.execute(delete(M.WorkerRun).where(M.WorkerRun.worker_id == worker.id))
        db.execute(update(M.Artifact).where(M.Artifact.worker_id == worker.id).values(worker_id=None))
        _reset_worker_bus_state(booking_id, worker.id)
        db.delete(worker)
    alloc = db.scalar(select(M.IdentityAllocation).where(
        M.IdentityAllocation.booking_id == booking_id, M.IdentityAllocation.identity_id == identity_id,
        M.IdentityAllocation.released_at.is_(None)))
    if alloc:
        alloc.released_at = _now()
    bi = db.scalar(select(M.BookingIdentity).where(
        M.BookingIdentity.booking_id == booking_id, M.BookingIdentity.identity_id == identity_id))
    if bi:
        db.delete(bi)
    db.commit()
    return {"detached": True}


# ── Worker control (supervisor + bus) ─────────────────────────────────────────
def _live_account(identity: M.Identity, sub_account: M.SubAccount) -> dict:
    """Build the account dict LiveEngineOps/booking_payload's from_account merge expects. Field
    names/derivation (gender expansion, first/last split) match storm-auto's confirmed
    _build_schedule_payload account_map exactly. passport_issued/passport_expiry are intentionally
    absent -- libs.db.models.Identity doesn't track them yet, so booking_payload's merge falls
    through to the `dynamic` today±yr defaults for those two fields, which is the documented,
    intended fallback behavior, not a bug."""
    first, _, last = (identity.full_name or "").partition(" ")
    gender_raw = (identity.gender or "M").upper()
    return {
        "username": sub_account.username,
        "password": sub_account.password,
        "email": sub_account.email or "",
        "first_name": first,
        "last_name": last or first,
        "birthdate": identity.birthdate or "",
        "gender_expanded": "MALE" if gender_raw in ("M", "MALE") else "FEMALE",
        "traveldoc": identity.passport_no or "",
    }


def _rank_live_sub_accounts(db: Session, identity_id: str) -> list[M.SubAccount]:
    """ALL usable (non-BURNED) sub-accounts for an identity, ranked best-first (LOGIN_OK > HELD >
    VERIFIED > REGISTERED > NEW). The full ranked list -- not just the top pick -- is what
    LiveEngineOps.do_login() rotates through once its sticky-retry budget on the current sub-account
    is exhausted (operator spec: sticky=2, rotating=number of subaccounts, e.g. a 3-subaccount
    identity gets up to 2x3=6 login attempts total, ported from storm-auto's confirmed
    app/worker.py:323-326 sticky-then-rotate pattern)."""
    rank = {SubAccountStatus.LOGIN_OK: 0, SubAccountStatus.HELD: 1, SubAccountStatus.VERIFIED: 2,
            SubAccountStatus.REGISTERED: 3, SubAccountStatus.NEW: 4}
    candidates = list(db.scalars(
        select(M.SubAccount).where(M.SubAccount.identity_id == identity_id,
                                   M.SubAccount.status != SubAccountStatus.BURNED)
    ).all())
    candidates.sort(key=lambda s: rank.get(s.status, 99))
    return candidates


def _pick_live_sub_account(db: Session, identity_id: str) -> M.SubAccount | None:
    candidates = _rank_live_sub_accounts(db, identity_id)
    return candidates[0] if candidates else None


def _geos(value) -> set[str] | None:
    """Parse a PROXY_GEOS setting ("DE,IT") into a set. Blank/absent -> None = no restriction."""
    parts = {g.strip().upper() for g in str(value or "").replace(";", ",").split(",") if g.strip()}
    return parts or None


def _do_launch_worker(db: Session, w: M.Worker) -> None:
    """Resolve + FREEZE effective settings (global<booking<template), spawn, send launch command.

    For a live-engine booking (ENGINE_MODE=live), extends the launch payload with everything
    LiveEngineOps needs (account, proxy, quest_steps, form_defaults, capsolver keys) -- the worker
    process touches only the bus, never the DB, so all of this must travel through the command
    payload (Phase C6). Only marks the worker RUNNING after BOTH the process spawn and the
    command-group creation are confirmed to have succeeded (Phase F5: confirm-before-commit, the
    same ordering principle as the signal-latch fix -- never record a state change the underlying
    action hasn't actually completed)."""
    bk = db.get(M.Booking, w.booking_id)
    # CONFIRMED LIVE 2026-08-14: the operator stopped "Test 3018" at 15:01:24 and a scout process
    # came up at 15:01:47 -- 23s AFTER the stop. A launch decided before the stop (hold-respawn, or
    # a fleet launch still inside its WARMUP_START_JITTER sleep) spawns long after it, and the stop
    # can only kill processes that already exist when it runs. So the decision to launch has to be
    # re-validated at the moment of spawning, not just when it was made.
    #
    # This is the single choke point every launch path goes through -- fleet launch, relaunch,
    # hold-respawn -- so the check belongs here rather than in each caller.
    if bk is None or bk.status in _LAUNCH_BLOCKED_BOOKING_STATUSES:
        logger.warning("launch aborted for worker %s: booking is %s",
                       w.id, bk.status if bk else "missing")
        return
    if w.status == WorkerStatus.STOPPED:
        logger.warning("launch aborted for worker %s: it was stopped while the launch was in flight",
                       w.id)
        return
    # HOST_SESSION_CAP: one live worker is one Chromium tree (~50-60 processes, measured), and this
    # is a shared host running Postgres and Redis too. The setting existed in the catalog with a
    # documented meaning and NO READER anywhere -- the recurring trap in this codebase, and the one
    # guardrail that actually matters when two bookings run at once, since each is sized without
    # knowing about the other. Counted across ALL bookings for that reason.
    cap = int(get_global(db).get("HOST_SESSION_CAP", 175) or 0)
    if cap > 0:
        live = db.scalar(select(func.count()).select_from(M.Worker).where(
            M.Worker.status == WorkerStatus.RUNNING)) or 0
        if live >= cap:
            logger.warning("launch aborted for worker %s: HOST_SESSION_CAP reached (%d running >= %d)",
                           w.id, live, cap)
            raise HTTPException(409, f"host session cap reached ({live}/{cap} sessions running)")
    tpl = db.get(M.WorkerTemplate, w.template_id) if w.template_id else None
    layers = {L.GLOBAL: get_global(db), L.BOOKING: (bk.settings_json or {})}
    if tpl:
        layers[L.TEMPLATE] = (tpl.settings_json or {})
    eff = resolve(layers)
    eff["ROLE"] = str(w.role)                     # role comes from the worker row, not settings

    launch_kwargs: dict = {}
    engine_mode = str(eff.get("ENGINE_MODE", "sim"))
    if engine_mode == "live":
        identity = db.get(M.Identity, w.identity_id)
        ranked_subs = _rank_live_sub_accounts(db, w.identity_id) if identity else []
        sub_account = ranked_subs[0] if ranked_subs else None
        if identity is None or sub_account is None:
            raise HTTPException(409, f"worker {w.id}: no usable sub-account for identity "
                                     f"{w.identity_id} -- cannot launch in live mode")
        # Stashed on the frozen settings blob (no dedicated column) so the Hub has a durable record
        # of which sub-account this worker used -- needed by the booked-PDF event handler
        # (events.py's _persist_booked_pdf) to fill BookedAppointment.sub_account_id.
        eff["_sub_account_id"] = sub_account.id
        w.sub_account_id = sub_account.id
        try:
            proxy, warm_proxy = lease_proxy(
                db, w.id, provider=str(eff.get("PROXY_PROVIDER", "soax")),
                kind=eff.get("PROXY_KIND") or None, health_check=check_proxy,
                geos=_geos(eff.get("PROXY_GEOS")),
            )
        except ProxyLeaseError as e:
            raise HTTPException(409, f"worker {w.id}: {e}")

        # Phase-2 (hold/apply/discovery) proxy, only leased when the swap is actually switched on
        # (HOLD_SWAP_AFTER_FOUND) -- a worker with swap off keeps its phase-1 proxy for the whole
        # run, matching do_swap()'s off-by-default behavior in ops_live.py.
        hold_proxy = None
        if bool(eff.get("HOLD_SWAP_AFTER_FOUND", False)):
            try:
                _hold_proxy_row, hold_proxy = lease_proxy(
                    db, w.id, provider=str(eff.get("HOLD_PROXY_PROVIDER", "soax")),
                    kind=eff.get("HOLD_PROXY_KIND") or None, health_check=check_proxy,
                    geos=_geos(eff.get("HOLD_PROXY_GEOS")),
                )
            except ProxyLeaseError as e:
                # The phase-1 lease already succeeded -- release it, otherwise it's stranded (this
                # worker never reaches RUNNING, so D3's dead-worker reap -- which only scans RUNNING
                # workers -- would never release it either).
                release_proxy(db, w.id)
                raise HTTPException(
                    409, f"worker {w.id}: HOLD_SWAP_AFTER_FOUND is on but no hold proxy available: {e}"
                )

        quest_steps = get_config(db, "quest_steps", {}) or {}
        form_defaults = get_config(db, "form_defaults", {}) or {}
        g = get_global(db)
        capsolver_keys = [k.strip() for k in str(g.get("CAPSOLVER_KEYS", "")).split(",") if k.strip()]
        # Sub-account rotation candidates (do_login()'s rotate-after-sticky-exhausted loop): the FULL
        # ranked list, each already carrying its own sub_account_id, so the worker (deliberately
        # DB-less) can rotate entirely locally without a Hub round-trip per attempt.
        accounts = [dict(_live_account(identity, sa), sub_account_id=sa.id) for sa in ranked_subs]
        launch_kwargs = dict(
            _engine="live",
            account=_live_account(identity, sub_account),
            accounts=accounts,
            posto_id=bk.posto_id,
            nationality=bk.nationality,
            residence=bk.residence,
            quest_steps=quest_steps,
            form_defaults=form_defaults,
            capsolver_keys=capsolver_keys,
            warm_proxy=warm_proxy,
            hold_proxy=hold_proxy,
            sub_account_id=sub_account.id,
            identity_id=identity.id,
            booking_id=bk.id,
        )
    else:
        launch_kwargs = dict(_engine="sim")

    w.effective_settings_json = eff
    db.commit()   # freeze settings before spawn; status flips only after spawn+group are confirmed

    spawned = False
    try:
        supervisor.spawn(w.id)
        spawned = True
        _bus.ensure_command_group(w.id)
        _bus.send_command(w.id, "launch", cfg=eff, **launch_kwargs)
    except Exception:
        if spawned:
            supervisor.stop(w.id)
        _set_worker_status(w, WorkerStatus.FAILED, detail="launch failed")
        db.commit()
        raise

    # Regression (confirmed live, 2026-08-13): setting substate=IDLE here used to race the freshly
    # spawned worker process's OWN "started" event (which ALSO sets substate=IDLE, then immediately
    # LOGGING_IN/... as the phase machine advances) -- under a fast serial launch this write almost
    # always won the race and landed first, so the clobber was never observed. Once launches started
    # running concurrently on background threads (P4, _launch_worker_fleet, needed for the
    # live-proxy-lease health probe), the ordering inverted often enough to be common: the worker
    # process would race ahead, get several real status_change events applied by EventHub (up to and
    # including HOLDING), and then THIS write would land late and stomp substate back to IDLE --
    # permanently, if the worker had nothing further to report (already holding, no more events
    # coming). That worker then became invisible to the scheduler's HOLDING-only assignment query
    # despite genuinely holding, and the dashboard showed a stale substate that never matched reality
    # again. Fix: never write `substate` here at all -- the worker's own "started" event (handled by
    # EventHub, same as every subsequent transition) is the ONLY source of truth for substate, so
    # there's nothing left to race.
    # Deliberately a BARE status write, not _set_worker_status: that helper clears `substate`, which
    # is precisely the write this block's comment above forbids here. By now the worker may already
    # have emitted several real status_change events, and clearing substate would resurrect the
    # 2026-08-13 race. The requeue-to-READY that preceded this launch already cleared the stale one.
    w.status = WorkerStatus.RUNNING
    # Clear any LAUNCH_FAILED_PREFIX marker from a previous attempt. It is what _hold_respawn keys
    # its retry off, so leaving it behind on a NOW-SUCCESSFUL launch would both mislead the operator
    # ("launch failed" next to a running worker) and re-arm the retry the moment this worker next
    # touches READY for an unrelated reason.
    if (w.substate_detail or "").startswith(LAUNCH_FAILED_PREFIX):
        w.substate_detail = None
    db.commit()


@app.post("/workers/{worker_id}/launch", response_model=WorkerOut)
def launch_worker(worker_id: str, db: Session = Depends(get_db),
                  _op: Principal = Depends(require_operator)) -> M.Worker:
    w = db.get(M.Worker, worker_id)
    if not w:
        raise HTTPException(404, "worker not found")
    _require_worker_action(w, "launch")
    _do_launch_worker(db, w)
    db.refresh(w)
    return w


@app.post("/bookings/{booking_id}/start")
def start_booking(booking_id: str, db: Session = Depends(get_db),
                  _op: Principal = Depends(require_operator)) -> dict:
    """Start Instantly — launch every READY worker now (warmup begins immediately). Does **not**
    fire slot-open: that's a separate decision, made later either by the timed auto-schedule
    (/schedule + slot_open_at) or manually (POST .../fire-slot-open). Also the resume path for a
    STOPPED booking.

    EVERY start is a FRESH RUN (operator decision 2026-08-14). `started_at` is re-stamped and the
    signal latch (slot_open_fired_at/slot_found_fired_at) is CLEARED, so the booking always enters
    WARMING with both fire-slot-open and fire-slot-found available again.

    Confirmed bug this fixes: those two timestamps persisted across a stop/start cycle, so a booking
    restarted after its signals had fired went straight back to RUNNING with both fire buttons
    permanently disabled (the endpoints correctly reported `already_fired`) -- the operator had no
    way to re-fire a signal on what was, from their point of view, a brand-new run. The old
    resume-into-RUNNING shortcut is deliberately gone: after a restart, slot-open is re-fired
    explicitly, either manually or by the timed auto-schedule.

    A booking already at WARMING/ARMED/RUNNING is left alone (this isn't a restart -- it just
    launches any still-READY worker, PLUS any STOPPED/FAILED worker requeued to READY here first,
    see below). A worker that already reached COMPLETED (booked, or a finished dry-run/discovery) is
    never touched -- it's done, not stale.

    Confirmed bug (2026-08-13): "Start Instantly" clicked on a booking that had been running for a
    while did nothing, because every worker had drifted to STOPPED/FAILED and this endpoint only
    ever looked at READY ones. Fixed by requeuing STOPPED/FAILED workers to READY first, resetting
    each one's bus state via _reset_worker_bus_state (command stream, respawn backoff, and signal
    latch membership -- see its docstring; the last of these is what closes the follow-up "I can't
    fire any signal after restart" bug, since a relaunched worker otherwise never gets slot_open/
    slot_found re-sent despite genuinely never having received them in its new process)."""
    from libs.domain.state_machines import IllegalTransition, booking_transition
    bk = db.get(M.Booking, booking_id)
    if not bk:
        raise HTTPException(404, "booking not found")
    _requeue_stale_workers(db, booking_id)

    workers = list(db.scalars(select(M.Worker).where(
        M.Worker.booking_id == booking_id, M.Worker.status == WorkerStatus.READY)).all())
    jitter = float(_resolve_booking_setting(db, bk, "WARMUP_START_JITTER", 0.0) or 0.0)
    # EVERY Start is a fresh run -- not only one from DRAFT/SCHEDULED/STOPPED. Gating this on the
    # prior status meant clicking Start on a booking that was already RUNNING/WARMING/ARMED silently
    # skipped all of it, so the run inherited the previous one's fired-signal timestamps, its
    # notified-sets and its slot pool. Confirmed live 2026-08-14: a restarted "Test 3018" showed
    # "slot-open fired @15:25Z" carried over from the run before it, with the latch still set so the
    # signal could not be genuinely re-fired.
    # Re-anchor the booking timeout and clear the signal latch (both timestamps AND the
    # per-worker "already notified" sets, which are keyed by booking+signal in Redis) so
    # slot-open/slot-found can genuinely be fired again on this run.
    bk.started_at = _now()
    bk.slot_open_fired_at = None
    bk.slot_found_fired_at = None
    # Retire the previous run's discovered slots. They are per-run observations of a portal that
    # has moved on, and leaving them AVAILABLE makes the scheduler fire slot_found on the very
    # next tick -- confirmed live 2026-08-14: booking Test2 restarted at 08:44:22 and had
    # slot_open AND slot_found stamped at 08:44:25, the same instant, purely because 24 slots
    # from the prior run were still sitting in the table. The run skipped the entire slot-open
    # discovery phase on stale data.
    #
    # CLOSED, not deleted: SlotStatus.CLOSED already means "no longer believed present" and
    # reopens on rediscovery, so this keeps the history (and any BookedAppointment FK) intact
    # while making the pool empty as far as _fire_slot_found_if_new and _assign_slots care.
    # TAKEN is left alone -- that is a real booking, not an observation.
    # Note the CLOSED slots are included here, unlike the status filter below: a slot can be CLOSED
    # and STILL hold an open lease (the false-slot revert closed slots without releasing them), and
    # an orphan lease on a retired slot is exactly what _reclaim_stale_slot_leases later trips over.
    for lease in db.scalars(select(M.SlotLease).join(M.Slot, M.SlotLease.slot_id == M.Slot.id).where(
            M.Slot.booking_id == booking_id, M.Slot.status != SlotStatus.TAKEN,
            M.SlotLease.released_at.is_(None))).all():
        lease.released_at = _now()
        lease.outcome = SlotLeaseOutcome.EXPIRED
    stale_slots = db.scalars(select(M.Slot).where(
        M.Slot.booking_id == booking_id,
        M.Slot.status.notin_([SlotStatus.TAKEN, SlotStatus.CLOSED]))).all()
    for slot in stale_slots:
        for lease in db.scalars(select(M.SlotLease).where(
                M.SlotLease.slot_id == slot.id, M.SlotLease.released_at.is_(None))).all():
            lease.released_at = _now()
            lease.outcome = SlotLeaseOutcome.EXPIRED
        slot.status = SlotStatus.CLOSED
    for signal_name in ("slot_open", "slot_found"):
        try:
            _bus.r.delete(notified_key(booking_id, signal_name))
        except Exception:
            pass
    scheduler.reset_scout_pool(booking_id)
    # Already WARMING means the fresh-run reset above has put it exactly where a restart wants it;
    # there is no self-transition in the table and asking for one is a 409, not a state change.
    if bk.status != BookingStatus.WARMING:
        try:
            bk.status = booking_transition(bk.status, BookingStatus.WARMING)
        except IllegalTransition as e:
            raise HTTPException(409, str(e))
    db.commit()
    # Launch only AFTER the transition above is COMMITTED. The fleet launch runs on its own thread
    # with its own DB session, so starting it earlier means that session reads the booking in its
    # PRE-start status -- confirmed live 2026-08-14: Start was clicked on a STOPPED booking, the
    # thread saw STOPPED, and _do_launch_worker's stop-guard correctly refused all six workers while
    # the endpoint went on to report a successful start. The UI sat on "Launching 6 workers" with
    # nothing running. Returns immediately -- a live fleet launch takes minutes (real per-proxy
    # browser health probes), and running it inline meant the request never came back and every
    # impatient re-click stacked another concurrent fleet launch on top.
    launching = _begin_fleet_launch(booking_id, [w.id for w in workers], jitter_secs=jitter)
    # `launching` False with workers queued means a fleet launch for this booking was ALREADY in
    # flight -- the click was a no-op, not a failure. The dashboard surfaces that rather than
    # leaving the operator to guess (and click again).
    return {"started": len(workers), "booking_status": str(bk.status),
            "launching": launching, "already_launching": bool(workers) and not launching}


@app.post("/bookings/{booking_id}/fire-slot-open")
def fire_slot_open(booking_id: str, db: Session = Depends(get_db),
                   _op: Principal = Depends(require_operator)) -> dict:
    """Manually fire slot-open now — the operator-triggered alternative to /schedule's timed
    auto-fire (which needs slot_open_at + WARMING/ARMED status). Idempotent: already-fired is a
    no-op. Per-worker delivery (including to any worker that joins/respawns afterward) is the
    scheduler's _latch_signals job on its next tick, not this endpoint's — this only records the
    decision and publishes the broadcast once."""
    bk = db.get(M.Booking, booking_id)
    if not bk:
        raise HTTPException(404, "booking not found")
    if bk.slot_open_fired_at is not None:
        return {"already_fired": True, "booking_status": str(bk.status)}
    if bk.status not in (BookingStatus.WARMING, BookingStatus.ARMED):
        raise HTTPException(409, f"cannot fire slot-open from status {bk.status}")
    bk.slot_open_fired_at = _now()
    db.commit()
    _sched_on_slot_open(db, bk)
    return {"fired": True, "booking_status": str(bk.status)}


@app.post("/bookings/{booking_id}/fire-slot-found")
def fire_slot_found(booking_id: str, db: Session = Depends(get_db),
                    _op: Principal = Depends(require_operator)) -> dict:
    """Manually fire slot-found now — the operator-triggered alternative to the scheduler's own
    automatic _fire_slot_found_if_new (which only fires once a real AVAILABLE Slot row exists in
    the DB; there was previously no way to force it, unlike slot-open which has always had this
    manual override). Idempotent: already-fired is a no-op. Requires slot-open to have already
    fired -- slot-found is semantically downstream of it (RUNNING status, scouts split-polling)."""
    bk = db.get(M.Booking, booking_id)
    if not bk:
        raise HTTPException(404, "booking not found")
    if bk.slot_found_fired_at is not None:
        return {"already_fired": True, "booking_status": str(bk.status)}
    if bk.slot_open_fired_at is None:
        raise HTTPException(409, "cannot fire slot-found before slot-open")
    scheduler.fire_slot_found_manual(db, bk)
    return {"fired": True, "booking_status": str(bk.status)}


@app.post("/bookings/{booking_id}/schedule", response_model=BookingOut)
def schedule_booking(booking_id: str, db: Session = Depends(get_db),
                     _op: Principal = Depends(require_operator)) -> M.Booking:
    """Arm the booking for AUTO start — the scheduler launches at warmup_start_at and fires slot-open
    at slot_open_at with no operator input."""
    bk = db.get(M.Booking, booking_id)
    if not bk:
        raise HTTPException(404, "booking not found")
    if not bk.warmup_start_at:
        raise HTTPException(400, "set warmup_start_at before scheduling")
    # STOPPED is schedulable. It was refused, and that made auto-start unreachable after the FIRST
    # run of a booking: plan_booking_actions only emits "start" for a SCHEDULED booking, so an
    # operator who stopped a run, edited the times and pressed Schedule got a 409 and a warmup that
    # never fired. Confirmed 2026-08-19. STOPPED is documented as the resumable stop, and
    # "stop, retime, schedule for tomorrow" is the ordinary way a booking gets reused.
    if bk.status not in (BookingStatus.DRAFT, BookingStatus.SCHEDULED, BookingStatus.STOPPED):
        raise HTTPException(409, f"cannot schedule a {bk.status} booking")
    if _aware(bk.warmup_start_at) <= _now():
        raise HTTPException(400, "warmup_start_at is in the past -- it would fire on the next tick; "
                                 "set a future time, or use Start now")
    # A scheduled run is a FRESH run, exactly as /start is: clear the previous run's fired-signal
    # timestamps and per-worker notified-sets, so slot-open/slot-found genuinely fire again on the
    # clock instead of being suppressed by a latch left over from the last run.
    bk.started_at = None
    bk.slot_open_fired_at = None
    bk.slot_found_fired_at = None
    for signal_name in ("slot_open", "slot_found"):
        try:
            _bus.r.delete(notified_key(booking_id, signal_name))
        except Exception:
            pass
    scheduler.reset_scout_pool(booking_id)
    bk.status = BookingStatus.SCHEDULED
    db.commit()
    db.refresh(bk)
    return bk


@app.post("/bookings/{booking_id}/stop")
def stop_booking(booking_id: str, db: Session = Depends(get_db),
                 _op: Principal = Depends(require_operator)) -> dict:
    """Stop every running worker in the booking. Marks the booking STOPPED (resumable via /start),
    NOT COMPLETED -- confirmed bug fix: a manual stop was previously indistinguishable from a
    successfully-finished booking, and restarting afterward silently no-opped."""
    bk = db.get(M.Booking, booking_id)
    if not bk:
        raise HTTPException(404, "booking not found")
    running = list(db.scalars(select(M.Worker).where(
        M.Worker.booking_id == booking_id, M.Worker.status == WorkerStatus.RUNNING)).all())
    for w in running:
        _bus.send_command(w.id, "stop")
        supervisor.stop(w.id)
        _set_worker_status(w, WorkerStatus.STOPPED)
        # Reset now, not just at requeue time (start_booking) -- see _reset_worker_bus_state's
        # docstring -- so a single-worker restart right after this stop is safe too.
        _reset_worker_bus_state(booking_id, w.id)
    if bk.status not in (BookingStatus.COMPLETED, BookingStatus.ABORTED, BookingStatus.STOPPED):
        try:
            bk.status = booking_transition(bk.status, BookingStatus.STOPPED)
        except IllegalTransition as e:
            raise HTTPException(409, str(e))
    db.commit()
    return {"stopped": len(running), "booking_status": str(bk.status)}


@app.get("/bookings/{booking_id}/summary")
def booking_summary(booking_id: str, db: Session = Depends(get_db)) -> dict:
    """Run overview: booking status + worker counts by status/role/substate."""
    bk = db.get(M.Booking, booking_id)
    if not bk:
        raise HTTPException(404, "booking not found")
    workers = list(db.scalars(select(M.Worker).where(M.Worker.booking_id == booking_id)).all())
    by_status: dict[str, int] = {}
    by_role: dict[str, int] = {}
    booked = 0
    for w in workers:
        by_status[str(w.status)] = by_status.get(str(w.status), 0) + 1
        by_role[str(w.role)] = by_role.get(str(w.role), 0) + 1
        if str(w.substate) == "booked":
            booked += 1
    return {
        "booking_status": str(bk.status), "workers": len(workers),
        "by_status": by_status, "by_role": by_role, "booked": booked,
        "warmup_start_at": bk.warmup_start_at.isoformat() if bk.warmup_start_at else None,
        "slot_open_at": bk.slot_open_at.isoformat() if bk.slot_open_at else None,
    }


@app.get("/workers/{worker_id}/logs")
def worker_logs(worker_id: str, limit: int = 200, db: Session = Depends(get_db)) -> dict:
    """A worker's recent log lines, replayed from the shared `evt` Redis stream.

    Confirmed 2026-08-14: worker logs were WEBSOCKET-ONLY. The dashboard's per-worker log view
    showed whatever `log_line` events had streamed in since the browser tab connected, and nothing
    else -- so a worker that did its work before the page loaded (or before a refresh) showed "no
    log lines received over the WS yet for this worker" while 31 of its lines, including its whole
    successful login/warmup/swap/hold history, sat right here in Redis. Exactly when an operator
    most wants the log -- explaining something that already happened -- was exactly when it was
    unavailable.

    Bounded by EVENT_STREAM_MAXLEN (the stream is capped, see libs/bus/bus.py), so this is
    best-effort recent history, not an archive: a busy fleet ages older lines out. WorkerRun.log_path
    exists in the schema for durable per-run logs but nothing writes it yet."""
    if not db.get(M.Worker, worker_id):
        raise HTTPException(404, "worker not found")
    out: list[dict] = []
    try:
        for _eid, fields in _bus.r.xrevrange(EVENT_STREAM, count=5000):
            payload = json.loads(fields.get("data", "{}") or "{}")
            if payload.get("worker_id") != worker_id:
                continue
            etype = fields.get("type")
            if etype == "log_line" and payload.get("text"):
                out.append({"ts": float(fields.get("ts") or 0), "text": payload["text"]})
            elif etype == "terminal":
                out.append({"ts": float(fields.get("ts") or 0),
                            "text": f"[terminal] {payload.get('outcome','')}: {payload.get('detail','')}"})
            if len(out) >= limit:
                break
    except Exception as e:
        raise HTTPException(503, f"could not read event stream: {e}")
    out.reverse()   # xrevrange walks newest-first; the log view wants oldest-first
    return {"worker_id": worker_id, "lines": out, "truncated": len(out) >= limit}


@app.post("/workers/{worker_id}/stop", response_model=WorkerOut)
def stop_worker(worker_id: str, db: Session = Depends(get_db),
                _op: Principal = Depends(require_operator)) -> M.Worker:
    w = db.get(M.Worker, worker_id)
    if not w:
        raise HTTPException(404, "worker not found")
    _require_worker_action(w, "stop")
    _bus.send_command(worker_id, "stop")
    supervisor.stop(worker_id)
    _set_worker_status(w, WorkerStatus.STOPPED)
    _reset_worker_bus_state(w.booking_id, worker_id)   # see its docstring -- never leave a stale
    db.commit()                                        # command/signal-latch state behind
    db.refresh(w)
    return w


@app.post("/workers/{worker_id}/restart", response_model=WorkerOut)
def restart_worker(worker_id: str, db: Session = Depends(get_db),
                   _op: Principal = Depends(require_operator)) -> M.Worker:
    """Stop (verified-kill via supervisor.stop, F15) then relaunch through the normal launch path --
    the dashboard-visible version of what _sched_on_relaunch already does for hold-phase auto-respawn."""
    w = db.get(M.Worker, worker_id)
    if not w:
        raise HTTPException(404, "worker not found")
    _require_worker_action(w, "restart")
    if w.status == WorkerStatus.RUNNING:
        _bus.send_command(worker_id, "stop")
        supervisor.stop(worker_id)
    _reset_worker_bus_state(w.booking_id, worker_id)   # see its docstring
    _set_worker_status(w, WorkerStatus.READY)
    db.commit()
    _do_launch_worker(db, w)
    db.refresh(w)
    return w


@app.websocket("/ws/events")
async def ws_events(ws: WebSocket) -> None:
    """Live worker status/log stream for the dashboard (fanned from the evt Redis stream)."""
    await ws.accept()
    q = event_hub.subscribe()
    try:
        while True:
            item = await q.get()
            if item is EventHub.CLOSE or item == EventHub.CLOSE:
                break          # Hub is shutting down -- release the socket so uvicorn can finish
            await ws.send_json(item)
    except WebSocketDisconnect:
        pass
    finally:
        event_hub.unsubscribe(q)


# ── Dashboard (SPA) — served by the Hub on the same origin/port (headless-server friendly) ────
# Registered LAST so every API route above wins; the SPA fallback serves index.html for client
# routes. Only active once the dashboard is built (apps/dashboard/dist).
_DIST = Path(__file__).resolve().parents[1] / "dashboard" / "dist"
if _DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def _spa_root() -> FileResponse:
        return FileResponse(_DIST / "index.html")

    @app.get("/{full_path:path}", include_in_schema=False)
    def _spa_fallback(full_path: str) -> FileResponse:
        # unknown non-API GET path -> let the SPA router handle it. EXCEPT the reserved docs paths
        # -- when ENABLE_API_DOCS is off, FastAPI simply never registers those routes, so without
        # this guard this catch-all would silently swallow /docs, /redoc, /openapi.json and serve
        # index.html (200) instead of a 404, defeating the point of disabling them.
        if full_path in ("docs", "redoc", "openapi.json"):
            raise HTTPException(404, "not found")
        candidate = _DIST / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_DIST / "index.html")

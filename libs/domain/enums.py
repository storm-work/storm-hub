"""Canonical enums for storm-hub domain objects.

These mirror the state machines in the approved spec (§4) exactly. Keeping them in one
place means the DB schema (migrations), the API, and the state-machine transition tables
all reference a single source of truth.
"""
from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """str-valued Enum (3.10 has no stdlib StrEnum). Value == the wire/db string."""

    def __str__(self) -> str:  # so f"{x}" and json give the bare value
        return self.value


# ── Identity ────────────────────────────────────────────────────────────────
class IdentityType(StrEnum):
    TEST = "test"
    REAL = "real"


class IdentityStatus(StrEnum):
    EMPTY = "empty"        # 0 sub-accounts
    ACTIVE = "active"      # >=1 sub-account, usable for bookings
    RETIRED = "retired"    # manually retired
    BOOKED = "booked"      # a sub-account booked -> auto-retired to booked list
    ARCHIVED = "archived"  # terminal/hidden


# ── Sub-account (C7: `new`, was `generated`) ──────────────────────────────────
class SubAccountStatus(StrEnum):
    NEW = "new"
    REGISTERED = "registered"
    VERIFIED = "verified"
    LOGIN_OK = "login_ok"   # durable: passed the reCAPTCHA login wall at least once
    HELD = "held"
    BOOKED = "booked"
    BURNED = "burned"
    # Operator override: gave up on this account (exhausted registration attempts, never reached
    # VERIFIED). Distinct from BURNED, which the PORTAL rejected -- RETIRED is our own bookkeeping
    # decision to stop trying, so it can filter out of "not verified" work queues without implying
    # the portal did anything. Reset (regen credentials) already provides the un-retire path, direct-
    # assignment back to NEW, same as it already does for BURNED -- no separate rehab route needed.
    RETIRED = "retired"


# The ONE definition of "this sub-account can actually attempt a live login". Anything outside this
# set is not a weaker candidate, it is a login that cannot succeed: NEW was never registered at the
# portal at all, and REGISTERED never confirmed its email, so there is no account behind those
# credentials to log into -- while the attempt still costs a proxy lease, a CAPTCHA solve and a slot
# in the sticky/rotating login budget. BOOKED is excluded because that account's mission
# is over (a second appointment for the same person is a defect), BURNED because the portal ruled
# it out, RETIRED because an operator did.
#
# Read by: _rank_live_sub_accounts (which sub-accounts a live launch may use and rotate through),
# list_identities (IdentityOut.ready_sub_accounts, the field the attach panel filters on), and
# tests/hub/test_sub_account_readiness_parity.py, which fails if those two ever disagree. Identity
# status is NOT a substitute: IdentityStatus.ACTIVE means ">=1 sub-account row exists", which is
# true the moment sub-accounts are GENERATED and says nothing about verification.
READY_SUB_ACCOUNT_STATUSES = frozenset({
    SubAccountStatus.VERIFIED, SubAccountStatus.LOGIN_OK, SubAccountStatus.HELD,
})


# ── Booking ───────────────────────────────────────────────────────────────────
class BookingType(StrEnum):
    TEST = "test"
    REAL = "real"


class BookingStatus(StrEnum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    WARMING = "warming"      # auto at warmup_start_at
    ARMED = "armed"          # all workers warmed + holding
    RUNNING = "running"      # slot_open fired
    STOPPED = "stopped"      # operator/timeout stop; resumable (mirrors WorkerStatus.STOPPED) --
                              # NOT the same as COMPLETED (confirmed bug: stop was previously
                              # conflated with completed, and restart from it silently no-opped)
    COMPLETED = "completed"
    ABORTED = "aborted"


# Booking statuses during which an identity is exclusively allocated (C2). STOPPED stays "active"
# here (same reasoning as WorkerStatus.STOPPED) -- a paused-but-resumable booking still owns its
# identities; only COMPLETED/ABORTED/DRAFT release them.
BOOKING_ACTIVE_STATUSES = frozenset(
    {BookingStatus.SCHEDULED, BookingStatus.WARMING, BookingStatus.ARMED, BookingStatus.RUNNING,
     BookingStatus.STOPPED}
)


# ── Worker ────────────────────────────────────────────────────────────────────
class WorkerRole(StrEnum):
    SCOUT = "scout"
    APPLY = "apply"


class WorkerStatus(StrEnum):
    READY = "ready"
    RUNNING = "running"
    STOPPED = "stopped"      # operator/timeout stop -- somebody decided to end this run
    # The phase machine ran to a clean end WITHOUT booking: an apply worker never got a slot
    # assigned, or its apply bounced (libs/engine/phases.py's two PhaseResult("held", ...) exits).
    # Confirmed bug 2026-08-14: `held` had no entry in events.py's _TERMINAL_STATUS, so it hit the
    # FAILED default -- a worker that did nothing wrong was shown red, counted as failed, and burned
    # MAX_RESPAWN_ATTEMPTS. Distinct from FAILED (something broke) and from STOPPED (somebody
    # decided); like both, it's relaunchable, and hold-respawn DOES retry it.
    HELD = "held"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkerSubstate(StrEnum):
    """Declared in LIFECYCLE ORDER, which is the order libs/engine/phases.py actually walks:

        IDLE -> LOGGING_IN -> LOGGED_IN -> [SWAPPED] -> KEEPING
             -> (slot-open / slot-found) -> WARMING_UP -> WARMED
             -> DISCOVERING / POLLING / APPLYING -> BOOKED     (FAILED from anywhere)

    KEEPING BEFORE WARMING_UP, and that is the whole point of the ordering being written down.
    Warmup has been DEFERRED to the signal that needs it since 2026-09-09: a KEEPING worker is
    holding an AUTHENTICATED session and is by definition not warmed yet. This enum, and the
    dashboard's run-summary grid, both still listed WARMING_UP first -- describing the lifecycle
    backwards on the exact two states an operator watches during a run. Fixed 2026-09-16.

    (WARMING_UP precedes KEEPING only in `warmup_only` mode, a terminal diagnostic path that never
    reaches KEEPING.)

    Nothing iterates this enum in declaration order -- the order is documentation, and reordering
    is safe for exactly that reason. Keep it honest.
    """
    IDLE = "idle"
    LOGGING_IN = "logging_in"
    LOGGED_IN = "logged_in"
    SWAPPED = "swapped"          # detail carries the ISP exit ip
    KEEPING = "keeping"
    WARMING_UP = "warming_up"
    WARMED = "warmed"
    DISCOVERING = "discovering"
    POLLING = "polling"
    APPLYING = "applying"
    BOOKED = "booked"
    FAILED = "failed"            # detail carries the failing step


# Machine-readable marker written into Worker.substate_detail by a worker, read back by the Hub's
# _assign_slots. It lives HERE rather than in either side because both processes must agree on the
# literal: the worker (apps/worker/ops_live.py) writes it and the Hub (apps/hub/scheduler.py) filters
# on it, and a drifted copy would silently stop excluding anything -- failing open, in the direction
# that hands unbookable workers real slot leases.
NOT_BOOKABLE_PREFIX = "not bookable:"


# ── Slots (C5) ────────────────────────────────────────────────────────────────
class SlotStatus(StrEnum):
    AVAILABLE = "available"
    LEASED = "leased"
    CLOSED = "closed"            # reconcile: absent from N consecutive scans
    TAKEN = "taken"             # exclusive-mode single-capacity booked


class SlotLeaseOutcome(StrEnum):
    BOOKED = "booked"
    BOUNCED = "bounced"
    EXPIRED = "expired"
    # storm-auto's second release tier (slot_manager.release(lease, "form_stale")): OUR booking
    # form-state expired, so the slot itself is still real. Recorded distinctly from BOUNCED so it
    # never counts against the slot -- neither toward retiring it nor toward the Hub's false-slot
    # verdict, which must be evidence that the POOL is phantom, not that our session lapsed.
    FORM_STALE = "form_stale"
    # The worker's own session/network failed before it could even submit (a dead proxy tunnel, a
    # failed re-warm). Like FORM_STALE this says nothing about the slot, so it must never count
    # against it -- recorded distinctly so reports can tell "we broke" from "our form lapsed".
    SESSION = "session"


# ── Registration jobs (C5) ────────────────────────────────────────────────────
class RegistrationMode(StrEnum):
    ITERATIVE = "iterative"
    PARALLEL = "parallel"


class RegistrationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    REGISTERED = "registered"
    VERIFIED = "verified"
    FAILED = "failed"


# ── Users & sessions (C8 / R2-C7) ─────────────────────────────────────────────
class UserRole(StrEnum):
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


class SessionPurpose(StrEnum):
    VIEW = "view"
    OPERATE = "operate"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"       # operate seat blocked while another operator holds it
    ENDED = "ended"


class HandoverStatus(StrEnum):
    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"


# Roles permitted to hold an OPERATE session (R2-C7).
OPERATE_ROLES = frozenset({UserRole.ADMIN, UserRole.OPERATOR})


# ── Identity run-history events (C6) ──────────────────────────────────────────
class IdentityEvent(StrEnum):
    LOGIN_OK = "login_ok"
    WARMUP_OK = "warmup_ok"
    HELD = "held"
    SWAPPED = "swapped"
    BOOKED = "booked"
    FAILED = "failed"


# ── Settings layers (§6) ──────────────────────────────────────────────────────
class SettingsLayer(StrEnum):
    GLOBAL = "global"
    BOOKING = "booking"
    TEMPLATE = "template"     # worker template
    WORKER = "worker"         # worker instance (highest precedence)

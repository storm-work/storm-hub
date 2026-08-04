"""Explicit state machines for the domain objects (spec §4).

Each machine is a transition table (from -> allowed set) plus a `transition()` helper that
raises `IllegalTransition` on an unlisted move. Terminal states have an empty allowed set.
These are pure functions so they unit-test without a DB or network.
"""
from __future__ import annotations

from .enums import (
    BookingStatus,
    IdentityStatus,
    SlotStatus,
    SubAccountStatus,
    WorkerStatus,
    WorkerSubstate,
)


class IllegalTransition(ValueError):
    def __init__(self, machine: str, frm, to):
        super().__init__(f"{machine}: illegal transition {frm} -> {to}")
        self.machine, self.frm, self.to = machine, frm, to


def _transition(machine: str, table: dict, frm, to):
    allowed = table.get(frm, frozenset())
    if to not in allowed:
        raise IllegalTransition(machine, frm, to)
    return to


# ── Identity ──────────────────────────────────────────────────────────────────
IDENTITY_TRANSITIONS = {
    IdentityStatus.EMPTY:    frozenset({IdentityStatus.ACTIVE, IdentityStatus.ARCHIVED}),
    IdentityStatus.ACTIVE:   frozenset({IdentityStatus.BOOKED, IdentityStatus.RETIRED,
                                        IdentityStatus.EMPTY, IdentityStatus.ARCHIVED}),
    IdentityStatus.BOOKED:   frozenset({IdentityStatus.ARCHIVED}),
    IdentityStatus.RETIRED:  frozenset({IdentityStatus.ACTIVE, IdentityStatus.ARCHIVED}),
    IdentityStatus.ARCHIVED: frozenset(),
}


def identity_transition(frm: IdentityStatus, to: IdentityStatus) -> IdentityStatus:
    return _transition("identity", IDENTITY_TRANSITIONS, frm, to)


# ── Sub-account ───────────────────────────────────────────────────────────────
SUBACCOUNT_TRANSITIONS = {
    SubAccountStatus.NEW:        frozenset({SubAccountStatus.REGISTERED, SubAccountStatus.BURNED}),
    SubAccountStatus.REGISTERED: frozenset({SubAccountStatus.VERIFIED, SubAccountStatus.BURNED}),
    SubAccountStatus.VERIFIED:   frozenset({SubAccountStatus.LOGIN_OK, SubAccountStatus.HELD,
                                            SubAccountStatus.BURNED}),
    # login_ok is durable; a proven account can hold, book, or burn, but never regress below verified.
    SubAccountStatus.LOGIN_OK:   frozenset({SubAccountStatus.HELD, SubAccountStatus.BOOKED,
                                            SubAccountStatus.BURNED}),
    SubAccountStatus.HELD:       frozenset({SubAccountStatus.BOOKED, SubAccountStatus.LOGIN_OK,
                                            SubAccountStatus.BURNED}),
    SubAccountStatus.BOOKED:     frozenset(),
    SubAccountStatus.BURNED:     frozenset({SubAccountStatus.VERIFIED}),  # rehab allowed
}


def subaccount_transition(frm: SubAccountStatus, to: SubAccountStatus) -> SubAccountStatus:
    return _transition("sub_account", SUBACCOUNT_TRANSITIONS, frm, to)


# ── Booking ───────────────────────────────────────────────────────────────────
BOOKING_TRANSITIONS = {
    BookingStatus.DRAFT:     frozenset({BookingStatus.SCHEDULED, BookingStatus.ABORTED}),
    BookingStatus.SCHEDULED: frozenset({BookingStatus.WARMING, BookingStatus.RUNNING,
                                        BookingStatus.ABORTED}),
    BookingStatus.WARMING:   frozenset({BookingStatus.ARMED, BookingStatus.RUNNING,
                                        BookingStatus.ABORTED}),
    BookingStatus.ARMED:     frozenset({BookingStatus.RUNNING, BookingStatus.ABORTED}),
    BookingStatus.RUNNING:   frozenset({BookingStatus.COMPLETED, BookingStatus.ABORTED}),
    BookingStatus.COMPLETED: frozenset(),
    BookingStatus.ABORTED:   frozenset(),
}


def booking_transition(frm: BookingStatus, to: BookingStatus) -> BookingStatus:
    return _transition("booking", BOOKING_TRANSITIONS, frm, to)


# ── Worker (top-level status) ─────────────────────────────────────────────────
WORKER_TRANSITIONS = {
    WorkerStatus.READY:     frozenset({WorkerStatus.RUNNING, WorkerStatus.STOPPED}),
    WorkerStatus.RUNNING:   frozenset({WorkerStatus.STOPPED, WorkerStatus.COMPLETED,
                                       WorkerStatus.FAILED}),
    WorkerStatus.STOPPED:   frozenset({WorkerStatus.RUNNING}),          # resume from checkpoint
    WorkerStatus.FAILED:    frozenset({WorkerStatus.RUNNING}),          # retry
    WorkerStatus.COMPLETED: frozenset(),
}


def worker_transition(frm: WorkerStatus, to: WorkerStatus) -> WorkerStatus:
    return _transition("worker", WORKER_TRANSITIONS, frm, to)


# Worker substates (only meaningful while status == RUNNING). The happy path is a forward
# chain; several back-edges are legal (rewarm, re-discover, hold<->poll), so we model the
# realistic graph rather than a strict line.
WORKER_SUBSTATE_TRANSITIONS = {
    WorkerSubstate.IDLE:        frozenset({WorkerSubstate.LOGGING_IN, WorkerSubstate.FAILED}),
    WorkerSubstate.LOGGING_IN:  frozenset({WorkerSubstate.LOGGED_IN, WorkerSubstate.LOGGING_IN,
                                           WorkerSubstate.FAILED}),
    WorkerSubstate.LOGGED_IN:   frozenset({WorkerSubstate.WARMING_UP, WorkerSubstate.FAILED}),
    WorkerSubstate.WARMING_UP:  frozenset({WorkerSubstate.WARMED, WorkerSubstate.WARMING_UP,
                                           WorkerSubstate.FAILED}),
    WorkerSubstate.WARMED:      frozenset({WorkerSubstate.SWAPPED, WorkerSubstate.HOLDING,
                                           WorkerSubstate.DISCOVERING, WorkerSubstate.FAILED}),
    WorkerSubstate.SWAPPED:     frozenset({WorkerSubstate.HOLDING, WorkerSubstate.DISCOVERING,
                                           WorkerSubstate.FAILED}),
    WorkerSubstate.HOLDING:     frozenset({WorkerSubstate.HOLDING, WorkerSubstate.DISCOVERING,
                                           WorkerSubstate.POLLING, WorkerSubstate.APPLYING,
                                           WorkerSubstate.WARMING_UP, WorkerSubstate.FAILED}),
    WorkerSubstate.DISCOVERING: frozenset({WorkerSubstate.POLLING, WorkerSubstate.APPLYING,
                                           WorkerSubstate.HOLDING, WorkerSubstate.DISCOVERING,
                                           WorkerSubstate.FAILED}),
    WorkerSubstate.POLLING:     frozenset({WorkerSubstate.DISCOVERING, WorkerSubstate.APPLYING,
                                           WorkerSubstate.HOLDING, WorkerSubstate.POLLING,
                                           WorkerSubstate.FAILED}),
    WorkerSubstate.APPLYING:    frozenset({WorkerSubstate.BOOKED, WorkerSubstate.HOLDING,
                                           WorkerSubstate.POLLING, WorkerSubstate.FAILED}),
    WorkerSubstate.BOOKED:      frozenset(),
    WorkerSubstate.FAILED:      frozenset({WorkerSubstate.LOGGING_IN, WorkerSubstate.WARMING_UP}),
}


def worker_substate_transition(frm: WorkerSubstate, to: WorkerSubstate) -> WorkerSubstate:
    return _transition("worker_substate", WORKER_SUBSTATE_TRANSITIONS, frm, to)


# ── Slot (C5) ─────────────────────────────────────────────────────────────────
SLOT_TRANSITIONS = {
    SlotStatus.AVAILABLE: frozenset({SlotStatus.LEASED, SlotStatus.CLOSED, SlotStatus.TAKEN}),
    SlotStatus.LEASED:    frozenset({SlotStatus.AVAILABLE, SlotStatus.TAKEN, SlotStatus.CLOSED}),
    SlotStatus.CLOSED:    frozenset({SlotStatus.AVAILABLE}),   # reappearance reopens
    SlotStatus.TAKEN:     frozenset(),
}


def slot_transition(frm: SlotStatus, to: SlotStatus) -> SlotStatus:
    return _transition("slot", SLOT_TRANSITIONS, frm, to)

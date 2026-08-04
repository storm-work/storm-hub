"""Pure domain rules/invariants (spec §4 role assignment, §C2 identity exclusivity).

Kept as free functions so the orchestrator, API validators, and tests all share one definition.
"""
from __future__ import annotations

from .enums import (
    BOOKING_ACTIVE_STATUSES,
    BookingStatus,
    BookingType,
    IdentityType,
    WorkerRole,
)


class RuleViolation(ValueError):
    pass


def resolve_worker_role(
    booking_type: BookingType,
    identity_type: IdentityType,
    assigned_role: WorkerRole | None,
) -> WorkerRole:
    """Determine a worker's role when an identity is attached to a booking (spec §4).

    - TEST booking: only TEST identities; role is REQUIRED and operator-assigned (scout OR apply)
      so a test booking can rehearse the full scout+apply scenario.
    - REAL booking: role is DERIVED by identity type — test identity -> scout, real identity -> apply
      (any assigned_role is ignored).
    """
    if booking_type == BookingType.TEST:
        if identity_type != IdentityType.TEST:
            raise RuleViolation("a TEST booking may only contain TEST identities")
        if assigned_role is None:
            raise RuleViolation("TEST booking requires an explicit scout/apply role per identity")
        return assigned_role

    # REAL booking — derive, ignore assigned_role.
    return WorkerRole.SCOUT if identity_type == IdentityType.TEST else WorkerRole.APPLY


def can_allocate_identity(
    existing_allocation_statuses: list[BookingStatus],
) -> bool:
    """C2 exclusivity: an identity may join a booking only if it is not already allocated to another
    booking that is in an ACTIVE status (scheduled/warming/armed/running). `existing_allocation_statuses`
    is the list of statuses of the *other* bookings this identity is currently allocated to.
    """
    return not any(st in BOOKING_ACTIVE_STATUSES for st in existing_allocation_statuses)


def assert_can_allocate_identity(
    identity_id: str,
    existing_allocation_statuses: list[BookingStatus],
) -> None:
    if not can_allocate_identity(existing_allocation_statuses):
        raise RuleViolation(
            f"identity {identity_id} is already allocated to an active booking "
            f"(statuses: {[str(s) for s in existing_allocation_statuses]})"
        )

import pytest

from libs.domain.enums import BookingStatus, BookingType, IdentityType, WorkerRole
from libs.domain.rules import (
    RuleViolation,
    assert_can_allocate_identity,
    can_allocate_identity,
    resolve_worker_role,
)


def test_real_booking_derives_role_by_identity_type():
    assert resolve_worker_role(BookingType.REAL, IdentityType.REAL, None) == WorkerRole.APPLY
    assert resolve_worker_role(BookingType.REAL, IdentityType.TEST, None) == WorkerRole.SCOUT
    # assigned_role is ignored in real bookings
    assert resolve_worker_role(BookingType.REAL, IdentityType.REAL, WorkerRole.SCOUT) == WorkerRole.APPLY


def test_test_booking_requires_explicit_role_and_only_test_identities():
    assert resolve_worker_role(BookingType.TEST, IdentityType.TEST, WorkerRole.SCOUT) == WorkerRole.SCOUT
    assert resolve_worker_role(BookingType.TEST, IdentityType.TEST, WorkerRole.APPLY) == WorkerRole.APPLY
    with pytest.raises(RuleViolation):
        resolve_worker_role(BookingType.TEST, IdentityType.TEST, None)      # role required
    with pytest.raises(RuleViolation):
        resolve_worker_role(BookingType.TEST, IdentityType.REAL, WorkerRole.APPLY)  # real id in test booking


def test_identity_exclusivity_c2():
    # free: no other active allocation
    assert can_allocate_identity([]) is True
    assert can_allocate_identity([BookingStatus.COMPLETED, BookingStatus.ABORTED, BookingStatus.DRAFT]) is True
    # blocked: an active allocation exists
    assert can_allocate_identity([BookingStatus.RUNNING]) is False
    assert can_allocate_identity([BookingStatus.COMPLETED, BookingStatus.WARMING]) is False
    with pytest.raises(RuleViolation):
        assert_can_allocate_identity("id-1", [BookingStatus.ARMED])

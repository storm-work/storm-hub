import pytest

from libs.domain.enums import (
    BookingStatus,
    IdentityStatus,
    SlotStatus,
    SubAccountStatus,
    WorkerStatus,
    WorkerSubstate,
)
from libs.domain.state_machines import (
    IllegalTransition,
    booking_transition,
    identity_transition,
    slot_transition,
    subaccount_transition,
    worker_substate_transition,
    worker_transition,
)


def test_identity_happy_path():
    assert identity_transition(IdentityStatus.EMPTY, IdentityStatus.ACTIVE) == IdentityStatus.ACTIVE
    assert identity_transition(IdentityStatus.ACTIVE, IdentityStatus.BOOKED) == IdentityStatus.BOOKED


def test_identity_booked_is_terminal_except_archive():
    assert identity_transition(IdentityStatus.BOOKED, IdentityStatus.ARCHIVED) == IdentityStatus.ARCHIVED
    with pytest.raises(IllegalTransition):
        identity_transition(IdentityStatus.BOOKED, IdentityStatus.ACTIVE)


def test_subaccount_full_chain():
    chain = [
        SubAccountStatus.NEW, SubAccountStatus.REGISTERED, SubAccountStatus.VERIFIED,
        SubAccountStatus.LOGIN_OK, SubAccountStatus.HELD, SubAccountStatus.BOOKED,
    ]
    for frm, to in zip(chain, chain[1:]):
        assert subaccount_transition(frm, to) == to


def test_subaccount_cannot_skip_or_regress():
    with pytest.raises(IllegalTransition):
        subaccount_transition(SubAccountStatus.NEW, SubAccountStatus.LOGIN_OK)  # skip
    with pytest.raises(IllegalTransition):
        subaccount_transition(SubAccountStatus.BOOKED, SubAccountStatus.HELD)   # regress from terminal


def test_subaccount_burn_and_rehab():
    assert subaccount_transition(SubAccountStatus.VERIFIED, SubAccountStatus.BURNED) == SubAccountStatus.BURNED
    assert subaccount_transition(SubAccountStatus.BURNED, SubAccountStatus.VERIFIED) == SubAccountStatus.VERIFIED


def test_subaccount_retire_reachable_from_every_non_terminal_state():
    for frm in (SubAccountStatus.NEW, SubAccountStatus.REGISTERED, SubAccountStatus.VERIFIED,
               SubAccountStatus.LOGIN_OK, SubAccountStatus.HELD):
        assert subaccount_transition(frm, SubAccountStatus.RETIRED) == SubAccountStatus.RETIRED


def test_subaccount_retired_is_terminal_within_the_table():
    # No table-defined way back -- Reset's direct status assignment is the un-retire path, same as
    # it already is for BURNED, bypassing this table entirely rather than adding a second rung to it.
    with pytest.raises(IllegalTransition):
        subaccount_transition(SubAccountStatus.RETIRED, SubAccountStatus.NEW)


def test_booking_lifecycle():
    assert booking_transition(BookingStatus.DRAFT, BookingStatus.SCHEDULED)
    assert booking_transition(BookingStatus.WARMING, BookingStatus.ARMED)
    assert booking_transition(BookingStatus.RUNNING, BookingStatus.COMPLETED)
    with pytest.raises(IllegalTransition):
        booking_transition(BookingStatus.COMPLETED, BookingStatus.RUNNING)
    # fire-now: scheduled can jump straight to running (past warmup)
    assert booking_transition(BookingStatus.SCHEDULED, BookingStatus.RUNNING)


def test_booking_stop_is_resumable_and_distinct_from_completed():
    # Confirmed bug fix (F13): stopping a booking was previously conflated with COMPLETED, and
    # restarting a stopped booking silently no-opped. STOPPED must be reachable from the active
    # states and must be resumable back to RUNNING/WARMING, unlike the terminal COMPLETED/ABORTED.
    assert booking_transition(BookingStatus.RUNNING, BookingStatus.STOPPED)
    assert booking_transition(BookingStatus.WARMING, BookingStatus.STOPPED)
    assert booking_transition(BookingStatus.STOPPED, BookingStatus.RUNNING)
    assert booking_transition(BookingStatus.STOPPED, BookingStatus.ABORTED)
    with pytest.raises(IllegalTransition):
        booking_transition(BookingStatus.COMPLETED, BookingStatus.STOPPED)
    with pytest.raises(IllegalTransition):
        # a genuinely terminal give-up still can't be resumed, unlike STOPPED
        booking_transition(BookingStatus.ABORTED, BookingStatus.RUNNING)


def test_worker_status_and_resume():
    assert worker_transition(WorkerStatus.READY, WorkerStatus.RUNNING)
    assert worker_transition(WorkerStatus.RUNNING, WorkerStatus.STOPPED)
    assert worker_transition(WorkerStatus.STOPPED, WorkerStatus.RUNNING)   # resume
    assert worker_transition(WorkerStatus.FAILED, WorkerStatus.RUNNING)    # retry
    with pytest.raises(IllegalTransition):
        worker_transition(WorkerStatus.COMPLETED, WorkerStatus.RUNNING)


def test_worker_substate_confirmed_book_path():
    # idle -> logging_in -> logged_in -> warming_up -> warmed -> swapped -> holding
    #      -> discovering -> applying -> booked  (the 0729/0730 confirmed chain)
    seq = [
        WorkerSubstate.IDLE, WorkerSubstate.LOGGING_IN, WorkerSubstate.LOGGED_IN,
        WorkerSubstate.WARMING_UP, WorkerSubstate.WARMED, WorkerSubstate.SWAPPED,
        WorkerSubstate.KEEPING, WorkerSubstate.DISCOVERING, WorkerSubstate.APPLYING,
        WorkerSubstate.BOOKED,
    ]
    for frm, to in zip(seq, seq[1:]):
        assert worker_substate_transition(frm, to) == to


def test_worker_substate_rejects_illegal():
    with pytest.raises(IllegalTransition):
        worker_substate_transition(WorkerSubstate.BOOKED, WorkerSubstate.APPLYING)  # terminal


def test_slot_reconcile_reopen():
    assert slot_transition(SlotStatus.AVAILABLE, SlotStatus.CLOSED)
    assert slot_transition(SlotStatus.CLOSED, SlotStatus.AVAILABLE)   # reappearance reopens
    assert slot_transition(SlotStatus.AVAILABLE, SlotStatus.LEASED)
    # TAKEN is no longer terminal -- see test_a_taken_slot_can_reopen_when_rediscovered. What must
    # still be impossible is inventing a booking out of an unseen slot.
    with pytest.raises(IllegalTransition):
        slot_transition(SlotStatus.CLOSED, SlotStatus.TAKEN)


def test_a_taken_slot_can_reopen_when_rediscovered():
    """TAKEN was terminal. A slot carries MULTIPLE capacity, so a booking landing on it is not the
    same fact as it being full -- and the scout re-sighting it is the portal saying capacity remains
    (booking "Test 3059", 2026-08-20: slot f94b0b47 leased concurrently by four workers, one
    booked). Absence still never reopens anything; only an actual re-sighting does."""
    from libs.domain.enums import SlotStatus
    from libs.domain.state_machines import slot_transition

    assert slot_transition(SlotStatus.TAKEN, SlotStatus.AVAILABLE) == SlotStatus.AVAILABLE


def test_the_substate_enum_is_declared_in_lifecycle_order():
    """KEEPING comes BEFORE WARMING_UP, and this pins it.

    Both the enum and the dashboard's run-summary grid listed WARMING_UP first until 2026-09-16 --
    describing the lifecycle backwards on the two states an operator watches most during a run.
    They had been wrong since 2026-09-09, when warmup became DEFERRED to the slot-open/slot-found
    signal: a KEEPING worker is holding an AUTHENTICATED session and is by definition not warmed.

    The enum's declaration order is documentation (nothing iterates it), which is exactly why it
    drifted unnoticed -- so it gets an assertion instead of a comment.
    """
    from libs.domain.enums import WorkerSubstate as S

    order = [s for s in S]
    def before(a, b):
        return order.index(a) < order.index(b)

    assert before(S.LOGGING_IN, S.LOGGED_IN)
    assert before(S.LOGGED_IN, S.KEEPING)
    assert before(S.KEEPING, S.WARMING_UP), "KEEPING must precede WARMING_UP -- warmup is deferred"
    assert before(S.WARMING_UP, S.WARMED)
    assert before(S.WARMED, S.DISCOVERING)
    assert before(S.APPLYING, S.BOOKED)


def test_keeping_to_warming_up_is_a_legal_transition():
    """The machine has always agreed warmup follows keeping -- it was only the DISPLAYS of the
    order that were stale. Pinned so the table and the enum cannot disagree in future."""
    from libs.domain.enums import WorkerSubstate as S
    from libs.domain.state_machines import WORKER_SUBSTATE_TRANSITIONS

    assert S.WARMING_UP in WORKER_SUBSTATE_TRANSITIONS[S.KEEPING]
    assert S.KEEPING in WORKER_SUBSTATE_TRANSITIONS[S.LOGGED_IN]


def test_warmed_to_applying_is_legal():
    """A WARMED apply worker handed a slot enters _run_apply, which goes straight to APPLYING. This
    was missing from WARMED's transitions, so `warmed -> applying` raised IllegalTransition and
    CRASHED the worker (41 crashes on 'Prev 0923-3018', warmed workers dying instead of booking).
    KEEPING/POLLING/DISCOVERING all reach APPLYING; WARMED must too."""
    from libs.domain.enums import WorkerSubstate as S
    from libs.domain.state_machines import WORKER_SUBSTATE_TRANSITIONS, worker_substate_transition

    assert S.APPLYING in WORKER_SUBSTATE_TRANSITIONS[S.WARMED]
    worker_substate_transition(S.WARMED, S.APPLYING)   # must not raise

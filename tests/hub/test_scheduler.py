from datetime import datetime, timedelta, timezone

from apps.hub.scheduler import plan_booking_actions
from libs.domain.enums import BookingStatus

NOW = datetime(2026, 8, 4, 11, 0, tzinfo=timezone.utc)


def test_scheduled_start_fires_at_warmup_time():
    past = NOW - timedelta(minutes=1)
    future = NOW + timedelta(minutes=1)
    assert plan_booking_actions(BookingStatus.SCHEDULED, past, None, NOW) == ["start"]
    assert plan_booking_actions(BookingStatus.SCHEDULED, future, None, NOW) == []
    # not scheduled -> no auto start
    assert plan_booking_actions(BookingStatus.DRAFT, past, None, NOW) == []


def test_slot_open_fires_when_warming_and_time_reached():
    past = NOW - timedelta(minutes=1)
    assert plan_booking_actions(BookingStatus.WARMING, None, past, NOW) == ["slot_open"]
    assert plan_booking_actions(BookingStatus.ARMED, None, past, NOW) == ["slot_open"]
    # already running -> slot_open not re-fired
    assert plan_booking_actions(BookingStatus.RUNNING, None, past, NOW) == []
    # future slot_open -> nothing
    assert plan_booking_actions(BookingStatus.WARMING, None, NOW + timedelta(minutes=5), NOW) == []


def test_start_and_slot_open_can_both_be_due():
    past = NOW - timedelta(minutes=1)
    # a scheduled booking whose warmup time passed -> start (slot_open needs WARMING status first)
    assert plan_booking_actions(BookingStatus.SCHEDULED, past, past, NOW) == ["start"]

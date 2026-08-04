"""Discovery pure-logic tests.

The `parse_periodos_ocupados` expectations are GROUND TRUTH captured from storm-auto's confirmed
parser (`app/core/batch_apply._parse_periodos_ocupados`) on the same inputs, so this proves the
clean rewrite is behaviorally identical to the 113-booking-proven implementation.
"""
from datetime import date

import pytest

from libs.engine.discovery import (
    GettimeStatus,
    candidate_dates,
    classify_gettime_body,
    parse_periodos_ocupados,
    parse_special_days,
)

# (input, (free, occupied-active)) — captured from storm-auto's parser.
PERIODOS_CASES = [
    ("", ([], [])),
    ("341267891011121314", ([5], [3, 4])),
    ("1267891011121314", ([3, 4, 5], [])),
    ("12345678910111213142", (None, None)),
    ("1234567891011121314", ([], [])),
    (
        "123456789101112131460",
        (list(range(15, 60)), []),
    ),
]


@pytest.mark.parametrize("raw,expected", PERIODOS_CASES)
def test_parse_periodos_matches_confirmed(raw, expected):
    assert parse_periodos_ocupados(raw) == expected


def test_free_is_holes_minus_closed_prefix():
    # "34" closed active prefix, inactive tail 1,2,6..14 -> active range {3,4,5}, free = {5}
    free, occ = parse_periodos_ocupados("341267891011121314")
    assert free == [5] and occ == [3, 4]


# ── gettime classification ────────────────────────────────────────────────────
def test_classify_valid_vs_blocked():
    assert classify_gettime_body("var SPECIAL_DAYS = []; // small payload") == GettimeStatus.OK
    assert classify_gettime_body("x" * 6000) == GettimeStatus.BLOCKED
    assert classify_gettime_body("<html>DataDome challenge</html>") == GettimeStatus.BLOCKED
    assert classify_gettime_body("<meta name='HandheldFriendly'>") == GettimeStatus.BLOCKED


def test_parse_special_days():
    body = (
        "SPECIAL_DAYS[2026]=new Array(); "
        "SPECIAL_DAYS[2026][8] = new Array('1','15','25');"
        "SPECIAL_DAYS[2026][9] = new Array(3);"
    )
    sd = parse_special_days(body)
    assert sd[2026][8] == {1, 15, 25}
    assert sd[2026][9] == {3}


# ── candidate-date window ─────────────────────────────────────────────────────
def test_candidate_dates_skips_weekend_and_special():
    # Mon 2026-08-03 .. ; special-close Aug 5 (Wed); expect Mon,Tue,Thu,Fri (skip Wed + weekend)
    special = {2026: {8: {5}}}
    today = date(2026, 8, 2)  # Sunday
    got = candidate_dates(special, num_days=4, today=today)
    assert got == [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 6), date(2026, 8, 7)]


def test_candidate_dates_respects_start_and_end():
    today = date(2026, 8, 2)
    got = candidate_dates(
        {}, num_days=10, today=today, start=date(2026, 8, 10), end=date(2026, 8, 12)
    )
    # Aug 10 Mon, 11 Tue, 12 Wed — all in window
    assert got == [date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12)]


def test_candidate_dates_near_edge_is_tomorrow_not_today():
    today = date(2026, 8, 3)  # Monday
    got = candidate_dates({}, num_days=1, today=today)
    assert got == [date(2026, 8, 4)]  # tomorrow, never today

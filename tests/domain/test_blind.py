"""libs/domain/blind.py -- the blind-apply grid and its measured period table."""
import pytest

from libs.domain import blind as B


def test_period_table_matches_the_measured_history():
    """Pinned to the database as of 2026-09-18 (every PDF-verified booking + every discovered
    slot). Periods are POSTO-SPECIFIC: 3 only at 2032, 7/8 only at 3018/3059. A drift here means
    the operator's blind grid will bounce on periods that posto never opens."""
    assert B.default_blind_periods("2032") == [3, 4, 5]
    assert B.default_blind_periods("3018") == [4, 5, 7, 8]
    assert B.default_blind_periods("3059") == [4, 5, 7, 8]
    # unknown posto -> the union, never nothing
    assert B.default_blind_periods("5084") == [3, 4, 5, 7, 8]
    assert B.default_blind_periods(None) == [3, 4, 5, 7, 8]


def test_dates_accept_list_or_string_and_never_include_weekends():
    """Every discovered slot in the history is a weekday. 2026-10-10/11 are Sat/Sun."""
    assert B.parse_dates("2026-10-09, 2026-10-10,2026-10-11;2026-10-12") == ["2026-10-09", "2026-10-12"]
    assert B.parse_dates(["2026-10-13", "2026-10-13"]) == ["2026-10-13"]      # dedup
    assert B.parse_dates(None) == [] and B.parse_dates("") == []
    with pytest.raises(ValueError):
        B.parse_dates("next tuesday")


def test_periods_validate_and_dedup():
    assert B.parse_periods("3,4,4,5") == [3, 4, 5]
    assert B.parse_periods([7, 8]) == [7, 8]
    with pytest.raises(ValueError):
        B.parse_periods("0")
    with pytest.raises(ValueError):
        B.parse_periods("morning")


def test_grid_is_date_major_soonest_first():
    """_assign_slots orders by date then period; the grid matches so the first combinations tried
    are the soonest dates, which are both most valuable and likeliest to be taken by someone else."""
    g = B.blind_grid(["2026-10-13", "2026-10-12"], [4, 3])
    assert g == [("2026-10-12", 3), ("2026-10-12", 4), ("2026-10-13", 3), ("2026-10-13", 4)]


def test_weekdays_between_is_inclusive_and_skips_weekends():
    assert B.weekdays_between("2026-10-09", "2026-10-13") == ["2026-10-09", "2026-10-12", "2026-10-13"]

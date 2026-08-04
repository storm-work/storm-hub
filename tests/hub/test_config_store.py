from datetime import date

from apps.hub.config_store import resolve_dynamic


def test_resolve_literal_dates_normalized():
    assert resolve_dynamic("2026/11/01") == "2026/11/01"
    assert resolve_dynamic("2026-11-10") == "2026/11/10"   # dashes -> slashes


def test_resolve_rolling_offsets():
    t = date(2026, 8, 4)
    assert resolve_dynamic("today+10d", today=t) == "2026/08/14"
    assert resolve_dynamic("today-3yr", today=t) == "2023/08/05"   # 3*365 days
    assert resolve_dynamic("today+5yr", today=t) == "2031/08/03"


def test_resolve_garbage_is_empty():
    assert resolve_dynamic("") == ""
    assert resolve_dynamic("nonsense") == ""

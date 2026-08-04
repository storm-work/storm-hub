"""gettime decoding + candidate-date computation — pure logic ported from storm-auto.

The browser nav that fetches the gettime/getPeriodosOcupados bodies is the live (Phase 2b) part;
everything here operates on already-fetched text and dates, so it unit-tests offline against
captured fixtures. Behavior matches the confirmed serial/parallel discovery path.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from enum import Enum

_SPECIAL_DAYS_RE = re.compile(
    r"SPECIAL_DAYS\[(\d{4})\]\[(\d+)\]\s*=\s*new Array\((.*?)\);"
)


class GettimeStatus(str, Enum):
    OK = "ok"          # a valid gettime payload
    BLOCKED = "blocked"  # DataDome/WAF interstitial or oversized page — caller must restore/retry


def classify_gettime_body(body: str) -> GettimeStatus:
    """Distinguish a real gettime payload from a degraded/blocked session.

    A valid gettime is a small JS payload (~400-600 bytes); a DataDome/WAF interstitial is a full
    HTML page with block markers. Bias to BLOCKED when uncertain (missing an opening is far worse
    than an extra retry) — mirrors the confirmed outcome-classification.
    """
    low = (body or "").lower()
    if len(body or "") > 5000 or "handheldfriendly" in low or "datadome" in low:
        return GettimeStatus.BLOCKED
    return GettimeStatus.OK


def parse_special_days(body: str) -> dict[int, dict[int, set[int]]]:
    """Extract SPECIAL_DAYS[year][month] = [day,...] (closed/holiday days) from a gettime body."""
    special: dict[int, dict[int, set[int]]] = {}
    for year, month, days_str in _SPECIAL_DAYS_RE.findall(body or ""):
        days = {int(d.strip("'\"")) for d in days_str.split(",") if d.strip()}
        special.setdefault(int(year), {})[int(month)] = days
    return special


def candidate_dates(
    special_days: dict[int, dict[int, set[int]]],
    num_days: int,
    *,
    today: date,
    start: date | None = None,
    end: date | None = None,
) -> list[date]:
    """Compute the open weekdays to scan (spec §6 date-window: CAL_START/END + CAL_NUM_DAYS).

    - Near edge = max(tomorrow, start) if start else tomorrow.
    - Far edge  = end (inclusive) if given, else unbounded (limited only by num_days).
    - Skips weekends and SPECIAL_DAYS (closed/holiday days).
    Returns up to num_days dates.
    """
    tomorrow = today + timedelta(days=1)
    cur = max(tomorrow, start) if start else tomorrow
    out: list[date] = []
    while len(out) < num_days and (end is None or cur <= end):
        if cur.weekday() < 5 and cur.day not in special_days.get(cur.year, {}).get(cur.month, set()):
            out.append(cur)
        cur += timedelta(days=1)
    return out

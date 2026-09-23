"""Blind apply — the (date x period) grid an operator seeds when discovery cannot see what the
portal shows.

WHY THIS EXISTS (operator direction 2026-09-18). Two independent records say our discovery can miss
real availability: /slots is on file as under-reporting ("parses/navigates correctly but
under-reports real availability", slots_reference_implementation, superseded 2026-07-06), and the
calendar path manufactures phantoms from the portal's fallback template under load (F-phantom-slots).
On Booking 0918 the portal's own calendar showed 12/13/14 October open while fifteen sessions polling
/slots all got `[]`. When the operator has confirmed availability BY EYE and the system finds nothing,
the right move is to try the combinations directly: the apply path already classifies a bounce off a
taken/invalid slot as the SLOT's problem ("no_slot" -- release, next), not the session's, and a slot
that books is real by evidence.

THE PERIOD TABLE BELOW IS MEASURED, NOT ASSUMED. Every PDF-verified booking and every discovered slot
in the database as of 2026-09-18, grouped by posto:

    posto   PDF-verified bookings on period   ever discovered
    2032    3, 4, 5                            3, 4, 5
    3018    4, 5, 7, 8                         4, 5, 7, 8
    3059    4, 5, 7, 8                         4, 5, 7, 8

Periods are POSTO-SPECIFIC: 3 exists only at 2032, 7 and 8 only at 3018/3059. Period 3 is
09:00-10:00, 4 is 10:00-11:00, 5 is 11:00-12:00 (operator). A posto with no history gets the union,
which costs a few extra bounces and misses nothing. Capacity per (date, period) is at least 3 --
three PDF-verified bookings landed on 2026-10-09 p5 -- so several workers targeting one combination
is expected, not wasteful. Every discovered slot was a weekday; weekends are never seeded.

Keep this table in step with the data: `SELECT posto_id, period FROM booked_appointments JOIN ...`
is the source of truth, and a posto whose real periods drift will bounce until this is updated.
"""
from __future__ import annotations

from datetime import date, timedelta

PROVEN_PERIODS_BY_POSTO: dict[str, tuple[int, ...]] = {
    "2032": (3, 4, 5),
    "3018": (4, 5, 7, 8),
    "3059": (4, 5, 7, 8),
}

# Union of everything ever booked or found. The fallback for a posto with no history of its own.
ALL_PROVEN_PERIODS: tuple[int, ...] = (3, 4, 5, 7, 8)


def default_blind_periods(posto_id: str | None) -> list[int]:
    """The periods worth trying blind at this posto -- its own proven set, or the union."""
    return list(PROVEN_PERIODS_BY_POSTO.get(str(posto_id or "").strip(), ALL_PROVEN_PERIODS))


def parse_dates(value) -> list[str]:
    """Accept a list or a comma/space-separated string of ISO dates; validate; drop weekends."""
    if value is None:
        return []
    raw = value if isinstance(value, (list, tuple)) else str(value).replace(";", ",").split(",")
    out: list[str] = []
    for item in raw:
        s = str(item).strip()
        if not s:
            continue
        d = date.fromisoformat(s)          # raises ValueError on garbage -- caller turns into a 400
        if d.weekday() >= 5:
            continue                        # never seeded: no discovered slot has ever been a weekend
        iso = d.isoformat()
        if iso not in out:
            out.append(iso)
    return out


def parse_periods(value) -> list[int]:
    if value is None:
        return []
    raw = value if isinstance(value, (list, tuple)) else str(value).replace(";", ",").split(",")
    out: list[int] = []
    for item in raw:
        s = str(item).strip()
        if not s:
            continue
        p = int(s)                          # ValueError on garbage -- caller turns into a 400
        if p < 1:
            raise ValueError(f"period must be >= 1, got {p}")
        if p not in out:
            out.append(p)
    return out


def blind_grid(dates: list[str], periods: list[int]) -> list[tuple[str, int]]:
    """Every (date, period) combination, date-major so the soonest dates are tried first --
    _assign_slots orders by date then period, so this matches the order workers will receive."""
    return [(d, p) for d in sorted(dates) for p in sorted(periods)]


def weekdays_between(start: str, end: str) -> list[str]:
    """Inclusive weekday range, for defaulting the dates from the booking's CAL window."""
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    out: list[str] = []
    cur = a
    while cur <= b:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out

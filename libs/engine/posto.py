"""Which posto is "the new one"? Pure -- the Hub calls it on what a worker saw in the Formulario dropdown.

Today's booking (2026-11-02, CCV Praia "Permanencia" at Sao Nicolau) is on a posto whose id nobody knows
yet. It shows up in the Formulario `f0sf1` dropdown (the list the portal accepts for the declared
residence), probably when the slots open. A posto is NEW when it is not in the known set (the operator's
POSTO_KNOWN plus the booking's current posto). POSTO_HINT -- a name fragment such as "nicolau" -- picks
between several new ones.
"""
from __future__ import annotations

import unicodedata


def _fold(text: str) -> str:
    """Lower-case, accent-free: 'SÃO NICOLAU' matches 'sao nicolau'."""
    return "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c)).lower()


def parse_ids(raw) -> set[str]:
    """'5084, 5086;5087' -> {'5084', '5086', '5087'}. Accepts a str or an iterable."""
    if isinstance(raw, str):
        parts = raw.replace(";", ",").replace(" ", ",").split(",")
    else:
        parts = list(raw or [])
    return {str(p).strip() for p in parts if str(p).strip()}


def new_postos(options, known, current: str | None = None) -> list[tuple[str, str]]:
    """The (value, label) options that are not in `known` and are not the booking's current posto."""
    skip = parse_ids(known) | ({str(current)} if current else set())
    return [(str(v), str(lbl)) for v, lbl in options if str(v).strip() and str(v) not in skip]


def pick_new_posto(options, known, current: str | None = None, hint: str = "") -> tuple[str, str] | None:
    """The posto to switch to, or None when there is nothing safe to pick.

    A hint match wins. Otherwise a SINGLE new option is taken; several new options and no hint match
    is ambiguous -- None, so the operator decides instead of the fleet guessing a decoy."""
    fresh = new_postos(options, known, current)
    if not fresh:
        return None
    h = _fold(hint).strip()
    if h:
        hit = [o for o in fresh if h in _fold(o[1])]
        if hit:
            return hit[0]
    return fresh[0] if len(fresh) == 1 else None

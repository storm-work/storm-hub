"""getPeriodosOcupados decoder — ported from storm-auto's CONFIRMED parser (batch_apply).

The portal returns a separator-less digit string of occupied period ids. Its confirmed structure
(live 0709 payloads) is TWO strictly-ascending runs:

    [CLOSED active periods]  ++  [INACTIVE (always-occupied background) periods]

- INACTIVE periods are the always-occupied background (never bookable).
- ACTIVE periods are the ones the portal opens/closes: the "holes" in the inactive run.
  A CLOSED active period sits in the front prefix; an OPEN one is ABSENT.
  => active_range = holes in the inactive run ; free = active_range − closed_prefix.

This is CEILING-INDEPENDENT — the active range is derived from the string's own shape, so it works
for postos whose period counts we don't know (e.g. 5084) without a hardcoded ceiling.

Kept byte-for-byte behaviorally identical to the proven implementation; only renamed/module-scoped.
"""
from __future__ import annotations


def tokenize_periods_structural(s: str, max_val: int = 99):
    """Tokenize the separator-less digit string using its confirmed STRUCTURE: two strictly-ascending
    runs of DISTINCT period ids (at most ONE descent). Tokens are 1-2 digits, no leading zero,
    value 1..max_val.

    Among all tokenizations of that shape, pick the one CONSISTENT with the active-range model
    (closed prefix ⊆ holes-in-the-inactive-tail) and, as a tiebreak, the SMALLEST active range.

    Returns (closed_prefix:set, inactive_tail:list, active_holes:set) or None if untokenizable.
    """
    n = len(s)
    if n == 0:
        return set(), [], set()
    results: list[list[int]] = []
    _seen_cap = [0]

    def rec(i, prev, broke, seen, toks):
        if _seen_cap[0] > 50000 or len(results) > 500:
            return
        if i == n:
            _seen_cap[0] += 1
            results.append(toks)
            return
        for L in (1, 2):
            if i + L > n:
                continue
            ch = s[i:i + L]
            if ch[0] == "0":
                continue
            v = int(ch)
            if v > max_val or v in seen:
                continue
            if prev is not None and v <= prev:
                if broke:
                    continue
                nb = True
            else:
                nb = broke
            rec(i + L, v, nb, seen | {v}, toks + [v])

    rec(0, None, False, set(), [])
    best = None
    best_key = None
    for toks in results:
        b = next((j for j in range(1, len(toks)) if toks[j] <= toks[j - 1]), None)
        prefix = set(toks[:b]) if b else set()
        tail = toks[b:] if b else toks
        hi = max(toks)
        holes = {p for p in range(1, hi + 1) if p not in set(tail)}
        if not (prefix <= holes):     # inconsistent with the active-range model
            continue
        key = (len(holes), len(prefix), len(toks))   # smallest active range, then shortest prefix
        if best_key is None or key < best_key:
            best_key, best = key, (prefix, tail, holes)
    return best


def parse_periodos_ocupados(s: str, max_period: int | None = None):
    """Decode an occupied string into (free, occupied-active) ACTIVE period id lists.

    Returns (sorted free ids, sorted closed-active ids), or (None, None) if the string can't be
    tokenized to the two-ascending-runs shape. Empty string -> ([], []).
    """
    s = s or ""
    if not s:
        return [], []
    max_val = max(99, max_period) if max_period else 99
    res = tokenize_periods_structural(s, max_val=max_val)
    if not res:
        return None, None
    prefix, _tail, holes = res
    return sorted(holes - prefix), sorted(prefix)

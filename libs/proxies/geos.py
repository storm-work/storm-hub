"""The exit countries each rotating provider supports -- the single source of truth for the
per-worker geo dropdown and the PATCH /workers/{id}/geo validation.

WHY PER PROVIDER. soax and webshare do not offer the same country set, and picking a country a
provider cannot serve breaks that worker's exit. So the dropdown is filtered to the worker's
resolved provider (operator decision 2026-09-24), and validation rejects a code that provider does
not list.

CODE FORM. Stored provider-neutrally as an UPPERCASE code (e.g. "PT"); the provider layer applies
its own case at mint time -- soax lowercases (`-country-pt-`), webshare uppercases (`-PT-`), see
libs/proxies/providers/. Labels are for the dropdown only.

POPULATING THIS. Paste the operator's authoritative soax / webshare lists below, keyed by the code
each provider expects (normalized to uppercase here), value = human label. Until a provider's dict
is populated, validation for that provider is PERMISSIVE (any non-empty code accepted) so the
mechanism works end to end before the data lands -- tighten automatically the moment the dict fills.
"""
from __future__ import annotations

# code (UPPERCASE) -> label.  <<< PASTE THE OPERATOR'S SOAX LIST HERE >>>
SOAX_GEOS: dict[str, str] = {
    # "PT": "Portugal", "ES": "Spain", "DE": "Germany", "GB": "United Kingdom", "FR": "France", ...
}

# code (UPPERCASE) -> label.  <<< PASTE THE OPERATOR'S WEBSHARE LIST HERE >>>
WEBSHARE_GEOS: dict[str, str] = {
    # "PT": "Portugal", "ES": "Spain", "DE": "Germany", "GB": "United Kingdom", "FR": "France", ...
}

_BY_PROVIDER: dict[str, dict[str, str]] = {"soax": SOAX_GEOS, "webshare": WEBSHARE_GEOS}


def normalize_geo(code: str | None) -> str:
    """Canonical stored form: trimmed, uppercase. '' for blank -- callers treat '' as 'unbound'."""
    return str(code or "").strip().upper()


def supported_geos(provider: str | None) -> dict[str, str]:
    """{code: label} the provider supports, for the dropdown. Unknown provider -> {} (no options)."""
    return _BY_PROVIDER.get(str(provider or "").strip().lower(), {})


def is_valid_geo(provider: str | None, code: str | None) -> bool:
    """Is `code` a country this provider can serve?

    Permissive while the provider's list is empty (not yet populated) -- any non-empty code passes,
    so the endpoint and dropdown work before the authoritative list is pasted in. Once the dict is
    filled, only listed codes pass. A blank code is always valid: it means 'unbind' (re-randomize).
    """
    norm = normalize_geo(code)
    if not norm:
        return True                       # clearing the binding
    table = supported_geos(provider)
    if not table:
        return True                       # list not populated yet -- see module docstring
    return norm in table

"""Provider-specific proxy credential construction -- Phase 3 (docs/engineering/DECISIONS.md D32).

Relocated from scripts/gen_proxy_pool.py, which now imports these instead of building credential
strings inline -- same grammar, same output shape, proven by tests/proxies/test_credentials.py
against both the documented contract (docs/engineering/PROXY_CONTRACTS.md) and a golden sample
captured from the script before this relocation.

These are per-credential builders, not the bulk/country-splitting orchestration in the script --
that stays there, since it is specifically the CLI's bulk-import-file feature. A future Hub-side
caller minting one fresh credential at a time would call these functions directly.
"""
from __future__ import annotations

import random
import string

SOAX_ENDPOINT = "proxy.soax.com:5000"
WEBSHARE_ENDPOINT = "p.webshare.io:80"


def soax_session_id(seen: set[str] | None = None) -> str:
    """A fresh, collision-avoided SOAX session id -- 16 random alphanumeric characters.

    Matches D2 (the session id is the proxy's identity; never invent a shape the provider does not
    expect -- this IS the confirmed shape). `seen`, if given, is a caller-owned set checked and
    updated in place so a whole batch never repeats one within itself; each call still returns a
    freshly-random id even when `seen` is omitted.
    """
    if seen is None:
        seen = set()
    while True:
        sid = "".join(random.choices(string.ascii_letters + string.digits, k=16))
        if sid not in seen:
            seen.add(sid)
            return sid


def build_soax_credential(*, package: str, password: str, session_id: str, sessionlength: int,
                          bindttl: int, idlettl: int, country: str | None = None) -> str:
    """One SOAX credential string.

    `country=None` omits the `-country-{cc}` segment entirely, so SOAX serves any exit in the
    package -- the confirmed representation of "random" (PROXY_CONTRACTS.md, D2): never pass the
    literal string `"random"` as `country`.
    """
    geo = f"-country-{country.lower()}" if country else ""
    return (f"package-{package}{geo}-sessionid-{session_id}-"
            f"sessionlength-{sessionlength}-bindttl-{bindttl}-idlettl-{idlettl}"
            f"-opt-wb:{password}@{SOAX_ENDPOINT}")


def build_webshare_credential(*, prefix: str, password: str, session_id: str | int,
                              country: str | None = None) -> str:
    """One Webshare credential string.

    `session_id` is the actual rotation unit (D2) -- a numeric value pins one exit (sticky); the
    literal string `"rotate"` gives a new exit per request (PROXY_CONTRACTS.md, confirmed
    2026-08-25). This function does not special-case either value, matching the confirmed contract
    that any session id, whatever it is, goes in the same position.

    `country=None` omits the `-{CC}-` segment -- Webshare carries geo in the username, so "random"
    here means dropping that segment, not passing a literal country value.
    """
    if country:
        return f"{WEBSHARE_ENDPOINT}:{prefix}-{country.upper()}-{session_id}:{password}"
    return f"{WEBSHARE_ENDPOINT}:{prefix}-{session_id}:{password}"

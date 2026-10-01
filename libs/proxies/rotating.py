"""Mint / roll / read a rotating proxy URL -- a thin dispatcher over libs/proxies/providers/.

WHY THIS IS THIN. Everything provider-specific -- the connection-string format, which credential
fields exist, how a session id is written and re-written -- lives in one module per provider under
`providers/`. This file only routes. Adding a third provider therefore touches `providers/<new>.py`
and the registry, and nothing here, in the Hub, or in the worker.

WHY ROTATING PROXIES ARE NOT DB ROWS (operator direction 2026-09-15, DECISIONS.md D39). A rotating
"proxy" was never a resource; it was a pre-generated session-id string. The system stored 42,600 of
them and spent a lease table, a health-scoring system, a tiered selection query and a Hub
round-trip managing strings it could produce in a microsecond. The codebase had already reached
that conclusion without acting on it (lease.py:153, D19, health.py:121-140: "There is no row-level
subject to screen").

WHAT SETTLED THE OBJECTION. `_candidate_query` preferred an "established" session id over a fresh
one, on a measurement CONFIRMED_FACTS.md §16 had already retracted. The live data closed it by
arithmetic: soax's own params are sessionlength=3600 / idlettl=900, and 0 of 42,600 rows had been
used within EITHER window. A session id cannot be "established" past its own TTL, so the pool held
no live sessions to lose. (Airtight for soax, whose TTLs are ours; webshare's session lifetime is
provider-determined and still unmeasured -- see Q2.)

STATIC/ISP IS UNAFFECTED and shares no code with this file. A static resource is a finite,
individually-owned IP where a probe verdict has a real subject: it keeps the lease table, the
health scoring and Hub-side assignment, in libs/proxies/lease.py.
"""
from __future__ import annotations

import re

from . import providers
from .providers import ProviderError, new_session_id

# Back-compat alias: this exception was `RotatingPoolError` when minting lived in this module, and
# apps/hub/main.py catches it by that name to turn a misconfigured pool into a 409.
RotatingPoolError = ProviderError

__all__ = ["RotatingPoolError", "ProviderError", "mint_rotating_url", "roll_rotating_url",
           "rotating_session_id", "new_session_id", "reseat_geo"]


def reseat_geo(url: str, geo: str | None, *, provider: str = "") -> str:
    """Return `url` with its exit COUNTRY changed to `geo` (None/'' = drop the country = any exit),
    keeping the session id and credentials intact.

    Unlike libs/proxies/lease._inject_geo (which only ADDS a country and no-ops when one is already
    present), this REPLACES it. A worker uses this to vary its exit geo on each exit roll, so it is
    never bound to a single (possibly throttled) country for its whole run -- the worst case for the
    login throttle (2026-10-01, operator direction).

    Implemented for soax (the rotating production provider), whose country is a URL parameter
    `-country-cc`. Webshare carries geo inside a provider-specific username prefix that string
    surgery cannot reseat safely, so a webshare URL is returned UNCHANGED (its geo stays whatever it
    was minted with). Any unrecognised URL is returned unchanged too (D2: never corrupt a credential
    we do not understand)."""
    u = url or ""
    if not (provider == "soax" or "soax" in u):
        return u
    cc = (geo or "").strip().lower()
    if not cc:
        return re.sub(r"-country-[a-z]{2}", "", u, count=1, flags=re.I)
    if re.search(r"-country-[a-z]{2}", u, flags=re.I):
        return re.sub(r"-country-[a-z]{2}", f"-country-{cc}", u, count=1, flags=re.I)
    return re.sub(r"(package-[A-Za-z0-9]+)", rf"\1-country-{cc}", u, count=1)


def mint_rotating_url(provider: str, kind: str | None = None, *, settings: dict | None = None,
                      geo: str | None = None, scheme: str = "http",
                      env: dict | None = None) -> str:
    """One fresh rotating connection URL: http://user:pass@host:port.

    No DB, no network, no shared state -- which is exactly what makes it safe to call from any
    worker without coordination. The session id is freshly random per call (62^16), so two workers
    minting simultaneously collide far below any failure rate the system already tolerates. That is
    the "unique session id throughout the hub" requirement met without a registry, and a registry
    would reintroduce the shared-state round-trip this design removes.

    `geo` goes into the CREDENTIAL, the only place either provider accepts it; None means no
    restriction and never the literal string "random" (D2).

    `settings` carries the dashboard's operational knobs (the TTLs); credentials come from the
    environment regardless of what is in it. `env` overrides the environment, for tests only.
    """
    prov = providers.get(provider)
    creds = providers.resolve_credentials(provider, kind, settings=settings, env=env)
    # The session id's SHAPE is a provider fact (soax accepts 16-alphanumeric, webshare only digits),
    # so the provider mints its own -- not the shared alphanumeric one, which webshare rejects.
    return prov.mint(creds, session_id=prov.new_session_id(), geo=geo or None, scheme=scheme)


def roll_rotating_url(url: str, *, provider: str = "") -> str:
    """Same proxy URL, FRESH session id -- i.e. a different exit. This is the only way a worker
    changes exit now: no Hub round-trip, no lease, no pool contention.

    Pure and total: anything unrecognised comes back unchanged, so a caller can always use the
    result (D2 -- rolling a username we do not understand would stop it authenticating). `provider`
    is an optional hint; without it the grammar is detected from the URL.
    """
    prov = providers.get(provider) if provider else providers.for_url(url)
    return prov.roll(url) if prov is not None else url


def rotating_session_id(url: str, *, provider: str = "") -> str:
    """The session id in `url` -- the only thing identifying which exit a credential holds.

    On a gateway provider the host:port is a constant (every webshare credential we own points at
    `p.webshare.io:80`), so identifying a proxy by endpoint identifies nothing. Returns "" when
    there is none, and never any part of the password.
    """
    prov = providers.get(provider) if provider else providers.for_url(url)
    return prov.read_id(url) if prov is not None else ""

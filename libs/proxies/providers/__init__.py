"""Rotating-proxy provider registry — the one place that knows which providers exist.

ADDING A PROVIDER is a two-step change and touches nothing else in the system:
  1. write `libs/proxies/providers/<name>.py` with a `RotatingProvider` subclass (copy soax.py or
     webshare.py -- they are each ~70 lines and the whole contract is in base.py);
  2. add it to `_PROVIDERS` below.
Its credentials then come from the environment as `{env_prefix}_{FIELD}`, and the dashboard's
existing provider pickers pick it up from `names()` without further work.

CREDENTIALS COME FROM THE ENVIRONMENT (operator direction 2026-09-16). Login/password/package are
deployment facts, not operational settings: they are the same for every booking, they change only
when a subscription changes, and they are secrets. Putting them in the database meant they were
editable from a web dashboard, readable by anything with a DB connection, and -- as actually
happened -- recoverable only from 42,600 proxy rows that a migration was about to delete.

The dashboard still owns the OPERATIONAL knobs, which is the correct split: geo selection,
concurrency limits, which pool a booking draws from. Those are resolved at launch and passed in.

A DB-settings fallback is kept for one deliberate reason: `SOAX_ROTATING_PASSWORD` existed nowhere
but those proxy rows, so `scripts/backfill_rotating_credentials.py` lifts it into global settings.
Environment always wins; the fallback exists so a host that has not yet had the value added to its
.env keeps working, and it is the only reason `settings` is a parameter here.
"""
from __future__ import annotations

import os

from .base import Pool, ProviderError, RotatingProvider, new_session_id
from .capsolver import Capsolver
from .soax import Soax
from .webshare import Webshare

_PROVIDERS: dict[str, RotatingProvider] = {
    p.name: p for p in (Soax(), Webshare(), Capsolver())
}

__all__ = ["Pool", "ProviderError", "RotatingProvider", "new_session_id",
           "get", "names", "resolve_credentials", "for_url"]


def names() -> list[str]:
    return sorted(_PROVIDERS)


def get(provider: str) -> RotatingProvider:
    p = _PROVIDERS.get(str(provider or "").strip().lower())
    if p is None:
        raise ProviderError(
            f"unknown rotating provider {provider!r}; known providers are {', '.join(names())}")
    return p


def for_url(url: str) -> RotatingProvider | None:
    """Which provider's grammar does `url` use? None if nothing recognises it.

    This is what lets `roll()`/`read_id()` dispatch without the caller knowing or caring which
    provider a worker happens to be on -- the replacement for the hard-coded if/else over both
    grammars that used to live in lease.py.
    """
    for p in _PROVIDERS.values():
        if p.owns(url):
            return p
    return None


def resolve_credentials(provider: str, kind: str | None = None, *,
                        settings: dict | None = None, env: dict | None = None,
                        creds: dict | None = None) -> dict:
    """Everything `mint()` needs for one pool: secrets from the environment, options from settings.

    The two namespaces resolve from DIFFERENT sources ON PURPOSE (see Pool's docstring):

        secrets  -- os.environ[f"{env_prefix}_{FIELD}"] ONLY. Not from `settings`, so a database
                    row or a dashboard field can never supply a credential.
        options  -- settings[NAME], else os.environ[NAME], else the declared default.

    `env` overrides the environment mapping and exists for TESTS. It is not a convenience: because
    apps/hub/config.py loads .env at import, anything that imports the Hub has live production
    credentials sitting in os.environ, and a test that merely asserted on a minted URL would
    silently exercise -- and print -- the real password. Tests pass `env=` and stay hermetic.

    Raises ProviderError naming the exact missing variable. The launch endpoint surfaces that
    message verbatim as a 409, because the operator's next action is to go and set it. A worker
    launched with a half-built credential authenticates as nobody, and the failure then reads as a
    portal problem rather than a configuration one.
    """
    prov = get(provider)
    pool = prov.pool(kind)
    settings = settings or {}
    environ = os.environ if env is None else env
    out: dict = {}
    missing: list[str] = []

    # SECRETS. Normally from the environment ONLY (`{env_prefix}_{FIELD}`). When `creds` is given
    # -- a NAMED rotating pool whose account creds live in the DB (operator direction 2026-10-08) --
    # the secret comes from there instead, and the Hub (the only caller with DB access) is what
    # loads and passes it. The worker never sees this path: it receives an already-minted URL. The
    # missing-field error names the DB field when creds was supplied, the env var otherwise, so the
    # operator is told where to go set it.
    for fieldname in pool.secrets:
        if creds is not None:
            val = creds.get(fieldname)
            key = f"{pool.name} pool field {fieldname!r}"
        else:
            key = f"{pool.env_prefix}_{fieldname}"
            val = environ.get(key)
        if val is None or str(val).strip() == "":
            missing.append(key)
            continue
        out[fieldname] = str(val).strip()

    for key, default in pool.options.items():
        val = settings.get(key)
        if val is None or str(val).strip() == "":
            val = environ.get(key)
        out[key] = default if (val is None or str(val).strip() == "") else str(val).strip()

    if missing:
        raise ProviderError(
            f"{prov.name}/{pool.name} cannot be minted: {', '.join(missing)} not configured. "
            f"Set it in the environment (.env) -- rotating credentials are not database settings.")
    return out

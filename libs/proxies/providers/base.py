"""The contract every rotating-proxy provider module implements.

ONE MODULE PER PROVIDER (operator direction 2026-09-16). Adding a provider means adding one file
next to soax.py/webshare.py and registering it -- nothing else in the system changes. That is the
whole point of this layer: the provider's connection-string grammar, its credential fields, and the
way a session id is written and re-written inside that string are ALL facts about the provider, and
they belong in one place that a person can read end to end when onboarding a new one.

WHAT A PROVIDER MODULE OWNS
  * `secrets`   -- the credential fields it needs (login/password, plus anything provider-specific
                   such as SOAX's package number), declared per POOL and read from the ENVIRONMENT.
  * `options`   -- non-secret operational knobs with defaults (the TTLs), overridable from the
                   dashboard settings.
  * `mint()`    -- build one connection URL from credentials + a fresh session id + optional geo.
  * `roll()`    -- rewrite an existing URL to a NEW session id, i.e. move to a different exit.
  * `read_id()` -- extract the session id from a URL (identity, for logging and change detection).
  * `owns()`    -- recognise its own credential grammar in a URL.

WHAT IT DELIBERATELY DOES NOT OWN
  * Credential VALUES. Those come from the environment (see `resolve_credentials` in this
    package's __init__), never from the database and never from a literal in this repo.
  * Operational policy -- geo selection, concurrency limits, which pool a booking uses. Those are
    dashboard settings resolved at launch and passed IN.
  * Anything to do with static/ISP proxies. A static proxy is a finite, individually-owned IP that
    is leased from the database and health-scored per row; a rotating "proxy" is a string we mint.
    They are different kinds of thing and share no code path.

WHY roll()/read_id() LIVE HERE. They used to sit in libs/proxies/lease.py, which is otherwise the
STATIC leasing module, and they carried a hard-coded if/else over both providers' grammars. Every
new provider meant editing that shared branch -- exactly the "add a case to the big switch" shape
this refactor removes. They are pure string grammar, so they belong beside the grammar that
produced the string.

D2 / PROXY_CONTRACTS.md:131 IS THE RULE THAT CONSTRAINS roll(): "the session id is the proxy's
identity ... never invent one." Rolling a value in a slot the provider defines is legal; appending
an id to a username whose grammar we do not recognise is not -- it rewrites the USERNAME and stops
authenticating. Every implementation of roll() must return the URL UNCHANGED when it does not
recognise the credential, so a caller can always use the result.
"""
from __future__ import annotations

import random
import string
from dataclasses import dataclass, field


class ProviderError(ValueError):
    """The requested provider/pool cannot be used as configured."""


def new_session_id(seen: set[str] | None = None) -> str:
    """A fresh session id: 16 random alphanumerics, the confirmed SOAX shape (D2).

    Deliberately the SAME shape for every provider. Webshare's generator historically used `1..n`
    integers, which are unique only within one generator run -- not a property that survives
    workers minting independently, which is what they do now. 62^16 makes collision negligible
    without any registry, and a registry would reintroduce the shared-state round-trip that
    self-rotation exists to remove.

    `seen`, when given, is a caller-owned set checked and updated in place so one batch never
    repeats an id within itself.
    """
    if seen is None:
        seen = set()
    while True:
        sid = "".join(random.choices(string.ascii_letters + string.digits, k=16))
        if sid not in seen:
            seen.add(sid)
            return sid


@dataclass(frozen=True)
class Pool:
    """One purchasable pool within a provider.

    A provider can sell several pools that share a grammar but not a credential -- SOAX's
    `rotating` and `mobile` are different packages with DIFFERENT PASSWORDS, and conflating them
    silently authenticates one pool with the other's password. That trap is recorded in
    scripts/gen_proxy_pool.py and it has bitten this project for real: 11,000 generated rows
    carried the mobile password against a rotating package.

    TWO NAMESPACES, AND THE SPLIT IS THE DESIGN (operator direction 2026-09-16):

    `secrets` -- login/password/package. Read from the ENVIRONMENT ONLY, as
        `{env_prefix}_{FIELD}`. Deployment facts, identical for every booking, changing only when a
        subscription changes. Never from the database, never editable from a web dashboard, never a
        literal in this repo.

    `options` -- operational knobs (the TTLs). Read from the dashboard SETTINGS first, then the
        environment, then the default below. These are per-run policy an operator legitimately
        tunes, and they are not secret. Their names are NOT pool-prefixed: `SOAX_IDLETTL` applies to
        every SOAX pool, because it describes the provider's behaviour rather than one purchase.

    Keeping secrets out of the settings namespace is not cosmetic. While they shared it, resolution
    had to consult ambient os.environ, and because apps/hub/config.py loads .env at import, the
    TEST SUITE resolved live production credentials -- one duly appeared in a pytest failure
    message. Secrets now come from exactly one place, and tests override it explicitly.
    """
    name: str
    env_prefix: str
    secrets: tuple[str, ...] = ("PASSWORD",)
    options: dict = field(default_factory=dict)


class RotatingProvider:
    """Base class. Subclass it, fill in the class attributes, implement the three grammar methods."""

    name: str = ""
    endpoint: str = ""
    pools: dict[str, Pool] = {}

    # ── credentials ──────────────────────────────────────────────────────────────────────────
    def pool(self, kind: str | None) -> Pool:
        """Resolve a pool name. A falsy kind means the provider's default (first declared) pool."""
        if not kind:
            return next(iter(self.pools.values()))
        p = self.pools.get(str(kind).lower())
        if p is None:
            raise ProviderError(
                f"{self.name}: unknown pool {kind!r}; known pools are "
                f"{', '.join(sorted(self.pools))}")
        return p

    # ── session id: shape is a PROVIDER fact ─────────────────────────────────────────────────
    def new_session_id(self, seen: set[str] | None = None) -> str:
        """A fresh session id in THIS provider's accepted shape. Default: the 16-alphanumeric SOAX
        shape (`new_session_id`). A provider whose gateway REJECTS letters in the session segment
        overrides this -- Webshare does, and paid for it: a 16-alphanumeric id there authenticates
        as nobody (400/407) and the worker silently falls back to another provider's exits."""
        return new_session_id(seen)

    # ── grammar: the three things a provider module must implement ───────────────────────────
    def mint(self, creds: dict, *, session_id: str, geo: str | None = None,
             scheme: str = "http") -> str:
        raise NotImplementedError

    def roll(self, url: str) -> str:
        """Same URL, new session id. MUST return `url` unchanged if the grammar is unrecognised."""
        raise NotImplementedError

    def read_id(self, url: str) -> str:
        """The session id in `url`, or "" if there is none. MUST never return part of the password."""
        raise NotImplementedError

    def owns(self, url: str) -> bool:
        """Does `url` look like this provider's credential? Used to dispatch roll/read_id."""
        return bool(url) and self.endpoint.split(":")[0] in url


def split_url(url: str):
    """(scheme, user, sep, password, hostport) or None if this is not a credentialed proxy URL.

    Shared because every provider needs the identical decomposition before touching its own
    grammar, and duplicating it per module is how the two copies drift apart.
    """
    if not url or "@" not in url:
        return None
    head, _, hostport = url.rpartition("@")
    scheme, _, cred = head.partition("://")
    user, sep, pwd = cred.partition(":")
    if not user:
        return None
    return scheme, user, sep, pwd, hostport

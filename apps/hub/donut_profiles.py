"""Which sub-accounts already have a Donut profile.

Donut blocks an account for 24 hours when it creates too many profiles in one hour (Donut issue #681;
our registrar hit it on 2026-10-29 after ~236 profiles). While that block is on, a login on a sub-account
WITHOUT a profile cannot even launch a browser (HTTP 429 WAYFERN_GENERATION_LIMIT_REACHED), so the live
sub-account ranking can be told to offer only sub-accounts whose profile already exists
(DONUT_EXISTING_PROFILES_ONLY).
"""
from __future__ import annotations

import logging
import time

from libs.engine.vendor import donut

logger = logging.getLogger("storm.hub.donut_profiles")

_TTL_S = 60.0
_cache: tuple[float, frozenset[str]] | None = None


def existing_profile_names(g: dict) -> frozenset[str] | None:
    """The `storm-*` profile names Donut has right now, cached 60s. None when Donut cannot be asked
    (not running, no token): callers must then NOT filter -- an unreachable Donut is not a reason to
    empty every identity."""
    global _cache
    now = time.time()
    if _cache is not None and now - _cache[0] < _TTL_S:
        return _cache[1]
    try:
        api = donut._Api(url=g.get("DONUT_API_URL") or "", token=g.get("DONUT_API_TOKEN") or "")
        r = api.call("GET", "/v1/profiles")
        profs = r.get("profiles", r) if isinstance(r, dict) else r
        names = frozenset(str(p.get("name")) for p in (profs or []) if str(p.get("name", "")).startswith("storm-"))
    except Exception as e:
        logger.warning("donut profile listing failed (%s) -- not filtering sub-accounts", str(e)[:120])
        return None
    _cache = (now, names)
    return names


def keep_with_profile(subs: list, g: dict) -> list:
    """`subs` reduced to those whose Donut profile exists; unchanged when the setting is off, the
    backend is not donut, or Donut cannot be asked."""
    if str(g.get("BROWSER_BACKEND", "") or "").lower() != "donut":
        return subs
    # `g` is the raw settings map (every value a string): "false" is truthy, so parse it.
    if str(g.get("DONUT_EXISTING_PROFILES_ONLY", "true")).strip().lower() in ("false", "0", "no", "off", ""):
        return subs
    names = existing_profile_names(g)
    if names is None:
        return subs
    kept = [s for s in subs if donut.profile_name(s.username) in names]
    if len(kept) != len(subs):
        logger.info("DONUT_EXISTING_PROFILES_ONLY: %d/%d sub-accounts have a profile", len(kept), len(subs))
    return kept

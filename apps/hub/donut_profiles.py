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
import uuid

from sqlalchemy import select

import libs.db.models as M
from libs.domain.enums import READY_SUB_ACCOUNT_STATUSES
from libs.engine.vendor import donut

from .donut_sweep import _DEAD_IDENT, _DEAD_SUB, _PENDING_SUB, known_profile_index

logger = logging.getLogger("storm.hub.donut_profiles")

# Donut's hourly new-profile budget (Pro = 100/h, a 24h block above it) is shared with the registrar
# through this Redis window; batch creation on the Identities page must count against the SAME window.
_BUDGET_KEY = "donut:profile_creates"
_BUDGET_WINDOW_S = 3600.0

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


# ── Operator tools (Identities page) ────────────────────────────────────────────
# Register+verify can run on the Chromium path (no Donut profile); the booking path then needs a Donut
# profile per sub-account. These let an operator create those profiles for already-verified accounts in
# a batch, reconcile what Donut holds against the sub-account table, and clean up what no login can use.

def _budget_slots_left(r, cap: int, now: float | None = None) -> int:
    """How many NEW profiles may still be created this rolling hour, from the shared Donut window.
    cap <= 0 (budget disabled) or no redis -> unbounded (returned as a large number)."""
    if cap <= 0 or r is None:
        return 1 << 30
    now = time.time() if now is None else now
    try:
        r.zremrangebyscore(_BUDGET_KEY, 0, now - _BUDGET_WINDOW_S)
        return max(0, cap - int(r.zcard(_BUDGET_KEY)))
    except Exception as e:
        logger.warning("donut budget check failed (%s) -- treating budget as full", str(e)[:120])
        return 0


def _budget_take(r, now: float | None = None) -> None:
    """Record one profile creation in the shared window, so the registrar and this page pace together."""
    if r is None:
        return
    now = time.time() if now is None else now
    try:
        r.zadd(_BUDGET_KEY, {f"{now:.3f}-{uuid.uuid4().hex[:8]}": now})
    except Exception:
        pass


def _create_one(api, name: str, g: dict) -> None:
    """POST a Donut account-scope Wayfern profile (fingerprint only, no proxy -- a launch sets the exit).
    Mirrors donut._ensure_profile's create body but reads OS / keep-state from the Global settings `g`
    rather than the worker's env, since the Hub does not run with the Donut env exported."""
    os_val = str(g.get("DONUT_PROFILE_OS") or "").strip().lower() or "windows"
    keep_state = str(g.get("DONUT_PROFILE_KEEP_STATE") or "0").strip() == "1"
    body = {"name": name, "browser": "wayfern", "version": "latest",
            "wayfern_config": {"os": os_val}, "ephemeral": False}
    resp = api.call("POST", "/v1/profiles", body)
    pid = str(donut._unwrap(resp, "profile").get("id") or "")
    if not pid:
        raise donut.DonutError("Donut POST /v1/profiles returned no id")
    # Every session starts from an empty jar unless keep-state is on -- same as the launch path.
    api.call("PUT", f"/v1/profiles/{pid}", {"clear_on_close": not keep_state})


def create_profiles(db, g: dict, usernames: list[str], *, redis=None) -> dict:
    """Create a Donut profile for each of `usernames` that has none yet. Returns a report
    {created, existed, deferred, errors, unreachable}. Respects Donut's hourly new-profile budget
    (DONUT_PROFILES_PER_HOUR) through the shared Redis window: a username that would exceed it is left
    `deferred` rather than created, so a batch never trips the 24h block. Backfills each created (or
    already-present) sub-account's donut_profile / browser_backend so the row reflects reality."""
    global _cache
    out = {"created": [], "existed": [], "deferred": [], "errors": [], "unreachable": False}
    if str(g.get("BROWSER_BACKEND", "") or "chromium").strip().lower() != "donut":
        out["errors"].append("BROWSER_BACKEND is not donut -- profiles are only used on the Donut path")
        return out
    try:
        api = donut._Api(url=g.get("DONUT_API_URL") or "", token=g.get("DONUT_API_TOKEN") or "")
        # List FRESH, not through the 60s existing_profile_names cache: a stale "absent" here would make
        # us re-POST a profile that already exists -- double-spending the hourly quota, which is the very
        # 24h block this budget exists to avoid (an operator clicking twice, or a concurrent registrar).
        profs = donut._items(api.call("GET", "/v1/profiles", timeout=15), "profiles")
    except donut.DonutError as e:
        out["unreachable"] = True
        out["errors"].append(str(e)[:200])
        return out
    present = frozenset(str(p.get("name")) for p in (profs or [])
                        if str(p.get("name", "")).startswith(donut._PREFIX))

    rows = db.scalars(select(M.SubAccount).where(M.SubAccount.username.in_(usernames))).all()
    subs_by_name: dict[str, M.SubAccount] = {}
    for sa in rows:
        n = donut.profile_name(sa.username)
        if n:
            subs_by_name[n] = sa

    try:
        cap = int(g.get("DONUT_PROFILES_PER_HOUR") or 0)
    except (TypeError, ValueError):
        cap = 0
    left = _budget_slots_left(redis, cap)

    for name, sa in subs_by_name.items():
        if name in present:
            out["existed"].append(sa.username)
            if sa.donut_profile != name or (sa.browser_backend or "").lower() != "donut":
                sa.donut_profile, sa.browser_backend = name, "donut"      # backfill the row
            continue
        if left <= 0:
            out["deferred"].append(sa.username)
            continue
        try:
            _create_one(api, name, g)
        except donut.DonutRateLimited:
            out["deferred"].append(sa.username)       # budget already full on Donut's side -- stop asking
            left = 0
            continue
        except donut.DonutError as e:
            out["errors"].append(f"{sa.username}: {str(e)[:120]}")
            continue
        _budget_take(redis)
        left -= 1
        sa.donut_profile, sa.browser_backend = name, "donut"
        out["created"].append(sa.username)
    db.commit()
    if out["created"]:
        _cache = None   # the shared 60s list would otherwise hide the new profiles from keep_with_profile
    if out["created"] or out["deferred"]:
        logger.info("donut profile create: %d made, %d existed, %d deferred (budget), %d errors",
                    len(out["created"]), len(out["existed"]), len(out["deferred"]), len(out["errors"]))
    return out


def delete_profiles_for(g: dict, usernames: list[str]) -> dict:
    """Delete the Donut profiles (and their proxy entries) of sub-accounts we just removed, so they do
    not linger as orphans for the next cleanup. Best effort, never raises: a RUNNING profile is skipped
    (something is using it), and an unreachable Donut just leaves them for 🧹 Donut cleanup."""
    out = {"deleted": 0, "skipped_running": 0, "errors": []}
    if not usernames or str(g.get("BROWSER_BACKEND", "") or "chromium").strip().lower() != "donut":
        return out
    wanted = {donut.profile_name(u) for u in usernames if u}
    try:
        api = donut._Api(url=g.get("DONUT_API_URL") or "", token=g.get("DONUT_API_TOKEN") or "")
        profiles = donut._items(api.call("GET", "/v1/profiles", timeout=15), "profiles")
    except Exception as e:
        out["errors"].append(f"Donut unreachable: {str(e)[:120]}")
        return out
    for p in profiles:
        if p.get("name") not in wanted:
            continue
        if p.get("is_running"):
            out["skipped_running"] += 1
            continue
        try:
            api.call("DELETE", f"/v1/profiles/{p['id']}", timeout=15)
            out["deleted"] += 1
        except Exception as e:
            out["errors"].append(f"{p.get('name')}: {str(e)[:100]}")
            continue
        if p.get("proxy_id"):
            try:
                api.call("DELETE", f"/v1/proxies/{p['proxy_id']}", timeout=15)
            except Exception:
                pass
    return out


def profile_status(db, g: dict) -> dict:
    """Reconcile the live Donut profiles against the sub-account table. Pure read, never mutates.
    Classifies every `storm-*` profile as matched / junk (orphan | finished | abandoned signup) and
    counts the verified sub-accounts that still have NO profile -- the Identities page's status check."""
    out = {"reachable": False, "backend": str(g.get("BROWSER_BACKEND") or "chromium").strip().lower(),
           "note": "", "profiles_total": 0, "running": 0, "matched": 0,
           "junk_total": 0, "junk": {"orphan": 0, "finished": 0, "abandoned": 0}, "junk_sample": [],
           "missing_total": 0, "missing_sample": [], "ready_total": 0}
    if out["backend"] != "donut":
        out["note"] = "BROWSER_BACKEND is not donut; profiles are not in use"
        return out
    try:
        api = donut._Api(url=g.get("DONUT_API_URL") or "", token=g.get("DONUT_API_TOKEN") or "")
        profiles = donut._items(api.call("GET", "/v1/profiles", timeout=15), "profiles")
    except Exception as e:
        out["note"] = f"Donut could not be reached: {str(e)[:160]}"
        return out
    out["reachable"] = True
    known, _pending = known_profile_index(db, donut.profile_name)

    ready_names: set[str] = set()
    for (u,) in db.execute(select(M.SubAccount.username).where(
            M.SubAccount.status.in_(list(READY_SUB_ACCOUNT_STATUSES)))).all():
        n = donut.profile_name(u)
        if n:
            ready_names.add(n)
    out["ready_total"] = len(ready_names)

    have: set[str] = set()
    for p in profiles:
        name = str(p.get("name") or "")
        if not name.startswith(donut._PREFIX):
            continue
        have.add(name)
        out["profiles_total"] += 1
        if p.get("is_running"):
            out["running"] += 1
        st = known.get(name)
        if st is None:
            reason, cat = "orphan: no sub-account has this profile", "orphan"
        elif st[0] in _DEAD_SUB or st[1] in _DEAD_IDENT:
            reason, cat = f"finished account (sub-account {st[0]}, identity {st[1]})", "finished"
        elif st[0] in _PENDING_SUB:
            reason, cat = f"abandoned signup (sub-account {st[0]}, no pending registration)", "abandoned"
        else:
            out["matched"] += 1
            continue
        out["junk_total"] += 1
        out["junk"][cat] += 1
        if len(out["junk_sample"]) < 20:
            out["junk_sample"].append({"name": name, "reason": reason, "running": bool(p.get("is_running"))})

    missing = sorted(ready_names - have)
    out["missing_total"] = len(missing)
    out["missing_sample"] = missing[:20]
    return out

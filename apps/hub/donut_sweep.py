"""Stop Donut profiles that no live worker or registrar owns.

A Donut profile is a separate process from the worker that launched it: if the worker exits without
stopping it (a clean exit used to leave all three apply profiles of booking "Test 2032 Donut" running for
hours after they had booked), nothing else ever will. The Hub sweeps them at startup.

Conservative on purpose -- a live session killed by mistake is far worse than an idle browser left
running, so a profile is only stopped when ALL of these hold:
  * it is a `storm-*` profile (the prefix the bot gives its own; anything else is the operator's),
  * it is running and was launched more than GRACE_S ago,
  * it is not the profile of ANY sub-account of an identity a live worker is attached to (a worker
    rotates through its identity's sub-accounts, so its current one is not enough),
  * it is not the profile of a sub-account with a queued/running registration job while a registrar
    process is alive.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

import libs.db.models as M
from libs.domain.enums import RegistrationStatus
from .settings_store import get_global
from .supervisor import live_keys

logger = logging.getLogger("storm.hub.donut_sweep")

GRACE_S = 600.0
JUNK_GRACE_S = 7200.0        # a profile must have sat unused this long before it may be DELETED
MAX_JUNK_FRACTION = 0.5      # refuse a pass that would delete more than this share of the profiles
_PREFIX = "storm-"
_DEAD_SUB = {"booked", "burned", "retired"}
_DEAD_IDENT = {"booked", "retired", "archived"}
_PENDING_SUB = {"new", "registered"}


def plan_sweep(profiles: list[dict], protected: set[str], now: float,
               grace_s: float = GRACE_S) -> list[dict]:
    """Pure: the running, unowned, old-enough `storm-*` profiles from a Donut profile listing."""
    out = []
    for p in profiles:
        name = str(p.get("name") or "")
        if not name.startswith(_PREFIX) or not p.get("is_running") or name in protected:
            continue
        launched = p.get("last_launch")
        if isinstance(launched, (int, float)) and now - launched < grace_s:
            continue
        out.append(p)
    return out


def probe_identity_ids(cmdlines: list[list[str]] | None = None) -> set[str]:
    """Identity ids named by `--identity-id X` on a live probe/scripts process.

    A probe (probes/*.py, scripts/window_*) runs OUTSIDE apps.worker, so the sweep saw its Donut profile
    as unowned and stopped it ~10-30 min after launch: the 2026-10-30 manual soak died at 19.6 min and
    the 2026-11-02 exit-stability test at 19.9 min, both `Target page, context or browser has been
    closed`. A probe that names its identity owns that identity's profiles."""
    if cmdlines is None:
        cmdlines = []
        try:
            entries = os.listdir("/proc")
        except FileNotFoundError:
            entries = []
        for e in entries:
            if not e.isdigit():
                continue
            try:
                raw = Path(f"/proc/{e}/cmdline").read_bytes()
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
            cmdlines.append([x.decode(errors="replace") for x in raw.split(b"\x00") if x])
    out: set[str] = set()
    for parts in cmdlines:
        if not any(("probes/" in x or "scripts/" in x or x.startswith("scripts.")) for x in parts):
            continue
        for i, x in enumerate(parts[:-1]):
            if x == "--identity-id":
                out.add(parts[i + 1])
    return out


def protected_profile_names(db: Session, profile_name) -> set[str]:
    """Profile names a live process may be using, from the DB plus a /proc scan."""
    names: set[str] = set()
    probe_idents = probe_identity_ids()
    if probe_idents:
        for u in db.scalars(select(M.SubAccount.username).where(
                M.SubAccount.identity_id.in_(probe_idents))).all():
            names.add(profile_name(u))
    live_workers = live_keys("apps.worker")
    if live_workers:
        idents = {w.identity_id for w in db.scalars(
            select(M.Worker).where(M.Worker.id.in_(live_workers))).all()}
        if idents:
            for u in db.scalars(select(M.SubAccount.username).where(
                    M.SubAccount.identity_id.in_(idents))).all():
                names.add(profile_name(u))
    if live_keys("apps.registrar"):
        for u in db.scalars(
                select(M.SubAccount.username)
                .join(M.RegistrationJob, M.RegistrationJob.sub_account_id == M.SubAccount.id)
                .where(M.RegistrationJob.status.in_(
                    [RegistrationStatus.QUEUED, RegistrationStatus.RUNNING]))).all():
            names.add(profile_name(u))
    return names


def plan_junk(profiles: list[dict], known: dict[str, tuple[str, str]], protected: set[str],
              now: float, grace_s: float = JUNK_GRACE_S) -> list[tuple[dict, str]]:
    """Pure: (profile, reason) for every stopped `storm-*` profile that is not worth keeping.

    Every profile counts toward Donut's 24h new-profile block when it is CREATED and clutters the host
    afterwards, so a profile no future login can use should not outlive its purpose:
      * orphan            -- no sub-account has this name (its account was deleted or regenerated);
      * finished account  -- the sub-account is booked/burned/retired, or its identity is booked/retired;
      * abandoned signup  -- the account never got past new/registered and no registration job is
                             pending for it.
    `known` maps profile name -> (sub-account status, identity status). Running, recently used or
    protected profiles are never planned."""
    out = []
    for p in profiles:
        name = str(p.get("name") or "")
        if not name.startswith(_PREFIX) or p.get("is_running") or name in protected:
            continue
        launched = p.get("last_launch")
        if isinstance(launched, (int, float)) and now - launched < grace_s:
            continue
        st = known.get(name)
        if st is None:
            out.append((p, "orphan: no sub-account has this profile"))
        elif st[0] in _DEAD_SUB or st[1] in _DEAD_IDENT:
            out.append((p, f"finished account (sub-account {st[0]}, identity {st[1]})"))
        elif st[0] in _PENDING_SUB:
            out.append((p, f"abandoned signup (sub-account {st[0]}, no pending registration)"))
    return out


def known_profile_index(db: Session, profile_name) -> tuple[dict[str, tuple[str, str]], set[str]]:
    """({profile name: (sub-account status, identity status)}, names with a queued/running signup)."""
    known: dict[str, tuple[str, str]] = {}
    rows = db.execute(select(M.SubAccount.username, M.SubAccount.status, M.Identity.status)
                      .join(M.Identity, M.Identity.id == M.SubAccount.identity_id)).all()
    for username, st, ist in rows:
        n = profile_name(username)
        if n:
            known[n] = (str(getattr(st, "value", st)), str(getattr(ist, "value", ist)))
    pending: set[str] = set()
    for u in db.scalars(
            select(M.SubAccount.username)
            .join(M.RegistrationJob, M.RegistrationJob.sub_account_id == M.SubAccount.id)
            .where(M.RegistrationJob.status.in_(
                [RegistrationStatus.QUEUED, RegistrationStatus.RUNNING]))).all():
        n = profile_name(u)
        if n:
            pending.add(n)
    return known, pending


def cleanup_junk_profiles(db: Session, *, dry_run: bool = False, now: float | None = None,
                          force: bool = False, report: dict | None = None) -> list[tuple[str, str]]:
    """Delete the junk profiles (and their Donut proxy entry); returns (name, reason). Never raises.

    Fails SAFE: an empty sub-account table (wrong database, mid-restore) would make every profile an
    orphan, so nothing is deleted then, nor when a pass would delete over MAX_JUNK_FRACTION of them.

    `force` lifts ONLY the fraction guard -- for an operator who has seen the list (deleting identities
    legitimately orphans every profile they had, which the guard alone can never tell from a wrong
    database). The empty-table guard always holds. `report`, when given, receives
    {"refused": reason, "planned": [(name, reason), ...]} on a refusal, so a caller can say WHY nothing
    was deleted instead of reading the empty result as "nothing to clean"."""
    try:
        g = get_global(db)
        if str(g.get("BROWSER_BACKEND") or "chromium").strip().lower() != "donut":
            return []
        import libs.engine.vendor  # noqa: F401
        import donut
        api = donut._Api(url=g.get("DONUT_API_URL") or "", token=g.get("DONUT_API_TOKEN") or "")
        profiles = donut._items(api.call("GET", "/v1/profiles", timeout=15), "profiles")
        known, pending = known_profile_index(db, donut.profile_name)
        if not known:
            logger.warning("donut cleanup: no sub-accounts in the database -- deleting nothing")
            if report is not None:
                report["refused"] = "no sub-accounts in the database (wrong DB or mid-restore?) -- deleting nothing"
            return []
        protected = protected_profile_names(db, donut.profile_name) | pending
        victims = plan_junk(profiles, known, protected, time.time() if now is None else now)
        mine = [p for p in profiles if str(p.get("name") or "").startswith(_PREFIX)]
        if not force and mine and len(victims) > MAX_JUNK_FRACTION * len(mine):
            logger.warning("donut cleanup: %d of %d profiles look like junk -- more than %d%%, refusing "
                           "to delete anything", len(victims), len(mine), int(MAX_JUNK_FRACTION * 100))
            if report is not None:
                report["refused"] = (f"{len(victims)} of {len(mine)} profiles look like junk -- over "
                                     f"{int(MAX_JUNK_FRACTION * 100)}%, so the safety guard deleted nothing")
                report["planned"] = [(p["name"], why) for p, why in victims]
            return []
        done = []
        for p, why in victims:
            if not dry_run:
                try:
                    api.call("DELETE", f"/v1/profiles/{p['id']}", timeout=15)
                except Exception as e:
                    logger.warning("donut cleanup: could not delete %s: %s", p.get("name"), str(e)[:120])
                    continue
                if p.get("proxy_id"):
                    try:
                        api.call("DELETE", f"/v1/proxies/{p['proxy_id']}", timeout=15)
                    except Exception:
                        pass
            done.append((p["name"], why))
        if done:
            logger.info("donut cleanup: %s %d junk profile(s): %s", "would delete" if dry_run else "deleted",
                        len(done), "; ".join(f"{n} [{w}]" for n, w in done))
        return done
    except Exception:
        logger.exception("donut cleanup failed -- leaving profiles as they are")
        return []


def _launch_ts(p: dict) -> float:
    ll = p.get("last_launch")
    return float(ll) if isinstance(ll, (int, float)) else 0.0


def plan_duplicates(profiles: list[dict], names: set[str] | None = None) -> list[dict]:
    """Pure: the extra copies of every `storm-*` profile name that appears more than once (limited to
    `names` when given). One profile name = one account's device; a second copy is a second device
    that launches pick at random (`_find_id` takes the first listed).

    Keeps ONE per name: a running copy (in use) over a stopped one, then the most recently launched
    (it carries the account's history), then the first listed. A running extra is never planned --
    something is using it; it is left for a later pass."""
    groups: dict[str, list[tuple[int, dict]]] = {}
    for i, p in enumerate(profiles):
        name = str(p.get("name") or "")
        if name.startswith(_PREFIX) and (names is None or name in names):
            groups.setdefault(name, []).append((i, p))
    out = []
    for copies in groups.values():
        if len(copies) < 2:
            continue
        keep = max(copies, key=lambda ip: (bool(ip[1].get("is_running")), _launch_ts(ip[1]), -ip[0]))
        out += [p for i, p in copies if i != keep[0] and not p.get("is_running")]
    return out


def delete_profiles(api, victims: list[dict]) -> list[str]:
    """DELETE each profile and its proxy entry; returns the names actually deleted. Never raises."""
    done = []
    for p in victims:
        try:
            api.call("DELETE", f"/v1/profiles/{p['id']}", timeout=15)
        except Exception as e:
            logger.warning("donut: could not delete %s (%s): %s", p.get("name"), p.get("id"), str(e)[:120])
            continue
        if p.get("proxy_id"):
            try:
                api.call("DELETE", f"/v1/proxies/{p['proxy_id']}", timeout=15)
            except Exception:
                pass
        done.append(str(p.get("name")))
    return done


def cleanup_duplicate_profiles(db: Session, *, dry_run: bool = False) -> list[str]:
    """Delete extra copies of a duplicated profile name (see plan_duplicates). Returns their names
    (one entry per deleted copy). No fraction guard: a duplicate is redundant by definition -- the
    kept copy is the same account's device -- so removing it can never strand an account. Never raises."""
    try:
        g = get_global(db)
        if str(g.get("BROWSER_BACKEND") or "chromium").strip().lower() != "donut":
            return []
        import libs.engine.vendor  # noqa: F401
        import donut
        api = donut._Api(url=g.get("DONUT_API_URL") or "", token=g.get("DONUT_API_TOKEN") or "")
        victims = plan_duplicates(donut._items(api.call("GET", "/v1/profiles", timeout=15), "profiles"))
        if not victims:
            return []
        done = [str(p.get("name")) for p in victims] if dry_run else delete_profiles(api, victims)
        logger.info("donut cleanup: %s %d duplicate profile cop(ies): %s",
                    "would delete" if dry_run else "deleted", len(done), ", ".join(done))
        return done
    except Exception:
        logger.exception("donut duplicate cleanup failed -- leaving profiles")
        return []


def plan_stray_proxies(proxies: list[dict], profiles: list[dict]) -> list[dict]:
    """Pure: `storm-*` Donut proxy entries no profile points at. Entries the bot did not name are the
    operator's and are never planned."""
    used = {p.get("proxy_id") for p in profiles if p.get("proxy_id")}
    return [x for x in proxies if str(x.get("name") or "").startswith(_PREFIX) and x.get("id") not in used]


def cleanup_stray_proxies(db: Session, *, dry_run: bool = False, settle_s: float = 15.0) -> list[str]:
    """Delete `storm-*` proxy entries no profile uses. A launch creates the proxy entry a moment before
    its profile, so an entry is only deleted if it is STILL unreferenced after `settle_s` seconds.
    Never raises."""
    try:
        g = get_global(db)
        if str(g.get("BROWSER_BACKEND") or "chromium").strip().lower() != "donut":
            return []
        import libs.engine.vendor  # noqa: F401
        import donut
        api = donut._Api(url=g.get("DONUT_API_URL") or "", token=g.get("DONUT_API_TOKEN") or "")

        def _lists():
            return (donut._items(api.call("GET", "/v1/proxies", timeout=15), "proxies"),
                    donut._items(api.call("GET", "/v1/profiles", timeout=15), "profiles"))
        first = {x["id"] for x in plan_stray_proxies(*_lists())}
        if not first:
            return []
        time.sleep(settle_s)
        proxies, profiles = _lists()
        victims = [x for x in plan_stray_proxies(proxies, profiles) if x["id"] in first]
        for x in victims:
            if not dry_run:
                try:
                    api.call("DELETE", f"/v1/proxies/{x['id']}", timeout=15)
                except Exception as e:
                    logger.warning("donut cleanup: could not delete proxy %s: %s", x.get("name"), str(e)[:100])
        if victims:
            logger.info("donut cleanup: %s %d stray proxy entr(ies)",
                        "would delete" if dry_run else "deleted", len(victims))
        return [x["name"] for x in victims]
    except Exception:
        logger.exception("donut proxy cleanup failed -- leaving them")
        return []


def sweep_donut_profiles(db: Session, *, dry_run: bool = False, now: float | None = None) -> list[str]:
    """Stop the unowned running profiles; returns their names. Never raises: it runs at boot."""
    try:
        g = get_global(db)
        if str(g.get("BROWSER_BACKEND") or "chromium").strip().lower() != "donut":
            return []
        import libs.engine.vendor  # noqa: F401  (puts the vendored modules on sys.path)
        import donut
        api = donut._Api(url=g.get("DONUT_API_URL") or "", token=g.get("DONUT_API_TOKEN") or "")
        profiles = donut._items(api.call("GET", "/v1/profiles", timeout=15), "profiles")
        victims = plan_sweep(profiles, protected_profile_names(db, donut.profile_name),
                             time.time() if now is None else now)
        stopped = []
        for p in victims:
            if not dry_run:
                api.call("POST", f"/v1/profiles/{p['id']}/kill", timeout=15)
            stopped.append(p["name"])
        if stopped:
            logger.info("donut sweep: %s %d orphaned profile(s): %s",
                        "would stop" if dry_run else "stopped", len(stopped), ", ".join(stopped))
        return stopped
    except Exception:
        logger.exception("donut sweep failed -- leaving profiles as they are")
        return []


if __name__ == "__main__":
    import sys
    from .db import SessionLocal
    logging.basicConfig(level=logging.INFO)
    with SessionLocal() as _db:
        names = sweep_donut_profiles(_db, dry_run="--dry-run" in sys.argv)
        junk = cleanup_junk_profiles(_db, dry_run="--dry-run" in sys.argv)
        strays = cleanup_stray_proxies(_db, dry_run="--dry-run" in sys.argv)
    print(("would stop: " if "--dry-run" in sys.argv else "stopped: ") + (", ".join(names) or "nothing"))
    print(("would delete: " if "--dry-run" in sys.argv else "deleted: ")
          + ("; ".join(f"{n} [{w}]" for n, w in junk) or "nothing"))
    print(("would delete proxies: " if "--dry-run" in sys.argv else "deleted proxies: ") + str(len(strays)))

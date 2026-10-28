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
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

import libs.db.models as M
from libs.domain.enums import RegistrationStatus
from .settings_store import get_global
from .supervisor import live_keys

logger = logging.getLogger("storm.hub.donut_sweep")

GRACE_S = 600.0
_PREFIX = "storm-"


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


def protected_profile_names(db: Session, profile_name) -> set[str]:
    """Profile names a live process may be using, from the DB plus a /proc scan."""
    names: set[str] = set()
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
    print(("would stop: " if "--dry-run" in sys.argv else "stopped: ") + (", ".join(names) or "nothing"))

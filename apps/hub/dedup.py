"""Identity de-duplication on the passport key — shared by import and manual-add.

The rule (operator, 2026-10-02), applied wherever an identity ENTERS the system:

  * KEY     = normalized passport_no (UPPER + strip). A passport identifies a person.
  * fresh   (passport never seen)                      -> CREATE a new row at HIGH priority.
  * exists, and the best existing row is already HIGH
    AND has a READY (verified/login_ok/held) sub-acct  -> IGNORE the incoming copy entirely:
                                                          keep the established row, add NOTHING,
                                                          never wipe it.
  * exists, any other case (not HIGH, or not verified) -> PROMOTE that row to HIGH and REUSE it;
                                                          merge only genuinely-new sub-accounts.
                                                          NEVER create a second row for the passport.

This replaces the old "supersede" behaviour, which demoted the old rows to LOW but still created a
brand-new HIGH row every time — so re-importing the same file multiplied the rows and buried the
verified originals under empty duplicates (the exact bug this fixes).
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from libs.db import models as M
from libs.domain import priority as P
from libs.domain.enums import READY_SUB_ACCOUNT_STATUSES

# decision tokens
CREATE = "create"
PROMOTE = "promote"
IGNORE = "ignore"


def norm_passport(p: str | None) -> str:
    return (p or "").strip().upper()


def ready_counts(db: Session) -> dict[str, int]:
    """identity_id -> number of READY (verified/login_ok/held) sub-accounts, in one query."""
    counts: dict[str, int] = defaultdict(int)
    for iid, status in db.execute(
            select(M.SubAccount.identity_id, M.SubAccount.status)).all():
        if status in READY_SUB_ACCOUNT_STATUSES:
            counts[iid] += 1
    return counts


def index_by_passport(db: Session) -> dict[str, list[M.Identity]]:
    """Normalized passport -> the identity rows already carrying it (never a bare flag: the same
    passport can legitimately have several rows until this dedup has run against the DB)."""
    idx: dict[str, list[M.Identity]] = defaultdict(list)
    for row in db.scalars(select(M.Identity).where(M.Identity.passport_no.isnot(None))).all():
        key = norm_passport(row.passport_no)
        if key:
            idx[key].append(row)
    return idx


def pick_best(rows: list[M.Identity], ready: dict[str, int]) -> M.Identity | None:
    """The row a duplicate passport should collapse onto: most READY sub-accounts, then highest
    priority, then OLDEST (the most established row)."""
    if not rows:
        return None
    return max(rows, key=lambda r: (
        ready.get(r.id, 0),
        r.priority,
        -(r.created_at.timestamp() if r.created_at else 0.0),   # oldest wins the tie
    ))


def decide(existing: list[M.Identity], ready: dict[str, int]) -> tuple[str, M.Identity | None]:
    """Return (action, target). target is the existing row to reuse for PROMOTE/IGNORE, else None."""
    best = pick_best(existing, ready)
    if best is None:
        return CREATE, None
    verified = ready.get(best.id, 0) > 0
    if verified and best.priority >= P.HIGH:
        return IGNORE, best
    return PROMOTE, best

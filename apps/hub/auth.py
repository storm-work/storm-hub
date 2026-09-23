"""Authentication + the View/Operate session model (spec §12c / R2-C7).

- Passwords: PBKDF2-HMAC-SHA256 (stdlib, no native dep), stored as `pbkdf2_sha256$iters$salt$hash`.
- Tokens: JWT (HS256) carrying the user id + the user_session id.
- Sessions: on login the user picks View or Operate. Operate requires admin/operator role and there
  can be at most ONE active operate session host-wide (DB partial unique index) — a second operate
  login is created PAUSED (the seat is taken) and must request handover. Mutating endpoints require a
  live active operate session.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from libs.db import models as M
from libs.domain.enums import OPERATE_ROLES, SessionPurpose, SessionStatus, UserRole

from .config import settings
from .db import get_db

_PBKDF2_ITERS = 240_000


# ── Password hashing ──────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITERS)
    return f"pbkdf2_sha256${_PBKDF2_ITERS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, digest = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters))
        return secrets.compare_digest(dk.hex(), digest)
    except Exception:
        return False


# ── JWT ───────────────────────────────────────────────────────────────────────
def make_token(user_id: str, session_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "sid": session_id,
        "iat": now,
        "exp": now + timedelta(hours=settings.jwt_ttl_hours),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def _decode(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "invalid or expired token")


def try_decode(token: str) -> dict | None:
    """Best-effort decode for non-request-blocking callers (e.g. audit logging middleware) —
    never raises, returns None on any invalid/expired/missing token."""
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


# ── Current-principal dependencies ────────────────────────────────────────────
class Principal:
    def __init__(self, user: M.User, session: M.UserSession):
        self.user = user
        self.session = session

    @property
    def is_operating(self) -> bool:
        return (
            self.session.purpose == SessionPurpose.OPERATE
            and self.session.status == SessionStatus.ACTIVE
        )


_bearer = HTTPBearer(auto_error=False, description="Paste the token from /auth/login")


def principal_from_token(token: str, db: Session) -> Principal:
    """Resolve a raw token WITHOUT FastAPI's dependency machinery.

    Exists for the websocket at /ws/events: a browser's WebSocket constructor cannot set an
    Authorization header, so the token arrives as a query parameter and has to be validated by hand
    before the socket is accepted. Same checks as current_principal, which now delegates here so the
    two paths cannot drift apart.
    """
    data = _decode(token)
    user = db.get(M.User, data.get("sub"))
    session = db.get(M.UserSession, data.get("sid"))
    if not user or not session or session.status == SessionStatus.ENDED:
        raise HTTPException(401, "session ended or not found")
    return Principal(user, session)


def current_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> Principal:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(401, "missing bearer token")
    return principal_from_token(creds.credentials, db)


def require_operator(principal: Principal = Depends(current_principal)) -> Principal:
    """Guard for mutating endpoints — caller must hold the live active operate seat."""
    if not principal.is_operating:
        raise HTTPException(
            423, "operate seat required (log in with purpose=operate; the seat may be held by another user)"
        )
    return principal


def require_viewer(principal: Principal = Depends(current_principal)) -> Principal:
    """Guard for READ endpoints — any authenticated principal, operate seat or not.

    This is the half of the permission model that was specified and then never written. The VIEWER
    role has existed in libs/domain/enums.py and the login form has offered "View (read-only)" as a
    purpose from the start, but no guard ever backed either of them, so every read route was left
    with a bare `Depends(get_db)` and answered anonymous callers. GET /identities served full_name,
    birthdate and passport_no of every applicant to anyone who could reach the port.

    Deliberately does NOT require the operate seat: reading is not an operation on the fleet, and
    contending for the single seat to look at a list would make the viewer role useless — which is
    the same reasoning require_admin already applies.
    """
    return principal


def require_admin(principal: Principal = Depends(current_principal)) -> Principal:
    """Guard for admin-only surfaces. Deliberately does NOT require the operate seat: reading
    credentials is not an operation on the fleet, and making it contend for the single seat would
    push an admin into taking control just to export a list."""
    from libs.domain.enums import UserRole
    if principal.user.role != UserRole.ADMIN:
        raise HTTPException(403, "admin role required")
    return principal


# ── Session lifecycle helpers ─────────────────────────────────────────────────
def active_operator(db: Session) -> M.UserSession | None:
    return db.scalars(
        select(M.UserSession).where(
            M.UserSession.purpose == SessionPurpose.OPERATE,
            M.UserSession.status == SessionStatus.ACTIVE,
        )
    ).first()


def open_session(db: Session, user: M.User, purpose: SessionPurpose) -> tuple[M.UserSession, str | None]:
    """Create a session for a login. Returns (session, notice).

    View: always active, UNLESS the user is a VIEWER and already has an active VIEW session --
    capped at 1 (per-user), created PAUSED with a notice past that; see /sessions/extra-view/*
    for the request-an-extra-session flow. Operate: requires an operate-capable role; if the seat
    is taken it's created PAUSED (notice names the current holder). The DB partial unique index is
    the final backstop for operate (host-wide); the viewer cap has no DB-level backstop since it
    must be exceedable by exactly one on admin grant.
    """
    now = datetime.now(timezone.utc)
    notice = None
    if purpose == SessionPurpose.OPERATE:
        if user.role not in OPERATE_ROLES:
            raise HTTPException(403, "operate requires admin or operator role")
        holder = active_operator(db)
        status = SessionStatus.ACTIVE
        if holder is not None and holder.user_id != user.id:
            status = SessionStatus.PAUSED
            notice = f"operate seat currently held by another user (session {holder.id}); logged in PAUSED — request handover"
        elif holder is not None and holder.user_id == user.id:
            # same user already operating — reuse the notion by staying active (index allows only one;
            # create paused to avoid collision, caller can reuse existing)
            status = SessionStatus.PAUSED
            notice = "you already hold an active operate session elsewhere"
    elif user.role == UserRole.VIEWER:
        # A viewer is capped at 1 active VIEW session (operator instruction, 2026-09-10) -- unlike
        # the operate seat this is PER-USER, not host-wide, and unlike operate it's meant to be
        # exceedable by exactly one on admin grant (see ExtraSessionRequest/POST
        # /sessions/extra-view/*), which a static partial-unique index can't express (a granted
        # extra session must coexist as a SECOND active row, not replace the first) -- so this is
        # enforced here in Python, not via a DB constraint the way the operate seat is.
        existing = db.scalars(select(M.UserSession).where(
            M.UserSession.user_id == user.id,
            M.UserSession.purpose == SessionPurpose.VIEW,
            M.UserSession.status == SessionStatus.ACTIVE,
        )).first()
        if existing is not None:
            status = SessionStatus.PAUSED
            notice = "you already have an active session — request an extra session from an admin"
        else:
            status = SessionStatus.ACTIVE
    else:
        # admin/operator choosing to log in with purpose=view (e.g. to see the read-only
        # dashboard): unrestricted, unchanged -- the cap above is scoped to VIEWER specifically.
        status = SessionStatus.ACTIVE

    sess = M.UserSession(user_id=user.id, purpose=purpose, status=status, started_at=now, last_seen=now)
    db.add(sess)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "operate seat just taken — retry as paused / request handover")
    return sess, notice

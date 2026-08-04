"""storm-hub Manager Hub — FastAPI application (Phase 3, first launchable slice).

DB-backed endpoints that exercise the domain layer over HTTP: settings catalog, identities,
bookings, and attach-identity → materialize-worker (enforcing the role rules + C2 identity
exclusivity). Redis/worker-control and WS log streaming are added later in Phase 3.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from libs.db import models as M
from libs.domain.enums import BOOKING_ACTIVE_STATUSES, SessionPurpose, SessionStatus, UserRole
from libs.domain.enums import SettingsLayer as L
from libs.domain.rules import RuleViolation, resolve_worker_role
from libs.domain.settings import CATALOG, settings_for_layer, validate_layer

from libs.domain.enums import HandoverStatus

from .auth import (
    Principal,
    active_operator,
    current_principal,
    hash_password,
    make_token,
    open_session,
    require_operator,
    verify_password,
)
from .db import engine, get_db
from .schemas import (
    AttachIdentity,
    BookingCreate,
    BookingOut,
    BootstrapAdmin,
    IdentityCreate,
    IdentityOut,
    LoginRequest,
    LoginResponse,
    UserCreate,
    UserOut,
    WhoAmI,
    WorkerOut,
)

app = FastAPI(title="storm-hub", version="0.1.0")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Health & meta ─────────────────────────────────────────────────────────────
@app.get("/health")
def health() -> dict:
    try:
        with engine.connect() as c:
            n = c.execute(
                text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
            ).scalar()
        return {"status": "ok", "db": "ok", "tables": int(n or 0)}
    except Exception as e:  # pragma: no cover - infra failure path
        return {"status": "degraded", "db": f"error: {e.__class__.__name__}"}


@app.get("/settings/catalog")
def settings_catalog() -> dict:
    """The settings taxonomy (§6), grouped by home layer — powers the dashboard settings editors."""
    out: dict[str, list[dict]] = {}
    for layer in (L.GLOBAL, L.BOOKING, L.TEMPLATE):
        out[str(layer)] = [
            {
                "key": s.key,
                "type": s.type.__name__,
                "default": s.default,
                "group": s.group,
                "override_layers": sorted(str(x) for x in s.override_layers),
                "note": s.note,
            }
            for s in settings_for_layer(layer)
        ]
    out["_meta"] = {"total": len(CATALOG), "precedence": "worker > template > booking > global"}
    return out


# ── Auth & sessions (§12c / R2-C7) ────────────────────────────────────────────
@app.post("/auth/bootstrap", response_model=UserOut, status_code=201)
def bootstrap_admin(body: BootstrapAdmin, db: Session = Depends(get_db)) -> M.User:
    """Create the FIRST admin — only works while the users table is empty (headless bootstrap)."""
    if db.scalar(select(func.count()).select_from(M.User)):
        raise HTTPException(409, "already bootstrapped; ask an admin to create users")
    user = M.User(username=body.username, password_hash=hash_password(body.password), role=UserRole.ADMIN)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@app.post("/auth/users", response_model=UserOut, status_code=201)
def create_user(
    body: UserCreate, db: Session = Depends(get_db), me: Principal = Depends(current_principal)
) -> M.User:
    if me.user.role != UserRole.ADMIN:
        raise HTTPException(403, "only admin can create users")
    if db.scalar(select(M.User).where(M.User.username == body.username)):
        raise HTTPException(409, "username taken")
    user = M.User(username=body.username, password_hash=hash_password(body.password), role=body.role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@app.post("/auth/login", response_model=LoginResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    user = db.scalar(select(M.User).where(M.User.username == body.username))
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "bad credentials")
    sess, notice = open_session(db, user, body.purpose)
    user.last_login = _now()
    db.commit()
    db.refresh(sess)
    return LoginResponse(
        token=make_token(user.id, sess.id),
        user=UserOut.model_validate(user),
        purpose=sess.purpose,
        session_status=str(sess.status),
        notice=notice,
    )


@app.post("/auth/logout")
def logout(db: Session = Depends(get_db), me: Principal = Depends(current_principal)) -> dict:
    me.session.status = SessionStatus.ENDED
    db.commit()
    return {"status": "logged_out"}


@app.get("/auth/whoami", response_model=WhoAmI)
def whoami(me: Principal = Depends(current_principal)) -> WhoAmI:
    return WhoAmI(
        username=me.user.username,
        role=me.user.role,
        purpose=me.session.purpose,
        session_status=str(me.session.status),
        is_operating=me.is_operating,
    )


@app.get("/sessions")
def list_active_sessions(db: Session = Depends(get_db), me: Principal = Depends(current_principal)) -> list[dict]:
    rows = db.scalars(
        select(M.UserSession).where(M.UserSession.status != SessionStatus.ENDED)
    ).all()
    out = []
    for s in rows:
        u = db.get(M.User, s.user_id)
        out.append({"session_id": s.id, "user": u.username if u else None,
                    "purpose": str(s.purpose), "status": str(s.status)})
    return out


# ── Operate-seat handover (§12c) ──────────────────────────────────────────────
@app.post("/sessions/handover/request")
def request_handover(db: Session = Depends(get_db), me: Principal = Depends(current_principal)) -> dict:
    """A paused Operate session asks for the seat. If the seat is free it's granted immediately;
    otherwise a pending request is created for the current holder to grant/deny."""
    if me.session.purpose != SessionPurpose.OPERATE:
        raise HTTPException(400, "log in with purpose=operate before requesting the seat")
    if me.is_operating:
        raise HTTPException(400, "you already hold the operate seat")
    holder = active_operator(db)
    if holder is None:
        me.session.status = SessionStatus.ACTIVE
        db.commit()
        return {"status": "granted", "note": "seat was free"}
    req = M.OpsHandoverRequest(operate_session_id=holder.id, requester_user_id=me.user.id)
    db.add(req)
    db.commit()
    db.refresh(req)
    holder_user = db.get(M.User, holder.user_id)
    return {"status": "pending", "request_id": req.id,
            "current_holder": holder_user.username if holder_user else None}


@app.post("/sessions/handover/{req_id}/grant")
def grant_handover(req_id: str, db: Session = Depends(get_db),
                   op: Principal = Depends(require_operator)) -> dict:
    req = db.get(M.OpsHandoverRequest, req_id)
    if not req:
        raise HTTPException(404, "handover request not found")
    if req.operate_session_id != op.session.id:
        raise HTTPException(403, "only the current holder can grant this request")
    if req.status != HandoverStatus.PENDING:
        raise HTTPException(409, f"request already {req.status}")
    # End my seat first, then promote the requester's paused operate session (keeps the single-active index happy).
    op.session.status = SessionStatus.ENDED
    db.flush()
    reqsess = db.scalars(
        select(M.UserSession).where(
            M.UserSession.user_id == req.requester_user_id,
            M.UserSession.purpose == SessionPurpose.OPERATE,
            M.UserSession.status == SessionStatus.PAUSED,
        ).order_by(M.UserSession.started_at.desc())
    ).first()
    if reqsess:
        reqsess.status = SessionStatus.ACTIVE
    req.status = HandoverStatus.GRANTED
    db.commit()
    return {"status": "granted", "promoted_session": reqsess.id if reqsess else None}


@app.post("/sessions/handover/{req_id}/deny")
def deny_handover(req_id: str, db: Session = Depends(get_db),
                  op: Principal = Depends(require_operator)) -> dict:
    req = db.get(M.OpsHandoverRequest, req_id)
    if not req:
        raise HTTPException(404, "handover request not found")
    if req.operate_session_id != op.session.id:
        raise HTTPException(403, "only the current holder can deny this request")
    req.status = HandoverStatus.DENIED
    db.commit()
    return {"status": "denied"}


@app.post("/sessions/{sid}/force-release")
def force_release(sid: str, db: Session = Depends(get_db),
                  me: Principal = Depends(current_principal)) -> dict:
    """Admin frees a stale operate seat."""
    if me.user.role != UserRole.ADMIN:
        raise HTTPException(403, "only admin can force-release")
    s = db.get(M.UserSession, sid)
    if not s:
        raise HTTPException(404, "session not found")
    s.status = SessionStatus.ENDED
    db.commit()
    return {"status": "released", "session_id": sid}


# ── Identities ────────────────────────────────────────────────────────────────
@app.post("/identities", response_model=IdentityOut, status_code=201)
def create_identity(
    body: IdentityCreate, db: Session = Depends(get_db), _op: Principal = Depends(require_operator)
) -> M.Identity:
    ident = M.Identity(
        full_name=body.full_name,
        type=body.type,
        birthdate=body.birthdate,
        passport_no=body.passport_no,
        gender=body.gender,
        nationality=body.nationality,
    )
    db.add(ident)
    db.commit()
    db.refresh(ident)
    return ident


@app.get("/identities", response_model=list[IdentityOut])
def list_identities(db: Session = Depends(get_db)) -> list[M.Identity]:
    return list(db.scalars(select(M.Identity).order_by(M.Identity.created_at.desc())).all())


# ── Bookings ──────────────────────────────────────────────────────────────────
@app.post("/bookings", response_model=BookingOut, status_code=201)
def create_booking(
    body: BookingCreate, db: Session = Depends(get_db), _op: Principal = Depends(require_operator)
) -> M.Booking:
    # Validate the booking-layer settings against the catalog (no unknown/wrong-layer keys).
    try:
        clean = validate_layer(L.BOOKING, body.settings)
    except Exception as e:
        raise HTTPException(400, f"invalid booking settings: {e}")
    bk = M.Booking(
        name=body.name,
        type=body.type,
        posto_id=body.posto_id,
        nationality=body.nationality,
        residence=body.residence,
        settings_json=clean,
    )
    db.add(bk)
    db.commit()
    db.refresh(bk)
    return bk


@app.get("/bookings", response_model=list[BookingOut])
def list_bookings(db: Session = Depends(get_db)) -> list[M.Booking]:
    return list(db.scalars(select(M.Booking).order_by(M.Booking.created_at.desc())).all())


@app.get("/bookings/{booking_id}", response_model=BookingOut)
def get_booking(booking_id: str, db: Session = Depends(get_db)) -> M.Booking:
    bk = db.get(M.Booking, booking_id)
    if not bk:
        raise HTTPException(404, "booking not found")
    return bk


# ── Attach identity → materialize worker (rules + C2 exclusivity) ─────────────
@app.post("/bookings/{booking_id}/identities", response_model=WorkerOut, status_code=201)
def attach_identity(
    booking_id: str, body: AttachIdentity, db: Session = Depends(get_db),
    _op: Principal = Depends(require_operator),
) -> M.Worker:
    bk = db.get(M.Booking, booking_id)
    if not bk:
        raise HTTPException(404, "booking not found")
    ident = db.get(M.Identity, body.identity_id)
    if not ident:
        raise HTTPException(404, "identity not found")

    # Role rule (spec §4): test booking assigns role; real booking derives it.
    try:
        role = resolve_worker_role(bk.type, ident.type, body.assigned_role)
    except RuleViolation as e:
        raise HTTPException(400, str(e))

    # C2 exclusivity: block if the identity is already allocated to another ACTIVE booking.
    other_active = db.scalars(
        select(M.Booking.status)
        .join(M.IdentityAllocation, M.IdentityAllocation.booking_id == M.Booking.id)
        .where(
            M.IdentityAllocation.identity_id == ident.id,
            M.IdentityAllocation.released_at.is_(None),
            M.IdentityAllocation.booking_id != bk.id,
        )
    ).all()
    if any(st in BOOKING_ACTIVE_STATUSES for st in other_active):
        raise HTTPException(409, "identity already allocated to another active booking")

    alloc = M.IdentityAllocation(identity_id=ident.id, booking_id=bk.id, allocated_at=_now())
    db.add(alloc)
    db.add(M.BookingIdentity(booking_id=bk.id, identity_id=ident.id, assigned_role=body.assigned_role))
    worker = M.Worker(booking_id=bk.id, identity_id=ident.id, role=role)
    db.add(worker)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "identity already attached / allocated (concurrent)")
    db.refresh(worker)
    return worker


@app.get("/bookings/{booking_id}/workers", response_model=list[WorkerOut])
def list_workers(booking_id: str, db: Session = Depends(get_db)) -> list[M.Worker]:
    return list(db.scalars(select(M.Worker).where(M.Worker.booking_id == booking_id)).all())

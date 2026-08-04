"""storm-hub Manager Hub — FastAPI application (Phase 3, first launchable slice).

DB-backed endpoints that exercise the domain layer over HTTP: settings catalog, identities,
bookings, and attach-identity → materialize-worker (enforcing the role rules + C2 identity
exclusivity). Redis/worker-control and WS log streaming are added later in Phase 3.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
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
from libs.bus import Bus, connect
from libs.domain.enums import WorkerStatus, WorkerSubstate
from libs.domain.settings import resolve

from .config import settings
from .db import engine, get_db
from .events import EventHub
from .importers import parse_identities_csv, parse_proxies
from .settings_store import get_global, set_layer
from .supervisor import Supervisor
from .schemas import (
    AttachIdentity,
    BookingCreate,
    BookingOut,
    BootstrapAdmin,
    GlobalSettingsPut,
    IdentityCreate,
    IdentityImport,
    IdentityOut,
    ImportResult,
    LoginRequest,
    LoginResponse,
    ProxyCreate,
    ProxyImport,
    ProxyOut,
    UserCreate,
    UserOut,
    WhoAmI,
    WorkerOut,
)

supervisor = Supervisor(settings.redis_url)
event_hub = EventHub(settings.redis_url)
_bus = Bus(connect(settings.redis_url))


@asynccontextmanager
async def lifespan(app: FastAPI):
    event_hub.start()          # background thread tailing the evt stream -> DB + WS
    yield
    event_hub.stop()
    supervisor.shutdown_all()


app = FastAPI(title="storm-hub", version="0.1.0", lifespan=lifespan)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Health & meta ─────────────────────────────────────────────────────────────
@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    from sqlalchemy import inspect as _inspect
    try:
        db.execute(text("SELECT 1"))
        n = len(_inspect(db.get_bind()).get_table_names())
        return {"status": "ok", "db": "ok", "tables": n}
    except Exception as e:  # infra failure path (e.g. Postgres down)
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


@app.post("/identities/import", response_model=ImportResult)
def import_identities(body: IdentityImport, db: Session = Depends(get_db),
                      _op: Principal = Depends(require_operator)) -> ImportResult:
    parsed, errors = parse_identities_csv(body.csv, default_type=str(body.default_type))
    created = subs = 0
    existing = {u for (u,) in db.execute(select(M.SubAccount.username)).all()}
    for pi in parsed:
        ident = M.Identity(full_name=pi.full_name, type=pi.type, birthdate=pi.birthdate,
                           passport_no=pi.passport_no, gender=pi.gender, nationality=pi.nationality,
                           status="active" if pi.sub_accounts else "empty")
        db.add(ident)
        db.flush()
        created += 1
        for sa in pi.sub_accounts:
            if sa.username in existing:
                errors.append(f"skip duplicate username {sa.username}")
                continue
            db.add(M.SubAccount(identity_id=ident.id, username=sa.username, password=sa.password,
                                email=sa.email, email_pass=sa.email_pass, status=sa.status))
            existing.add(sa.username)
            subs += 1
    db.commit()
    return ImportResult(created=created, sub_accounts=subs, errors=errors)


# ── Proxy pool (DB-backed) ────────────────────────────────────────────────────
@app.get("/proxies", response_model=list[ProxyOut])
def list_proxies(db: Session = Depends(get_db)) -> list[M.Proxy]:
    return list(db.scalars(select(M.Proxy).order_by(M.Proxy.created_at.desc())).all())


@app.post("/proxies", response_model=ProxyOut, status_code=201)
def create_proxy(body: ProxyCreate, db: Session = Depends(get_db),
                 _op: Principal = Depends(require_operator)) -> M.Proxy:
    p = M.Proxy(provider=body.provider, kind=body.kind, endpoint=body.endpoint,
                credentials_ref=body.credentials_ref, max_sessions=body.max_sessions, active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@app.post("/proxies/import", response_model=ImportResult)
def import_proxies(body: ProxyImport, db: Session = Depends(get_db),
                   _op: Principal = Depends(require_operator)) -> ImportResult:
    parsed, errors = parse_proxies(body.text)
    existing = {e for (e,) in db.execute(select(M.Proxy.endpoint)).all()}
    created = 0
    for pp in parsed:
        if pp.endpoint in existing:
            errors.append(f"skip duplicate endpoint {pp.endpoint}")
            continue
        db.add(M.Proxy(provider=body.provider, kind=body.kind, endpoint=pp.endpoint,
                       credentials_ref=pp.credentials_ref, max_sessions=body.max_sessions, active=True))
        existing.add(pp.endpoint)
        created += 1
    db.commit()
    return ImportResult(created=created, errors=errors)


# ── Global settings (capsolver keys, browser/solver defaults) ─────────────────
_SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASS")


@app.get("/settings/global")
def read_global_settings(db: Session = Depends(get_db),
                         me: Principal = Depends(current_principal)) -> dict:
    """Return stored global settings; secret-looking values are masked."""
    out = {}
    for k, v in get_global(db).items():
        out[k] = "•••set" if v and any(h in k.upper() for h in _SECRET_HINTS) else v
    return out


@app.put("/settings/global")
def write_global_settings(body: GlobalSettingsPut, db: Session = Depends(get_db),
                          _op: Principal = Depends(require_operator)) -> dict:
    try:
        stored = set_layer(db, L.GLOBAL, body.values)
    except Exception as e:
        raise HTTPException(400, f"invalid global settings: {e}")
    return {"saved": sorted(body.values.keys()), "total_stored": len(stored)}


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


# ── Worker control (supervisor + bus) ─────────────────────────────────────────
@app.post("/workers/{worker_id}/launch", response_model=WorkerOut)
def launch_worker(worker_id: str, db: Session = Depends(get_db),
                  _op: Principal = Depends(require_operator)) -> M.Worker:
    w = db.get(M.Worker, worker_id)
    if not w:
        raise HTTPException(404, "worker not found")
    bk = db.get(M.Booking, w.booking_id)
    tpl = db.get(M.WorkerTemplate, w.template_id) if w.template_id else None
    # Resolve + FREEZE the effective settings for this launch (spec §6): global (DB) < booking < template.
    layers = {L.GLOBAL: get_global(db), L.BOOKING: (bk.settings_json or {})}
    if tpl:
        layers[L.TEMPLATE] = (tpl.settings_json or {})
    eff = resolve(layers)
    eff["ROLE"] = str(w.role)                     # role comes from the worker row, not settings
    w.effective_settings_json = eff
    w.status = WorkerStatus.RUNNING
    w.substate = WorkerSubstate.IDLE
    db.commit()
    db.refresh(w)
    # spawn the process, then send the launch command (stream persists — order-independent)
    supervisor.spawn(worker_id)
    _bus.send_command(worker_id, "launch", cfg=eff)
    return w


@app.post("/workers/{worker_id}/stop", response_model=WorkerOut)
def stop_worker(worker_id: str, db: Session = Depends(get_db),
                _op: Principal = Depends(require_operator)) -> M.Worker:
    w = db.get(M.Worker, worker_id)
    if not w:
        raise HTTPException(404, "worker not found")
    _bus.send_command(worker_id, "stop")
    supervisor.stop(worker_id)
    w.status = WorkerStatus.STOPPED
    db.commit()
    db.refresh(w)
    return w


@app.websocket("/ws/events")
async def ws_events(ws: WebSocket) -> None:
    """Live worker status/log stream for the dashboard (fanned from the evt Redis stream)."""
    await ws.accept()
    q = event_hub.subscribe()
    try:
        while True:
            item = await q.get()
            await ws.send_json(item)
    except WebSocketDisconnect:
        pass
    finally:
        event_hub.unsubscribe(q)


# ── Dashboard (SPA) — served by the Hub on the same origin/port (headless-server friendly) ────
# Registered LAST so every API route above wins; the SPA fallback serves index.html for client
# routes. Only active once the dashboard is built (apps/dashboard/dist).
_DIST = Path(__file__).resolve().parents[1] / "dashboard" / "dist"
if _DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def _spa_root() -> FileResponse:
        return FileResponse(_DIST / "index.html")

    @app.get("/{full_path:path}", include_in_schema=False)
    def _spa_fallback(full_path: str) -> FileResponse:
        # unknown non-API GET path -> let the SPA router handle it
        candidate = _DIST / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_DIST / "index.html")

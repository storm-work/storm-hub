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
from libs.domain.enums import (
    BOOKING_ACTIVE_STATUSES,
    BookingStatus,
    SessionPurpose,
    SessionStatus,
    UserRole,
)
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
from .config_store import get_config, seed_defaults, seed_templates, set_config
from .db import SessionLocal, engine, get_db
from .events import EventHub
from .genaccounts import gen_fake_identities, gen_subaccounts
from .importers import parse_identities, parse_proxies
from .settings_store import get_global, set_layer
from .supervisor import Supervisor
from .schemas import (
    AttachIdentity,
    BookingCreate,
    BookingOut,
    BookingUpdate,
    BootstrapAdmin,
    GenSubAccounts,
    GenTestIdentities,
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
    TemplateCreate,
    TemplateOut,
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
    try:
        db = SessionLocal()
        seed_defaults(db)      # form_defaults / quest_steps into the DB if absent
        seed_templates(db)     # default scout + apply worker templates
        db.close()
    except Exception:
        pass                   # DB may be down at boot; endpoints still report degraded
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
    parsed, errors = parse_identities(body.csv, default_type=str(body.default_type))
    created = subs = 0
    existing = {u for (u,) in db.execute(select(M.SubAccount.username)).all()}
    known_passports = {p for (p,) in db.execute(
        select(M.Identity.passport_no).where(M.Identity.passport_no.isnot(None))).all()}
    for pi in parsed:
        if pi.passport_no and pi.passport_no in known_passports:
            errors.append(f"skip duplicate passport {pi.passport_no}")
            continue
        ident = M.Identity(full_name=pi.full_name, type=pi.type, birthdate=pi.birthdate,
                           passport_no=pi.passport_no, gender=pi.gender, nationality=pi.nationality,
                           status="active" if pi.sub_accounts else "empty")
        if pi.passport_no:
            known_passports.add(pi.passport_no)
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


# ── Sub-account generation ────────────────────────────────────────────────────
@app.post("/identities/gen-subaccounts", response_model=ImportResult)
def gen_subaccounts_bulk(body: GenSubAccounts, db: Session = Depends(get_db),
                         _op: Principal = Depends(require_operator)) -> ImportResult:
    """Generate `count` sub-accounts for each selected identity (multi-select in the dashboard)."""
    existing = {u for (u,) in db.execute(select(M.SubAccount.username)).all()}
    subs = 0
    errors: list[str] = []
    for iid in body.identity_ids:
        ident = db.get(M.Identity, iid)
        if not ident:
            errors.append(f"identity {iid} not found")
            continue
        first, _, last = (ident.full_name or "x x").partition(" ")
        for sa in gen_subaccounts(first, last or "x", body.count, existing):
            db.add(M.SubAccount(identity_id=ident.id, username=sa.username, password=sa.password,
                                status="new"))
            subs += 1
        if ident.status == "empty":
            ident.status = "active"
    db.commit()
    return ImportResult(created=len(body.identity_ids), sub_accounts=subs, errors=errors)


@app.post("/identities/gen-test", response_model=ImportResult)
def gen_test_identities(body: GenTestIdentities, db: Session = Depends(get_db),
                        _op: Principal = Depends(require_operator)) -> ImportResult:
    """Generate N fake TEST identities, each with sub-accounts."""
    existing_u = {u for (u,) in db.execute(select(M.SubAccount.username)).all()}
    existing_p = {p for (p,) in db.execute(
        select(M.Identity.passport_no).where(M.Identity.passport_no.isnot(None))).all()}
    gen = gen_fake_identities(body.count, body.accounts_per_identity,
                              existing_usernames=existing_u, existing_passports=existing_p,
                              nationality=body.nationality)
    subs = 0
    for gi in gen:
        ident = M.Identity(full_name=gi.full_name, type="test", birthdate=gi.birthdate,
                           passport_no=gi.passport_no, gender=gi.gender, nationality=gi.nationality,
                           status="active" if gi.sub_accounts else "empty")
        db.add(ident)
        db.flush()
        for sa in gi.sub_accounts:
            db.add(M.SubAccount(identity_id=ident.id, username=sa.username, password=sa.password,
                                status="new"))
            subs += 1
    db.commit()
    return ImportResult(created=len(gen), sub_accounts=subs)


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
    # dedup on (endpoint, credentials_ref): SOAX shares one endpoint (proxy.soax.com:5000) across
    # many distinct session strings, so endpoint alone would collapse the whole pool to one row.
    existing = {(e, c) for (e, c) in
                db.execute(select(M.Proxy.endpoint, M.Proxy.credentials_ref)).all()}
    created = 0
    for pp in parsed:
        key = (pp.endpoint, pp.credentials_ref)
        if key in existing:
            errors.append(f"skip duplicate {pp.endpoint}")
            continue
        db.add(M.Proxy(provider=body.provider, kind=body.kind, endpoint=pp.endpoint,
                       credentials_ref=pp.credentials_ref, max_sessions=body.max_sessions, active=True))
        existing.add(key)
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


# ── Worker templates ──────────────────────────────────────────────────────────
@app.get("/templates", response_model=list[TemplateOut])
def list_templates(db: Session = Depends(get_db)) -> list[M.WorkerTemplate]:
    return list(db.scalars(select(M.WorkerTemplate).order_by(M.WorkerTemplate.created_at.asc())).all())


@app.post("/templates", response_model=TemplateOut, status_code=201)
def create_template(body: TemplateCreate, db: Session = Depends(get_db),
                    _op: Principal = Depends(require_operator)) -> M.WorkerTemplate:
    settings_json = dict(body.settings or {})
    settings_json.setdefault("ROLE", str(body.role))
    try:
        validate_layer(L.TEMPLATE, settings_json)
    except Exception as e:
        raise HTTPException(400, f"invalid template settings: {e}")
    tpl = M.WorkerTemplate(name=body.name, role=body.role, settings_json=settings_json)
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    return tpl


@app.put("/templates/{template_id}", response_model=TemplateOut)
def update_template(template_id: str, body: TemplateCreate, db: Session = Depends(get_db),
                    _op: Principal = Depends(require_operator)) -> M.WorkerTemplate:
    tpl = db.get(M.WorkerTemplate, template_id)
    if not tpl:
        raise HTTPException(404, "template not found")
    settings_json = dict(body.settings or {})
    settings_json.setdefault("ROLE", str(body.role))
    try:
        validate_layer(L.TEMPLATE, settings_json)
    except Exception as e:
        raise HTTPException(400, f"invalid template settings: {e}")
    tpl.name, tpl.role, tpl.settings_json = body.name, body.role, settings_json
    db.commit()
    db.refresh(tpl)
    return tpl


@app.delete("/templates/{template_id}", status_code=204)
def delete_template(template_id: str, db: Session = Depends(get_db),
                    _op: Principal = Depends(require_operator)) -> None:
    tpl = db.get(M.WorkerTemplate, template_id)
    if tpl:
        db.delete(tpl)
        db.commit()


# ── Editable configs (form_defaults, quest_steps) ─────────────────────────────
@app.get("/configs")
def list_configs(db: Session = Depends(get_db), me: Principal = Depends(current_principal)) -> list[str]:
    return [r.key for r in db.scalars(select(M.AppConfig)).all()]


@app.get("/configs/{key}")
def read_config(key: str, db: Session = Depends(get_db),
                me: Principal = Depends(current_principal)) -> dict:
    val = get_config(db, key)
    if val is None:
        raise HTTPException(404, f"config {key} not found")
    return {"key": key, "value": val}


@app.put("/configs/{key}")
def write_config(key: str, body: dict, db: Session = Depends(get_db),
                 _op: Principal = Depends(require_operator)) -> dict:
    value = body.get("value", body)
    if not isinstance(value, (dict, list)):
        raise HTTPException(400, "config value must be a JSON object or array")
    set_config(db, key, value)
    return {"key": key, "saved": True}


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
        warmup_start_at=body.warmup_start_at,
        slot_open_at=body.slot_open_at,
        settings_json=clean,
    )
    db.add(bk)
    db.commit()
    db.refresh(bk)
    return bk


@app.put("/bookings/{booking_id}", response_model=BookingOut)
def update_booking(booking_id: str, body: BookingUpdate, db: Session = Depends(get_db),
                   _op: Principal = Depends(require_operator)) -> M.Booking:
    bk = db.get(M.Booking, booking_id)
    if not bk:
        raise HTTPException(404, "booking not found")
    if body.settings is not None:
        try:
            bk.settings_json = validate_layer(L.BOOKING, body.settings)
        except Exception as e:
            raise HTTPException(400, f"invalid booking settings: {e}")
    for field in ("name", "nationality", "residence", "warmup_start_at", "slot_open_at"):
        val = getattr(body, field)
        if val is not None:
            setattr(bk, field, val)
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

    # Template: default to the first template matching the resolved role if none given.
    template_id = body.template_id
    if template_id:
        tpl = db.get(M.WorkerTemplate, template_id)
        if not tpl:
            raise HTTPException(404, "template not found")
        if tpl.role != role:
            raise HTTPException(400, f"template role {tpl.role} != worker role {role}")
    else:
        tpl = db.scalars(select(M.WorkerTemplate).where(M.WorkerTemplate.role == role)
                         .order_by(M.WorkerTemplate.created_at.asc())).first()
        template_id = tpl.id if tpl else None

    alloc = M.IdentityAllocation(identity_id=ident.id, booking_id=bk.id, allocated_at=_now())
    db.add(alloc)
    db.add(M.BookingIdentity(booking_id=bk.id, identity_id=ident.id, assigned_role=body.assigned_role))
    worker = M.Worker(booking_id=bk.id, identity_id=ident.id, role=role, template_id=template_id)
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
def _do_launch_worker(db: Session, w: M.Worker) -> None:
    """Resolve + FREEZE effective settings (global<booking<template), spawn, send launch command."""
    bk = db.get(M.Booking, w.booking_id)
    tpl = db.get(M.WorkerTemplate, w.template_id) if w.template_id else None
    layers = {L.GLOBAL: get_global(db), L.BOOKING: (bk.settings_json or {})}
    if tpl:
        layers[L.TEMPLATE] = (tpl.settings_json or {})
    eff = resolve(layers)
    eff["ROLE"] = str(w.role)                     # role comes from the worker row, not settings
    w.effective_settings_json = eff
    w.status = WorkerStatus.RUNNING
    w.substate = WorkerSubstate.IDLE
    db.commit()
    supervisor.spawn(w.id)
    _bus.send_command(w.id, "launch", cfg=eff)


@app.post("/workers/{worker_id}/launch", response_model=WorkerOut)
def launch_worker(worker_id: str, db: Session = Depends(get_db),
                  _op: Principal = Depends(require_operator)) -> M.Worker:
    w = db.get(M.Worker, worker_id)
    if not w:
        raise HTTPException(404, "worker not found")
    _do_launch_worker(db, w)
    db.refresh(w)
    return w


@app.post("/bookings/{booking_id}/start")
def start_booking(booking_id: str, db: Session = Depends(get_db),
                  _op: Principal = Depends(require_operator)) -> dict:
    """Start Instantly — launch every READY worker in the booking (the run begins)."""
    bk = db.get(M.Booking, booking_id)
    if not bk:
        raise HTTPException(404, "booking not found")
    workers = list(db.scalars(select(M.Worker).where(
        M.Worker.booking_id == booking_id, M.Worker.status == WorkerStatus.READY)).all())
    for w in workers:
        _do_launch_worker(db, w)
    if bk.status in (BookingStatus.DRAFT, BookingStatus.SCHEDULED, BookingStatus.WARMING,
                     BookingStatus.ARMED):
        bk.status = BookingStatus.RUNNING
        db.commit()
    return {"started": len(workers), "booking_status": str(bk.status)}


@app.post("/bookings/{booking_id}/stop")
def stop_booking(booking_id: str, db: Session = Depends(get_db),
                 _op: Principal = Depends(require_operator)) -> dict:
    """Stop every running worker in the booking and mark it completed."""
    bk = db.get(M.Booking, booking_id)
    if not bk:
        raise HTTPException(404, "booking not found")
    running = list(db.scalars(select(M.Worker).where(
        M.Worker.booking_id == booking_id, M.Worker.status == WorkerStatus.RUNNING)).all())
    for w in running:
        _bus.send_command(w.id, "stop")
        supervisor.stop(w.id)
        w.status = WorkerStatus.STOPPED
    if bk.status == BookingStatus.RUNNING:
        bk.status = BookingStatus.COMPLETED
    db.commit()
    return {"stopped": len(running), "booking_status": str(bk.status)}


@app.get("/bookings/{booking_id}/summary")
def booking_summary(booking_id: str, db: Session = Depends(get_db)) -> dict:
    """Run overview: booking status + worker counts by status/role/substate."""
    bk = db.get(M.Booking, booking_id)
    if not bk:
        raise HTTPException(404, "booking not found")
    workers = list(db.scalars(select(M.Worker).where(M.Worker.booking_id == booking_id)).all())
    by_status: dict[str, int] = {}
    by_role: dict[str, int] = {}
    booked = 0
    for w in workers:
        by_status[str(w.status)] = by_status.get(str(w.status), 0) + 1
        by_role[str(w.role)] = by_role.get(str(w.role), 0) + 1
        if str(w.substate) == "booked":
            booked += 1
    return {
        "booking_status": str(bk.status), "workers": len(workers),
        "by_status": by_status, "by_role": by_role, "booked": booked,
        "warmup_start_at": bk.warmup_start_at.isoformat() if bk.warmup_start_at else None,
        "slot_open_at": bk.slot_open_at.isoformat() if bk.slot_open_at else None,
    }


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

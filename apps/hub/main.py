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
from libs.domain.enums import BOOKING_ACTIVE_STATUSES
from libs.domain.enums import SettingsLayer as L
from libs.domain.rules import RuleViolation, resolve_worker_role
from libs.domain.settings import CATALOG, settings_for_layer, validate_layer

from .db import engine, get_db
from .schemas import (
    AttachIdentity,
    BookingCreate,
    BookingOut,
    IdentityCreate,
    IdentityOut,
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


# ── Identities ────────────────────────────────────────────────────────────────
@app.post("/identities", response_model=IdentityOut, status_code=201)
def create_identity(body: IdentityCreate, db: Session = Depends(get_db)) -> M.Identity:
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
def create_booking(body: BookingCreate, db: Session = Depends(get_db)) -> M.Booking:
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
def attach_identity(booking_id: str, body: AttachIdentity, db: Session = Depends(get_db)) -> M.Worker:
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

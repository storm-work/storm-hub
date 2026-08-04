"""Pydantic request/response models for the Hub API."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from libs.domain.enums import (
    BookingType,
    IdentityType,
    SessionPurpose,
    UserRole,
    WorkerRole,
)


# ── Auth / sessions ───────────────────────────────────────────────────────────
class BootstrapAdmin(BaseModel):
    username: str
    password: str


class UserCreate(BaseModel):
    username: str
    password: str
    role: UserRole = UserRole.OPERATOR


class UserOut(BaseModel):
    id: str
    username: str
    role: UserRole
    model_config = {"from_attributes": True}


class LoginRequest(BaseModel):
    username: str
    password: str
    purpose: SessionPurpose = SessionPurpose.VIEW


class LoginResponse(BaseModel):
    token: str
    user: UserOut
    purpose: SessionPurpose
    session_status: str          # active | paused
    notice: str | None = None


class WhoAmI(BaseModel):
    username: str
    role: UserRole
    purpose: SessionPurpose
    session_status: str
    is_operating: bool


# ── Identities ────────────────────────────────────────────────────────────────
class IdentityCreate(BaseModel):
    full_name: str
    type: IdentityType
    birthdate: str | None = None
    passport_no: str | None = None
    gender: str | None = None
    nationality: str | None = None


class IdentityOut(BaseModel):
    id: str
    full_name: str
    type: IdentityType
    status: str
    passport_no: str | None = None
    nationality: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


# ── Bookings ──────────────────────────────────────────────────────────────────
class BookingCreate(BaseModel):
    name: str
    type: BookingType
    posto_id: str
    nationality: str | None = None
    residence: str | None = None
    settings: dict = Field(default_factory=dict)  # BOOKING-layer settings (validated by resolver)


class BookingOut(BaseModel):
    id: str
    name: str
    type: BookingType
    posto_id: str
    status: str
    nationality: str | None = None
    residence: str | None = None
    settings_json: dict = Field(default_factory=dict)
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


# ── Attach identity → worker ──────────────────────────────────────────────────
class AttachIdentity(BaseModel):
    identity_id: str
    assigned_role: WorkerRole | None = None  # required for TEST bookings; ignored for REAL


class WorkerOut(BaseModel):
    id: str
    booking_id: str
    identity_id: str
    role: WorkerRole
    status: str
    substate: str | None = None

    model_config = {"from_attributes": True}

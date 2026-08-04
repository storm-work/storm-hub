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
    warmup_start_at: datetime | None = None
    slot_open_at: datetime | None = None
    settings: dict = Field(default_factory=dict)  # BOOKING-layer settings (validated by resolver)


class BookingUpdate(BaseModel):
    name: str | None = None
    nationality: str | None = None
    residence: str | None = None
    warmup_start_at: datetime | None = None
    slot_open_at: datetime | None = None
    settings: dict | None = None


class BookingOut(BaseModel):
    id: str
    name: str
    type: BookingType
    posto_id: str
    status: str
    nationality: str | None = None
    residence: str | None = None
    warmup_start_at: datetime | None = None
    slot_open_at: datetime | None = None
    settings_json: dict = Field(default_factory=dict)
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


# ── Attach identity → worker ──────────────────────────────────────────────────
class AttachIdentity(BaseModel):
    identity_id: str
    assigned_role: WorkerRole | None = None  # required for TEST bookings; ignored for REAL
    template_id: str | None = None           # worker template to arm this worker with


class WorkerOut(BaseModel):
    id: str
    booking_id: str
    identity_id: str
    role: WorkerRole
    status: str
    substate: str | None = None

    model_config = {"from_attributes": True}


# ── Proxies + imports + global settings ───────────────────────────────────────
class ProxyOut(BaseModel):
    id: str
    provider: str
    kind: str
    endpoint: str
    active: bool
    health: str | None = None
    max_sessions: int
    model_config = {"from_attributes": True}


class ProxyCreate(BaseModel):
    provider: str = "isp"
    kind: str = "static_res"
    endpoint: str
    credentials_ref: str | None = None
    max_sessions: int = 2


class ProxyImport(BaseModel):
    provider: str = "isp"
    kind: str = "static_res"
    max_sessions: int = 2
    text: str                    # newline-separated proxy lines


class IdentityImport(BaseModel):
    csv: str
    default_type: IdentityType = IdentityType.TEST


class ImportResult(BaseModel):
    created: int
    sub_accounts: int = 0
    skipped: int = 0
    errors: list[str] = Field(default_factory=list)


class GlobalSettingsPut(BaseModel):
    values: dict


class GenSubAccounts(BaseModel):
    identity_ids: list[str]
    count: int = 3                # sub-accounts per identity


class GenTestIdentities(BaseModel):
    count: int = 10               # number of fake test identities
    accounts_per_identity: int = 3
    nationality: str = "CPV"


# ── Worker templates ──────────────────────────────────────────────────────────
class TemplateCreate(BaseModel):
    name: str
    role: WorkerRole
    settings: dict = Field(default_factory=dict)   # TEMPLATE-layer settings (validated)


class TemplateOut(BaseModel):
    id: str
    name: str
    role: WorkerRole
    settings_json: dict = Field(default_factory=dict)
    logic_snapshot: str = "latest"
    model_config = {"from_attributes": True}

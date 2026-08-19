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
    comment: str | None = None
    target_posto: str | None = None


class IdentityUpdate(BaseModel):
    comment: str | None = None
    target_posto: str | None = None


class IdentityOut(BaseModel):
    id: str
    full_name: str
    type: IdentityType
    status: str
    birthdate: str | None = None
    gender: str | None = None
    passport_no: str | None = None
    nationality: str | None = None
    comment: str | None = None
    target_posto: str | None = None
    created_at: datetime | None = None
    # C2 exclusivity, surfaced so the UI can filter/explain instead of letting an operator pick an
    # identity that attach_identity will 409. Set only when the identity currently holds an
    # unreleased allocation to an ACTIVE booking; None means free to attach.
    allocated_booking_id: str | None = None
    allocated_booking_name: str | None = None

    model_config = {"from_attributes": True}


class BookedIdentityOut(BaseModel):
    """One row per IDENTITY that has completed its mission -- never per sub-account. An identity is
    a person; its sub-accounts are just login attempts on their behalf, so a second appointment for
    the same person is a defect. Names the sub-account that actually got through."""
    identity_id: str
    full_name: str
    type: str
    nationality: str | None = None
    passport_no: str | None = None
    sub_account_id: str
    sub_account_username: str
    booking_id: str
    booking_name: str
    posto_id: str
    booked_at: datetime | None = None
    slot_date: str | None = None
    slot_period: int | None = None
    pdf_artifact_id: str | None = None


class BookedCredentialsRequest(BaseModel):
    identity_ids: list[str]


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
    # Regression: these three DB columns existed and were written correctly, but were never
    # returned here -- the dashboard's Booking Overview panel read booking.slot_open_fired_at/
    # slot_found_fired_at expecting a real signal-fire display and silently got `undefined` every
    # time, since the API response never carried them.
    started_at: datetime | None = None
    slot_open_fired_at: datetime | None = None
    slot_found_fired_at: datetime | None = None

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
    sub_account_id: str | None = None

    model_config = {"from_attributes": True}


# ── Proxies + imports + global settings ───────────────────────────────────────
class ProxyOut(BaseModel):
    id: str
    provider: str
    kind: str
    endpoint: str
    active: bool
    health: str | None = None
    # Surfaced so the Proxies page can show WHAT is condemned, not just how many. lease_proxy
    # filters on is_healthy, so a False here means the proxy is out of the pool entirely -- the
    # single most consequential field on this row, and it was not being returned at all.
    is_healthy: bool = True
    score: float = 0.0
    geo: str | None = None
    max_sessions: int
    model_config = {"from_attributes": True}


class ProxyCreate(BaseModel):
    provider: str = "soax"
    kind: str = "static"
    endpoint: str
    credentials_ref: str | None = None
    max_sessions: int = 2


class ProxyImport(BaseModel):
    provider: str = "soax"
    kind: str = "static"
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
    # Populated by identity-related imports/generators (empty/unused for proxy imports) so the
    # dashboard can chain follow-up actions (gen-subaccounts, then registration/run) onto exactly
    # the rows this call just created, without a second lookup.
    identity_ids: list[str] = Field(default_factory=list)
    sub_account_ids: list[str] = Field(default_factory=list)


class GlobalSettingsPut(BaseModel):
    values: dict


class GenSubAccounts(BaseModel):
    identity_ids: list[str]
    count: int = 3                # sub-accounts per identity


# ── Registration subsystem ─────────────────────────────────────────────────────
class SubAccountOut(BaseModel):
    id: str
    identity_id: str
    identity_name: str
    identity_type: str
    username: str
    status: str
    email: str | None = None
    registration_status: str | None = None  # latest registration_jobs.status for this account, if any
    attempts: int = 0
    created_at: datetime | None = None


class SubAccountResetRequest(BaseModel):
    sub_account_ids: list[str]


class RegistrationRunRequest(BaseModel):
    sub_account_ids: list[str]
    concurrency: int = 10
    browser_concurrency: int | None = None   # defaults to concurrency
    provider: str = "soax"                   # proxy provider for registration (soax confirmed
                                              # preferred; see conversation_extract)
    kind: str | None = None
    mode: str = "parallel"                   # RegistrationMode value


class RegistrationJobOut(BaseModel):
    id: str
    sub_account_id: str
    mode: str
    status: str
    attempts: int
    proxy_id: str | None = None
    solver_used: str | None = None
    created_at: datetime | None = None
    model_config = {"from_attributes": True}


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


# ── Booked appointments / artifacts / audit (read-only viewers) ───────────────
class ArtifactOut(BaseModel):
    id: str
    worker_id: str | None = None
    kind: str
    path: str
    created_at: datetime | None = None
    model_config = {"from_attributes": True}


class BookedAppointmentOut(BaseModel):
    id: str
    booking_id: str
    identity_id: str
    sub_account_id: str
    appointment_ref: str | None = None
    pdf_artifact_id: str | None = None
    booked_at: datetime | None = None
    model_config = {"from_attributes": True}


class SlotOut(BaseModel):
    id: str
    booking_id: str
    posto_id: str
    date: str
    period: int
    status: str
    capacity_estimate: int | None = None
    miss_count: int = 0
    discovered_by: str | None = None
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    created_at: datetime | None = None
    model_config = {"from_attributes": True}


class AuditRowOut(BaseModel):
    id: str
    actor: str | None = None
    action: str
    target: str | None = None
    payload: dict = Field(default_factory=dict)
    ts: datetime
    model_config = {"from_attributes": True}

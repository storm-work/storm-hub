"""Pydantic request/response models for the Hub API."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

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
    # Selection preference, higher first. See libs/domain/priority.py for the named levels.
    priority: int | None = None


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
    priority: int = 100
    created_at: datetime | None = None
    # C2 exclusivity, surfaced so the UI can filter instead of letting an operator pick an identity
    # that attach_identity will 409. Set whenever the identity holds an UNRELEASED allocation, to
    # any booking, whatever that booking's status -- which is what list_identities actually queries,
    # and what the partial unique index enforces. (This said "to an ACTIVE booking", which was never
    # what the endpoint did; the attach panel hides on this field, so the narrower claim described a
    # panel that would have offered identities held by a stopped booking.) None means free.
    allocated_booking_id: str | None = None
    allocated_booking_name: str | None = None
    # How many of this identity's sub-accounts are past the portal's email-verification step, i.e.
    # could actually attempt a login (libs.domain.enums.READY_SUB_ACCOUNT_STATUSES). Computed by
    # list_identities, never stored.
    #
    # This field exists because the attach panel used to answer that question CLIENT-SIDE, off
    # `status == "active"` -- which per IdentityStatus's own definition only means ">=1 sub-account
    # row exists", and gen_subaccounts_bulk sets it the instant sub-accounts are generated. So
    # identities whose sub-accounts were all still NEW/REGISTERED were offered as attachable. The
    # rule that decides this belongs on the server, next to the launch gate that enforces it, not
    # re-derived in JSX from a status the server happens to expose. 0 means "nothing to log in with
    # yet"; the launch gate 409s on exactly that condition.
    ready_sub_accounts: int = 0

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
    # DETECTOR booking ids whose discovered slots are injected into this booking's pool (cross-
    # booking feed, migration cp000). Empty = no feed.
    discovery_feeder_booking_ids: list[str] | None = None


class BookingUpdate(BaseModel):
    name: str | None = None
    type: BookingType | None = None
    posto_id: str | None = None
    nationality: str | None = None
    residence: str | None = None
    warmup_start_at: datetime | None = None
    slot_open_at: datetime | None = None
    settings: dict | None = None
    # Pass [] to clear the feed; None (default) leaves it unchanged.
    discovery_feeder_booking_ids: list[str] | None = None


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
    # Visibility flag, orthogonal to `status` -- see the Booking model. The dashboard hides archived
    # bookings by default and needs this to render the Unarchive action, so it must be surfaced:
    # a column written but never returned is the exact regression called out just below.
    archived_at: datetime | None = None
    # Regression: these three DB columns existed and were written correctly, but were never
    # returned here -- the dashboard's Booking Overview panel read booking.slot_open_fired_at/
    # slot_found_fired_at expecting a real signal-fire display and silently got `undefined` every
    # time, since the API response never carried them.
    started_at: datetime | None = None
    slot_open_fired_at: datetime | None = None
    slot_found_fired_at: datetime | None = None
    # Cross-booking feed (cp000): the DETECTOR booking ids feeding this pool. [] / null = none.
    discovery_feeder_booking_ids: list[str] | None = None

    model_config = {"from_attributes": True}


# ── Attach identity → worker ──────────────────────────────────────────────────
class AttachIdentity(BaseModel):
    identity_id: str
    assigned_role: WorkerRole | None = None  # required for TEST bookings; ignored for REAL
    template_id: str | None = None           # worker template to arm this worker with
    # Phase 3: the WORKER settings layer had no input source at all before this (only its OUTPUT,
    # effective_settings_json, existed) -- L.WORKER is fully implemented in the resolver's
    # precedence but nothing ever populated it. Validated the same way booking/template settings
    # already are (validate_layer) before being stored.
    worker_settings: dict | None = None


class WorkerOut(BaseModel):
    id: str
    booking_id: str
    identity_id: str
    role: WorkerRole
    status: str
    substate: str | None = None
    # The machine-readable context behind the substate -- carries the NOT_BOOKABLE_PREFIX marker for
    # a KEEPING worker whose session went dead (keepalive dead-trip / re-warm fail). Was written to
    # the DB and read by the dashboard's workerView, but never RETURNED here, so a dead 'keeping'
    # worker was indistinguishable from a healthy one on the page (the "holding is a lie" gap).
    substate_detail: str | None = None
    sub_account_id: str | None = None
    # The exit country this worker is bound to (consistent across the run + every rotation; persists
    # across relaunch). NULL = unbound -> launch draws from the booking's PROXY_GEOS set. Set/cleared
    # per worker via PATCH /workers/{id}/geo.
    proxy_geo: str | None = None

    model_config = {"from_attributes": True}


# ── Proxies + imports + global settings ───────────────────────────────────────
class ProxyOut(BaseModel):
    id: str
    provider: str
    kind: str
    # rotating (default) = session template, health never excludes it from selection (D19).
    # static = a finite resource; health DOES exclude it (Phase 3, libs/proxies/lease.py).
    resource_type: str = "rotating"
    pool_name: str | None = None
    endpoint: str
    active: bool
    # Tier 2 -- portal reachability. Separate from is_healthy/health (tier 1) on purpose; see the
    # Proxy model. NULL means never portal-checked, which is NOT the same as "failed".
    portal_ok: bool | None = None
    portal_health: str | None = None
    checked_at: datetime | None = None
    health: str | None = None
    # Surfaced so the Proxies page can show WHAT is condemned, not just how many. Whether a False
    # here actually removes the proxy from selection now depends on resource_type: never, for
    # rotating (D19); yes, for static (Phase 3).
    is_healthy: bool = True
    score: float = 0.0
    geo: str | None = None
    max_sessions: int
    model_config = {"from_attributes": True}


class ProxyCreate(BaseModel):
    provider: str = "soax"
    kind: str = "mobile"
    resource_type: Literal["rotating", "static"] = "rotating"
    endpoint: str
    credentials_ref: str | None = None
    max_sessions: int = 2
    # Named ISP pool. Present on BOTH create and import deliberately: a schema that accepts the
    # field on one path and silently drops it on the other is the delivery gap this project keeps
    # hitting -- the row is created, looks right in the table, and is then invisible to every
    # booking that assigned the pool.
    pool_name: str | None = None


class ProxyImport(BaseModel):
    provider: str = "soax"
    kind: str = "mobile"
    resource_type: Literal["rotating", "static"] = "rotating"
    max_sessions: int = 2
    # Named ISP pool to import into. Blank/None = unnamed, preserving the pre-2026-09-16 behaviour
    # for any caller that does not care. A name that does not exist yet simply creates the pool --
    # there is no separate create step, because a pool IS the set of rows carrying the name.
    pool_name: str | None = None
    text: str                    # newline-separated proxy lines


class ProxyHealthCheck(BaseModel):
    ids: list[str]
    # Run tier 2 (browser -> portal) as well as tier 1 (raw -> neutral url). Off by default: tier 2
    # costs ~20-25s of Chromium per proxy against ~1s for tier 1, and tier 1 alone answers the
    # question that matters for pool hygiene ("is this proxy dead").
    portal: bool = False


class ProxyBulkUpdate(BaseModel):
    ids: list[str]
    # Only the fields that are safe to change in bulk on a live pool. Credentials/endpoint are
    # deliberately absent: those identify the resource, and editing them in bulk would silently
    # repoint leases that workers are currently holding.
    max_sessions: int | None = None
    pool_name: str | None = None


class ProxyBulkDelete(BaseModel):
    ids: list[str]
    # Delete rows whose TIER 1 has not actually failed. Off by default, and the refusal is the
    # point: an ISP proxy is a purchased, individually-owned IP, and tier 2 failing means the
    # PORTAL was unreachable from it -- which on 2026-08-28 was true of 4,489 perfectly good
    # proxies at once. Without this gate a portal outage would present the whole pool as garbage.
    force: bool = False


class BlindApplyRequest(BaseModel):
    """Seed the slot pool with a (date x period) grid for blind apply -- see libs/domain/blind.py.

    Both lists default from the booking: dates from the CAL window, periods from the posto's proven
    set. The operator normally narrows `dates` to what they actually saw on the portal's calendar.
    """
    dates: list[str] | str | None = None
    periods: list[int] | str | None = None
    # Seed even though discovery has AVAILABLE slots. Off by default: blind apply is for when the
    # system sees nothing and the operator sees something, and seeding on top of real finds would
    # dilute the pool with phantoms the fleet then spends attempts on.
    force: bool = False


class IdentityImport(BaseModel):
    csv: str
    default_type: IdentityType = IdentityType.TEST
    # Applied to every REAL identity this import creates that doesn't already carry one (the CSV
    # format itself has no target_posto column -- app-enforced real-identities-only, same as
    # IdentityCreate's own target_posto). None/absent leaves it unset, matching pre-existing behavior.
    default_target_posto: str | None = None


class ImportResult(BaseModel):
    created: int
    sub_accounts: int = 0
    skipped: int = 0
    # DEPRECATED (2026-10-02): the old "supersede" path demoted duplicates to LOW and created a new
    # row. Dedup replaced it, so this stays only for response back-compat and is now always empty.
    superseded: list[str] = Field(default_factory=list)
    # Dedup outcomes (2026-10-02): `promoted` = existing passport rows REUSED and bumped to HIGH
    # (no new row created); `ignored` = existing rows already HIGH + verified, so the incoming copy
    # was dropped untouched. `created` still counts only genuinely-new identity rows.
    promoted: list[str] = Field(default_factory=list)
    ignored: list[str] = Field(default_factory=list)
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
    # Same selector the booking path has (PROXY_GEOS): comma-separated exit countries, blank = all.
    # Registration hits the same portal behind the same reCAPTCHA, so the geo of the IP matters here
    # for the same reason it does at login.
    geos: str | None = None
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
    # The default template for its role -- 'Auto (by role)' attach resolves to this one. At most one
    # per role.
    is_default: bool = False
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
    # Resolved server-side. The dashboard used to look these up in /identities, which no longer
    # returns identities that have booked -- and every appointment is BY DEFINITION one of those,
    # so every name silently degraded to an id fragment.
    identity_name: str | None = None
    sub_account_username: str | None = None
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


class IdentityBulkDelete(BaseModel):
    """Bulk removal of identities that are no longer usable.

    Reports per-id outcomes rather than failing the whole call on the first refusal: an operator
    clearing out a burned batch wants the ones that CAN go to go, and a precise list of which could
    not and why -- not an all-or-nothing 409 that leaves them guessing which id caused it.
    """
    identity_ids: list[str]
    force: bool = False


class IdentityBulkDeleteResult(BaseModel):
    deleted: list[str] = Field(default_factory=list)
    refused: dict[str, str] = Field(default_factory=dict)   # identity_id -> why


class IdentityBulkPriority(BaseModel):
    """Bulk override of the attach-panel selection preference. See libs/domain/priority.py for the
    named levels (HIGH=200, NORMAL=100, LOW=0) -- any int is accepted, matching the single-identity
    PATCH's own priority field."""
    identity_ids: list[str]
    priority: int

"""SQLAlchemy models for the storm-hub schema (spec §3).

One class per table. Status columns use `sa.Enum(<DomainEnum>, native_enum=False)` so the DB
stores the same string values the domain layer uses, with a CHECK constraint, portably. JSON
settings blobs use generic JSON (JSONB in the PG migration).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from libs.domain import enums as E

from .base import Base, PKMixin, TimestampMixin


def _enum(py_enum, **kw):
    return Enum(py_enum, native_enum=False, validate_strings=True, length=32, **kw)


# ── Users & sessions (C8 / R2-C7) ─────────────────────────────────────────────
class User(Base, PKMixin, TimestampMixin):
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[E.UserRole] = mapped_column(_enum(E.UserRole), nullable=False)
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserSession(Base, PKMixin, TimestampMixin):
    __tablename__ = "user_sessions"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    purpose: Mapped[E.SessionPurpose] = mapped_column(_enum(E.SessionPurpose), nullable=False)
    status: Mapped[E.SessionStatus] = mapped_column(
        _enum(E.SessionStatus), nullable=False, default=E.SessionStatus.ACTIVE
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Single active OPERATE session host-wide is enforced by a partial unique index in the migration.


class OpsHandoverRequest(Base, PKMixin, TimestampMixin):
    __tablename__ = "ops_handover_requests"
    operate_session_id: Mapped[str] = mapped_column(ForeignKey("user_sessions.id"), nullable=False)
    requester_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    status: Mapped[E.HandoverStatus] = mapped_column(
        _enum(E.HandoverStatus), nullable=False, default=E.HandoverStatus.PENDING
    )


# ── Identities & sub-accounts ─────────────────────────────────────────────────
class Identity(Base, PKMixin, TimestampMixin):
    __tablename__ = "identities"
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    birthdate: Mapped[str | None] = mapped_column(String(16))
    passport_no: Mapped[str | None] = mapped_column(String(64), index=True)
    gender: Mapped[str | None] = mapped_column(String(16))
    nationality: Mapped[str | None] = mapped_column(String(8))
    type: Mapped[E.IdentityType] = mapped_column(_enum(E.IdentityType), nullable=False)
    status: Mapped[E.IdentityStatus] = mapped_column(
        _enum(E.IdentityStatus), nullable=False, default=E.IdentityStatus.EMPTY
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_identities_type_status", "type", "status"),)


class SubAccount(Base, PKMixin, TimestampMixin):
    __tablename__ = "sub_accounts"
    identity_id: Mapped[str] = mapped_column(ForeignKey("identities.id"), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255))
    email_pass: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[E.SubAccountStatus] = mapped_column(
        _enum(E.SubAccountStatus), nullable=False, default=E.SubAccountStatus.NEW
    )
    last_login_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)


class IdentityAllocation(Base, PKMixin, TimestampMixin):
    """C2: an identity is exclusively allocated to at most one ACTIVE booking at a time.
    (A partial unique index on active allocations enforces it in the migration.)"""
    __tablename__ = "identity_allocations"
    identity_id: Mapped[str] = mapped_column(ForeignKey("identities.id"), nullable=False, index=True)
    booking_id: Mapped[str] = mapped_column(ForeignKey("bookings.id"), nullable=False, index=True)
    allocated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IdentityEvent(Base, PKMixin, TimestampMixin):
    """C6: durable per-identity run-history log."""
    __tablename__ = "identity_events"
    identity_id: Mapped[str] = mapped_column(ForeignKey("identities.id"), nullable=False, index=True)
    sub_account_id: Mapped[str | None] = mapped_column(ForeignKey("sub_accounts.id"))
    booking_id: Mapped[str | None] = mapped_column(ForeignKey("bookings.id"))
    worker_run_id: Mapped[str | None] = mapped_column(ForeignKey("worker_runs.id"))
    event: Mapped[E.IdentityEvent] = mapped_column(_enum(E.IdentityEvent), nullable=False)
    step: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(Text)
    ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ── Proxies ───────────────────────────────────────────────────────────────────
class Proxy(Base, PKMixin, TimestampMixin):
    __tablename__ = "proxies"
    provider: Mapped[str] = mapped_column(String(16), nullable=False)     # soax | isp
    kind: Mapped[str] = mapped_column(String(24), nullable=False)         # datacenter|static_res|mobile
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    credentials_ref: Mapped[str | None] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    exit_ip: Mapped[str | None] = mapped_column(String(64))
    health: Mapped[str | None] = mapped_column(String(24))
    is_healthy: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    max_sessions: Mapped[int] = mapped_column(Integer, default=2, nullable=False)


class ProxyLease(Base, PKMixin, TimestampMixin):
    """Central proxy-lease authority — removes the cross-process proxy-cursor collision."""
    __tablename__ = "proxy_leases"
    proxy_id: Mapped[str] = mapped_column(ForeignKey("proxies.id"), nullable=False, index=True)
    worker_id: Mapped[str] = mapped_column(ForeignKey("workers.id"), nullable=False, index=True)
    leased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ── Bookings ──────────────────────────────────────────────────────────────────
class Booking(Base, PKMixin, TimestampMixin):
    __tablename__ = "bookings"
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[E.BookingType] = mapped_column(_enum(E.BookingType), nullable=False)
    posto_id: Mapped[str] = mapped_column(String(16), nullable=False)
    nationality: Mapped[str | None] = mapped_column(String(8))
    residence: Mapped[str | None] = mapped_column(String(8))
    status: Mapped[E.BookingStatus] = mapped_column(
        _enum(E.BookingStatus), nullable=False, default=E.BookingStatus.DRAFT, index=True
    )
    warmup_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    slot_open_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settings_json: Mapped[dict] = mapped_column(JSON, default=dict)


class BookingIdentity(Base, PKMixin):
    __tablename__ = "booking_identities"
    booking_id: Mapped[str] = mapped_column(ForeignKey("bookings.id"), nullable=False, index=True)
    identity_id: Mapped[str] = mapped_column(ForeignKey("identities.id"), nullable=False, index=True)
    assigned_role: Mapped[E.WorkerRole | None] = mapped_column(_enum(E.WorkerRole))  # null = derive
    __table_args__ = (UniqueConstraint("booking_id", "identity_id", name="uq_booking_identity"),)


# ── Worker templates (C3/C4/C9) ───────────────────────────────────────────────
class WorkerTemplate(Base, PKMixin, TimestampMixin):
    __tablename__ = "worker_templates"
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[E.WorkerRole] = mapped_column(_enum(E.WorkerRole), nullable=False)
    orchestration_logic_ref: Mapped[str | None] = mapped_column(String(128))
    settings_json: Mapped[dict] = mapped_column(JSON, default=dict)
    logic_snapshot: Mapped[str] = mapped_column(String(64), default="latest", nullable=False)


class TemplateVersion(Base, PKMixin, TimestampMixin):
    __tablename__ = "template_versions"
    template_id: Mapped[str] = mapped_column(ForeignKey("worker_templates.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    settings_json: Mapped[dict] = mapped_column(JSON, default=dict)
    logic_snapshot: Mapped[str] = mapped_column(String(64), default="latest", nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("template_id", "version", name="uq_template_version"),)


# ── Workers & runs ────────────────────────────────────────────────────────────
class Worker(Base, PKMixin, TimestampMixin):
    __tablename__ = "workers"
    booking_id: Mapped[str] = mapped_column(ForeignKey("bookings.id"), nullable=False, index=True)
    identity_id: Mapped[str] = mapped_column(ForeignKey("identities.id"), nullable=False, index=True)
    template_id: Mapped[str | None] = mapped_column(ForeignKey("worker_templates.id"))
    template_version_id: Mapped[str | None] = mapped_column(ForeignKey("template_versions.id"))
    role: Mapped[E.WorkerRole] = mapped_column(_enum(E.WorkerRole), nullable=False)
    status: Mapped[E.WorkerStatus] = mapped_column(
        _enum(E.WorkerStatus), nullable=False, default=E.WorkerStatus.READY
    )
    substate: Mapped[E.WorkerSubstate | None] = mapped_column(_enum(E.WorkerSubstate))
    substate_detail: Mapped[str | None] = mapped_column(String(255))
    effective_settings_json: Mapped[dict] = mapped_column(JSON, default=dict)  # frozen at launch


class WorkerRun(Base, PKMixin, TimestampMixin):
    __tablename__ = "worker_runs"
    worker_id: Mapped[str] = mapped_column(ForeignKey("workers.id"), nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(32))
    host_pid: Mapped[int | None] = mapped_column(Integer)
    checkpoint_ref: Mapped[str | None] = mapped_column(String(255))
    log_path: Mapped[str | None] = mapped_column(String(512))


# ── Slots (C5) ────────────────────────────────────────────────────────────────
class Slot(Base, PKMixin, TimestampMixin):
    __tablename__ = "slots"
    booking_id: Mapped[str] = mapped_column(ForeignKey("bookings.id"), nullable=False, index=True)
    posto_id: Mapped[str] = mapped_column(String(16), nullable=False)
    date: Mapped[str] = mapped_column(String(16), nullable=False)
    period: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[E.SlotStatus] = mapped_column(
        _enum(E.SlotStatus), nullable=False, default=E.SlotStatus.AVAILABLE
    )
    capacity_estimate: Mapped[int | None] = mapped_column(Integer)
    miss_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    discovered_by: Mapped[str | None] = mapped_column(String(36))
    first_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("booking_id", "date", "period", name="uq_slot_booking_date_period"),
    )


class SlotLease(Base, PKMixin, TimestampMixin):
    __tablename__ = "slot_leases"
    slot_id: Mapped[str] = mapped_column(ForeignKey("slots.id"), nullable=False, index=True)
    worker_id: Mapped[str] = mapped_column(ForeignKey("workers.id"), nullable=False, index=True)
    leased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[E.SlotLeaseOutcome | None] = mapped_column(_enum(E.SlotLeaseOutcome))


# ── Registration jobs (C5) ────────────────────────────────────────────────────
class RegistrationJob(Base, PKMixin, TimestampMixin):
    __tablename__ = "registration_jobs"
    sub_account_id: Mapped[str] = mapped_column(ForeignKey("sub_accounts.id"), nullable=False, index=True)
    mode: Mapped[E.RegistrationMode] = mapped_column(
        _enum(E.RegistrationMode), nullable=False, default=E.RegistrationMode.PARALLEL
    )
    status: Mapped[E.RegistrationStatus] = mapped_column(
        _enum(E.RegistrationStatus), nullable=False, default=E.RegistrationStatus.QUEUED, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    proxy_id: Mapped[str | None] = mapped_column(ForeignKey("proxies.id"))
    solver_used: Mapped[str | None] = mapped_column(String(32))
    log_ref: Mapped[str | None] = mapped_column(String(512))


# ── Booked appointments / PDFs (C5) ───────────────────────────────────────────
class BookedAppointment(Base, PKMixin, TimestampMixin):
    __tablename__ = "booked_appointments"
    booking_id: Mapped[str] = mapped_column(ForeignKey("bookings.id"), nullable=False, index=True)
    identity_id: Mapped[str] = mapped_column(ForeignKey("identities.id"), nullable=False, index=True)
    sub_account_id: Mapped[str] = mapped_column(ForeignKey("sub_accounts.id"), nullable=False)
    slot_id: Mapped[str | None] = mapped_column(ForeignKey("slots.id"))
    appointment_ref: Mapped[str | None] = mapped_column(String(128))
    pdf_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    booked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Artifact(Base, PKMixin, TimestampMixin):
    __tablename__ = "artifacts"
    worker_id: Mapped[str | None] = mapped_column(ForeignKey("workers.id"), index=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)   # pdf|screenshot|checkpoint
    path: Mapped[str] = mapped_column(String(512), nullable=False)


# ── Settings (layered store) & audit ──────────────────────────────────────────
class SettingRow(Base, PKMixin):
    __tablename__ = "settings"
    layer: Mapped[E.SettingsLayer] = mapped_column(_enum(E.SettingsLayer), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")  # booking/template/worker id
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        UniqueConstraint("layer", "scope_key", "key", name="uq_setting_scope_key"),
    )


class AuditRow(Base, PKMixin):
    __tablename__ = "audit"
    actor: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# Convenience: all tables, for create_all smoke tests and Alembic autogenerate target.
metadata = Base.metadata

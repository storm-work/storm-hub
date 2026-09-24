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
    func,
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


class ExtraSessionRequest(Base, PKMixin, TimestampMixin):
    """A viewer's request for a second concurrent VIEW session, past the 1-active-session-per-
    viewer cap (apps/hub/auth.py's open_session()). Unlike OpsHandoverRequest, granting this does
    NOT end any existing session -- the point is two concurrent ACTIVE sessions, not a transfer."""
    __tablename__ = "extra_session_requests"
    paused_session_id: Mapped[str] = mapped_column(ForeignKey("user_sessions.id"), nullable=False)
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
    comment: Mapped[str | None] = mapped_column(String(512))
    target_posto: Mapped[str | None] = mapped_column(String(16))  # real identities only (app-enforced)
    # Selection preference, higher first. Read by GET /identities' ordering, which is what the
    # attach panel lists -- so it decides what the operator reaches for first, not just what is
    # stored. Import sets fresh identities HIGH and demotes any duplicate they supersede to LOW,
    # so a re-imported passport takes precedence over the copy that was already there.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100, server_default="100")
    __table_args__ = (
        Index("ix_identities_type_status", "type", "status"),
        Index("ix_identities_priority", "priority"),
    )


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
    kind: Mapped[str] = mapped_column(String(24), nullable=False)         # rotating | mobile
    # rotating = reusable session template (the exit behind the row moves on its own; a failed probe
    # says nothing about the row -- D19). static = a genuinely finite, individually-owned resource
    # (a failed probe DOES mean the row is bad). Every row is 'rotating' until a static/ISP pool
    # actually exists (docs/engineering/PROXY_CONTRACTS.md: "we own none", as of this column's
    # introduction). Constrained to the two values by a Postgres CHECK (migration cd000) and by the
    # Pydantic schema at the API boundary -- unlike `provider`/`kind`, this field's whole purpose is
    # a clean binary distinction, so it is held stricter than its free-text siblings deliberately.
    resource_type: Mapped[str] = mapped_column(String(16), nullable=False, default="rotating")
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    credentials_ref: Mapped[str | None] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    exit_ip: Mapped[str | None] = mapped_column(String(64))
    health: Mapped[str | None] = mapped_column(String(160))
    is_healthy: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    max_sessions: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    # The pool cursor. Stamped on every lease AND every probe attempt, and ordered on NULLS-FIRST
    # ascending, so selection is least-recently-used: the pool is walked end to end before anything
    # repeats. Without it _candidate_query was deterministic (score, created_at) and hammered the
    # same handful of proxies forever while the rest of the pool sat idle.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Exit country, backfilled from credentials_ref. Login success against this portal varies ~8x
    # by geo (DE 23.7% / IT 23.4% ... CZ 2.9%, measured over 1392 real attempts), so this is the
    # single highest-leverage selector on the pool. NULL = unknown, never excluded by a filter.
    geo: Mapped[str | None] = mapped_column(String(8))
    # Named ISP pool this row belongs to (migration cj000). One purchase = one name, e.g.
    # "thordata-oct". NULL = unnamed, which is what every pre-cj000 row is and what a pool filter
    # treats as belonging to no named pool. A pool is a NAME, not a table: importing under a new
    # name creates the pool, importing under an existing one appends to it -- see the migration for
    # why that beat a proxy_pools table. Meaningful only for resource_type='static'; rotating
    # proxies have had no rows since ci000droprot.
    pool_name: Mapped[str | None] = mapped_column(String(64), index=True)
    # TIER 2 (migration ck000): can this IP reach the PORTAL? Diagnostic only -- deliberately not
    # `is_healthy`, which is tier 1 (can the proxy carry traffic at all, probed against a neutral
    # url) and is the only tier that gates selection or deletion. Keeping them apart is the
    # 2026-08-28 lesson: 4,489 rows were condemned by a throttling portal and connected 20/20 when
    # re-probed neutrally. NULL = never portal-checked.
    portal_ok: Mapped[bool | None] = mapped_column(Boolean)
    portal_health: Mapped[str | None] = mapped_column(String(160))
    # When either tier last ran. NOT last_used_at -- that is the lease cursor, and stamping it from
    # a manual probe would silently reorder the pool as a side effect of inspecting it.
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    slot_open_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    slot_found_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Visibility only (migration cl000). Deliberately NOT a BookingStatus value: `status` records
    # how the booking ENDED (COMPLETED with appointments reads very differently from STOPPED), and
    # that is precisely what an operator browsing old runs wants to see. Archiving must hide a
    # booking without rewriting its outcome. NULL = not archived.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    # The default template for its ROLE, used by 'Auto (by role)' attach (main.py's attach_identity).
    # At most one True per role -- the make-default endpoint clears the others. False = not default;
    # with no default flagged for a role, attach falls back to oldest-by-created_at (legacy behavior).
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


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
    # Phase 3: the WORKER settings layer's INPUT. The resolver (WORKER > TEMPLATE > BOOKING > GLOBAL)
    # already implemented this precedence correctly; nothing populated it because there was nowhere
    # to store a per-worker override before this column existed. Set at attach time
    # (AttachIdentity.worker_settings), validated the same way Booking/WorkerTemplate settings
    # already are. Distinct from effective_settings_json, which is the resolved OUTPUT frozen at
    # launch -- this is one of that resolution's inputs.
    settings_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # Which sub-account this worker is actually logged in with right now -- set at launch, then kept
    # in sync by do_login()'s sticky-then-rotate loop (ops_live.py) via the "account_rotated" event,
    # since a worker can rotate off its launch-time pick after repeated login failures.
    sub_account_id: Mapped[str | None] = mapped_column(ForeignKey("sub_accounts.id"))
    # The exit country this worker is BOUND to, kept consistent for the whole run and every
    # self-mint exit rotation (both providers' roll() preserve the country segment). NULL = unbound:
    # launch picks one uniformly from the booking's PROXY_GEOS set and freezes it here, so the choice
    # survives relaunch. The operator can set/clear it per worker from the dashboard; it changes only
    # when they change it. Stored as the provider-neutral code (mint lowercases for soax / uppercases
    # for webshare) -- see libs/proxies/geos.py.
    proxy_geo: Mapped[str | None] = mapped_column(String(16))


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
    # 'scout' = discovered by a scout's scan. 'blind' = operator-seeded for blind apply (migration
    # cm000): a (date, period) the operator believes is open but discovery could not confirm. The
    # apply machinery treats the two differently in three places -- see the migration's docstring.
    source: Mapped[str] = mapped_column(String(16), default="scout", nullable=False)
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


class AppConfig(Base, PKMixin):
    """Editable JSON config (form_defaults, quest_steps) — seeded from config/*.json, edited via the
    dashboard. Split conceptually into frequently-updated (travel dates) vs rarely-updated fields."""
    __tablename__ = "configs"
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
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

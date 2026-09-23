"""Add bookings.archived_at — hide finished bookings without rewriting their history.

Operator request 2026-09-17: archive bookings, and hide/show archived ones on the dashboard.

A TIMESTAMP, NOT A STATUS, and the distinction is the point. Identities archive by setting
`status = ARCHIVED` (IdentityStatus is closer to pool membership than to a lifecycle), but a
BOOKING's status is the record of how it ENDED -- COMPLETED with three appointments reads very
differently from STOPPED or ABORTED, and that is exactly the information an operator scrolling old
runs wants. Overwriting it with ARCHIVED would destroy the one fact archiving is meant to preserve.

Orthogonal also means the state machine is untouched: no new BookingStatus value, no new legal
transitions, and `plan_booking_actions` keeps seeing the same statuses it always has. Archiving is
purely a visibility flag.

NULL = not archived, which every existing row is.

Revision ID: cl000bkarchive
Revises: ck000portalhealth
"""
import sqlalchemy as sa
from alembic import op

revision = "cl000bkarchive"
down_revision = "ck000portalhealth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("bookings") as batch_op:
        batch_op.add_column(sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("bookings") as batch_op:
        batch_op.drop_column("archived_at")

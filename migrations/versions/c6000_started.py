"""Add bookings.started_at (booking-level timeout anchor, replaces removed per-worker hold timeout).

Revision ID: c6000started
Revises: c3000configs
"""
import sqlalchemy as sa
from alembic import op

revision = "c6000started"
down_revision = "c3000configs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("bookings", "started_at")

"""Add bookings.slot_open_fired_at / slot_found_fired_at (confirmed-send signal latch).

Revision ID: c7000signal
Revises: c6000started
"""
import sqlalchemy as sa
from alembic import op

revision = "c7000signal"
down_revision = "c6000started"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("slot_open_fired_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bookings", sa.Column("slot_found_fired_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("bookings", "slot_found_fired_at")
    op.drop_column("bookings", "slot_open_fired_at")

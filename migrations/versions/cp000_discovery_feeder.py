"""Add bookings.discovery_feeder_booking_ids — cross-booking slot injection.

Operator idea 2026-10-01: a real booking runs dozens of apply workers on precious authed sessions
that cannot be stopped/restarted cheaply. A separate DETECTOR booking with disposable fake
identities CAN be freely fixed and restarted; when its scouts discover slots, those
{date, period, posto} are a PORTAL FACT -- bookable by anyone warmed on that posto -- so the Hub
mirrors them into the main booking's pool and fires its slot-open/slot-found: the main's idle scouts
start discovering and its apply workers start booking, with no restart of the precious fleet.

A JSON list of feeder (detector) booking ids stored on the MAIN booking. NULL/empty = no feed, which
is every existing row, so behaviour is unchanged until an operator links a detector. One-directional;
see apps/hub/scheduler.py::_mirror_feeder_slots (posto must match; dedupe is by the main pool's
existing rows, so a reverted-CLOSED phantom is never re-injected).

Revision ID: cp000feederfeed
Revises: co000tpldefault
"""
import sqlalchemy as sa
from alembic import op

revision = "cp000feederfeed"
down_revision = "co000tpldefault"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("bookings") as batch_op:
        batch_op.add_column(sa.Column("discovery_feeder_booking_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("bookings") as batch_op:
        batch_op.drop_column("discovery_feeder_booking_ids")

"""Add slots.source — 'scout' (discovered) vs 'blind' (operator-seeded for blind apply).

Blind apply (operator direction 2026-09-18): when the operator has confirmed BY EYE that the portal
shows availability but our discovery finds nothing -- /slots is on record as under-reporting, and
the calendar path manufactures phantoms under load -- the operator seeds the slot pool with the
(date x period) grid and lets the existing apply machinery try each combination. A blind slot that
books is real by evidence; one that bounces enough times retires.

The column exists because three pieces of machinery must treat a blind slot differently from a
discovered one, and none of them can tell the two apart without it:
  * phases.py       -- form_stale on a blind slot means "next combination", never "re-warm and
                       retry the same one", and never the re-login escalation that costs sessions;
  * events.py       -- a bounce on a blind slot counts toward THAT SLOT's retirement (there is no
                       discovery miss-count to retire it otherwise);
  * scheduler.py    -- blind bounces are excluded from the false-slot-open revert, which would
                       otherwise wipe the whole pool after one sweep of a mostly-phantom grid.

NOT NULL, default 'scout': every existing row was discovered.

Revision ID: cm000slotsource
Revises: cl000bkarchive
"""
import sqlalchemy as sa
from alembic import op

revision = "cm000slotsource"
down_revision = "cl000bkarchive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("slots") as batch_op:
        batch_op.add_column(sa.Column("source", sa.String(length=16), nullable=False,
                                      server_default="scout"))


def downgrade() -> None:
    with op.batch_alter_table("slots") as batch_op:
        batch_op.drop_column("source")

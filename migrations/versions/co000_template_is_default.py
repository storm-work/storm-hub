"""Add worker_templates.is_default — the per-role default template for 'Auto (by role)' attach.

Operator request 2026-09-24: attaching with "Auto (by role)" was picking the OLDEST template of the
role (created_at ASC, main.py's attach_identity), which happened to be the soax-pinned "Apply w
Soax" / "Scout w Soax" -- so a booking's own proxy setting was silently overridden by the template
layer (TEMPLATE > BOOKING). The operator wants the plain "Apply"/"Scout" (no proxy definition, so the
BOOKING setting applies) to be the default instead. Oldest-wins is an accident of creation order, not
a choice; this makes the choice explicit.

At most one default per role (the make-default endpoint clears the others). NULL/False = not default,
which every existing row is -- attach falls back to the old created_at ASC pick when no row of the
role is flagged, so behaviour is unchanged until an operator marks one.

Revision ID: co000tpldefault
Revises: cn000workergeo
"""
import sqlalchemy as sa
from alembic import op

revision = "co000tpldefault"
down_revision = "cn000workergeo"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("worker_templates") as batch_op:
        batch_op.add_column(sa.Column("is_default", sa.Boolean(), nullable=False,
                                      server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("worker_templates") as batch_op:
        batch_op.drop_column("is_default")

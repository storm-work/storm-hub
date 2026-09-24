"""Add workers.proxy_geo — a per-worker exit country, bound consistently for the whole run.

Operator request 2026-09-24: bind an exit geo per worker and keep it CONSISTENT across the run
(including every self-mint exit rotation), changeable per worker from the dashboard, persisting until
the operator changes it.

Why a column, not a settings_json override. PROXY_GEOS (booking layer) is the SET a fleet spreads
across; proxy_geo (worker) is the ONE country this worker is bound to. They are different concepts,
and freezing a random pick into the worker's settings_json INPUT as a launch side effect would be
surprising and could collide with the full worker-settings editor. A dedicated nullable column is
the durable place launch reads BEFORE picking, which is what makes the binding survive relaunch.

Mechanics this leans on (already true): both providers' roll() change only the session id and keep
the -country-{cc} / -{CC}- segment, and the worker self-rolls -- so a geo baked into the initial
warm_proxy already holds for the worker's whole lifetime. This column just makes the CHOICE durable
and operator-controllable.

NULL = unbound, which every existing row is: launch then picks uniformly from the booking's
PROXY_GEOS set (if any) and freezes the pick here; an empty set stays "no geo restriction", as today.

Revision ID: cn000workergeo
Revises: cm000slotsource
"""
import sqlalchemy as sa
from alembic import op

revision = "cn000workergeo"
down_revision = "cm000slotsource"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workers") as batch_op:
        batch_op.add_column(sa.Column("proxy_geo", sa.String(16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workers") as batch_op:
        batch_op.drop_column("proxy_geo")

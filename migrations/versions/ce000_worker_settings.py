"""Add workers.settings_json: the WORKER settings layer's missing input.

Phase 3 of the proxy-architecture work (docs/engineering/DECISIONS.md D32). The settings resolver
(libs/domain/settings/resolver.py) has always correctly implemented WORKER > TEMPLATE > BOOKING >
GLOBAL precedence, and several catalog settings already declare `_WRK` (WORKER-override-eligible)
-- but nothing ever populated `layers[L.WORKER]` at launch (apps/hub/main.py's _do_launch_worker),
because `workers` had no column to source it from. `effective_settings_json` is the resolved
OUTPUT, frozen at launch; this is one of resolution's INPUTS, set at attach time.

Additive, nullable-safe default: every existing worker gets `{}`, which resolves identically to
today's behavior (an empty WORKER layer changes nothing).

Revision ID: ce000workersettings
Revises: cd000resourcetype
"""
import sqlalchemy as sa
from alembic import op

revision = "ce000workersettings"
down_revision = "cd000resourcetype"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workers") as batch_op:
        batch_op.add_column(
            sa.Column("settings_json", sa.JSON(), nullable=False, server_default="{}")
        )


def downgrade() -> None:
    with op.batch_alter_table("workers") as batch_op:
        batch_op.drop_column("settings_json")

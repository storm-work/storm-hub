"""identity group name

An operator-assigned batch/cohort label on identities, set in bulk at import (one group per file)
or per-identity, used by the identities and attach pages to filter and sort. Existing rows land on
NULL (ungrouped), so nothing is reclassified by the migration itself.

Revision ID: cq000identgroup
Revises: cp000feederfeed
"""
from alembic import op
import sqlalchemy as sa

revision = "cq000identgroup"
down_revision = "cp000feederfeed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("identities", sa.Column("group_name", sa.String(length=64), nullable=True))
    op.create_index("ix_identities_group_name", "identities", ["group_name"])


def downgrade() -> None:
    op.drop_index("ix_identities_group_name", table_name="identities")
    op.drop_column("identities", "group_name")

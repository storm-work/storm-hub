"""identity selection priority

Higher wins. Existing rows land on the NORMAL default (100) so nothing is reordered by the
migration itself; import is what starts assigning HIGH/LOW from here on.

Revision ID: cf000identpriority
Revises: ce000workersettings
"""
from alembic import op
import sqlalchemy as sa

revision = "cf000identpriority"
down_revision = "ce000workersettings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("identities",
                  sa.Column("priority", sa.Integer(), nullable=False, server_default="100"))
    op.create_index("ix_identities_priority", "identities", ["priority"])


def downgrade() -> None:
    op.drop_index("ix_identities_priority", table_name="identities")
    op.drop_column("identities", "priority")

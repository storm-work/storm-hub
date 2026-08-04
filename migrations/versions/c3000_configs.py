"""Add the configs table (editable form_defaults / quest_steps JSON).

Revision ID: c3000configs
Revises: c2000proxy
"""
import sqlalchemy as sa
from alembic import op

revision = "c3000configs"
down_revision = "c2000proxy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "configs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="uq_config_key"),
    )


def downgrade() -> None:
    op.drop_table("configs")

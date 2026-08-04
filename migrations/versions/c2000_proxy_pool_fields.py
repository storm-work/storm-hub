"""Add proxy pool-management fields: is_healthy + score.

Populated with defaults on import (is_healthy=true, score=0) for the ISP/SOAX pools.

Revision ID: c2000proxy
Revises: b1000partial
"""
import sqlalchemy as sa
from alembic import op

revision = "c2000proxy"
down_revision = "b1000partial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("proxies", sa.Column("is_healthy", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("proxies", sa.Column("score", sa.Float(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("proxies", "score")
    op.drop_column("proxies", "is_healthy")

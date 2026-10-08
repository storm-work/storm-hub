"""rotating pools (named rotating-proxy credential entries)

A rotating pool is ONE credential (a soax package or a webshare account) from which workers mint
unlimited session ids -- one row per account, never per session (the ci000droprot principle holds).
Reverses the 2026-09-16 env-only rule for the multi-account case: the fixed env-prefix model could
hold only one account per pool kind. Secrets live in `credentials` (JSONB), never returned by the
read API and never logged; the Hub resolves them at mint time and the minted URL carries them to the
DB-free worker. Bookings select a pool by PROXY_PROVIDER="pool:<name>"; built-in env pools remain the
default, so nothing already running is reclassified.

Revision ID: cr000rotpools
Revises: cq000identgroup
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "cr000rotpools"
down_revision = "cq000identgroup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    json_type = postgresql.JSONB if bind.dialect.name == "postgresql" else sa.JSON
    op.create_table(
        "rotating_pools",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False, server_default="rotating"),
        sa.Column("credentials", json_type(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("name", name="uq_rotating_pools_name"),
    )


def downgrade() -> None:
    op.drop_table("rotating_pools")

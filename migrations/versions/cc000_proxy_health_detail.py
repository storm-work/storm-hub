"""Widen proxies.health so a probe failure is actually diagnosable.

The column was String(24) and apply_result() truncates to match, so every real failure reason was
cut off mid-word. What the pool actually recorded, 2026-08-20, for 600 soax/mobile rows:

    355  'ok'
    158  'Error: Page.goto: net::E'      <- the net::ERR_* code, the ONLY useful part, is gone
     75  'TimeoutError: Page.goto:'
     12  'RuntimeError: browser_na'

245 of 600 proxies were in a failed state and there was no way to tell from the DB whether that was
ERR_TUNNEL_CONNECTION_FAILED (provider refusing the destination), ERR_TIMED_OUT (slow exit),
ERR_PROXY_CONNECTION_FAILED (dead session), or something else -- and those have completely different
remedies. 160 chars fits a full Playwright error line without turning the column into a log.

Revision ID: cc000healthdetail
Revises: cb000proxygeo
"""
import sqlalchemy as sa
from alembic import op

revision = "cc000healthdetail"
down_revision = "cb000proxygeo"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("proxies") as batch_op:
        batch_op.alter_column("health", type_=sa.String(length=160), existing_nullable=True)


def downgrade() -> None:
    # Truncate first: values written since the upgrade can exceed 24 chars, and Postgres refuses to
    # narrow a column whose data would not fit.
    op.execute("UPDATE proxies SET health = substring(health from 1 for 24) WHERE health IS NOT NULL")
    with op.batch_alter_table("proxies") as batch_op:
        batch_op.alter_column("health", type_=sa.String(length=24), existing_nullable=True)

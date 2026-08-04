"""Partial unique indexes enforcing two domain invariants that model metadata can't express.

1. Single active OPERATE session host-wide (§12c / R2-C7): at most one user_sessions row with
   purpose='operate' AND status='active'.
2. Identity active-allocation exclusivity (C2): an identity has at most one un-released
   allocation (released_at IS NULL) at a time — its exclusive active-booking lock.

Both use the portable `CREATE UNIQUE INDEX ... WHERE ...` partial-index form (Postgres + SQLite).

Revision ID: b1000partial
Revises: a9f93f220a52
"""
from alembic import op

revision = "b1000partial"
down_revision = "a9f93f220a52"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX uq_single_active_operate "
        "ON user_sessions (purpose) "
        "WHERE purpose = 'operate' AND status = 'active'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_identity_active_allocation "
        "ON identity_allocations (identity_id) "
        "WHERE released_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_identity_active_allocation")
    op.execute("DROP INDEX IF EXISTS uq_single_active_operate")

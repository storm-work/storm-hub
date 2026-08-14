"""Add workers.sub_account_id -- which sub-account a live worker is actually logged in with,
kept in sync by do_login()'s sticky-then-rotate loop (2026-08-14, sub-account rotation).

Revision ID: c9000workersubaccount
Revises: c8000identnotes
"""
import sqlalchemy as sa
from alembic import op

revision = "c9000workersubaccount"
down_revision = "c8000identnotes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workers") as batch_op:
        batch_op.add_column(sa.Column("sub_account_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_workers_sub_account_id", "sub_accounts", ["sub_account_id"], ["id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("workers") as batch_op:
        batch_op.drop_constraint("fk_workers_sub_account_id", type_="foreignkey")
        batch_op.drop_column("sub_account_id")

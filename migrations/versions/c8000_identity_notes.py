"""Add identities.comment / identities.target_posto (dashboard identity-table columns requested
2026-08-05, never landed before the accident -- see conversation_extract msg #222).

Revision ID: c8000identnotes
Revises: c7000signal
"""
import sqlalchemy as sa
from alembic import op

revision = "c8000identnotes"
down_revision = "c7000signal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("identities", sa.Column("comment", sa.String(length=512), nullable=True))
    # valid for real identities only -- enforced at the application layer, not the DB
    op.add_column("identities", sa.Column("target_posto", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("identities", "target_posto")
    op.drop_column("identities", "comment")

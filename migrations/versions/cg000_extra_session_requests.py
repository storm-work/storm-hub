"""extra session requests

A viewer's request for a second concurrent VIEW session, past the 1-active-session-per-viewer
cap. Mirrors ops_handover_requests' shape (requester + status enum), reusing the same
HandoverStatus enum -- granting this does NOT end any session, unlike a handover grant.

Revision ID: cg000extrasession
Revises: cf000identpriority
"""
from alembic import op
import sqlalchemy as sa

revision = "cg000extrasession"
down_revision = "cf000identpriority"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "extra_session_requests",
        sa.Column("paused_session_id", sa.String(length=36), nullable=False),
        sa.Column("requester_user_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.Enum("PENDING", "GRANTED", "DENIED", name="handoverstatus",
                                    native_enum=False, length=32), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["paused_session_id"], ["user_sessions.id"]),
        sa.ForeignKeyConstraint(["requester_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("extra_session_requests")

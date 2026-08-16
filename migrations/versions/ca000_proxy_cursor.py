"""Add proxies.last_used_at -- the proxy-pool cursor.

_candidate_query ordered by (score DESC, created_at ASC): fully deterministic, so every lease walked
the pool from the SAME starting point. The first few proxies were leased and re-probed over and
over while the rest of a 1000-strong pool sat untouched, and a failing candidate stayed at the head
of the queue -- which is why the "no healthy proxy after probing 5 candidate(s)" error kept naming
the identical five UUIDs. After a health reset it is worse still: every score is 0.0, so the order
collapses to pure creation order.

Least-recently-used ordering IS the cursor, and a better one than an explicit index: it needs no
cursor row to keep in sync, it is transactional with the lease itself, and it is inherently
cross-process safe -- which an in-process cursor is not (storm-auto's app.cli and app.scout each
kept their own and handed out identical proxy slots; see the proxy-cursor-cross-process-collision
note).

Revision ID: ca000proxycursor
Revises: c9000workersubaccount
"""
import sqlalchemy as sa
from alembic import op

revision = "ca000proxycursor"
down_revision = "c9000workersubaccount"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("proxies") as batch_op:
        batch_op.add_column(sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True))
    # NULL sorts first under NULLS FIRST, so every existing proxy is "never used" and the pool is
    # walked once end-to-end before anything repeats -- the fair starting state.
    op.create_index("ix_proxies_pool_cursor", "proxies",
                    ["provider", "kind", "last_used_at"])


def downgrade() -> None:
    op.drop_index("ix_proxies_pool_cursor", table_name="proxies")
    with op.batch_alter_table("proxies") as batch_op:
        batch_op.drop_column("last_used_at")

"""Add proxies.portal_ok / portal_health / checked_at — the second health tier.

Operator requirement 2026-09-16: "we will need 2 tier healthy check - one with normal ping and
another with reachability check to the portal."

WHY THE TIERS NEED SEPARATE COLUMNS rather than one `health` string. They answer different
questions and only one of them is a verdict about the proxy:

  tier 1 (is_healthy / health)  -- can this proxy carry traffic at all? Probed against a NEUTRAL
      url. This is the liveness verdict, it gates selection (D33, static rows only), and it is what
      the dashboard's DELETE action keys off.
  tier 2 (portal_ok / portal_health) -- can this IP reach the portal right now? Probed with a real
      browser against the portal. DIAGNOSTIC ONLY. It must never gate selection or deletion.

Collapsing them is not a style question; it is the 2026-08-28 incident. While the portal throttled,
4,489 rows flipped to is_healthy=False and 73 of 120 workers never launched. Re-checked against a
neutral url the condemned rows connected 20/20 — "the flag was recording the portal, not the proxy."
Survivable then, because the rows were regenerable strings. Not survivable now: these are purchased,
individually-owned ISP IPs and the operator can delete them from this page.

`checked_at` is separate from `last_used_at` on purpose — that column is the lease cursor (LRU
selection order) and stamping it from a manual probe would reorder the pool as a side effect of
looking at it.

Revision ID: ck000portalhealth
Revises: cj000poolname
"""
import sqlalchemy as sa
from alembic import op

revision = "ck000portalhealth"
down_revision = "cj000poolname"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("proxies") as batch_op:
        batch_op.add_column(sa.Column("portal_ok", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("portal_health", sa.String(length=160), nullable=True))
        batch_op.add_column(sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("proxies") as batch_op:
        batch_op.drop_column("checked_at")
        batch_op.drop_column("portal_health")
        batch_op.drop_column("portal_ok")

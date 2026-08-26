"""Add proxies.resource_type: rotating | static.

Phase 3 of the proxy-architecture work (docs/engineering/DECISIONS.md D32): the pool has never
distinguished a rotating session template (soax/webshare today -- the exit behind a row moves on
its own, a failed probe says nothing about the row) from a static/ISP resource (a genuinely finite,
individually-owned IP, where a failed probe DOES mean the row is bad). `kind` was free text and
never enforced this distinction at all.

Every existing row becomes 'rotating' -- there is no static inventory in this system yet
(PROXY_CONTRACTS.md: "we own none"), so nothing else is true of any row that exists today. This is
additive and changes no existing selection behavior: libs/proxies/lease.py's health-as-non-filter
rule (D19) is now scoped to resource_type='rotating' rows specifically, which is every row that
exists at the time of this migration.

Revision ID: cd000resourcetype
Revises: cc000healthdetail
"""
import sqlalchemy as sa
from alembic import op

revision = "cd000resourcetype"
down_revision = "cc000healthdetail"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("proxies") as batch_op:
        batch_op.add_column(
            sa.Column("resource_type", sa.String(length=16), nullable=False,
                     server_default="rotating")
        )
        batch_op.create_check_constraint(
            "ck_proxies_resource_type", "resource_type IN ('rotating', 'static')"
        )


def downgrade() -> None:
    with op.batch_alter_table("proxies") as batch_op:
        batch_op.drop_constraint("ck_proxies_resource_type", type_="check")
        batch_op.drop_column("resource_type")

"""Add proxies.pool_name — named ISP pools.

Operator requirement 2026-09-16: "when I purchased 10 ISP proxies from Thordata I need to add those
to the DB on the dashboard ... if that is a new [name] then a new pool will be created, or if an
existing one, 10 proxies will be added to that pool." And then: a booking must be able to draw from
SEVERAL pools at once, because one purchase is routinely smaller than the worker count.

A POOL IS A NAME ON A ROW, not a table. Pools are discovered with `SELECT DISTINCT pool_name`, so
"create or append" needs no create step and no lifecycle: importing under a new name creates the
pool by definition, importing under an existing one appends. The alternative -- a `proxy_pools`
table with a foreign key -- buys referential integrity and per-pool metadata we have no use for,
at the cost of a join on the lease hot path and an orphan-pool cleanup problem. Revisit only if a
pool ever needs attributes of its own (a cost, a contract expiry, a vendor contact).

NULL means "unnamed", and every existing row stays NULL. That is deliberate rather than
backfilling something like 'default': a pool filter treats NULL as belonging to no named pool, so
existing rows keep being selected exactly as before by callers that pass no filter, and are never
accidentally swept into a named pool a booking asked for.

Scoped to ISP/static in practice, not by constraint. Rotating proxies have no rows at all since
ci000droprot, so nothing else can carry this column today.

Revision ID: cj000poolname
Revises: ci000droprot
"""
import sqlalchemy as sa
from alembic import op

revision = "cj000poolname"
down_revision = "ci000droprot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("proxies") as batch_op:
        batch_op.add_column(sa.Column("pool_name", sa.String(length=64), nullable=True))
    # Selection filters on (resource_type, pool_name) on the lease path, and the dashboard groups
    # by pool_name for the pool tree. Cheap index, and the ISP table is small -- but the lease path
    # is latency-sensitive in a way the old rotating pool never was, because a static lease is a
    # real allocation a worker blocks on.
    op.create_index("ix_proxies_pool_name", "proxies", ["pool_name"])


def downgrade() -> None:
    op.drop_index("ix_proxies_pool_name", table_name="proxies")
    with op.batch_alter_table("proxies") as batch_op:
        batch_op.drop_column("pool_name")

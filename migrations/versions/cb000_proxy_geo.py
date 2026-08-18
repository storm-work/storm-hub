"""Add proxies.geo -- the country a proxy exits from, so leasing can be restricted to the geos that
actually clear the login wall.

Measured over 1392 real login attempts (2026-08-18), success rate by exit geo:

    DE 23.7%   IT 23.4%   PT 10.8%   NL 10.6%   ES 8.1%   LU 4.9%   CZ 2.9%

An 8x spread, and the pool was being spent almost backwards: CZ took the MOST attempts (277) at the
WORST rate, DE the fewest (152) at the best. Restricting to DE+IT roughly doubles the expected login
rate with no other change.

Backfilled from credentials_ref, where the provider encodes it (`sodade2026-DE-17`). NULL means
"geo unknown" (soax's credential format carries none) and is never excluded by a filter -- a geo
filter narrows the pool, it must not silently empty it.

Revision ID: cb000proxygeo
Revises: ca000proxycursor
"""
import sqlalchemy as sa
from alembic import op

revision = "cb000proxygeo"
down_revision = "ca000proxycursor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("proxies") as batch_op:
        batch_op.add_column(sa.Column("geo", sa.String(length=8), nullable=True))
    # Backfill from the credential, e.g. "sodade2026-DE-17" -> "DE". Deliberately a plain UPDATE
    # rather than a Python loop: it runs once, against every row, inside the migration's own
    # transaction.
    op.execute(
        r"""
        UPDATE proxies
           SET geo = upper(substring(credentials_ref from '-([A-Za-z]{2})-'))
         WHERE credentials_ref IS NOT NULL
           AND credentials_ref ~ '-[A-Za-z]{2}-'
        """
        if op.get_bind().dialect.name == "postgresql" else
        # SQLite (tests) has no regex; the credential shape is fixed enough to slice.
        r"""
        UPDATE proxies
           SET geo = upper(substr(credentials_ref,
                                  instr(credentials_ref, '-') + 1, 2))
         WHERE credentials_ref LIKE '%-__-%'
        """
    )
    op.create_index("ix_proxies_geo_cursor", "proxies", ["provider", "geo", "last_used_at"])


def downgrade() -> None:
    op.drop_index("ix_proxies_geo_cursor", table_name="proxies")
    with op.batch_alter_table("proxies") as batch_op:
        batch_op.drop_column("geo")

"""sub-account registration device

Records where a sub-account's registration last succeeded: browser backend, Donut profile name, a hash
of the device fingerprint and the exit country. Booking pins the account to that country and checks the
device hash at login. Existing rows land on NULL (registered before this was recorded), so nothing is
reclassified by the migration itself.

Revision ID: cs000subacctdevice
Revises: cr000rotpools
"""
from alembic import op
import sqlalchemy as sa

revision = "cs000subacctdevice"
down_revision = "cr000rotpools"
branch_labels = None
depends_on = None

_COLS = (
    ("browser_backend", sa.String(length=16)),
    ("donut_profile", sa.String(length=32)),
    ("fingerprint_hash", sa.String(length=32)),
    ("registered_geo", sa.String(length=2)),
    ("fingerprint_recorded_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    for name, typ in _COLS:
        op.add_column("sub_accounts", sa.Column(name, typ, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_COLS):
        op.drop_column("sub_accounts", name)

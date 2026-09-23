"""rename worker substate 'holding' -> 'keeping'

HOLDING never meant "a warmed session is being kept alive" -- it means the AUTHENTICATED session
is. Since the auth-only keepalive consolidation (2026-09-09) and deferred warmup, a worker in this
state is by definition NOT warmed: warmup happens on the slot-open (scout) / slot-found (apply)
signal. The old name was actively misleading in live triage, so the enum value is renamed.

Data-only: rewrites any rows still carrying the old value. `substate` is a plain VARCHAR with a
non-native enum (see libs/db/models.py's _enum), so there is no PG enum type to ALTER.

Revision ID: ch000keeping
Revises: cg000extrasession
"""
from alembic import op

revision = "ch000keeping"
down_revision = "cg000extrasession"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE workers SET substate = 'keeping' WHERE substate = 'holding'")


def downgrade() -> None:
    op.execute("UPDATE workers SET substate = 'holding' WHERE substate = 'keeping'")

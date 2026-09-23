"""drop the rotating proxy rows -- workers mint their own session ids now

Operator direction 2026-09-15: rotating pools stop being DB inventory. A rotating row was never an
exit; it was a session TEMPLATE, and the session id inside it is a string we choose. The codebase
had been saying so for months without acting on it (libs/proxies/lease.py:153, D19,
libs/proxies/health.py:121-140): "There is no row-level subject to screen. All 32,600 rows are
resource_type='rotating' and resolve to TWO credentials on TWO endpoints."

What the rows cost us, all measured this week: 46-124s of Hub proxy-rotation latency per rotation,
the proxy_request/wait_proxy round-trip that saturated a 4-thread executor, one DB-pool consumer
that fed the audit deadlock, a 42,600-row table that collapsed the Proxies page, and 19,983 soax
rows carrying is_healthy=false condemnations that mean nothing about a per-session exit.

What settled the one objection (Q2, "does an established session id beat a fresh one?"): arithmetic,
not experiment. Our soax params are sessionlength=3600 / idlettl=900, and 0 of 42,600 rows were last
used inside EITHER window (10,010 never used; 32,590 last used > 1h ago). No row held a live
provider-side session, so lease_proxy's tier 1 ("established, verdict unknown") was ranking 32,590
rows on a property none of them had. The pool was not supplying established sessions to lose.

STATIC/ISP rows are deliberately untouched. Those are finite, individually-owned IPs where a
per-row health verdict has a real subject -- that is the whole reason the leasing system survives.

Irreversible by design: downgrade() does NOT fake a restore. The rows were generated, never
authored, and scripts/gen_proxy_pool.py still regenerates them -- see downgrade()'s note.

Revision ID: ci000droprot
Revises: ch000keeping
"""
from alembic import op

revision = "ci000droprot"
down_revision = "ch000keeping"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Leases first: proxy_leases.proxy_id is a live FK, and deleting the parent rows out from under
    # it is exactly the 500 that the Detach bug produced (release_proxy never deleted the lease row).
    # 10,927 lease rows existed at authoring time, 0 of them unreleased, so nothing in flight is lost.
    #
    # A plain `<> 'static'` is exhaustive here and needs no NULL guard: cd000_proxy_resource_type
    # added the column as NOT NULL with server_default='rotating' AND a check constraint
    # `resource_type IN ('rotating','static')`, so every row holds exactly one of two values. (It
    # also has to be plain SQL: `IS DISTINCT FROM` is Postgres-only, and these migrations must run
    # on the SQLite bootstrap path too -- verified by running this on a scratch SQLite DB.)
    op.execute(
        "DELETE FROM proxy_leases WHERE proxy_id IN "
        "(SELECT id FROM proxies WHERE resource_type <> 'static')"
    )
    # registration_jobs.proxy_id is the SECOND foreign key into proxies, and the first attempt at
    # this migration missed it -- it failed on the live DB with
    #   ForeignKeyViolation: ... violates foreign key constraint "registration_jobs_proxy_id_fkey"
    # (1,041 of 1,060 jobs referenced a rotating proxy). The transaction rolled back cleanly, but
    # the lesson is to enumerate referencing tables from pg_constraint rather than from memory.
    #
    # NULLed, not deleted, and the distinction matters: a registration_job is a HISTORICAL RECORD of
    # a real sub-account registration attempt, while a proxy_lease is transient allocation
    # bookkeeping. Deleting 1,041 job rows to satisfy an FK would destroy the registration history
    # to tidy up a proxy pool. The column is nullable and 19 rows already hold NULL, so "no proxy
    # recorded" is a shape this table and its readers already handle.
    op.execute(
        "UPDATE registration_jobs SET proxy_id = NULL WHERE proxy_id IN "
        "(SELECT id FROM proxies WHERE resource_type <> 'static')"
    )
    op.execute("DELETE FROM proxies WHERE resource_type <> 'static'")


def downgrade() -> None:
    """Deliberately a no-op.

    There is nothing to restore: these rows carried no state worth preserving (the session ids were
    randomly generated, and by the TTL arithmetic above none of them referenced a live provider
    session). Writing a downgrade that re-inserted 42,600 fabricated rows would be a lie that
    reinstated the latency and the dead health verdicts.

    To genuinely revert this change, re-run scripts/gen_proxy_pool.py in its rotating mode, which is
    how the deleted rows were produced in the first place and is kept working for exactly this
    reason.
    """

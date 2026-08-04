"""Offline schema smoke test: the ORM metadata is self-consistent and creates cleanly.

Runs against in-memory SQLite (no Postgres needed) — proves every FK target exists, no duplicate
table/constraint names, and the enum columns build. The real PG schema is produced by Alembic.
"""
from sqlalchemy import create_engine, inspect

from libs.db import metadata


EXPECTED_TABLES = {
    "users", "user_sessions", "ops_handover_requests",
    "identities", "sub_accounts", "identity_allocations", "identity_events",
    "proxies", "proxy_leases",
    "bookings", "booking_identities",
    "worker_templates", "template_versions",
    "workers", "worker_runs",
    "slots", "slot_leases",
    "registration_jobs", "booked_appointments", "artifacts",
    "settings", "audit",
}


def test_metadata_creates_on_sqlite_and_has_all_tables():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    tables = set(inspect(engine).get_table_names())
    missing = EXPECTED_TABLES - tables
    assert not missing, f"missing tables: {missing}"


def test_no_orphan_foreign_keys():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    insp = inspect(engine)
    names = set(insp.get_table_names())
    for t in names:
        for fk in insp.get_foreign_keys(t):
            assert fk["referred_table"] in names, f"{t} -> {fk['referred_table']} missing"

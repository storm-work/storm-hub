"""One-off remediation for the 2026-09-25 stuck-ISP state (booking 0924-3018):

  1. Release proxy leases still held by NON-RUNNING (STOPPED/FAILED/READY) workers -- the stop-leak
     bug left finite ISP IPs leased forever.
  2. Un-condemn static/ISP proxy rows falsely marked is_healthy=false by the portal-throttle
     false-condemnation (the whole capsolver pool went unhealthy while the IPs were fine).

Safe to re-run (idempotent): a second run releases 0 leases and un-condemns 0 rows. Prints
before/after counts. This fixes the DATA on the running Hub; the CODE fixes that stop it recurring
ship with the next deploy.

    cd /srv/projects/storm-hub && .venv/bin/python scripts/remediate_stuck_isp.py
"""
import os
import sys
from datetime import datetime, timezone

# Make `apps`/`libs` importable no matter what directory this is run from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from apps.hub.db import SessionLocal  # noqa: E402


def _counts(d):
    leaked = d.execute(text(
        "select count(*) from proxy_leases l join workers w on w.id=l.worker_id "
        "where l.released_at is null and w.status != 'RUNNING'")).scalar()
    condemned = d.execute(text(
        "select count(*) from proxies where resource_type='static' and is_healthy=false")).scalar()
    return leaked, condemned


def main():
    d = SessionLocal()
    try:
        leaked, condemned = _counts(d)
        print(f"BEFORE: leaked leases (non-RUNNING workers)={leaked}  condemned static rows={condemned}")

        r1 = d.execute(text(
            "update proxy_leases set released_at=:n where released_at is null "
            "and worker_id in (select id from workers where status != 'RUNNING')"),
            {"n": datetime.now(timezone.utc)})
        r2 = d.execute(text(
            "update proxies set is_healthy=true, health='ok' "
            "where resource_type='static' and is_healthy=false"))
        d.commit()
        print(f"ACTION: leases released={r1.rowcount}  ISP rows un-condemned={r2.rowcount}")

        leaked, condemned = _counts(d)
        print(f"AFTER:  leaked leases={leaked}  condemned static rows={condemned}")
        by_prov = d.execute(text(
            "select provider, count(*) from proxies where resource_type='static' and is_healthy=true "
            "group by provider order by provider")).all()
        print("healthy static ISP rows by provider:", [tuple(r) for r in by_prov])
    finally:
        d.close()


if __name__ == "__main__":
    main()

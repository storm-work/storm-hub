"""Backfill 'By Others' and 'PDF missing' labels on identities from PRIOR runs (the new events.py
logic only labels NEW events). Read-then-write, idempotent, prints what it changed.

  - BY OTHERS: an identity with an 'already_booked_at_portal' IdentityEvent and NO BookedAppointment
    was booked by a competitor, not us. Set status=RETIRED (raw, bypassing the state machine since
    the old code wrongly left it BOOKED) and comment='By Others ...'.
  - PDF MISSING: a BookedAppointment with pdf_artifact_id IS NULL is a booking we made but never
    captured a PDF for. Comment the identity 'PDF missing ... VERIFY on the portal'.

    cd /srv/projects/storm-hub && .venv/bin/python scripts/backfill_booking_labels.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from apps.hub.db import SessionLocal  # noqa: E402

_BY_OTHERS = ("By Others — portal reports this applicant already holds an appointment; "
              "not booked by us (no BookedAppointment).")
_PDF_MISSING = ("PDF missing — submit confirmed the booking but no PDF was captured; "
                "VERIFY this appointment on the portal.")


def main():
    d = SessionLocal()
    try:
        # BY OTHERS: already_booked event, no appointment of ours.
        others = d.execute(text(
            "select distinct ie.identity_id from identity_events ie "
            "where ie.step = 'already_booked_at_portal' "
            "and not exists (select 1 from booked_appointments ba where ba.identity_id = ie.identity_id)"
        )).scalars().all()
        n_others = d.execute(text(
            "update identities set status='retired', comment=:c "
            "where id = any(:ids) and (comment is null or comment not like 'By Others%')"),
            {"c": _BY_OTHERS, "ids": list(others)}).rowcount if others else 0

        # PDF MISSING: our booking, no pdf_artifact.
        pdfless = d.execute(text(
            "select distinct identity_id from booked_appointments where pdf_artifact_id is null"
        )).scalars().all()
        n_pdf = d.execute(text(
            "update identities set comment=:c "
            "where id = any(:ids) and (comment is null or comment not like 'PDF missing%')"),
            {"c": _PDF_MISSING, "ids": list(pdfless)}).rowcount if pdfless else 0

        d.commit()
        print(f"BY OTHERS: {len(others)} candidate identities, {n_others} relabelled -> RETIRED")
        print(f"PDF MISSING: {len(pdfless)} appointments without a PDF, {n_pdf} identities flagged")
    finally:
        d.close()


if __name__ == "__main__":
    main()

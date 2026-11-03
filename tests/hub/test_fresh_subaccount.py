"""FRESH_SUBACCOUNT_PER_RUN: on Start, mint one brand-new sub-account per attached identity and
queue a registration run for them (apps.hub.main._fresh_subaccounts_on_start). The scheduler-side
launch-when-verified watchdog is covered in test_scheduler_resilience.py."""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import apps.hub.main as hub_main
from libs.db import metadata
from libs.db import models as M
from libs.domain.enums import BookingStatus, SubAccountStatus, WorkerRole, WorkerStatus


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = Session()
    yield s
    s.close()


@pytest.fixture()
def spy_registration(monkeypatch):
    """Capture the subs handed to the registration run instead of spawning a registrar."""
    calls = []

    def _fake(db, subs, **kw):
        calls.append((list(subs), kw))
        return "reg-test", len(subs)

    monkeypatch.setattr(hub_main, "_spawn_registration_run", _fake)
    return calls


def _booking(db, fresh=True):
    bk = M.Booking(name="b", type="real", posto_id="5084", status=BookingStatus.WARMING,
                   settings_json=({"FRESH_SUBACCOUNT_PER_RUN": True} if fresh else {}))
    db.add(bk)
    db.commit()
    return bk


def _identity(db, name):
    ident = M.Identity(full_name=name, type="real")
    db.add(ident)
    db.commit()
    return ident


def _worker(db, bk, ident, status=WorkerStatus.READY):
    w = M.Worker(booking_id=bk.id, identity_id=ident.id, role=WorkerRole.APPLY, status=status)
    db.add(w)
    db.commit()
    return w


def test_off_by_default_mints_nothing(db, spy_registration):
    bk = _booking(db, fresh=False)
    _worker(db, bk, _identity(db, "Ana Silva"))

    assert hub_main._fresh_subaccounts_on_start(db, bk) == 0
    assert spy_registration == []
    assert db.scalar(select(M.SubAccount)) is None


def test_mints_one_fresh_account_per_distinct_identity(db, spy_registration):
    bk = _booking(db)
    a, b = _identity(db, "Ana Silva"), _identity(db, "Bob Costa")
    _worker(db, bk, a)
    _worker(db, bk, a)          # two workers on the SAME identity -> still ONE fresh account
    _worker(db, bk, b)

    minted = hub_main._fresh_subaccounts_on_start(db, bk)

    assert minted == 2
    subs = db.scalars(select(M.SubAccount)).all()
    assert len(subs) == 2
    assert {s.status for s in subs} == {SubAccountStatus.NEW}
    assert {s.identity_id for s in subs} == {a.id, b.id}
    assert all((s.notes or "").startswith("fresh-per-run") for s in subs)
    # the freshly-minted (not pre-existing) subs are exactly what gets registered
    (passed, _kw), = spy_registration
    assert {s.id for s in passed} == {s.id for s in subs}


def test_mints_even_when_identity_has_an_old_verified_account(db, spy_registration):
    bk = _booking(db)
    a = _identity(db, "Ana Silva")
    _worker(db, bk, a)
    db.add(M.SubAccount(identity_id=a.id, username="old-ana", password="p",
                        status=SubAccountStatus.VERIFIED))
    db.commit()

    minted = hub_main._fresh_subaccounts_on_start(db, bk)

    assert minted == 1                          # a brand-new account regardless of the old one
    fresh = db.scalars(select(M.SubAccount).where(M.SubAccount.status == SubAccountStatus.NEW)).all()
    assert len(fresh) == 1 and fresh[0].username != "old-ana"
    (passed, _kw), = spy_registration
    assert [s.id for s in passed] == [fresh[0].id]   # only the fresh one is queued


def test_no_identity_bound_workers_mints_nothing(db, spy_registration):
    bk = _booking(db)                           # fresh mode on, but no workers
    assert hub_main._fresh_subaccounts_on_start(db, bk) == 0
    assert spy_registration == []

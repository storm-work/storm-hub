import time

import pytest

from libs.engine import checkpoint as ckpt


@pytest.fixture(autouse=True)
def _isolated_checkpoints_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ckpt, "_CHECKPOINTS_DIR", tmp_path)


def test_save_and_load_roundtrip():
    ckpt.save_checkpoint(
        "identity-1", cookies={"Vistos_sid": "SID1"}, sub_account_id="sub-1", stage="schedule_jsp",
        posto_pdf="5084",
    )
    data = ckpt.load_checkpoint("identity-1")
    assert data["identity_id"] == "identity-1"
    assert data["sub_account_id"] == "sub-1"
    assert data["cookies"] == {"Vistos_sid": "SID1"}
    assert data["stage"] == "schedule_jsp"
    assert data["posto_pdf"] == "5084"


def test_load_missing_returns_none():
    assert ckpt.load_checkpoint("nope") is None


def test_keyed_by_identity_not_sub_account():
    # bug fix #5: a worker owns one identity for its whole lifetime but can pick a different sibling
    # sub-account on restore -- the checkpoint must be found by identity_id regardless of which
    # sub-account was active when it was saved.
    ckpt.save_checkpoint("identity-1", cookies={}, sub_account_id="sub-A")
    ckpt.save_checkpoint("identity-1", cookies={}, sub_account_id="sub-B")   # same identity, new sub
    data = ckpt.load_checkpoint("identity-1")
    assert data["sub_account_id"] == "sub-B"   # latest-wins overwrite, not versioned per sub-account
    assert ckpt.load_checkpoint("sub-A") is None   # never keyed by sub-account
    assert ckpt.load_checkpoint("sub-B") is None


def test_save_always_overwrites_latest_wins():
    ckpt.save_checkpoint("identity-1", cookies={"a": "1"}, sub_account_id="s", stage="login")
    ckpt.save_checkpoint("identity-1", cookies={"a": "2"}, sub_account_id="s", stage="schedule_jsp")
    data = ckpt.load_checkpoint("identity-1")
    assert data["cookies"] == {"a": "2"}
    assert data["stage"] == "schedule_jsp"


def test_cookies_full_and_extra_metadata_preserved():
    ckpt.save_checkpoint(
        "identity-1", cookies={"a": "1"}, sub_account_id="s",
        cookies_full=[{"name": "a", "value": "1", "httpOnly": True}],
        on_hold_pool=True, hold_proxy="http://proxy",
    )
    data = ckpt.load_checkpoint("identity-1")
    assert data["cookies_full"][0]["name"] == "a"
    assert data["on_hold_pool"] is True
    assert data["hold_proxy"] == "http://proxy"


def test_clear_checkpoint():
    ckpt.save_checkpoint("identity-1", cookies={}, sub_account_id="s")
    ckpt.clear_checkpoint("identity-1")
    assert ckpt.load_checkpoint("identity-1") is None
    ckpt.clear_checkpoint("identity-1")   # idempotent, no error on missing file


def test_cleanup_stale_by_age(tmp_path):
    ckpt.save_checkpoint("old-identity", cookies={}, sub_account_id="s")
    ckpt.save_checkpoint("fresh-identity", cookies={}, sub_account_id="s")
    old_path = tmp_path / "old-identity.json"
    old_mtime = time.time() - 10 * 86400   # 10 days old
    import os
    os.utime(old_path, (old_mtime, old_mtime))

    removed = ckpt.cleanup_stale(max_age_days=7)
    assert removed == 1
    assert ckpt.load_checkpoint("old-identity") is None
    assert ckpt.load_checkpoint("fresh-identity") is not None


def test_cleanup_stale_by_active_set():
    ckpt.save_checkpoint("identity-1", cookies={}, sub_account_id="s")
    ckpt.save_checkpoint("identity-2", cookies={}, sub_account_id="s")
    removed = ckpt.cleanup_stale(active_identity_ids={"identity-1"})
    assert removed == 1
    assert ckpt.load_checkpoint("identity-1") is not None
    assert ckpt.load_checkpoint("identity-2") is None


def test_cleanup_stale_empty_dir_is_noop():
    assert ckpt.cleanup_stale() == 0

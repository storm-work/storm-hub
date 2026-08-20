#!/usr/bin/env python3
"""Offline, assert-driven validation of the checkpoint on-disk contract (risk F11).

WHY THIS EXISTS. The reconstruction plan's F11 is not about a bug that happened here -- it is about
how the predecessor project lost four days. In the final captured minutes before that incident, the
checkpoint KEYING SCHEME was being redesigned live, against a running booking, with no dry run and
no way to confirm whether the migration had worked. The rule that came out of it:

    any checkpoint format or keying change ships as offline, assert-driven, reversible code FIRST,
    validated against fixtures, before it ever touches a live booking.

storm-auto has exactly this in probes/checkpoint_save_probe.py and checkpoint_resume_probe.py. This
file is the storm-hub equivalent, and it had been missing -- so a future format change had no safe
path. It writes ONLY into a temporary directory it creates and removes; it never reads, writes or
deletes anything under var/checkpoints/, and it touches no portal, no proxy and no database.

Run it before and after ANY change to libs/engine/checkpoint.py:

    .venv/bin/python probes/checkpoint_format_probe.py

Exit 0 = the contract still holds. Exit 1 = you have changed the format; decide deliberately whether
that is intended, and write the migration before deploying it.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from libs.engine import checkpoint as ckpt  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {label}")
    else:
        FAILURES.append(f"{label}{' -- ' + detail if detail else ''}")
        print(f"  FAIL  {label}{' -- ' + detail if detail else ''}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ckpt-probe-"))
    original = ckpt._CHECKPOINTS_DIR
    ckpt._CHECKPOINTS_DIR = tmp          # isolate: never touch the real var/checkpoints
    try:
        print(f"checkpoint format probe -- isolated dir {tmp}\n")

        # ── 1. keying ─────────────────────────────────────────────────────────
        # IDENTITY_ID, not sub_account_id. This is the exact scheme that was being changed live when
        # the predecessor project was lost, so it is asserted first and explicitly.
        ident = "id-11111111-2222-3333-4444-555555555555"
        sub = "sub-aaaa-bbbb"
        ckpt.save_checkpoint(ident, cookies={"Vistos_sid": "abc"}, sub_account_id=sub)
        files = list(tmp.glob("*"))
        check("keyed by identity_id", any(ident in f.name for f in files),
              f"files={[f.name for f in files]}")
        check("sub_account_id is NOT the filename key", not any(sub in f.name for f in files))

        # ── 2. round trip ─────────────────────────────────────────────────────
        got = ckpt.load_checkpoint(ident)
        check("load returns the saved payload", got is not None)
        check("identity_id round-trips", got and got.get("identity_id") == ident)
        check("sub_account_id round-trips", got and got.get("sub_account_id") == sub,
              "a restore must know WHICH sub-account the cookies belong to")
        check("cookies round-trip", got and got.get("cookies") == {"Vistos_sid": "abc"})
        check("stage defaults to 'login'", got and got.get("stage") == "login")
        check("saved_at is a timestamp", got and isinstance(got.get("saved_at"), (int, float)))

        # ── 3. the bookable-context marker ────────────────────────────────────
        ckpt.save_checkpoint(ident, cookies={"Vistos_sid": "x"}, sub_account_id=sub,
                             stage="schedule_jsp", posto_pdf="3018")
        got = ckpt.load_checkpoint(ident)
        check("stage='schedule_jsp' round-trips", got and got.get("stage") == "schedule_jsp",
              "this marker is what tells a restore the session is already bookable")
        check("posto_pdf round-trips", got and got.get("posto_pdf") == "3018",
              "the submit URL must use the posto the schedule page advertised, not posto_id")

        # ── 4. extras stored verbatim ─────────────────────────────────────────
        ckpt.save_checkpoint(ident, cookies={}, sub_account_id=sub,
                             cookies_full=[{"name": "Vistos_sid", "value": "v", "domain": "d"}],
                             on_hold_pool=True, hold_proxy="host:1", nationality="CPV")
        got = ckpt.load_checkpoint(ident)
        check("cookies_full round-trips", got and isinstance(got.get("cookies_full"), list)
              and got["cookies_full"][0]["name"] == "Vistos_sid",
              "the full Playwright dict list is what a browser-inject resume needs")
        check("**extra is stored verbatim",
              got and got.get("on_hold_pool") is True and got.get("hold_proxy") == "host:1"
              and got.get("nationality") == "CPV")

        # ── 5. latest-wins, no versioning ─────────────────────────────────────
        ckpt.save_checkpoint(ident, cookies={"gen": "1"}, sub_account_id=sub)
        ckpt.save_checkpoint(ident, cookies={"gen": "2"}, sub_account_id=sub)
        got = ckpt.load_checkpoint(ident)
        check("overwrite is latest-wins", got and got["cookies"] == {"gen": "2"})
        check("no stray files accumulate per save", len(list(tmp.glob("*.json"))) == 1,
              f"files={[f.name for f in tmp.glob('*')]}")
        check("the atomic tmp file never survives a save", not list(tmp.glob("*.tmp")),
              "save is tmp-write + rename; a leftover .tmp means the rename path changed")

        # ── 6. failure modes are survivable ───────────────────────────────────
        check("missing checkpoint returns None", ckpt.load_checkpoint("no-such-identity") is None)
        corrupt = ckpt._path("corrupt-identity")
        corrupt.write_text("{not valid json", encoding="utf-8")
        check("corrupt checkpoint returns None, never raises",
              ckpt.load_checkpoint("corrupt-identity") is None,
              "a half-written file must not be able to crash a worker launch")

        ckpt.clear_checkpoint(ident)
        check("clear removes the file", ckpt.load_checkpoint(ident) is None)
        ckpt.clear_checkpoint("no-such-identity")     # must not raise
        check("clearing a missing checkpoint is a no-op", True)

        # ── 7. cleanup_stale ──────────────────────────────────────────────────
        keep, drop = "id-keep", "id-drop"
        ckpt.save_checkpoint(keep, cookies={}, sub_account_id="s1")
        ckpt.save_checkpoint(drop, cookies={}, sub_account_id="s2")
        removed = ckpt.cleanup_stale(max_age_days=7, active_identity_ids={keep})
        check("cleanup_stale drops identities not in the active set", removed >= 1)
        check("cleanup_stale keeps an active identity", ckpt.load_checkpoint(keep) is not None,
              "dropping a LIVE identity's checkpoint would force a needless re-login")
        check("cleanup_stale removed the inactive one", ckpt.load_checkpoint(drop) is None)

        old = "id-old"
        ckpt.save_checkpoint(old, cookies={}, sub_account_id="s3")
        p = ckpt._path(old)
        ancient = time.time() - (30 * 86400)
        import os
        os.utime(p, (ancient, ancient))
        removed = ckpt.cleanup_stale(max_age_days=7)
        check("cleanup_stale drops files older than max_age_days", removed >= 1)
        check("the aged file is gone", ckpt.load_checkpoint(old) is None)

        print()
        if FAILURES:
            print(f"FAILED ({len(FAILURES)}):")
            for f in FAILURES:
                print(f"  - {f}")
            print("\nThe on-disk checkpoint contract has changed. If that is intended, write the "
                  "migration and validate it here BEFORE deploying to a running booking.")
            return 1
        print("all assertions passed -- the checkpoint contract is unchanged")
        return 0
    finally:
        ckpt._CHECKPOINTS_DIR = original
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

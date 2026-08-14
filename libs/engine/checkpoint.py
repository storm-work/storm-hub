"""Worker session checkpoints -- storage layer only.

Direct port of storm-auto's proven `app/core/session_store.py` (confirmed read in full: atomic
tmp-write+rename JSON files, per-account, with a `cleanup_stale()` retention sweep), with two
deliberate changes driven by confirmed post-mortem findings:

1. Keyed by `identity_id`, not sub_account/username (bug fix #5, confirmed corrected 2026-08-07: a
   worker owns one identity for its whole lifetime, but the picker can default to a different sibling
   sub-account on restore -- keying by identity_id means the worker always finds its own checkpoint
   regardless of which sub-account it was using). `sub_account_id`/`username` are recorded IN the
   checkpoint payload so restore adopts the exact account that was checkpointed, not just "any
   verified sub-account for this identity."
2. Storage stays on the **local filesystem** (`var/checkpoints/`), never Redis. This is a deliberate,
   explicit decision, not an oversight: checkpoint payloads carry full cookie jars and metadata that
   have no business inflating the Redis working set, and file-based storage is what's actually proven
   in production (storm-auto, 113 confirmed bookings) -- see the reconstruction plan's Root Cause
   Analysis for why Redis growth specifically must stay bounded.

Health/liveness CLASSIFICATION (alive vs dead vs down) requires a live browser probe and deliberately
does NOT live here -- that's apps/worker/ops_live.py's job (LiveEngineOps), keeping this module and
libs/engine/phases.py's PhaseMachine checkpoint-agnostic. This module is pure storage: save the latest
known-healthy state, load it back, list/delete/expire it.

Write policy: always overwrite with the latest state (no versioning/append-only) -- matches
storm-auto's `save()` semantics and the recovered design note ("keep checkpoints as the latest one
whenever healthy and valid").
"""
from __future__ import annotations

import json
import time
from pathlib import Path

_CHECKPOINTS_DIR = Path(__file__).resolve().parents[2] / "var" / "checkpoints"


def _path(identity_id: str) -> Path:
    _CHECKPOINTS_DIR.mkdir(parents=True, exist_ok=True)
    return _CHECKPOINTS_DIR / f"{identity_id}.json"


def save_checkpoint(
    identity_id: str,
    *,
    cookies: dict,
    sub_account_id: str,
    stage: str = "login",
    cookies_full: list | None = None,
    posto_pdf: str | None = None,
    **extra,
) -> None:
    """Save the latest known-healthy state for this identity. Always overwrites (latest-wins, no
    versioning) -- call this only after confirming the state being saved is actually healthy.

    `cookies`: an explicit URL-scoped jar (name->value) -- pass the BROWSER context's jar
    (get_all_cookies-equivalent), not a possibly-stale secondary client's, mirroring storm-auto's
    confirmed fix (Gap 3: cookies must come from the authoritative browser jar).
    `cookies_full`: the full Playwright cookie-dict list (all domains, httpOnly) for a browser-inject
    resume, when available.
    `stage`: `"login"` or `"schedule_jsp"` (bookable-context checkpoint), matching storm-auto's markers.
    `**extra`: any additional metadata worth carrying (e.g. posto_id, nationality, residence,
    on_hold_pool, hold_proxy) -- stored verbatim, retrievable via load_checkpoint.
    """
    data = {
        "identity_id": identity_id,
        "sub_account_id": sub_account_id,
        "cookies": cookies,
        "stage": stage,
        "posto_pdf": posto_pdf,
        "saved_at": time.time(),
        **extra,
    }
    if cookies_full:
        data["cookies_full"] = cookies_full
    tmp = _path(identity_id).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(_path(identity_id))


def load_checkpoint(identity_id: str) -> dict | None:
    """Return the full saved checkpoint dict, or None if none exists / it's corrupt."""
    p = _path(identity_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def clear_checkpoint(identity_id: str) -> None:
    p = _path(identity_id)
    if p.exists():
        p.unlink()


def cleanup_stale(max_age_days: int = 7, active_identity_ids: set[str] | None = None) -> int:
    """Remove checkpoint files that are either:
    - older than max_age_days (stale, from released/archived identities), or
    - not in active_identity_ids if that set is provided (unknown/no-longer-tracked identities).

    Returns the number of files removed. Call periodically from the Hub (never manually via a shell
    `rm` -- see the reconstruction plan's Phase F8: an ad hoc, unscoped manual cleanup command is
    exactly what caused this project's original catastrophic data loss. This function is the sanctioned,
    reviewed, scoped replacement for that reflex, ported directly from storm-auto's own
    `session_store.cleanup_stale()`, which already existed there for the same reason.)
    """
    if not _CHECKPOINTS_DIR.exists():
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for f in _CHECKPOINTS_DIR.glob("*.json"):
        try:
            if active_identity_ids is not None and f.stem not in active_identity_ids:
                f.unlink()
                removed += 1
                continue
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed

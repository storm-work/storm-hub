"""Pre-window readiness check. Read-only; exits 1 if anything FAILs.

    python -m scripts.window_preflight              # everything below
    python -m scripts.window_preflight --create-test  # also create+delete ONE throwaway Donut profile

Checks: Hub up and NEWER than the last commit (workers import the working tree, the Hub does not),
Redis, the Donut app and API, profile coverage of the usable sub-accounts, the effective settings that
matter in a hot window, every live booking's own overrides (a stored KEEPALIVE_MODE silently beats the
default), solver keys, PortalPulse, disk and load.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

os.environ.setdefault("PYTHONWARNINGS", "ignore")

import apps.hub.config  # noqa: F401
import redis
from sqlalchemy import select, text

from apps.hub.config import settings
from apps.hub.db import SessionLocal
from apps.hub.settings_store import get_global
from libs.db import models as M
from libs.domain.enums import SettingsLayer as L
from libs.domain.settings import resolve

FAILS = 0


def say(level: str, msg: str) -> None:
    global FAILS
    if level == "FAIL":
        FAILS += 1
    print(f"[{level:4}] {msg}")


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception:
        return ""


def main() -> int:
    with SessionLocal() as db:
        g = get_global(db)
        eff = resolve({L.GLOBAL: g})

        # ── Hub: up, and running the committed code ──
        try:
            import httpx
            h = httpx.get("http://127.0.0.1:8123/health", timeout=5).json()
            say("PASS" if h.get("status") == "ok" else "FAIL", f"Hub health: {h}")
        except Exception as e:
            say("FAIL", f"Hub not answering: {e}")
        started = _run(["systemctl", "show", "storm-hub", "-p", "ActiveEnterTimestamp", "--value"])
        head = _run(["git", "log", "-1", "--format=%ct"])
        try:
            st = datetime.strptime(started.replace(" UTC", ""), "%a %Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc).timestamp()
            say("PASS" if st > int(head) else "FAIL",
                f"Hub started {started}; last commit {datetime.fromtimestamp(int(head), timezone.utc):%F %T} UTC"
                + ("" if st > int(head) else "  -> RESTART the Hub: it does not run the latest commit"))
        except Exception:
            say("WARN", f"could not compare Hub start ({started!r}) with the last commit")
        dirty = _run(["git", "status", "--porcelain", "--untracked-files=no"])
        say("PASS" if not dirty else "WARN",
            "working tree clean" if not dirty else "uncommitted edits go LIVE on worker respawn:\n" + dirty)

        # ── Redis ──
        try:
            redis.from_url(settings.redis_url).ping()
            say("PASS", "Redis answers")
        except Exception as e:
            say("FAIL", f"Redis: {e}")

        # ── Donut ──
        usable_profiles = None
        if str(g.get("BROWSER_BACKEND", "")).lower() == "donut":
            try:
                import libs.engine.vendor  # noqa: F401
                import donut
                api = donut._Api(url=g.get("DONUT_API_URL") or "", token=g.get("DONUT_API_TOKEN") or "")
                profs = donut._items(api.call("GET", "/v1/profiles", timeout=10), "profiles")
                names = {p.get("name") for p in profs}
                say("PASS", f"Donut API answers; {len(profs)} profiles, "
                            f"{sum(1 for p in profs if p.get('is_running'))} running")
                if "--create-test" in sys.argv:
                    import uuid
                    try:
                        r = api.call("POST", "/v1/profiles", {
                            "name": "storm-tmp-preflight-" + uuid.uuid4().hex[:8], "browser": "wayfern",
                            "version": "latest", "wayfern_config": {"os": "windows"}, "ephemeral": True})
                        pid = (r.get("profile") or r).get("id")
                        api.call("DELETE", f"/v1/profiles/{pid}")
                        say("PASS", "Donut can CREATE a profile (quota not blocking)")
                    except Exception as e:
                        say("FAIL", f"Donut cannot create a profile: {str(e)[:120]}  "
                                    "(restart the Donut app if support lifted a block)")
                rows = db.execute(text(
                    "select i.id, s.username from identities i join sub_accounts s on s.identity_id=i.id "
                    "where i.status='ACTIVE' and s.status in ('VERIFIED','LOGIN_OK','HELD')")).all()
                by: dict = {}
                for iid, u in rows:
                    by.setdefault(iid, []).append(donut.profile_name(u) in names)
                usable_profiles = sum(any(v) for v in by.values())
                say("PASS", f"ACTIVE identities with a ready sub-account: {len(by)}; with a Donut profile: "
                            f"{usable_profiles}")
            except Exception as e:
                say("FAIL", f"Donut: {str(e)[:140]}")
        else:
            say("WARN", f"BROWSER_BACKEND={g.get('BROWSER_BACKEND')!r} (not donut)")

        # ── settings that matter in the window ──
        for k in ("BROWSER_BACKEND", "KEEPALIVE_MODE", "HYBRID_MODE", "PRIMP_IMPERSONATE", "PORTAL_PULSE",
                  "DONUT_EXISTING_PROFILES_ONLY", "REGISTRAR_BROWSER_BACKEND", "DONUT_PROFILES_PER_HOUR",
                  "QUEST_HOLD_MIN_S", "QUEST_HOLD_MAX_S"):
            print(f"       {k} = {eff.get(k)!r}")
        if eff.get("DONUT_EXISTING_PROFILES_ONLY") and usable_profiles is not None:
            say("WARN", "DONUT_EXISTING_PROFILES_ONLY is ON: sub-accounts without a profile are skipped "
                        "(fine while Donut blocks creation; turn OFF once it works)")
        if not eff.get("PORTAL_PULSE"):
            say("WARN", "PORTAL_PULSE is off: no fleet-wide up/down record for the window")
        if g.get("REGISTRAR_BROWSER_BACKEND"):
            say("WARN", f"REGISTRAR_BROWSER_BACKEND={g.get('REGISTRAR_BROWSER_BACKEND')!r} is set")

        # ── solver keys ──
        caps = [k for k in (g.get("CAPSOLVER_KEYS", "") or "").split(",") if k.strip()]
        anti = [k for k in (g.get("ANTICAPTCHA_KEYS", "") or "").split(",") if k.strip()]
        say("PASS" if caps or anti else "FAIL", f"solver keys: capsolver={len(caps)} anticaptcha={len(anti)}")

        # ── live bookings ──
        live = db.scalars(select(M.Booking).where(
            M.Booking.archived_at.is_(None), M.Booking.status.in_(
                ["SCHEDULED", "WARMING", "ARMED", "RUNNING", "DRAFT"]))).all()
        if not live:
            say("WARN", "no draft/scheduled/running booking exists yet")
        for b in live:
            ov = dict(b.settings_json or {})
            n_id = db.execute(text("select count(*) from booking_identities where booking_id=:b"),
                              {"b": b.id}).scalar()
            say("PASS", f"booking {b.name!r} [{b.status}] posto={b.posto_id} {b.nationality}/{b.residence} "
                        f"slot_open={b.slot_open_at} warmup_start={b.warmup_start_at} identities={n_id}")
            if ov:
                print(f"       overrides: {ov}")
            if ov.get("KEEPALIVE_MODE") not in (None, "quest_hold"):
                say("WARN", f"  {b.name!r} pins KEEPALIVE_MODE={ov['KEEPALIVE_MODE']!r} (not the default)")

        # ── host ──
        free = shutil.disk_usage("/srv").free / 1e9
        say("PASS" if free > 10 else "FAIL", f"disk free on /srv: {free:.0f} GB")
        load = os.getloadavg()[0]
        say("PASS" if load < 8 else "WARN", f"load1={load:.1f}")
        mem = _run(["free", "-g"]).splitlines()
        if len(mem) > 1:
            print("       " + " ".join(mem[1].split()))
    print("\nPREFLIGHT: " + ("FAIL" if FAILS else "OK"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())

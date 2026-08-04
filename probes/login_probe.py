"""Live login probe — validates the ported BrowserSession.login against the real portal.

Uses storm-hub's DB pools + global settings exactly as the engine will:
  - CAPSOLVER_KEYS from global settings (set it in the dashboard first)
  - a healthy SOAX proxy from the DB pool (warm/login role)

Usage:
  cd /srv/projects/storm-hub
  .venv/bin/python probes/login_probe.py <username> <password> [--pool soax|isp] [--headed]

  --pool  which proxy pool to log in through (default: soax = the login/warmup role).
          Pass --pool isp to test login on the ISP pool instead.

Makes ONE real login attempt (no booking). Run it yourself — it touches the live portal.
"""
import os
import random
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))


def _load_env():
    env = _ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    username, password = sys.argv[1], sys.argv[2]
    headed = "--headed" in sys.argv
    pool = "soax"
    if "--pool" in sys.argv:
        pool = sys.argv[sys.argv.index("--pool") + 1].lower()

    _load_env()
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from libs.db import models as M
    from libs.engine.session import BrowserSession
    from libs.proxies import conn_url

    eng = create_engine(os.environ["DATABASE_URL"])
    with Session(eng) as db:
        keys = [k.strip() for k in
                (db.scalar(select(M.SettingRow.value).where(M.SettingRow.key == "CAPSOLVER_KEYS")) or "").split(",")
                if k.strip()]
        if not keys:
            print("ERROR: set CAPSOLVER_KEYS in the dashboard (Settings -> Global settings) first")
            return 1
        candidates = db.scalars(
            select(M.Proxy).where(M.Proxy.provider == pool, M.Proxy.active.is_(True),
                                  M.Proxy.is_healthy.is_(True))
        ).all()
        if not candidates:
            print(f"ERROR: no healthy {pool.upper()} proxy in the pool (import one under Proxies)")
            return 1
        proxy = conn_url(random.choice(candidates))

    print(f"[probe] login {username}  via {pool.upper()}  keys={len(keys)}")
    s = BrowserSession(proxy=proxy, headless=not headed, field_retries=4,
                       nav_timeout_scale=3).start()
    try:
        res = s.login(username, password, keys, lang="PT", min_score=0)
        import json as _j
        diag = res.pop("diag", None)
        print(f"[probe] RESULT: {res}")
        if diag:
            print("[probe] PAGE DIAG:\n" + _j.dumps(diag, indent=2, ensure_ascii=False))
        print("[probe]", "LOGIN OK ✓" if res.get("ok") else "login NOT confirmed")
        return 0 if res.get("ok") else 1
    finally:
        s.close()


if __name__ == "__main__":
    raise SystemExit(main())

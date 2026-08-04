"""Live login via the VENDORED storm-auto session (proven bd.js-passing engine).

Uses storm-hub's DB pools + global-settings capsolver keys, but the browser is storm-auto's proven
session.py (which the A/B showed passes DataDome where the clean rewrite did not).

Usage:
  cd /srv/projects/storm-hub
  .venv/bin/python probes/login_vendored.py <username> <password> [--pool soax|isp] [--tries N] [--headed]

One real login attempt per try (no booking). Operator-run — touches the live portal.
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
    pool = sys.argv[sys.argv.index("--pool") + 1].lower() if "--pool" in sys.argv else "soax"
    tries = int(sys.argv[sys.argv.index("--tries") + 1]) if "--tries" in sys.argv else 3
    headed = "--headed" in sys.argv

    _load_env()
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from libs.db import models as M
    from libs.proxies import conn_url

    eng = create_engine(os.environ["DATABASE_URL"])
    with Session(eng) as db:
        keys = [k.strip() for k in
                (db.scalar(select(M.SettingRow.value).where(M.SettingRow.key == "CAPSOLVER_KEYS")) or "").split(",")
                if k.strip()]
        if not keys:
            print("ERROR: set CAPSOLVER_KEYS in the dashboard first")
            return 1
        cands = db.scalars(select(M.Proxy).where(M.Proxy.provider == pool, M.Proxy.active.is_(True),
                                                 M.Proxy.is_healthy.is_(True))).all()
        if not cands:
            print(f"ERROR: no healthy {pool.upper()} proxy")
            return 1
        proxies = [conn_url(p) for p in random.sample(cands, min(tries, len(cands)))]

    # capsolver keys via env too (the vendored solver also reads env in places)
    os.environ.setdefault("CAPSOLVER_KEYS", ",".join(keys))
    os.environ["HEADED"] = "1" if headed else "0"
    from libs.engine.vendor import get_session
    import json as _j

    for i, proxy in enumerate(proxies, 1):
        print(f"[probe] attempt {i}/{len(proxies)}  vendored login {username}  via {pool.upper()}")
        s = None
        try:
            s = get_session(proxy, headless=not headed)
            res = s.browser_login(username, password, lang="PT", capsolver_keys=keys, timeout=180)
            body = (res.get("body") or "") if isinstance(res, dict) else str(res)
            status = res.get("status") if isinstance(res, dict) else "?"
            try:
                parsed = _j.loads(body.strip())
            except Exception:
                parsed = {}
            ptype = parsed.get("type", "")
            ok = (status == 200 and ptype in ("", "200", "success")) or "terminar" in body.lower()
            print(f"[probe]   status={status} type={ptype!r} body_len={len(body)}  body[:160]={body[:160]!r}")
            if ok:
                print("[probe] LOGIN OK ✓ (vendored engine cleared bd.js + reCAPTCHA)")
                return 0
        except Exception as e:
            print(f"[probe]   error: {type(e).__name__}: {str(e)[:160]}")
        finally:
            if s is not None:
                try:
                    s.close_browser()
                except Exception:
                    pass
    print(f"[probe] login NOT confirmed after {len(proxies)} attempts")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

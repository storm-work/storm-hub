"""Hub configuration — loads storm-hub/.env (no dependency) into a small settings object."""
from __future__ import annotations

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv() -> None:
    env = _ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


class Settings:
    def __init__(self) -> None:
        _load_dotenv()
        self.database_url = os.environ.get("DATABASE_URL", "sqlite:///./_bootstrap.db")
        self.redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        # dev fallback secret keeps tests running without a .env; prod supplies JWT_SECRET.
        self.jwt_secret = os.environ.get("JWT_SECRET", "dev-insecure-secret-change-me")
        self.jwt_ttl_hours = int(os.environ.get("JWT_TTL_HOURS", "12"))
        # /docs, /redoc, /openapi.json disclose the full API schema unauthenticated -- closed by
        # default now that the Hub is reachable over the public internet; opt in for local dev.
        self.enable_api_docs = os.environ.get("ENABLE_API_DOCS", "0") == "1"


settings = Settings()

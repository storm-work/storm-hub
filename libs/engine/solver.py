"""reCAPTCHA solver — capsolver client, ported from storm-auto's confirmed solver.

The portal uses reCAPTCHA Enterprise (v2 checkbox). Confirmed policy (storm-auto memory):
  - login  -> ProxyLess task (capsolver's own IP)
  - register-> proxy-aware task (solve through the session's residential exit) when enabled

`score_token` estimates the Enterprise score from token length (shorter = auto-pass = higher score),
used to prefer better tokens. Network calls use httpx; the pure helpers are unit-tested.
"""
from __future__ import annotations

import re
import time

import httpx

SITE_KEY = "6LdOB9crAAAAADT4RFruc5sPmzLKIgvJVfL830d4"
AUTH_URL = "https://pedidodevistos.mne.gov.pt/VistosOnline/Authentication.jsp"
SCHED_URL = "https://pedidodevistos.mne.gov.pt/VistosOnline/Schedule.jsp"
POLL_DELAY = 3
MAX_POLLS = 60  # ~3 min

_ACTION_URL = {"SCHEDULE_EVISA": SCHED_URL}
_CREATE = "https://api.capsolver.com/createTask"
_RESULT = "https://api.capsolver.com/getTaskResult"


class SolverError(RuntimeError):
    pass


def score_token(token: str) -> int:
    """Estimate the Enterprise score (0-100) from token length; shorter = auto-pass = higher."""
    n = len(token or "")
    if n == 0:
        return 0
    if n < 2320:
        return 100
    if n < 2380:
        return 85
    if n < 2430:
        return 70
    if n < 2470:
        return 55
    if n < 2510:
        return 40
    if n < 2550:
        return 25
    return 10


def parse_proxy_for_capsolver(proxy_url: str | None) -> dict | None:
    """http://user:pass@host:port -> capsolver proxy_info dict (for proxy-aware register solving)."""
    if not proxy_url:
        return None
    m = re.match(r"^(https?)://(?:([^:@/]+):([^@/]+)@)?([^:/@]+):(\d+)/?$", proxy_url)
    if not m:
        return None
    scheme, user, pw, host, port = m.groups()
    return {"proxyType": scheme, "proxyAddress": host, "proxyPort": port,
            "proxyLogin": user or "", "proxyPassword": pw or ""}


def _build_task(action: str, proxy_info: dict | None) -> dict:
    page_url = _ACTION_URL.get(action, AUTH_URL)
    if proxy_info:
        return {
            "type": "ReCaptchaV2EnterpriseTask",
            "websiteURL": page_url,
            "websiteKey": SITE_KEY,
            "proxy": (f"{proxy_info['proxyType']}:{proxy_info['proxyAddress']}:{proxy_info['proxyPort']}"
                      f":{proxy_info.get('proxyLogin', '')}:{proxy_info.get('proxyPassword', '')}"),
        }
    return {"type": "ReCaptchaV2EnterpriseTaskProxyLess", "websiteURL": page_url, "websiteKey": SITE_KEY}


def _poll(api_key: str, task_id: str) -> str:
    with httpx.Client(timeout=30) as c:
        for i in range(MAX_POLLS):
            time.sleep(POLL_DELAY)
            data = c.post(_RESULT, json={"clientKey": api_key, "taskId": task_id}).json()
            if data.get("errorId", 0) != 0:
                raise SolverError(f"poll error: {data.get('errorDescription', data)}")
            if data.get("status") == "ready":
                return data["solution"]["gRecaptchaResponse"]
    raise SolverError(f"solver timeout after {MAX_POLLS * POLL_DELAY}s")


def _capsolver(api_key: str, action: str, proxy_info: dict | None) -> str:
    with httpx.Client(timeout=30) as c:
        data = c.post(_CREATE, json={"clientKey": api_key, "task": _build_task(action, proxy_info)}).json()
    task_id = data.get("taskId")
    if not task_id:
        raise SolverError(f"createTask failed: {data.get('errorDescription', data)}")
    return _poll(api_key, task_id)


def solve(keys: list[str], *, action: str = "LOGIN_EVISA", proxy_info: dict | None = None,
          min_score: int = 0) -> str:
    """Solve with capsolver, trying keys until a token meets `min_score` (or return the best seen).
    Raises SolverError if no key produces a token."""
    if not keys:
        raise SolverError("no capsolver keys configured")
    best = ""
    for key in keys:
        try:
            token = _capsolver(key, action, proxy_info)
        except SolverError:
            continue
        if score_token(token) >= min_score:
            return token
        if score_token(token) > score_token(best):
            best = token
    if best:
        return best
    raise SolverError("all capsolver keys failed")

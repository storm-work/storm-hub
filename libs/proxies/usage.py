"""Provider account-usage clients -- how much of a purchased data plan is left.

This is DISTINCT from the proxy credentials a pool carries (PREFIX/PASSWORD, PACKAGE/PASSWORD): those
authenticate the proxy itself, and say nothing about the account's remaining data allowance. Bandwidth
lives behind each provider's ACCOUNT API, reached with a separate account token the operator stores as
a secret setting (WEBSHARE_API_TOKEN), never in the pool's proxy credentials and never committed.

Webshare API v2 (apidocs.webshare.io):
  * limit -- GET /api/v2/subscription/plan/   -> each plan's `bandwidth_limit` (GB; 0 = unlimited)
  * used  -- GET /api/v2/stats/aggregate/?timestamp__gte=&timestamp__lte=  -> `bandwidth_total` (bytes),
             aggregated over the window; we query from the current billing period's start.
  * auth  -- header `Authorization: Token <WEBSHARE_API_TOKEN>`
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Webshare bills bandwidth in GB = 10^9 bytes (decimal), matching how `bandwidth_limit` is expressed.
_BYTES_PER_GB = 1_000_000_000
_WS_BASE = "https://proxy.webshare.io/api/v2"
_TIMEOUT = 15.0


def billing_period_start(billing_day: int, *, now: datetime | None = None) -> datetime:
    """The most recent occurrence of `billing_day` (day-of-month the plan renews), as the start of the
    current bandwidth period. Clamped to 1..28 so it exists in every month. E.g. billing_day=15 on
    Oct 9 -> Sep 15; on Oct 20 -> Oct 15."""
    now = now or datetime.now(timezone.utc)
    day = max(1, min(28, int(billing_day or 1)))
    if now.day >= day:
        start = now.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
    else:
        prev = (now.replace(day=1) - timedelta(days=1))   # last day of previous month
        start = prev.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
    return start


def webshare_usage(token: str, *, billing_day: int = 1,
                   now: datetime | None = None) -> dict:
    """Remaining data for a Webshare account. Returns a dict with:
        ok, limit_gb, used_gb, remaining_gb, unlimited, period_start (iso), error.
    Never raises -- a provider/network failure comes back as {ok: False, error: ...} so the endpoint
    can render a degraded widget rather than 500."""
    token = (token or "").strip()
    if not token:
        return {"ok": False, "error": "WEBSHARE_API_TOKEN is not set"}
    import httpx
    now = now or datetime.now(timezone.utc)
    start = billing_period_start(billing_day, now=now)
    headers = {"Authorization": f"Token {token}"}
    try:
        with httpx.Client(timeout=_TIMEOUT, headers=headers) as c:
            # LIMIT: sum bandwidth_limit across the account's plans (0 on any plan => unlimited).
            pr = c.get(f"{_WS_BASE}/subscription/plan/")
            if pr.status_code == 401:
                return {"ok": False, "error": "Webshare rejected the API token (401)"}
            pr.raise_for_status()
            plans = (pr.json() or {}).get("results") or []
            limits = [float(p.get("bandwidth_limit") or 0) for p in plans]
            unlimited = any(l == 0 for l in limits) or not limits
            limit_gb = None if unlimited else round(sum(limits), 2)

            # USED: aggregate bandwidth over the current billing period (bytes).
            sr = c.get(f"{_WS_BASE}/stats/aggregate/",
                       params={"timestamp__gte": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                               "timestamp__lte": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
            sr.raise_for_status()
            agg = sr.json() or {}
            # The endpoint returns a single aggregate object; tolerate a list (sum it) defensively.
            if isinstance(agg, list):
                used_bytes = sum(float((a or {}).get("bandwidth_total") or 0) for a in agg)
            else:
                used_bytes = float(agg.get("bandwidth_total") or 0)
    except Exception as e:  # noqa: BLE001 -- degrade, never 500 the widget
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}

    used_gb = round(used_bytes / _BYTES_PER_GB, 2)
    remaining_gb = None if (limit_gb is None) else round(max(0.0, limit_gb - used_gb), 2)
    return {"ok": True, "unlimited": unlimited, "limit_gb": limit_gb, "used_gb": used_gb,
            "remaining_gb": remaining_gb, "period_start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "error": None}

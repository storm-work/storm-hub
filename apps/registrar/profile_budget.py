"""Keep registration inside Donut's new-profile quota.

Donut (Pro plan) allows 100 new profiles per hour; going over triggers a 24-hour block
(WAYFERN_GENERATION_LIMIT_REACHED, hit on 2026-10-29 after ~236 profiles in two hours). Registering an
account creates its Donut profile on the first browser launch (retries reuse it), so each account START is
one slot in a rolling hour. The window lives in Redis so overlapping registrar runs share it.
"""
from __future__ import annotations

import asyncio
import time
import uuid

KEY = "donut:profile_creates"
WINDOW_S = 3600.0
MAX_NAP_S = 60.0


def _purge_and_count(r, now: float, window: float) -> int:
    r.zremrangebyscore(KEY, 0, now - window)
    return int(r.zcard(KEY))


async def reserve(r, cap: int, *, window: float = WINDOW_S, clock=time.time, sleep=asyncio.sleep,
                  log=print) -> float:
    """Wait until fewer than `cap` slots were taken in the last `window` seconds, then take one.
    Returns the seconds waited. cap <= 0 disables the budget."""
    if cap <= 0:
        return 0.0
    waited = 0.0
    while True:
        now = clock()
        if _purge_and_count(r, now, window) < cap:
            r.zadd(KEY, {f"{now:.3f}-{uuid.uuid4().hex[:8]}": now})
            return waited
        oldest = r.zrange(KEY, 0, 0, withscores=True)
        wait = (oldest[0][1] + window - now + 1.0) if oldest else 5.0
        nap = max(1.0, min(wait, MAX_NAP_S))
        if waited == 0.0:
            log(f"[registrar] Donut profile budget full ({cap}/h) -- waiting for a free slot")
        await sleep(nap)
        waited += nap

"""A local relay between Playwright and a Donut profile's CDP endpoint that keeps the session under
Donut's automation rate limit.

WHY. Wayfern (the browser behind a Donut profile) rate-limits automation: it answers CDP error -32000
"Automation rate limit exceeded (100 requests/minute). Retry shortly." once a profile's bucket is empty
(measured 2026-10-27: ~200 commands of burst, ~100/min sustained, per PROFILE -- 25 profiles of one
account each got their own full burst). Playwright then reports that as "Execution context was
destroyed, most likely because of a navigation", which the engine reads as a dead exit or a navigation
race: it re-anchors, rolls the proxy, re-logs-in. The limit is not an exit problem, so none of that helps.

WHAT. Only the commands that are measured to draw on the budget (Runtime.evaluate and
Runtime.callFunctionOn -- one per page.evaluate / locator query; input events and the rest are free) are
counted. They are (1) PACED by a token bucket that sits a little under the real limit, and (2) if Donut
rejects one anyway, RESENT after a short wait instead of handed to Playwright. Playwright never sees the
throttle, so no call site changes and nothing downstream misreads it. Everything else is forwarded
untouched.

The relay is one asyncio loop on its own thread per session (a worker process owns one session), bound
to 127.0.0.1 on an ephemeral port, and it carries only that session's CDP socket.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

from websockets.asyncio.client import connect as _ws_connect
from websockets.asyncio.server import serve as _ws_serve

# Measured to draw on the budget. releaseObject and the Input.* / Page.* / DOM.* families did not
# (100 mouse moves and 100 key events left the evaluate budget untouched). If another method turns out
# to count, the resend below still covers it -- only the pacing would be late.
COUNTED = ("Runtime.callFunctionOn", "Runtime.evaluate")
_RATE_MARK = "Automation rate limit exceeded"


class _Bucket:
    def __init__(self, burst: float, per_min: float):
        self.capacity = float(burst)
        self.rate = max(per_min, 0.1) / 60.0
        self.tokens = float(burst)
        self._at = time.monotonic()
        self._lock = asyncio.Lock()      # serialises waiters, so commands keep their order

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self._at) * self.rate)
        self._at = now

    async def acquire(self) -> float:
        """Wait for a token; returns the seconds spent waiting."""
        waited = 0.0
        async with self._lock:
            while True:
                self._refill()
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return waited
                gap = (1.0 - self.tokens) / self.rate
                waited += gap
                await asyncio.sleep(gap)

    def drain(self) -> None:
        """Donut just said its bucket is empty, whatever ours believed."""
        self._refill()
        self.tokens = 0.0


class CdpThrottle:
    def __init__(self, upstream_ws: str, per_min: float = 90.0, burst: int = 180,
                 retry_gap: float = 1.0, max_wait: float = 120.0, log=print):
        self.upstream = upstream_ws
        self.per_min, self.burst = per_min, burst
        self.retry_gap, self.max_wait = retry_gap, max_wait
        self._log = log
        self.stats = {"paced": 0, "waited_s": 0.0, "retried": 0, "gave_up": 0, "sent": 0}
        self.url: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle (callers are ordinary threads) -------------------------------------------------
    def start(self) -> str:
        self._thread = threading.Thread(target=self._run, name="cdp-throttle", daemon=True)
        self._thread.start()
        if not self._ready.wait(10.0) or self._error is not None or not self.url:
            raise RuntimeError(f"CDP throttle failed to start: {self._error!r}")
        return self.url

    def stop(self) -> None:
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(5.0)

    def summary(self) -> str:
        s = self.stats
        return (f"cdp throttle: {s['sent']} counted commands, {s['paced']} paced "
                f"({s['waited_s']:.1f}s waited), {s['retried']} resent after a Donut rate-limit reply, "
                f"{s['gave_up']} given up")

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as e:                       # noqa: BLE001 -- reported through start()
            self._error = e
            self._ready.set()

    async def _main(self) -> None:
        self._loop, self._stop = asyncio.get_running_loop(), asyncio.Event()
        self._bucket = _Bucket(self.burst, self.per_min)
        async with _ws_serve(self._handle, "127.0.0.1", 0, max_size=None, compression=None,
                             ping_interval=None) as server:
            port = server.sockets[0].getsockname()[1]
            self.url = f"ws://127.0.0.1:{port}/"
            self._ready.set()
            await self._stop.wait()

    # -- one Playwright connection ----------------------------------------------------------------
    async def _handle(self, client) -> None:
        async with _ws_connect(self.upstream, max_size=None, compression=None, ping_interval=None,
                               open_timeout=15) as up:
            pending: dict[int, tuple[str, float]] = {}      # id -> (raw command, first sent)

            async def resend(cid: int, raw: str) -> None:
                await asyncio.sleep(self.retry_gap)
                await self._bucket.acquire()
                if cid in pending:
                    await up.send(raw)

            async def to_browser() -> None:
                async for raw in client:
                    if isinstance(raw, str) and any(m in raw for m in COUNTED):
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            msg = None
                        if msg and msg.get("method") in COUNTED:
                            waited = await self._bucket.acquire()
                            if waited > 0:
                                self.stats["paced"] += 1
                                self.stats["waited_s"] += waited
                            self.stats["sent"] += 1
                            pending[msg["id"]] = (raw, time.monotonic())
                    await up.send(raw)

            async def to_client() -> None:
                async for raw in up:
                    if isinstance(raw, str) and _RATE_MARK in raw:
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            msg = None
                        cid = msg.get("id") if msg else None
                        if cid in pending and "error" in msg:
                            cmd, first = pending[cid]
                            if time.monotonic() - first <= self.max_wait:
                                self._bucket.drain()
                                if self.stats["retried"] == 0:
                                    self._log(f"[donut] Donut rate-limited a command the relay had "
                                              f"let through -- resending (relay allows "
                                              f"{self.per_min:g}/min, burst {self.burst}); a "
                                              f"lower DONUT_CDP_PER_MIN would avoid this")
                                self.stats["retried"] += 1
                                asyncio.create_task(resend(cid, cmd))
                                continue
                            self.stats["gave_up"] += 1
                            pending.pop(cid, None)
                    elif isinstance(raw, str) and pending:
                        # cheap id check only for replies to counted commands we are tracking
                        head = raw[:48]
                        if head.startswith('{"id":'):
                            try:
                                pending.pop(int(head[6:head.index(",", 6)]), None)
                            except ValueError:
                                pass
                    await client.send(raw)

            tasks = [asyncio.ensure_future(to_browser()), asyncio.ensure_future(to_client())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

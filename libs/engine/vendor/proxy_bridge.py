"""
Local HTTP CONNECT → SOCKS5 authenticating bridge.

Playwright/Chromium cannot authenticate with SOCKS5 proxies that require
username:password. This module runs a single asyncio event loop (daemon thread)
and creates one TCP listener per unique socks5:// URL on demand.

  start_bridge("socks5://user:pass@host:port")
      → "http://127.0.0.1:{N}"

Playwright uses the returned http:// URL as its proxy (no credentials needed —
the bridge holds them). primp uses the original socks5:// URL directly (it
handles SOCKS5 auth natively, no bridge needed).

Per-proxy listeners are cached — repeated calls for the same socks5 URL return
the same local port instantly. All listeners share one asyncio event loop so
there is no per-listener thread overhead.
"""

import asyncio
import socket
import struct
import threading
from urllib.parse import urlparse

# ── Shared asyncio infrastructure ─────────────────────────────────────────────

_loop_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None

# socks5_url → local TCP port
_bridge_lock = threading.Lock()
_bridges: dict[str, int] = {}
_servers: list = []          # keep asyncio.Server refs alive (prevent GC)


def _get_loop() -> asyncio.AbstractEventLoop:
    """Start (or return) the shared asyncio loop daemon thread."""
    global _loop, _loop_thread
    if _loop is not None:
        return _loop
    with _loop_lock:
        if _loop is not None:
            return _loop
        ready = threading.Event()

        def _run() -> None:
            global _loop
            _loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_loop)
            ready.set()
            _loop.run_forever()

        _loop_thread = threading.Thread(target=_run, daemon=True,
                                        name='socks5-bridge-loop')
        _loop_thread.start()
        ready.wait(timeout=5)
    return _loop


# ── SOCKS5 protocol ───────────────────────────────────────────────────────────

async def _socks5_handshake(
    r: asyncio.StreamReader,
    w: asyncio.StreamWriter,
    host: str,
    port: int,
    user: str,
    pwd: str,
) -> None:
    """SOCKS5 auth negotiation + CONNECT for (host, port)."""
    # Propose username/password auth (method 0x02)
    w.write(b'\x05\x01\x02')
    await w.drain()

    resp = await r.readexactly(2)
    if resp[0] != 5 or resp[1] != 2:
        raise ConnectionError(f"SOCKS5 rejected auth method: {resp!r}")

    # Sub-negotiation (RFC 1929)
    u, p = user.encode(), pwd.encode()
    w.write(bytes([1, len(u)]) + u + bytes([len(p)]) + p)
    await w.drain()

    resp = await r.readexactly(2)
    if resp[1] != 0:
        raise ConnectionError(f"SOCKS5 auth failed: {resp!r}")

    # CONNECT to target (ATYP = 0x03 domain)
    host_b = host.encode()
    w.write(bytes([5, 1, 0, 3, len(host_b)]) + host_b + struct.pack('>H', port))
    await w.drain()

    resp = await r.readexactly(4)
    if resp[1] != 0:
        raise ConnectionError(f"SOCKS5 CONNECT failed, code={resp[1]}")

    atype = resp[3]
    if atype == 1:    await r.readexactly(6)       # IPv4 + port
    elif atype == 3:
        n = (await r.readexactly(1))[0]
        await r.readexactly(n + 2)                 # domain + port
    elif atype == 4:  await r.readexactly(18)      # IPv6 + port


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await src.read(65536)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    except Exception:
        pass
    finally:
        try:
            dst.close()
        except Exception:
            pass


# ── Per-connection handler ─────────────────────────────────────────────────────

async def _handle(
    cr: asyncio.StreamReader,
    cw: asyncio.StreamWriter,
    socks_host: str,
    socks_port: int,
    socks_user: str,
    socks_pwd: str,
) -> None:
    try:
        line = (await cr.readline()).decode(errors='replace').strip()
        if not line.upper().startswith('CONNECT '):
            cw.write(b'HTTP/1.1 405 Method Not Allowed\r\n\r\n')
            cw.close()
            return

        target = line.split()[1]
        host, _, port_s = target.rpartition(':')
        port = int(port_s) if port_s.isdigit() else 443

        # Drain remaining HTTP headers
        while True:
            h = await cr.readline()
            if h in (b'\r\n', b'\n', b''):
                break

        # Connect upstream SOCKS5 and establish tunnel to target
        ur, uw = await asyncio.open_connection(socks_host, socks_port)
        await _socks5_handshake(ur, uw, host, port, socks_user, socks_pwd)

        cw.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
        await cw.drain()

        await asyncio.gather(_pipe(cr, uw), _pipe(ur, cw))

    except Exception:
        try:
            cw.write(b'HTTP/1.1 502 Bad Gateway\r\n\r\n')
            cw.close()
        except Exception:
            pass


# ── Public API ────────────────────────────────────────────────────────────────

def start_bridge(socks5_url: str) -> str:
    """
    Return "http://127.0.0.1:{port}" that tunnels CONNECT requests to socks5_url.

    One listener is created per unique socks5_url and cached permanently.
    Repeated calls for the same URL return the cached port instantly.
    All listeners share one asyncio event loop — no per-listener thread.

    Args:
        socks5_url: e.g. "socks5://Mylist1234-US-FR-DE-IT-ES-7:pass@p.webshare.io:80"

    Returns:
        Local HTTP proxy URL for Playwright. Keep the original socks5_url for primp.
    """
    # Fast path: already started
    with _bridge_lock:
        if socks5_url in _bridges:
            return f'http://127.0.0.1:{_bridges[socks5_url]}'

    parsed     = urlparse(socks5_url)
    socks_host = parsed.hostname or 'localhost'
    socks_port = parsed.port or 1080
    socks_user = parsed.username or ''
    socks_pwd  = parsed.password or ''

    loop = _get_loop()

    # Grab a free OS port while the lock is not held (bind + release immediately)
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]

    srv_ready = threading.Event()

    async def _start() -> None:
        async def _cb(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await _handle(r, w, socks_host, socks_port, socks_user, socks_pwd)

        srv = await asyncio.start_server(_cb, '127.0.0.1', port)
        _servers.append(srv)   # keep reference so GC doesn't close the socket
        srv_ready.set()

    fut = asyncio.run_coroutine_threadsafe(_start(), loop)
    fut.result(timeout=5)
    if not srv_ready.wait(timeout=3):
        raise RuntimeError(f"proxy_bridge: server failed to start for {socks5_url}")

    with _bridge_lock:
        _bridges[socks5_url] = port

    return f'http://127.0.0.1:{port}'


def bridge_url(socks5_url: str) -> str | None:
    """Return cached bridge URL for socks5_url, or None if not yet started."""
    port = _bridges.get(socks5_url)
    return f'http://127.0.0.1:{port}' if port else None


def bridge_count() -> int:
    """Number of active bridge listeners."""
    return len(_bridges)


# ── Standalone smoke-test ─────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse, sys
    ap = argparse.ArgumentParser(description='HTTP→SOCKS5 bridge smoke-test')
    ap.add_argument('--socks5', required=True, help='socks5://user:pass@host:port')
    args = ap.parse_args()

    url = start_bridge(args.socks5)
    print(f'Bridge listening: {url}  →  {args.socks5}')
    print('Press Ctrl+C to stop.')
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        sys.exit(0)

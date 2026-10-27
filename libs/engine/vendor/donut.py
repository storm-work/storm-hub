"""Donut Browser backend (BROWSER_BACKEND=donut): the browser comes from a Donut profile, not from
Playwright's own Chromium launch.

WHAT CHANGES AND WHAT DOES NOT. Only where the browser comes from. PlaywrightSession still owns it
through the same worker thread, the same page object, the same login flow and the same commands --
it just gets the browser by `connect_over_cdp` to a Donut-launched Wayfern profile instead of
`chromium.launch`. Wayfern owns the fingerprint (UA, client hints, canvas/WebGL, timezone, screen),
so in this mode the engine does NOT apply its own fingerprint pool, version alignment or stealth
script: two fingerprint layers on one browser contradict each other, which is worse than either.

The Donut desktop app must be running on THIS host, signed in to a plan that includes automation
(Pro/Team/Enterprise -- other plans answer 402), with its Local API enabled. The API listens on
127.0.0.1 only and every launched profile's CDP port is bound to 127.0.0.1 too.

PROFILES. DONUT_PROFILE_SCOPE:
  'account' (default) -- one persistent profile per sub-account (keyed by the session's fp_seed, the
      same seed DETERMINISTIC_FP uses), so an account presents the same device across logins, exactly
      what DETERMINISTIC_FP gives the Chromium path. It also keeps us under Donut's hourly limit on
      NEW fingerprints: a profile is created once per account, not once per session. The profile's
      cookies are cleared on close, so every session still starts with an empty jar like a fresh
      Chromium context does.
  'session' -- a throwaway profile per session, deleted on release. A new fingerprint every time;
      counts against the hourly new-fingerprint limit on every launch.
A session without an fp_seed always uses 'session'.

Each profile gets its own Donut proxy entry, rewritten with the session's exit on every launch (our
exits roll; Donut stores proxies as objects, not URLs).

API reference: https://donutbrowser.com/docs/rest-api (served as /v1/openapi.json by the app).
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import httpx

DEFAULT_API_URL = "http://127.0.0.1:10108"
_PREFIX = "storm-"

# name -> id caches, per process. A worker process launches a handful of sessions over its life, and
# GET /v1/profiles lists EVERY profile, so do not re-list it on every launch.
_cache_lock = threading.Lock()
_profile_ids: dict[str, str] = {}
_proxy_ids: dict[str, str] = {}


class DonutError(RuntimeError):
    pass


class DonutRateLimited(DonutError):
    """HTTP 429: Donut's hourly limit on profile runs or on new fingerprints. Carries Retry-After."""
    def __init__(self, msg: str, retry_after: float):
        super().__init__(msg)
        self.retry_after = retry_after


# Longest single Retry-After we sit out inside one launch before giving up on it (seconds). A longer
# wait means the hourly budget is spent: failing the launch fast lets the login loop back off as
# infrastructure instead of holding a worker thread for most of an hour.
_MAX_RATE_WAIT = 120.0
_RATE_RETRIES = 2


def enabled() -> bool:
    return os.environ.get("BROWSER_BACKEND", "chromium").strip().lower() == "donut"


def _headless() -> bool:
    return os.environ.get("DONUT_HEADLESS", "1").strip() != "0"


def _throttle_enabled() -> bool:
    return os.environ.get("DONUT_CDP_THROTTLE", "1").strip() != "0"


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "").strip() or default)
    except ValueError:
        return default


def _profile_os() -> str:
    return os.environ.get("DONUT_PROFILE_OS", "").strip().lower() or "windows"


def _scope() -> str:
    s = os.environ.get("DONUT_PROFILE_SCOPE", "").strip().lower()
    return s if s in ("account", "session") else "account"


class _Api:
    def __init__(self):
        self.base = (os.environ.get("DONUT_API_URL", "").strip() or DEFAULT_API_URL).rstrip("/")
        token = os.environ.get("DONUT_API_TOKEN", "").strip()
        if not token:
            raise DonutError("BROWSER_BACKEND=donut but DONUT_API_TOKEN is not set "
                             "(Global settings; copy it from Donut > Integrations > Local API)")
        self._headers = {"Authorization": f"Bearer {token}"}

    @property
    def host(self) -> str:
        return urlparse(self.base).hostname or "127.0.0.1"

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 30.0):
        try:
            r = httpx.request(method, self.base + path, json=body, headers=self._headers,
                              timeout=timeout)
        except httpx.HTTPError as e:
            raise DonutError(f"Donut API unreachable at {self.base} ({type(e).__name__}: {e}) -- "
                             f"is the Donut app running with its Local API enabled?") from e
        if r.status_code == 204:
            return None
        if r.status_code == 429:
            try:
                ra = float(r.headers.get("Retry-After") or (r.json() or {}).get("retryAfterSeconds") or 60)
            except (ValueError, TypeError, AttributeError):
                ra = 60.0
            raise DonutRateLimited(f"Donut {method} {path} -> HTTP 429 (rate limited, "
                                   f"Retry-After={ra:.0f}s): {r.text[:160]}", ra)
        if r.status_code >= 400:
            hint = {401: "token rejected", 402: "plan does not include automation"}
            raise DonutError(f"Donut {method} {path} -> HTTP {r.status_code}"
                             f"{' (' + hint[r.status_code] + ')' if r.status_code in hint else ''}: "
                             f"{r.text[:200]}")
        try:
            return r.json()
        except ValueError:
            return None


def proxy_settings(proxy_url: str) -> dict:
    """Our proxy URL -> Donut's proxy_settings object (http/https/socks4/socks5)."""
    u = urlparse(proxy_url)
    scheme = (u.scheme or "http").lower()
    if scheme == "socks5h":
        scheme = "socks5"
    if scheme not in ("http", "https", "socks4", "socks5"):
        raise DonutError(f"proxy scheme {scheme!r} is not supported by Donut")
    if not u.hostname or not u.port:
        raise DonutError("proxy URL has no host:port")
    return {"proxy_type": scheme, "host": u.hostname, "port": u.port,
            "username": unquote(u.username) if u.username else None,
            "password": unquote(u.password) if u.password else None}


def _unwrap(resp, key: str) -> dict:
    """Donut wraps single objects ({"profile": {...}}); tolerate a bare object too."""
    if isinstance(resp, dict) and isinstance(resp.get(key), dict):
        return resp[key]
    return resp if isinstance(resp, dict) else {}


def _items(resp, key: str) -> list:
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict) and isinstance(resp.get(key), list):
        return resp[key]
    return []


@dataclass
class Lease:
    """One running Donut profile owned by one PlaywrightSession."""
    profile_id: str
    proxy_id: str | None
    port: int
    host: str
    scope: str
    name: str
    released: bool = False
    throttle: object | None = None      # cdp_throttle.CdpThrottle, once connect() has put it in front

    @property
    def cdp_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def _find_id(api: _Api, cache: dict, path: str, key: str, name: str) -> str | None:
    with _cache_lock:
        if name in cache:
            return cache[name]
    for it in _items(api.call("GET", path), key):
        if isinstance(it, dict) and it.get("name") == name and it.get("id"):
            with _cache_lock:
                cache[name] = str(it["id"])
            return str(it["id"])
    return None


def _ensure_proxy(api: _Api, name: str, proxy_url: str | None, reuse: bool) -> str | None:
    if not proxy_url:
        return None
    body = {"name": name, "proxy_settings": proxy_settings(proxy_url)}
    pid = _find_id(api, _proxy_ids, "/v1/proxies", "proxies", name) if reuse else None
    if pid:
        try:
            # Full proxy_settings every time: a PUT that omits username/password clears them.
            api.call("PUT", f"/v1/proxies/{pid}", body)
            return pid
        except DonutError:
            with _cache_lock:
                _proxy_ids.pop(name, None)       # deleted behind our back -- recreate below
    pid = str(_unwrap(api.call("POST", "/v1/proxies", body), "proxy").get("id") or "")
    if not pid:
        raise DonutError("Donut POST /v1/proxies returned no id")
    if reuse:
        with _cache_lock:
            _proxy_ids[name] = pid
    return pid


def _ensure_profile(api: _Api, name: str, proxy_id: str | None, scope: str) -> str:
    if scope == "account":
        pid = _find_id(api, _profile_ids, "/v1/profiles", "profiles", name)
        if pid:
            try:
                api.call("PUT", f"/v1/profiles/{pid}",
                         {"proxy_id": proxy_id or "", "clear_on_close": True})
                return pid
            except DonutError:
                with _cache_lock:
                    _profile_ids.pop(name, None)
    body = {"name": name, "browser": "wayfern", "version": "latest",
            "wayfern_config": {"os": _profile_os()}, "ephemeral": scope == "session"}
    if proxy_id:
        body["proxy_id"] = proxy_id
    pid = str(_unwrap(api.call("POST", "/v1/profiles", body), "profile").get("id") or "")
    if not pid:
        raise DonutError("Donut POST /v1/profiles returned no id")
    if scope == "account":
        # The cookie jar must not outlive the session -- every session starts from an empty jar, as
        # a fresh Chromium context does. Engine and fingerprint OS are fixed at creation.
        api.call("PUT", f"/v1/profiles/{pid}", {"clear_on_close": True})
        with _cache_lock:
            _profile_ids[name] = pid
    return pid


def _rate_limited(fn, *args):
    """Run `fn`, sitting out Donut 429s (Retry-After, bounded by _MAX_RATE_WAIT) up to _RATE_RETRIES
    times. Every exit roll is a new profile run, so the hourly run limit is the one most likely to
    bite; waiting it out keeps the same account and exit instead of rolling into another 429."""
    for attempt in range(_RATE_RETRIES + 1):
        try:
            return fn(*args)
        except DonutRateLimited as e:
            if attempt == _RATE_RETRIES or e.retry_after > _MAX_RATE_WAIT:
                raise
            print(f"[donut] {e} -- waiting {e.retry_after:.0f}s ({attempt + 1}/{_RATE_RETRIES})")
            time.sleep(e.retry_after)


def launch(proxy_url: str | None, fp_seed: str | None) -> Lease:
    """Create/refresh this session's profile on `proxy_url` and start it. Returns the lease whose
    cdp_url Playwright connects to. Raises DonutError with an operator-readable reason."""
    return _rate_limited(_launch_once, proxy_url, fp_seed)


def _launch_once(proxy_url: str | None, fp_seed: str | None) -> Lease:
    api = _Api()
    scope = _scope() if fp_seed else "session"
    if scope == "account":
        name = _PREFIX + hashlib.sha1(fp_seed.encode()).hexdigest()[:12]
    else:
        name = f"{_PREFIX}tmp-{uuid.uuid4().hex[:12]}"
    proxy_id = _ensure_proxy(api, name, proxy_url, reuse=scope == "account")
    profile_id = _ensure_profile(api, name, proxy_id, scope)
    lease = Lease(profile_id, proxy_id, 0, api.host, scope, name)
    try:
        run = api.call("POST", f"/v1/profiles/{profile_id}/run",
                       {"url": "about:blank", "headless": _headless()}, timeout=90)
        if isinstance(run, dict) and run.get("attached"):
            # Already running: a browser left behind by a crashed worker on this account. Never share
            # it -- it carries another session's state and exit. Restart it on our proxy.
            print(f"[donut] profile {name} was already running -- restarting it")
            api.call("POST", f"/v1/profiles/{profile_id}/kill")
            time.sleep(1.0)
            run = api.call("POST", f"/v1/profiles/{profile_id}/run",
                           {"url": "about:blank", "headless": _headless()}, timeout=90)
        port = int((run or {}).get("remote_debugging_port") or 0)
        if not port:
            raise DonutError(f"Donut run returned no remote_debugging_port: {str(run)[:200]}")
        lease.port = port
    except Exception:
        release(lease)
        raise
    print(f"[donut] profile {name} ({scope}) running  cdp={lease.cdp_url}  "
          f"headless={int(_headless())}  os={_profile_os()}  proxy={'set' if proxy_id else 'none'}")
    return lease


def _browser_ws(lease: Lease) -> str:
    """The profile's browser-level CDP websocket (what connect_over_cdp resolves from the http URL)."""
    r = httpx.get(lease.cdp_url + "/json/version", timeout=5)
    r.raise_for_status()
    ws = str(r.json().get("webSocketDebuggerUrl") or "")
    if not ws:
        raise DonutError(f"{lease.cdp_url}/json/version has no webSocketDebuggerUrl")
    return ws


def _start_throttle(lease: Lease) -> str | None:
    """Put the rate-limit relay (cdp_throttle) in front of this profile; returns the endpoint to attach
    to, or None to attach directly. Never fatal: a session must not be lost to its own safety net."""
    if not _throttle_enabled():
        return None
    try:
        import cdp_throttle
        if lease.throttle is None:
            lease.throttle = cdp_throttle.CdpThrottle(
                _browser_ws(lease),
                per_min=_env_float("DONUT_CDP_PER_MIN", 90.0),
                burst=int(_env_float("DONUT_CDP_BURST", 180)))
            return lease.throttle.start()
        return lease.throttle.url
    except Exception as e:
        print(f"[donut] CDP throttle relay unavailable ({e}) -- attaching directly, no pacing")
        _stop_throttle(lease)
        return None


def _stop_throttle(lease: Lease) -> None:
    t, lease.throttle = lease.throttle, None
    if t is None:
        return
    try:
        t.stop()
        print(f"[donut] {t.summary()}")
    except Exception:
        pass


def connect(pw, lease: Lease, timeout_s: float = 30.0):
    """connect_over_cdp, retried while the freshly started profile opens its debugging port. The
    attach goes through the rate-limit relay unless DONUT_CDP_THROTTLE is off."""
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            return pw.chromium.connect_over_cdp(_start_throttle(lease) or lease.cdp_url, timeout=10_000)
        except Exception as e:
            last = e
            time.sleep(1.0)
    _stop_throttle(lease)
    raise DonutError(f"could not attach to Donut profile {lease.name} at {lease.cdp_url}: {last}")


def release(lease: Lease | None) -> None:
    """Stop the profile (and delete a throwaway one). Idempotent, never raises -- it runs from
    teardown paths, including a force-kill of a hung session."""
    if lease is None or lease.released:
        return
    lease.released = True
    _stop_throttle(lease)
    try:
        api = _Api()
    except DonutError:
        return
    steps = [("POST", f"/v1/profiles/{lease.profile_id}/kill")]
    if lease.scope == "session":
        steps.append(("DELETE", f"/v1/profiles/{lease.profile_id}"))
        if lease.proxy_id:
            steps.append(("DELETE", f"/v1/proxies/{lease.proxy_id}"))
    for method, path in steps:
        try:
            api.call(method, path, timeout=15)
        except DonutError as e:
            print(f"[donut] release {lease.name}: {e}")

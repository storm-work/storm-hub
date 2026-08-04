"""
reCAPTCHA Enterprise solver wrapper.
Supports: CapSolver, Anti-Captcha, 2Captcha, CapMonster.

All Enterprise v2 tasks use ProxyLess task types. Solver-service IPs have dedicated
high-trust Google reputation that produces higher reCAPTCHA Enterprise scores than
passing proxy IPs through proxy-aware tasks.

solve() rotates through the provided key list on failure.
"""

import os
import time
import threading
import requests

# Once race_all has collected at least one usable token, it waits only this long (seconds) for a
# HIGHER-scoring token from a slower provider before returning the best-so-far — instead of blocking
# the full 180s hard cap on a hung/slow second provider. 0730: 31 logins stalled the full 180s at
# collected=1/2 (one provider returned a below-min_score token, the other never responded), freezing
# each worker in logging_in on exactly the low-score tokens that then got rejected. Bounding the
# post-first-result wait cuts that stall to ~RACE_GRACE while still giving a fast better token a chance.
SOLVER_RACE_GRACE = float(os.environ.get("SOLVER_RACE_GRACE", "25"))
SOLVER_RACE_HARD_TIMEOUT = float(os.environ.get("SOLVER_RACE_HARD_TIMEOUT", "180"))

# Per-thread stop event -- set by race_all so _poll can abort polling when another solver wins.
# Thread-local avoids the module-level monkey-patch race condition with concurrent race_all calls.
_thread_stop: threading.local = threading.local()

SITE_KEY   = "6LdOB9crAAAAADT4RFruc5sPmzLKIgvJVfL830d4"
AUTH_URL   = "https://pedidodevistos.mne.gov.pt/VistosOnline/Authentication.jsp"
SCHED_URL  = "https://pedidodevistos.mne.gov.pt/VistosOnline/Schedule.jsp"
POLL_DELAY = 3
MAX_POLLS  = 60  # ~3 minutes

_V3_ACTIONS: frozenset[str] = frozenset()  # all actions use ReCaptchaV2Enterprise

# reCAPTCHA tokens are bound to the page URL -- each action must use the correct page
_ACTION_URL: dict[str, str] = {
    "SCHEDULE_EVISA": SCHED_URL,
}


def _parse_proxy(proxy_url: str | None) -> dict | None:
    """
    Parse http://user:pass@host:port -> {proxyType, proxyAddress, proxyPort,
    proxyLogin, proxyPassword} for CAPTCHA service APIs.
    Returns None if proxy_url is None/empty.
    proxyAddress is always an IP — hostname is resolved so AntiCaptcha (which
    rejects hostnames) can use the same proxy dict as other services.
    """
    if not proxy_url:
        return None
    try:
        import socket
        from urllib.parse import urlparse
        p = urlparse(proxy_url)
        try:
            address = socket.gethostbyname(p.hostname)
        except Exception:
            address = p.hostname  # fallback: pass hostname as-is
        return {
            "proxyType":     p.scheme or "http",
            "proxyAddress":  address,
            "proxyPort":     p.port or 6666,
            "proxyLogin":    p.username or "",
            "proxyPassword": p.password or "",
        }
    except Exception:
        return None


def score_token(token: str) -> int:
    """
    Estimate reCAPTCHA Enterprise score from token length.
    Shorter = auto-pass checkbox (no image challenge) = higher Google score.
    Based on observed token lengths: successful logins tend toward shorter tokens.
    Returns 0-100; higher is better.
    """
    n = len(token)
    if n < 2320:  return 100  # auto-pass -- no image challenge, highest score
    if n < 2380:  return 85
    if n < 2430:  return 70
    if n < 2470:  return 55   # confirmed working at this length (neupir2449: len=2446)
    if n < 2510:  return 40
    if n < 2550:  return 25
    return 10


def race_best(
    capsolver_keys: list[str], anticaptcha_keys: list[str],
    twocaptcha_keys: list[str], capmonster_keys: list[str],
    action: str, proxy: str | None = None,
    n_races: int = 3,
    min_score: int = 85,
) -> str:
    """
    Run up to n_races sequential solver races; return the highest-scoring token.
    Stops early only if a token reaches min_score (auto-pass quality, < ~2380 chars).
    For image-challenge tokens (~2400-2574), always runs all races and picks shortest.
    Running multiple races statistically improves success rate for strict Enterprise
    endpoints like LOGIN_EVISA.
    """
    best_token = ""
    best_score = 0

    for i in range(n_races):
        print(f"[solver] race_best: race {i+1}/{n_races}  best_score_so_far={best_score}")
        try:
            token = race_all(
                capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                action, proxy=proxy,
            )
            score = score_token(token)
            print(f"[solver] race_best: race {i+1} done  score={score}  token_len={len(token)}")
            if score > best_score:
                best_score = score
                best_token = token
            if score >= min_score:
                print(f"[solver] race_best: threshold met ({score}>={min_score}) -- stopping early")
                break
        except Exception as e:
            print(f"[solver] race_best: race {i+1} failed: {e}")

    if best_token:
        print(f"[solver] race_best: final  score={best_score}  token_len={len(best_token)}")
        return best_token
    raise RuntimeError(f"race_best: all {n_races} races failed")


def race_all(capsolver_keys: list[str], anticaptcha_keys: list[str],
             twocaptcha_keys: list[str], capmonster_keys: list[str],
             action: str, proxy: str | None = None,
             session_cookies: dict | None = None,
             min_score: int = 0) -> str:
    """
    Race all configured solvers in parallel.

    min_score=0 (default): first token wins, all others stopped immediately.
    min_score>0: don't stop other threads until a token with score >= min_score
    arrives, OR all threads have finished. Returns the highest-scoring token
    collected. Prevents fast-but-low-quality solvers (e.g. capmonster) from
    starving slower-but-better solvers (e.g. 2captcha) before they finish.

    session_cookies: dict of cookies from the primp session (e.g. Vistos_sid).
    """
    proxy_info = _parse_proxy(proxy)

    tasks: list[tuple[str, callable, str]] = []
    if capsolver_keys:
        tasks.append(("capsolver",   _capsolver,   capsolver_keys[0]))
    if anticaptcha_keys:
        tasks.append(("anticaptcha", _anticaptcha, anticaptcha_keys[0]))
    if twocaptcha_keys:
        tasks.append(("2captcha",    _twocaptcha,  twocaptcha_keys[0]))
    if capmonster_keys:
        tasks.append(("capmonster",  _capmonster,  capmonster_keys[0]))

    if not tasks:
        raise RuntimeError("race_all: no solver keys configured")

    total = len(tasks)
    results: list[tuple[str, int]] = []  # (token, score) pairs as they arrive
    errors:  list[str] = []
    finished = [0]
    lock = threading.Lock()
    done = threading.Event()
    stop = threading.Event()  # signals threads to stop polling

    def _run_guarded(label: str, fn: callable, key: str) -> None:
        _thread_stop.stop = stop  # bind this thread's stop event for _poll to check
        try:
            token = fn(key, action, proxy_info, session_cookies)
            s = score_token(token)
            with lock:
                results.append((token, s))
                finished[0] += 1
                print(f"[solver] race:{label} done  token_len={len(token)}  score={s}")
                if s >= min_score:
                    # Good enough -- stop all remaining threads
                    print(f"[solver] race:{label} WON (score={s}>={min_score})")
                    stop.set()
                    done.set()
                elif finished[0] == total:
                    # All threads done, no score met threshold -- take best available
                    done.set()
        except Exception as e:
            with lock:
                errors.append(f"{label}: {e}")
                finished[0] += 1
                print(f"[solver] race:{label} failed: {e}")
                if finished[0] == total:
                    done.set()

    threads = [
        threading.Thread(target=_run_guarded, args=(label, fn, key), daemon=True)
        for label, fn, key in tasks
    ]
    for t in threads:
        t.start()

    # Wait for a min_score win / all-finished (done), but don't block the full hard timeout on a slow
    # second provider once we ALREADY hold a usable token: after the first result lands, allow only
    # SOLVER_RACE_GRACE more for a better one, then return best-so-far. (min_score=0 sets done on the
    # first token anyway, so this only affects the min_score>0 path that caused the 180s stalls.)
    deadline = time.time() + SOLVER_RACE_HARD_TIMEOUT
    first_result_at: float | None = None
    while not done.wait(timeout=1.0):
        now = time.time()
        with lock:
            have_result = bool(results)
        if have_result:
            if first_result_at is None:
                first_result_at = now
            elif (now - first_result_at) >= SOLVER_RACE_GRACE:
                stop.set()   # stop the still-polling slow provider(s) — we're returning
                print(f"[solver] race_all: grace {SOLVER_RACE_GRACE:.0f}s elapsed after first token — "
                      f"returning best of collected={len(results)}/{total} (not waiting the hard cap)")
                break
        if now >= deadline:
            print(f"[solver] race_all: WARNING — timed out after {SOLVER_RACE_HARD_TIMEOUT:.0f}s  "
                  f"collected={len(results)}/{total}")
            break

    if results:
        best_token, best_score = max(results, key=lambda x: x[1])
        print(f"[solver] race_all: best score={best_score}  token_len={len(best_token)}  "
              f"collected={len(results)}/{total}")
        return best_token
    raise RuntimeError(f"race_all -- all {total} solvers failed: {' | '.join(errors)}")


def solve(service: str, keys: list[str], action: str = "LOGIN_EVISA",
          proxy: str | None = None,
          session_cookies: dict | None = None) -> str:
    """
    Try each key in order. Returns the first successful token.
    proxy: full proxy URL (http://user:pass@host:port); enables proxy-aware
           task type which gets much higher reCAPTCHA Enterprise scores.
    session_cookies: dict from primp client.get_cookies(); passed to the solver
           so the token is generated inside the same server session as the POST.
    Raises RuntimeError if all keys fail.
    """
    proxy_info = _parse_proxy(proxy)
    print(f"[solver] mode={'proxy-aware' if proxy_info else 'proxyless'}")

    last_err = None
    for i, key in enumerate(keys):
        try:
            label = f"key[{i}]={key[:8]}..."
            print(f"[solver] {service} {label} action={action}")
            return _SOLVERS[service](key, action, proxy_info, session_cookies)
        except Exception as e:
            print(f"[solver] {label} failed: {e}")
            last_err = e
    raise RuntimeError(f"All {len(keys)} keys failed for {service}. Last: {last_err}")


# -- CapSolver -----------------------------------------------------------------

def _capsolver(api_key: str, action: str, proxy_info: dict | None,
               session_cookies: dict | None = None) -> str:
    page_url = _ACTION_URL.get(action, AUTH_URL)
    if action in _V3_ACTIONS:
        task = {
            "type":        "ReCaptchaV3Task" if proxy_info else "ReCaptchaV3TaskProxyLess",
            "websiteURL":  page_url,
            "websiteKey":  SITE_KEY,
            "pageAction":  action,
            "isEnterprise": True,
        }
    elif proxy_info:
        # PROXY mode: solve THROUGH the session's residential IP so Google scores our real exit, not
        # capsolver's datacenter. Gated on proxy_info (RECAPTCHA_SOLVE_PROXY passes the session proxy).
        task = {
            "type":       "ReCaptchaV2EnterpriseTask",
            "websiteURL": page_url,
            "websiteKey": SITE_KEY,
            "proxy": (f"{proxy_info['proxyType']}:{proxy_info['proxyAddress']}:{proxy_info['proxyPort']}"
                      f":{proxy_info.get('proxyLogin','')}:{proxy_info.get('proxyPassword','')}"),
        }
    else:
        task = {
            "type":       "ReCaptchaV2EnterpriseTaskProxyLess",
            "websiteURL": page_url,
            "websiteKey": SITE_KEY,
        }

    r = requests.post("https://api.capsolver.com/createTask", json={
        "clientKey": api_key,
        "task": task,
    }, timeout=30)
    data = r.json()
    task_id = data.get("taskId")
    if not task_id:
        raise RuntimeError(f"createTask failed: {data}")
    return _poll("https://api.capsolver.com/getTaskResult", api_key, task_id)


# -- Anti-Captcha --------------------------------------------------------------

_AC_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _anticaptcha(api_key: str, action: str, proxy_info: dict | None,
                 session_cookies: dict | None = None) -> str:
    page_url = _ACTION_URL.get(action, AUTH_URL)
    if action in _V3_ACTIONS:
        task = {
            "websiteURL":   page_url,
            "websiteKey":   SITE_KEY,
            "minScore":     0.3,
            "pageAction":   action,
            "isEnterprise": True,
        }
        task["type"] = "RecaptchaV3Task" if proxy_info else "RecaptchaV3TaskProxyless"
    elif proxy_info:
        # PROXY mode: anti-captcha solves through the session's residential IP. _parse_proxy already
        # returns anti-captcha's native proxy fields (proxyType/Address/Port/Login/Password).
        task = {
            "type":       "RecaptchaV2EnterpriseTask",
            "websiteURL": page_url,
            "websiteKey": SITE_KEY,
            **proxy_info,
        }
    else:
        task = {
            "type":       "RecaptchaV2EnterpriseTaskProxyless",
            "websiteURL": page_url,
            "websiteKey": SITE_KEY,
        }
    task["userAgent"] = _AC_USER_AGENT

    r = requests.post("https://api.anti-captcha.com/createTask", json={
        "clientKey": api_key,
        "task": task,
    }, timeout=30)
    data = r.json()
    if data.get("errorId", 0) != 0:
        raise RuntimeError(f"createTask error: {data}")
    task_id = data.get("taskId")
    if not task_id:
        raise RuntimeError(f"createTask no taskId: {data}")
    return _poll("https://api.anti-captcha.com/getTaskResult", api_key, task_id)


# -- 2Captcha ------------------------------------------------------------------

def _twocaptcha(api_key: str, action: str, proxy_info: dict | None,
                session_cookies: dict | None = None) -> str:
    page_url = _ACTION_URL.get(action, AUTH_URL)
    if action in _V3_ACTIONS:
        task = {
            "type":         "RecaptchaV3TaskProxyless",
            "websiteURL":   page_url,
            "websiteKey":   SITE_KEY,
            "minScore":     0.3,
            "pageAction":   action,
            "isEnterprise": True,
        }
    else:
        task = {
            "type":       "RecaptchaV2EnterpriseTaskProxyless",
            "websiteURL": page_url,
            "websiteKey": SITE_KEY,
        }

    r = requests.post("https://api.2captcha.com/createTask", json={
        "clientKey": api_key,
        "task": task,
    }, timeout=30)
    data = r.json()
    if data.get("errorId", 0) != 0:
        raise RuntimeError(f"createTask error: {data}")
    task_id = data["taskId"]
    return _poll("https://api.2captcha.com/getTaskResult", api_key, task_id)


# -- CapMonster ----------------------------------------------------------------

def _capmonster(api_key: str, action: str, proxy_info: dict | None,
                session_cookies: dict | None = None) -> str:
    page_url = _ACTION_URL.get(action, AUTH_URL)
    if action in _V3_ACTIONS:
        task = {
            "type":         "RecaptchaV3TaskProxyless",
            "websiteURL":   page_url,
            "websiteKey":   SITE_KEY,
            "minScore":     0.3,
            "pageAction":   action,
            "isEnterprise": True,
        }
    else:
        task = {
            "type":       "RecaptchaV2EnterpriseTaskProxyless",
            "websiteURL": page_url,
            "websiteKey": SITE_KEY,
        }

    r = requests.post("https://api.capmonster.cloud/createTask", json={
        "clientKey": api_key,
        "task": task,
    }, timeout=30)
    data = r.json()
    if data.get("errorId", 0) != 0:
        raise RuntimeError(f"createTask error: {data}")
    task_id = data["taskId"]
    return _poll("https://api.capmonster.cloud/getTaskResult", api_key, task_id)


# -- Shared poll ---------------------------------------------------------------

def _poll(url: str, api_key: str, task_id: str) -> str:
    stop: threading.Event | None = getattr(_thread_stop, "stop", None)
    for i in range(MAX_POLLS):
        if stop and stop.is_set():
            raise RuntimeError("race: stopped (another solver won)")
        time.sleep(POLL_DELAY)
        if stop and stop.is_set():
            raise RuntimeError("race: stopped (another solver won)")
        r = requests.post(url, json={"clientKey": api_key, "taskId": task_id}, timeout=30)
        data = r.json()
        if data.get("errorId", 0) != 0:
            raise RuntimeError(f"poll error: {data}")
        if data.get("status") == "ready":
            token = data["solution"]["gRecaptchaResponse"]
            print(f"[solver] ready in ~{(i+1)*POLL_DELAY}s  token_len={len(token)}")
            return token
        pass  # still processing
    raise RuntimeError(f"Solver timeout after {MAX_POLLS * POLL_DELAY}s")


_SOLVERS = {
    "capsolver":   _capsolver,
    "anticaptcha": _anticaptcha,
    "2captcha":    _twocaptcha,
    "capmonster":  _capmonster,
}


def solve_datadome_capsolver(
    capsolver_keys: list[str],
    captcha_url: str,
    website_url: str,
    user_agent: str,
    proxy: str | None,
    timeout: int = 120,
) -> str | None:
    """
    Solve a DataDome slider challenge via capsolver DatadomeSliderTask.

    captcha_url: the geo.captcha-delivery.com/captcha/?... URL from the challenge iframe.
    proxy:       MUST be the same proxy the browser session uses (cookie is IP-bound).

    Returns the raw cookie string e.g. 'datadome=XXXXXX', or None on failure.
    """
    if not capsolver_keys or not captcha_url:
        return None

    proxy_info = _parse_proxy(proxy)
    if not proxy_info:
        print("[datadome] DatadomeSliderTask requires a proxy — skipping")
        return None

    proxy_str = (
        f"{proxy_info['proxyType']}://"
        f"{proxy_info['proxyLogin']}:{proxy_info['proxyPassword']}"
        f"@{proxy_info['proxyAddress']}:{proxy_info['proxyPort']}"
    )

    api_key = capsolver_keys[0]
    try:
        resp = requests.post(
            "https://api.capsolver.com/createTask",
            json={
                "clientKey": api_key,
                "task": {
                    "type":       "DatadomeSliderTask",
                    "websiteURL": website_url,
                    "captchaUrl": captcha_url,
                    "userAgent":  user_agent,
                    "proxy":      proxy_str,
                },
            },
            timeout=30,
        )
        data = resp.json()
    except Exception as e:
        print(f"[datadome] createTask request failed: {e}")
        return None

    if data.get("errorId"):
        print(f"[datadome] createTask error: {data}")
        return None

    task_id = data.get("taskId")
    if not task_id:
        print(f"[datadome] no taskId in response: {data}")
        return None

    print(f"[datadome] taskId={task_id} — polling")
    deadline = time.time() + timeout
    poll_n = 0
    while time.time() < deadline:
        time.sleep(5)
        poll_n += 1
        try:
            poll = requests.post(
                "https://api.capsolver.com/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
                timeout=30,
            ).json()
        except Exception as e:
            print(f"[datadome] poll {poll_n} request failed: {e}")
            continue

        status = poll.get("status")
        if status == "ready":
            cookie = poll.get("solution", {}).get("cookie", "")
            if cookie:
                print(f"[datadome] solved (poll {poll_n})  cookie_len={len(cookie)}")
                return cookie
            print(f"[datadome] ready but no cookie in solution: {poll}")
            return None
        elif status == "processing":
            print(f"[datadome] poll {poll_n}: processing")
        elif poll.get("errorId"):
            print(f"[datadome] poll {poll_n} error: {poll}")
            return None
        else:
            print(f"[datadome] poll {poll_n} unexpected: {poll}")
            continue

    print(f"[datadome] timeout after {timeout}s")
    return None

"""
HTTP session for the E-VISA portal.

Architecture (revised):
  1. PlaywrightSession launches a headless Chrome browser through the proxy,
     navigates to the target site, and runs stealth JS to bypass the
     FingerprintJS BotDetectorLib challenge (/ch/bd.js).
  2. Once the challenge is bypassed, cookies are extracted and injected into
     a lightweight primp.Client (same proxy) via explicit Cookie header.
     The BROWSER STAYS ALIVE to handle verification (which requires the same
     browser session/TLS fingerprint - primp alone hits the challenge again).
  3. All .get()/.post() calls use the primp client directly (fast, low overhead).
  4. Call verify_email(token_url) to use the browser for email verification.
     The browser is then closed automatically.

IP-binding note:
  Both Playwright and the primp client must use the same proxy URL (same
  sessionid) so they share the same exit IP.

Usage:
  s = get_session(proxy_url)   # blocks until challenge is bypassed (~15-25s)
  resp = s.post(URL, data={...}, headers={...})
  result = s.verify_email(token_url, insert_token_url, verify_url,
                          token_input, token_search, lang)
  s.close()
"""

import json as _json
import hashlib
import os
import queue
import random
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import donut as _donut

_SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"

# The primp TLS/JA3 + header-order impersonation target for this engine's own primp clients (the
# login-flow post-challenge calls). Driven by the PRIMP_IMPERSONATE setting via vendor_env so it stays
# aligned with the Chromium we actually run (the browser version-aligns separately, see the launch
# block). Unset = primp's generic/random default -- a cheap automation tell the strict-window WAF
# keys on (MEASURED 2026-10-16). primp does NOT raise on an unknown value; it silently falls back to
# 'random', so the resolved value is validated at the ops layer before use (ops_live._primp_impersonate).
IMPERSONATE = os.environ.get("PRIMP_IMPERSONATE") or None

# ── P1 anti-detection flags (all default-OFF → current behavior unchanged; A/B one at a time) ──────────
# HEADLESS_MODE: 'old' (default, headless=True) | 'new' (Chrome --headless=new, real render path) |
#   'headful' (visible window; needs a display / xvfb-run). New-headless is far less detectable than old.
# DEFAULT-ON anti-detection posture (2026-09-23). The portal runs Botd/DataDome-class bot detection
# (confirmed: window.LoadBotd + obfuscated challenge fns), and a reference bot built from a real
# successful-booking HAR insists the WAF BLOCKS a detectable browser -- our 2026-09-21 run went out
# with ALL of this OFF (headless=old, no stealth, WebRTC unblocked) and drowned in 403 "try again
# later". These now default ON; set the env var to "0" to disable one for an A/B.
#
# HEADLESS_MODE: 'new' (default) = Chrome --headless=new, the modern render path that is FAR less
#   detectable than 'old' and needs NO display; 'old' = legacy headless=True (a strong bot tell);
#   'headful' = a visible window, the reference's choice, which needs a real display / xvfb (installed
#   here but DISPLAY is unset, so it must be launched under a virtual display -- see the supervisor).
HEADLESS_MODE    = os.environ.get("HEADLESS_MODE", "new").strip().lower()
# DETERMINISTIC_FP: derive the fingerprint (UA/viewport/platform + canvas/WebGL noise) from a per-
#   ACCOUNT seed so ONE account = ONE stable device across every login/resume (a real user has one
#   device). Default ON; "0" = random.choice per launch (the old, detectable behavior).
DETERMINISTIC_FP = os.environ.get("DETERMINISTIC_FP", "1").strip() != "0"
# WEBRTC_BLOCK: force WebRTC through the proxy only (no STUN leak of the server's real datacenter IP
#   behind the residential proxy -- a first-order DataDome ban signal). Keeps RTCPeerConnection
#   present (no 'missing API' tell). Default ON; "0" = allow the leak.
WEBRTC_BLOCK     = os.environ.get("WEBRTC_BLOCK", "1").strip() != "0"
# STEALTH_HARDEN: make the fingerprint COHERENT -- override navigator.platform + navigator.userAgentData
#   (UA-CH) to MATCH the spoofed UA, and DELETE the webdriver property (not just null its getter). Without
#   this the real Linux host + real Chromium version leak through and CONTRADICT the UA (audit 0730 found the
#   browser claiming Windows+Linux+macOS at once = instant spoofed-bot flag). Default ON; "0" = leak.
STEALTH_HARDEN   = os.environ.get("STEALTH_HARDEN", "1").strip() != "0"
# HUMAN_BEHAVIOR=1: add human-like mouse movement + dwell around the login-form interactions (helps the
#   behavioral scoring DataDome/reCAPTCHA collect). Typing cadence is already humanized. Default OFF.
HUMAN_BEHAVIOR   = os.environ.get("HUMAN_BEHAVIOR", "").strip() == "1"


def _ensure_headful_display():
    """Return (display, xvfb_proc) for a headful browser on a possibly display-less host.

    - If $DISPLAY is already set (operator ran under xvfb-run / a real X), reuse it: (disp, None).
    - Else best-effort start a private Xvfb and return (":N", proc) so close() can terminate it.
    - If no display can be had (no Xvfb binary, or it won't start), return (None, None) and let the
      caller fall back to --headless=new rather than failing to launch.
    """
    disp = os.environ.get("DISPLAY", "").strip()
    if disp:
        # Reuse $DISPLAY ONLY if its X server is actually alive. A stale DISPLAY left over from a
        # PRIOR per-worker Xvfb this same process started and then terminated on a failed attempt
        # points at nothing -> Chromium dies "Missing X server" and never falls back to headless
        # (confirmed live 2026-09-23: 60 such failures on retries). A real shared display -- operator
        # set DISPLAY + a persistent Xvfb -- has its socket and is reused as intended.
        num = disp.lstrip(":").split(".")[0]
        if num.isdigit() and os.path.exists(f"/tmp/.X11-unix/X{num}"):
            return disp, None
        # stale/dead -> fall through and start a fresh one
    import shutil
    xvfb = shutil.which("Xvfb")
    if not xvfb:
        return None, None
    import subprocess
    # No preexec_fn / PR_SET_PDEATHSIG: this session runs in a MULTI-THREADED worker, and a
    # preexec_fn forked from a multithreaded process is unsafe (it can deadlock on a lock another
    # thread held at fork). Cleanup is handled explicitly -- close() terminates it, and the launch
    # path terminates it if the browser fails to come up.
    for _ in range(6):
        num = random.randint(100, 9999)
        sock = f"/tmp/.X11-unix/X{num}"
        if os.path.exists(sock):
            continue                     # that display number is already taken
        try:
            proc = subprocess.Popen(
                [xvfb, f":{num}", "-screen", "0", "1280x1024x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            return None, None
        # WAIT FOR THE DISPLAY TO BE USABLE, not just for the process to be alive. Xvfb takes a
        # moment to create its X socket; a Chromium launched before then dies with "Missing X
        # server or $DISPLAY" (confirmed live 2026-09-23). Poll for the socket.
        deadline = time.time() + 8.0
        while time.time() < deadline:
            if proc.poll() is not None:
                break                    # Xvfb exited (display busy / failed) -> try another number
            if os.path.exists(sock):
                return f":{num}", proc   # socket is up -> the display is genuinely usable
            time.sleep(0.15)
        try:
            proc.terminate()
        except Exception:
            pass
    return None, None


def _coherent_fp(ua: str) -> dict:
    """Derive a COHERENT device profile from the chosen UA so platform / userAgentData / WebGL all agree."""
    import re as _re
    m = _re.search(r"Chrome/(\d+)", ua or "")
    ver = m.group(1) if m else "131"
    if "Windows" in (ua or ""):
        return {"platform": "Win32", "uad_platform": "Windows", "plat_ver": "10.0.0", "ver": ver,
                "gl_vendor": "Google Inc. (NVIDIA)",
                "gl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0, D3D11)"}
    if "Macintosh" in (ua or ""):
        return {"platform": "MacIntel", "uad_platform": "macOS", "plat_ver": "14.5.0", "ver": ver,
                "gl_vendor": "Google Inc. (Apple)",
                "gl_renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)"}
    return {"platform": "Linux x86_64", "uad_platform": "Linux", "plat_ver": "6.5.0", "ver": ver,
            "gl_vendor": "Google Inc. (Intel)",
            "gl_renderer": "ANGLE (Intel, Mesa Intel(R) UHD Graphics (CML GT2), OpenGL 4.6)"}


def _seeded_rng(seed: str | None):
    """A deterministic RNG from a per-account seed when DETERMINISTIC_FP is on, else the module random."""
    if DETERMINISTIC_FP and seed:
        return random.Random(int(hashlib.sha256(seed.encode()).hexdigest()[:12], 16))
    return random

# -- Fingerprint pool ----------------------------------------------------------
# (user_agent, sec_ch_ua, viewport_w, viewport_h, platform)
# Claimed versions track the bundled Chromium (Chrome/149 at pin playwright==1.61.0). _fp_version_align
# below still corrects any residual drift at launch (future-proof), but keeping the pool current means
# that guard is a no-op on the happy path and the "fp version aligned" line stays quiet. Variety is in
# the OS/viewport/brand-mix + the deliberately-arbitrary "Not_A Brand" versions, not the Chrome major
# (real browsers auto-update, so a fleet almost all on one major is itself realistic).
_FP_POOL = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0",
        '"Microsoft Edge";v="149", "Chromium";v="149", "Not_A Brand";v="24"',
        1280, 800, "Windows",
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        '"Google Chrome";v="149", "Chromium";v="149", "Not_A Brand";v="24"',
        1920, 1080, "Windows",
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        '"Google Chrome";v="149", "Chromium";v="149", "Not_A Brand";v="99"',
        1366, 768, "Windows",
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        '"Google Chrome";v="149", "Chromium";v="149", "Not_A Brand";v="24"',
        1440, 900, "macOS",
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        '"Google Chrome";v="149", "Chromium";v="149", "Not_A Brand";v="8"',
        1600, 900, "Windows",
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        '"Google Chrome";v="149", "Chromium";v="149", "Not_A Brand";v="99"',
        1440, 900, "macOS",
    ),
]


def _pick_fingerprint(fp_seed: str | None = None) -> tuple:
    return _seeded_rng(fp_seed).choice(_FP_POOL)


def _get_proxy_timezone(proxy_url: str | None) -> str:
    """Detect the proxy exit-IP's timezone via ipwho.is. Falls back to Europe/Lisbon
    (all configured proxies are Portuguese ISP/SOAX-PT)."""
    _DEFAULT = "Europe/Lisbon"
    if not proxy_url:
        return _DEFAULT
    for svc in ("https://ipwho.is/json/", "https://ipapi.co/json/"):
        try:
            import primp
            client = primp.Client(impersonate=IMPERSONATE, proxy=proxy_url,
                                  follow_redirects=True, verify=False, timeout=8)
            r = client.get(svc)
            data = r.json()
            tz = data.get("timezone", {})
            if isinstance(tz, dict):
                tz = tz.get("id", "")
            tz = str(tz).strip()
            if tz and "/" in tz:
                print(f"[session] tz lookup OK: {tz}")
                return tz
        except Exception as _e:
            pass
    return _DEFAULT


BASE     = "https://pedidodevistos.mne.gov.pt"
AUTH_URL = BASE + "/VistosOnline/Authentication.jsp"
HOME_URL = BASE + "/VistosOnline/"

_HEADERS_BASE = {
    "User-Agent":         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    # PT-consistent (2026-09-26): we submit lang=PT to a Portuguese government portal, so the browser
    # must present as a Portuguese speaker -- an en-US Accept-Language against a PT-chosen session is
    # an incoherent story a WAF can score. pt-PT first, en kept as a secondary (a plausible bilingual).
    "Accept-Language":    "pt-PT,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding":    "gzip, deflate, br",
    "Cache-Control":      "no-cache",
    "Pragma":             "no-cache",
    "sec-ch-ua":          '"Microsoft Edge";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile":   "?0",
    "sec-ch-ua-platform": '"Windows"',
}

HEADERS_NAV = {
    **_HEADERS_BASE,
    "Accept":         "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

HEADERS_XHR = {
    **_HEADERS_BASE,
    "Accept":           "*/*",
    "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin":           BASE,
    "Referer":          AUTH_URL,
    "Sec-Fetch-Dest":   "empty",
    "Sec-Fetch-Mode":   "cors",
    "Sec-Fetch-Site":   "same-origin",
}


_RC_PROXY_WARNED = False

def _recaptcha_solve_proxy() -> bool:
    """RECAPTCHA_SOLVE_PROXY=1 mints the login reCAPTCHA token THROUGH the session's own proxy instead of
    proxyless (the solver's own IPs).

    An older note here called this CONFIRMED DEAD ("every pool REFUSES the solver's connection"). That
    was measured only against soax/mobile and webshare and was WRONG for soax/rotating -- the pool the
    342 verified registrations actually ran on, all of them proxied.

    Re-measured 2026-08-20, 5 solves/mode against LOGIN_EVISA:

        soax/rotating proxied   2/5 solved (score 10), 2x PROXY_CONNECT_REFUSED, 1x SOLVE_FAILED
        soax/mobile   proxied   0/1 solved (CAPTCHA_SOLVE_FAILED)
        proxyless               5/5 solved, score 10

    So proxy-mode WORKS on soax/rotating; it is just worse -- identical token score, ~60% fewer tokens
    per attempt. Proxyless remains the better default, but this is now a real operator A/B switch
    (settable per booking), not a broken knob, so the warning is informational rather than alarming.
    The solve path falls back to proxyless whenever a proxy-mode solve fails, so turning this on can
    cost latency but can no longer zero out login yield. `=force` still silences the note."""
    global _RC_PROXY_WARNED
    v = os.environ.get("RECAPTCHA_SOLVE_PROXY", "").strip().lower()
    on = v in ("1", "true", "yes", "on", "force")
    if on and v != "force" and not _RC_PROXY_WARNED:
        _RC_PROXY_WARNED = True
        print("[session] NOTE: RECAPTCHA_SOLVE_PROXY is ON — minting login tokens through this "
              "worker's own proxy. Measured 2026-08-20: works on soax/rotating but loses ~60% of "
              "solve attempts for the same token score; falls back to proxyless on failure. "
              "Set =force to silence.")
    return on


# Seconds the CALLER waits beyond the work's own budget. The caller must always outlive the worker
# thread: if it gives up first, the thread keeps running and its eventual reply lands in the result
# queue, where the NEXT unrelated call consumes it. Only browser_nav had this margin.
_QUEUE_MARGIN = 5.0


class _StampedResultQueue:
    """Result queue that tags every reply with the sequence number of the command that produced it.

    Risk F17, and the reason a slow portal hurt us out of all proportion to how slow it actually was.
    The session is a threaded command queue: the caller pushes a command and blocks on the result
    queue. Page operations inside the worker thread scale with NAV_TIMEOUT_SCALE; the CALLER-side
    waits mostly did not (measured 2026-08-20: 2 of 12 entry points scaled, while the worker
    templates run NAV_TIMEOUT_SCALE=4). So under load the caller abandoned a call that was still
    running, the thread finished, and its reply was handed to whatever asked next -- silently, since
    an untagged reply is indistinguishable from the right one. That is how portal slowness turned
    into state corruption instead of just waiting.

    Wrapping the queue rather than editing ~20 put() sites in the dispatch loop keeps the worker
    thread's code untouched: it still puts plain ("ok", ...) / ("err", ...) tuples, and the stamp is
    applied here from whichever command is currently being served.
    """
    __slots__ = ("_q", "_session")

    def __init__(self, session):
        self._q = queue.Queue()
        self._session = session

    def put(self, item):
        self._q.put((self._session._current_seq, item))

    def get(self, timeout=None):
        return self._q.get(timeout=timeout)

    def get_nowait(self):
        return self._q.get_nowait()


# The portal states WHY it refused a login in the page it serves back, and reading it is the only
# way to tell "your CAPTCHA was rejected" from "wrong password" from "we are throttling you". Kept
# as a module constant because BOTH rejection branches need it: this used to run only on the
# Authentication.jsp redirect, while the OTHER branch -- a bounce back to /login, which was 63% of
# rejections on 2026-08-21 -- recorded nothing but the URL and discarded the page.
#
# What the branch that DID look has been reporting all along, 554 times:
#     'Por favor, confirma o reCAPTCHA.'   -- please confirm the reCAPTCHA
#       8 times: 'Nao foi possivel de momento concretizar o seu pedido, por favor tente mais tarde'
# So the login wall is the CAPTCHA being rejected, in the portal's own words, and occasionally an
# explicit throttle. Not proxy quality, and not something inferable from a final URL.
_PAGE_ERROR_JS = """
() => {
    const els = [...document.querySelectorAll('.alert,.error,.errorMessage,#errorMessage,.text-danger,.msg-error,[class*="error"],[id*="error"],td.text-center.font-weight-bold')];
    for (const el of els) {
        const t = el.textContent.trim();
        if (t) return t.slice(0, 300);
    }
    const ps = document.querySelectorAll('p');
    for (const p of ps) {
        const t = p.textContent.trim();
        if (t && t.length > 5) return t.slice(0, 300);
    }
    return null;
}
"""


def _nav_scale() -> float:
    """NAV_TIMEOUT_SCALE (default 1.0): multiply browser nav/form-post timeouts for traffic-heavy
    ('storm') periods when the portal renders slowly and a normal timeout would abandon a still-alive
    page. INCREASE-ONLY (clamped >=1.0) so it never drops below browser_form_post's worst-case, which
    would corrupt the result queue. Set NAV_TIMEOUT_SCALE=2..3 for a known storm; unset = unchanged."""
    try:
        return max(1.0, min(5.0, float(os.environ.get("NAV_TIMEOUT_SCALE", "1"))))
    except (ValueError, TypeError):
        return 1.0


def _human_mouse(page, locator) -> None:
    """Move the mouse toward a target element along a jittered multi-step path + a small dwell, so the login
    emits real pointer signals (behavioral scoring). No-op unless HUMAN_BEHAVIOR=1."""
    if not HUMAN_BEHAVIOR:
        return
    try:
        box = locator.bounding_box(timeout=3000)
        if not box:
            return
        tx = box["x"] + box["width"] * random.uniform(0.3, 0.7)
        ty = box["y"] + box["height"] * random.uniform(0.3, 0.7)
        # a couple of waypoints, then the target — Playwright interpolates each move in `steps`.
        for _ in range(random.randint(1, 2)):
            page.mouse.move(random.uniform(50, 900), random.uniform(50, 600),
                            steps=random.randint(5, 12))
            time.sleep(random.uniform(0.05, 0.2))
        page.mouse.move(tx, ty, steps=random.randint(8, 20))
        time.sleep(random.uniform(0.1, 0.35))
    except Exception:
        pass


def _coherent_overrides_js(cf: dict, audio_noise: int = 3) -> str:
    """JS injected FIRST (before the legacy stealth) so the OS-substrate signals MATCH the spoofed UA:
    navigator.platform, navigator.userAgentData (+getHighEntropyValues), a TRUE removal of the webdriver
    property, plus B fingerprint-completeness surfaces (audio/mediaDevices/voices/battery)."""
    v = cf["ver"]
    _noise2 = audio_noise
    return f"""
// ── COHERENT fingerprint (STEALTH_HARDEN): one OS story across UA / platform / userAgentData ──
try {{ Object.defineProperty(navigator,'platform',{{get:()=>'{cf["platform"]}'}}); }} catch(e){{}}
// TRUE webdriver removal — delete the property so `'webdriver' in navigator` === false
try {{ delete Navigator.prototype.webdriver; }} catch(e){{}}
try {{ delete navigator.webdriver; }} catch(e){{}}
(function(){{
  const brands=[{{brand:'Chromium',version:'{v}'}},{{brand:'Google Chrome',version:'{v}'}},{{brand:'Not_A Brand',version:'24'}}];
  const fullList=[{{brand:'Chromium',version:'{v}.0.0.0'}},{{brand:'Google Chrome',version:'{v}.0.0.0'}},{{brand:'Not_A Brand',version:'24.0.0.0'}}];
  const uad={{
    brands:brands, mobile:false, platform:'{cf["uad_platform"]}',
    getHighEntropyValues:(hints)=>Promise.resolve({{
      architecture:'x86', bitness:'64', brands:brands, fullVersionList:fullList,
      mobile:false, model:'', platform:'{cf["uad_platform"]}', platformVersion:'{cf["plat_ver"]}',
      uaFullVersion:'{v}.0.0.0', wow64:false
    }}),
    toJSON:()=>({{brands:brands,mobile:false,platform:'{cf["uad_platform"]}'}})
  }};
  try {{ Object.defineProperty(navigator,'userAgentData',{{get:()=>uad, configurable:true}}); }} catch(e){{}}
}})();

// ── B: fingerprint COMPLETENESS (real-device surfaces that were unspoofed) ──
// AudioContext: add tiny deterministic noise to the audio fingerprint (coherent per-profile).
(function(){{
  const _an={_noise2};
  ['getChannelData'].forEach(fn=>{{
    const O=AudioBuffer.prototype[fn];
    if(O) AudioBuffer.prototype[fn]=function(...a){{
      const r=O.apply(this,a);
      for(let i=0;i<r.length;i+=1000){{ r[i]=r[i]+(_an*1e-7); }}
      return r;
    }};
  }});
  try{{const O2=AnalyserNode.prototype.getFloatFrequencyData;
    AnalyserNode.prototype.getFloatFrequencyData=function(a){{O2.apply(this,[a]);for(let i=0;i<a.length;i+=50){{a[i]=a[i]+_an*1e-4;}}}};}}catch(e){{}}
}})();
// mediaDevices: a coherent set (real desktops enumerate mic/camera/speaker) — not empty (headless tell).
try {{
  navigator.mediaDevices.enumerateDevices=()=>Promise.resolve([
    {{deviceId:'default',kind:'audioinput',label:'',groupId:'g1'}},
    {{deviceId:'default',kind:'audiooutput',label:'',groupId:'g1'}},
    {{deviceId:'cam1',kind:'videoinput',label:'',groupId:'g2'}}
  ]);
}} catch(e){{}}
// speechSynthesis voices (headless returns []; real Chrome has a set) — a coherent minimal list.
(function(){{
  const voices=[{{name:'Google US English',lang:'en-US',default:true,localService:false,voiceURI:'Google US English'}},
                {{name:'Microsoft David - English (United States)',lang:'en-US',default:false,localService:true,voiceURI:'Microsoft David'}}];
  try{{ if(window.speechSynthesis) speechSynthesis.getVoices=()=>voices; }}catch(e){{}}
}})();
// Battery (real desktops expose it; headless often doesn't).
try {{ navigator.getBattery=()=>Promise.resolve({{charging:true,chargingTime:0,dischargingTime:Infinity,level:1.0,
  addEventListener:()=>{{}},removeEventListener:()=>{{}}}}); }} catch(e){{}}
// mimeTypes coherent with the spoofed plugins.
try {{ Object.defineProperty(navigator,'pdfViewerEnabled',{{get:()=>true}}); }} catch(e){{}}
"""


def _make_stealth_script(vp_w: int, vp_h: int, fp_seed: str | None = None, ua: str | None = None) -> str:
    # Noise values: DETERMINISTIC per-account (stable device) when a seed is given + DETERMINISTIC_FP is on,
    # else random per-session (current behavior).
    _r = _seeded_rng(fp_seed)
    _noise = _r.randint(1, 9)
    # COHERENT mode (STEALTH_HARDEN + ua known): WebGL vendor/renderer match the OS of the UA; otherwise the
    # legacy (incoherent) random pick — kept only so the default path is byte-identical for A/B.
    if STEALTH_HARDEN and ua:
        _cf = _coherent_fp(ua)
        _gl_vendor, _gl_renderer = _cf["gl_vendor"], _cf["gl_renderer"]
        _coherent_js = _coherent_overrides_js(_cf, _r.randint(1, 9))
        _wd = "false"      # real Chrome returns navigator.webdriver === false (not undefined)
    else:
        _gl_vendor  = _r.choice(["Intel Inc.", "NVIDIA Corporation", "AMD"])
        _gl_renderer = _r.choice([
            "Intel Iris OpenGL Engine",
            "ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0)",
            "ANGLE (NVIDIA, NVIDIA GeForce GTX 1060 Direct3D11 vs_5_0 ps_5_0)",
        ])
        _coherent_js = ""
        _wd = "undefined"
    return _coherent_js + f"""
// Hide automation signals
Object.defineProperty(navigator,'webdriver',{{get:()=>{_wd}}});
delete navigator.__proto__.webdriver;

// Fake chrome object (real Chrome has this)
window.chrome={{runtime:{{}},loadTimes:function(){{}},csi:function(){{}},app:{{}}}};

// Realistic plugin list
Object.defineProperty(navigator,'plugins',{{get:()=>{{
  const p=[
    {{name:'Chrome PDF Plugin',filename:'internal-pdf-viewer',description:'Portable Document Format'}},
    {{name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai',description:''}},
    {{name:'Native Client',filename:'internal-nacl-plugin',description:''}}
  ];
  p.__proto__=PluginArray.prototype;
  return p;
}}}});

// Languages
Object.defineProperty(navigator,'languages',{{get:()=>['pt-PT','pt','en-US','en']}});

// Permissions API patch
const _origQuery=window.navigator.permissions.query;
window.navigator.permissions.query=(p)=>p.name==='notifications'
  ?Promise.resolve({{state:'denied'}}):_origQuery(p);

// Hardware / screen
Object.defineProperty(navigator,'hardwareConcurrency',{{get:()=>4}});
Object.defineProperty(navigator,'deviceMemory',{{get:()=>8}});
Object.defineProperty(screen,'colorDepth',{{get:()=>24}});
Object.defineProperty(screen,'width',{{get:()=>{vp_w}}});
Object.defineProperty(screen,'height',{{get:()=>{vp_h}}});

// Canvas fingerprint randomization (unique per session - prevents fingerprint tracking)
(function(){{
  const _noise = {_noise};
  const _oTDU = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = function(t,...a){{
    const ctx = this.getContext('2d');
    if(ctx && (t==='image/png'||!t)){{
      const w=this.width||300,h=this.height||150;
      if(w>0&&h>0){{
        ctx.fillStyle='rgba(255,255,255,0.0'+_noise+')';
        ctx.fillRect(0,0,1,1);
      }}
    }}
    return _oTDU.apply(this,[t,...a]);
  }};
  const _oGetCtx = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function(t,...a){{
    const ctx = _oGetCtx.apply(this,[t,...a]);
    if(ctx&&t==='2d'){{
      const _oFT=ctx.fillText.bind(ctx);
      ctx.fillText=function(tx,x,y,...rest){{
        ctx.save();
        ctx.shadowColor='rgba('+_noise+','+_noise+','+_noise+',0.01)';
        _oFT(tx,x,y,...rest);
        ctx.restore();
      }};
    }}
    return ctx;
  }};
}})();

// WebGL fingerprint (vendor/renderer)
(function(){{
  const _oGPA = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(p){{
    if(p===37445) return '{_gl_vendor}';
    if(p===37446) return '{_gl_renderer}';
    return _oGPA.call(this,p);
  }};
  if(window.WebGL2RenderingContext){{
    const _oGPA2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(p){{
      if(p===37445) return '{_gl_vendor}';
      if(p===37446) return '{_gl_renderer}';
      return _oGPA2.call(this,p);
    }};
  }}
}})();
"""


# -- Fake response (matches primp.Response interface) -------------------------

class _FakeResponse:
    def __init__(self, status_code: int, text: str, url: str):
        self.status_code = status_code
        self.text        = text
        self.url         = url
        self.cookies     = {}

    def json(self):
        try:
            return _json.loads(self.text)
        except _json.JSONDecodeError as exc:
            raise ValueError(f"browser_fetch response is not JSON: {self.text[:120]!r}") from exc


# -- PlaywrightSession ---------------------------------------------------------

def _kill_proc_tree(pid) -> None:
    """SIGKILL `pid` and ALL its descendants (Linux /proc). Best-effort, never raises.

    Used to force-kill a browser whose Playwright worker thread is HUNG (e.g. a page.goto stuck on a
    dead-proxy nav): the stuck thread never processes _CMD_CLOSE, so the graceful teardown
    (browser.close()/pw.stop()) never runs and the Chromium leaks. 0731 post-mortem: a SOAX bandwidth
    outage hung thousands of navs → close() timed out → 2600+ leaked chrome → inotify (128) exhausted →
    close() itself could no longer run → runaway leak → whole fleet died with Errno 24. Killing the driver
    (node) subtree here reclaims chrome + FDs + inotify immediately."""
    if not pid:
        return
    import os as _os, signal as _sig
    def _children(p):
        try:
            with open(f"/proc/{p}/task/{p}/children") as f:
                return [int(x) for x in f.read().split()]
        except Exception:
            return []
    order, seen, stack = [], set(), [int(pid)]
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p); order.append(p); stack.extend(_children(p))
    for p in reversed(order):        # kill children before parents
        try:
            _os.kill(p, _sig.SIGKILL)
        except Exception:
            pass


class PlaywrightSession:
    """
    Hybrid session:
    - Playwright browser bypasses the bot challenge and stays alive for verification.
    - primp.Client (with explicit Cookie header) handles all API calls (registration,
      login, apply steps) without the browser overhead of page.evaluate().
    - verify_email() uses the browser page to navigate to the token URL, which avoids
      the challenge page that primp alone would receive.

    Thread-safe: a dedicated worker thread owns Playwright.
    All primp calls are synchronous on the calling thread.
    verify_email() enqueues a job to the worker thread and waits for the result.
    close() enqueues a shutdown job and waits for the worker to exit.
    """

    _CMD_VERIFY          = "verify"
    _CMD_FETCH           = "fetch"
    _CMD_LOGIN           = "login"
    _CMD_EVAL            = "eval"
    _CMD_NAV             = "nav"
    _CMD_SAVE_STATE      = "save_state"
    _CMD_SCREENSHOT           = "screenshot"
    _CMD_NAV_SCREENSHOT       = "nav_screenshot"
    _CMD_CLOSE_BROWSER        = "close_browser"   # full shutdown after login; primp lives on
    _CMD_FORM_POST            = "form_post"        # DOM form.submit() -- real page nav, bypasses DataDome
    _CMD_SUBMIT_PAGE_FORM     = "submit_page_form"  # submit existing on-page form (no new form injected)
    _CMD_SUBMIT_FORM_BINARY   = "submit_form_binary"  # like submit_page_form, but returns raw response bytes
    _CMD_DATADOME_UNBLOCK     = "datadome_unblock"  # solve DataDome challenge for a non-login endpoint
    _CMD_GET_ALL_COOKIES      = "get_all_cookies"   # dump the FULL ctx cookie jar (all domains, incl httpOnly)
    _CMD_ADD_INIT             = "add_init"           # ctx.add_init_script(js) -- runs on every future nav
    _CMD_CLOSE           = "close"

    def __init__(self, proxy: str | None, inject_cookies: dict | None = None,
                 headless: bool = True, storage_state: str | None = None,
                 fp: tuple | None = None, inject_cookies_full: list | None = None,
                 bootstrap_url: str | None = None, fp_seed: str | None = None):
        self._fp_seed        = fp_seed        # per-account seed → deterministic fingerprint (DETERMINISTIC_FP)
        self.proxy           = proxy          # original URL - used by primp + CapSolver
        # Optional: land the launch bootstrap DIRECTLY on this URL instead of the default HOME→AUTH
        # (login-page) pair. Only for an already-authenticated re-open — a full jar (auth + DataDome)
        # must be injected so the target renders without a login redirect. Used by the SOAX→ISP swap to
        # skip 2 redundant login-page navs and land straight on Schedule.jsp. The DataDome challenge
        # check + primp setup below still run (on the target page). None = unchanged HOME→AUTH bootstrap.
        self._bootstrap_url  = bootstrap_url
        self._inject_cookies = inject_cookies or {}   # pre-load cookies -> skip WAF challenge
        # Full Playwright cookie dicts (name,value,domain,path,…) — carries cross-domain/httpOnly cookies
        # (e.g. DataDome) that the {name:value}@pedidodevistos injection above cannot. Used by the
        # SOAX→ISP migration probe to give the new browser the complete warm jar.
        self._inject_cookies_full = inject_cookies_full or []
        self._headless       = headless
        self._storage_state  = storage_state  # Playwright storage_state path for session restore
        self._fp_override    = fp             # fixed fingerprint tuple for session restoration
        self._fp: tuple | None = None         # set by _worker once fingerprint is chosen
        # Chromium cannot authenticate with SOCKS5 user:pass; route via a local
        # HTTP->SOCKS5 bridge. primp handles socks5:// natively and needs no bridge.
        if proxy and proxy.startswith('socks5://'):
            from proxy_bridge import start_bridge
            self._playwright_proxy = start_bridge(proxy)
        else:
            self._playwright_proxy = proxy
        self._primp   = None
        self._cookie_header: str = ""
        self._cmd_q:  queue.Queue = queue.Queue()
        self._seq_lock = threading.Lock()
        self._next_seq = 0      # command sequence, assigned by _send
        self._current_seq = 0   # the command the worker thread is serving right now
        self._res_q = _StampedResultQueue(self)
        self._driver_pid = None    # Playwright driver (node) pid — its subtree is THIS session's chromium;
                                   # force-killed if a hung worker prevents graceful close (see _kill_proc_tree)
        self._xvfb_proc = None     # a virtual display started for HEADLESS_MODE=headful on a display-less
                                   # host; terminated in close() so it doesn't leak
        self._donut_lease = None   # BROWSER_BACKEND=donut: the running Donut profile; stopped through
                                   # Donut's API on every teardown path (killing the driver does not stop it)
        # Last JS alert()/confirm() message the portal raised, captured by a page dialog handler and
        # read from the main thread. The portal signals "this applicant already has a request" as a
        # JS ALERT ("Este requerente já tem um pedido registado"), which Playwright auto-dismisses --
        # so a fetch-based flow never saw it (0 occurrences in logs). Set in the worker thread, read
        # via browser_last_dialog(); a plain str assignment is atomic enough across the GIL.
        self._last_dialog = None
        self._thread  = threading.Thread(target=self._worker, daemon=True)
        self._ready   = threading.Event()
        self._error: Exception | None = None
        self._thread.start()
        # The deadline must COVER the work it waits for (FAILURE_MODES F4: a caller timeout below the
        # callee's worst case abandons a still-live operation). The bootstrap inside _worker navigates
        # HOME (30 s) + AUTH (25 s) + waits for the form (15 s), each x NAV_TIMEOUT_SCALE, plus launch
        # and a 10 s settle -- ~290 s at production's scale 4, against a FIXED 120 s here. On
        # 2026-10-26 (Test 3059 x2) that killed 12 browser starts on a slow-but-answering portal, each
        # then restarted from scratch into the same slow navs. 120 s stays the floor (scale 1).
        _init_deadline = max(120.0, 30.0 + 70.0 * _nav_scale())
        if not self._ready.wait(timeout=_init_deadline):
            # F49 (run 1023): the caller never gets this object, so nothing would ever close() it --
            # the hung worker thread's Chromium was orphaned (x200 inits, 210 orphans, ~1,065 chrome
            # procs, load ~12), and every re-login during the outage started another one. Same
            # force-kill close()/close_browser() use for a hung worker. The driver pid is captured
            # right after Playwright starts, so it is set for any hang in launch or the bootstrap
            # navs; a hang inside Playwright's own start (no pid yet) has no browser to leak.
            _kill_proc_tree(self._driver_pid)
            _donut.release(self._donut_lease)
            if self._xvfb_proc is not None:
                try:
                    self._xvfb_proc.terminate()
                except Exception:
                    pass
                self._xvfb_proc = None
            raise RuntimeError(f"PlaywrightSession init timed out (>{_init_deadline:.0f}s)")
        if self._error:
            raise self._error

    def __del__(self):
        # GC fallback: if close() was never called, signal the worker thread to exit.
        # Timing is non-deterministic but prevents indefinitely orphaned processes.
        try:
            if self._thread.is_alive():
                self._cmd_q.put({"action": self._CMD_CLOSE})
        except Exception:
            pass

    # -- Worker thread (owns Playwright) --------------------------------------

    def _worker(self):
        # DRIVER=patchright → use the patched Playwright fork that defers Runtime.enable + hides the main
        # CDP-runtime leaks (the automation-protocol tell WAFs probe for). Drop-in, reuses the same Chromium.
        # DRIVER=playwright (default) = vanilla, unchanged. A1 anti-detection A/B.
        if os.environ.get("DRIVER", "playwright").strip().lower() == "patchright":
            from patchright.sync_api import sync_playwright
            print("[session] DRIVER=patchright (CDP-runtime leak deferred)")
        else:
            from playwright.sync_api import sync_playwright
        browser = None
        pw = None
        page = None
        # Launch-to-ready timing, per backend: the A/B report (probes/backend_ab.py) reads these two
        # lines to compare how long a session takes to become usable on chromium vs donut.
        _t_launch = time.time()
        _backend = "donut" if _donut.enabled() else "chromium"
        print(f"[session] launch begin backend={_backend}")
        try:
            if _donut.enabled():
                # BROWSER_BACKEND=donut: the browser is a Donut (Wayfern) profile we attach to over CDP;
                # Wayfern owns the fingerprint, so none of the Chromium launch/fingerprint code below runs.
                pw = sync_playwright().start()
                try:
                    self._driver_pid = pw._impl_obj._connection._transport._proc.pid
                except Exception:
                    self._driver_pid = None
                browser, ctx = self._launch_donut(pw)
            else:
                server, username, password = _parse_proxy(self._playwright_proxy)
                proxy_config = None
                if server:
                    proxy_config = {"server": server}
                    if username:
                        proxy_config["username"] = username
                    if password:
                        proxy_config["password"] = password

                if self._fp_override:
                    ua, sec_ch_ua, vp_w, vp_h, platform = self._fp_override
                    print(f"[session] fp restored  ua=...{ua[55:85]}  vp={vp_w}x{vp_h}")
                else:
                    ua, sec_ch_ua, vp_w, vp_h, platform = _pick_fingerprint(self._fp_seed)
                self._fp = (ua, sec_ch_ua, vp_w, vp_h, platform)
                tz = _get_proxy_timezone(self.proxy)

                # P1 launch flags (default-off): browser mode + WebRTC leak block.
                _args = ["--disable-blink-features=AutomationControlled",
                         "--disable-dev-shm-usage", "--no-sandbox"]
                if WEBRTC_BLOCK:
                    # WebRTC uses ONLY the proxy path → no STUN leak of the server's real IP.
                    _args.append("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
                if HEADLESS_MODE == "new":
                    _launch_headless = False; _args.append("--headless=new")
                elif HEADLESS_MODE == "headful":
                    # Headful needs a real display. Reuse $DISPLAY if the operator set one (e.g. ran the
                    # supervisor under xvfb-run); otherwise best-effort start a per-process Xvfb. If no
                    # display can be had, DON'T crash the worker -- fall back to --headless=new (which is
                    # still far less detectable than old) with a loud warning.
                    _disp, self._xvfb_proc = _ensure_headful_display()
                    if _disp:
                        os.environ["DISPLAY"] = _disp
                        _launch_headless = False
                        print(f"[session] browser mode = headful on DISPLAY={_disp}"
                              + ("  (auto-Xvfb)" if self._xvfb_proc is not None else "  ($DISPLAY)"))
                    else:
                        print("[session] HEADLESS_MODE=headful but no display and no Xvfb available "
                              "-- falling back to --headless=new")
                        _launch_headless = False; _args.append("--headless=new")
                else:
                    _launch_headless = self._headless
                if HEADLESS_MODE in ("new", "headful"):
                    print(f"[session] browser mode = {HEADLESS_MODE}"
                          + ("  +webrtc-block" if WEBRTC_BLOCK else ""))
                # Single per-session flag-dump so EVERY run's log verifiably shows exactly which anti-detection
                # branches are active (no more guessing whether an A/B argument actually took effect).
                print(f"[session] ANTIDETECT flags: headless={HEADLESS_MODE} stealth_harden={int(STEALTH_HARDEN)} "
                      f"det_fp={int(DETERMINISTIC_FP)} webrtc_block={int(WEBRTC_BLOCK)} human={int(HUMAN_BEHAVIOR)} "
                      f"native_solve={int(os.environ.get('NATIVE_SOLVE','').strip()=='1')} "
                      f"driver={os.environ.get('DRIVER','playwright').strip() or 'playwright'} "
                      f"fp_seed={'set' if self._fp_seed else 'NONE'}")

                pw = sync_playwright().start()
                # Capture the driver (node) subprocess pid so close() can force-kill this session's whole
                # browser subtree if the worker thread ever hangs (dead-proxy nav) and can't close gracefully.
                try:
                    self._driver_pid = pw._impl_obj._connection._transport._proc.pid
                except Exception:
                    self._driver_pid = None
                browser = pw.chromium.launch(
                    headless=_launch_headless,
                    args=_args,
                    proxy=proxy_config,
                    # Pass the env EXPLICITLY so headful Chromium actually sees DISPLAY. Relying on
                    # inheritance failed live 2026-09-23: Xvfb sockets existed but Chromium still died
                    # "launched a headed browser without a XServer running" -- the DISPLAY set on
                    # os.environ mid-launch did not reach the Playwright-spawned browser. This is the
                    # env the browser process gets.
                    env=dict(os.environ),
                )
                # ALIGN THE ADVERTISED VERSION WITH THE BROWSER WE ARE ACTUALLY RUNNING.
                #
                # _FP_POOL hardcodes Chrome 130/131. The Chromium that Playwright now ships is 149. So
                # every request advertised Chrome/131 while presenting 149's TLS handshake, JS feature
                # set and header ordering -- an 18-version mismatch, and one of the cheapest automation
                # signals there is to detect. It is also PROXY-INDEPENDENT, which is exactly the
                # operator's observation (2026-08-24): manual Chrome books fine through the very same
                # webshare and soax exits that our sessions cannot use.
                #
                # The pool was presumably correct when this engine was proven -- a Playwright upgrade
                # moved the browser and left the fingerprint behind, silently. Deriving the major
                # version from browser.version keeps them aligned across every future upgrade instead
                # of re-rotting; the pool still supplies the viewport/platform/brand variety it exists
                # for.
                try:
                    _real_major = (browser.version or "").split(".")[0]
                    if _real_major.isdigit() and _real_major not in ua:
                        import re as _re
                        _old_major = (_re.search(r"Chrome/(\d+)", ua) or [None, "?"])[1]
                        ua = _re.sub(r"(Chrome|Edg)/\d+", lambda m: f"{m.group(1)}/{_real_major}", ua)
                        # Only the REAL brands get the version bumped. The deliberately-fake
                        # "Not_A Brand" entry carries its own arbitrary version (the working HAR shows
                        # "Not;A=Brand";v="99" beside "Google Chrome";v="139"), and rewriting it to
                        # match would itself be an anomaly.
                        sec_ch_ua = _re.sub(
                            r'("(?:Google Chrome|Chromium|Microsoft Edge)";v=")\d+"',
                            lambda m: f'{m.group(1)}{_real_major}"', sec_ch_ua)
                        self._fp = (ua, sec_ch_ua, vp_w, vp_h, platform)
                        print(f"[session] fp version aligned: claimed Chrome/{_old_major} -> "
                              f"actual Chrome/{_real_major}")
                except Exception as _ve:
                    print(f"[session] fp version alignment skipped: {_ve}")

                ctx_kwargs = dict(
                    user_agent=ua,
                    viewport={"width": vp_w, "height": vp_h},
                    screen={"width": vp_w, "height": vp_h},
                    locale="pt-PT",   # PT-consistent: lang=PT session -> Portuguese-speaking browser
                    timezone_id=tz,
                    extra_http_headers={
                        "sec-ch-ua":          sec_ch_ua,
                        "sec-ch-ua-mobile":   "?0",
                        "sec-ch-ua-platform": f'"{platform}"',
                    },
                )
                if self._storage_state:
                    ctx_kwargs["storage_state"] = self._storage_state
                    print(f"[session] restoring session from {self._storage_state}")
                ctx = browser.new_context(**ctx_kwargs)
                ctx.add_init_script(_make_stealth_script(vp_w, vp_h, self._fp_seed, ua))

            # Pre-inject saved cookies so the server skips the WAF challenge
            # (used when reopening a browser for email verification after closing
            # the registration browser to free RAM during the email-poll wait).
            if self._inject_cookies_full:
                # Full jar (all domains/httpOnly) — filter to Playwright add_cookies-accepted fields.
                _allowed = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
                _ck = [{k: c[k] for k in _allowed if k in c} for c in self._inject_cookies_full]
                ctx.add_cookies(_ck)
                print(f"[session] pre-injected {len(_ck)} FULL cookies "
                      f"(names={[c.get('name') for c in _ck]})")
            elif self._inject_cookies:
                ctx.add_cookies([
                    {"name": k, "value": v, "domain": "pedidodevistos.mne.gov.pt",
                     "path": "/"}
                    for k, v in self._inject_cookies.items()
                ])
                print(f"[session] pre-injected {len(self._inject_cookies)} cookies (verify mode)")

            # A Donut profile opens with its own first tab -- drive that one rather than leave an
            # extra blank tab beside it.
            page = ctx.pages[0] if (self._donut_lease and ctx.pages) else ctx.new_page()
            if self._donut_lease:
                self._log_donut_fingerprint(page)

            # CAPTURE JS DIALOGS. The portal raises "Este requerente já tem um pedido registado"
            # (this applicant already has a request) as a JS alert(); with no handler Playwright
            # auto-dismisses it and the message is lost, so the worker only saw an opaque warmup
            # failure and re-ran the same permanently-blocked identity forever. Record the message
            # (read by the worker via browser_last_dialog) and accept the dialog so the flow proceeds.
            def _on_dialog(dialog):
                try:
                    self._last_dialog = dialog.message or ""
                except Exception:
                    self._last_dialog = ""
                try:
                    dialog.accept()
                except Exception:
                    try:
                        dialog.dismiss()
                    except Exception:
                        pass
            try:
                page.on("dialog", _on_dialog)
            except Exception as _de:
                print(f"[session] dialog handler not installed: {_de}")

            # Apply playwright-stealth for comprehensive bot-detection evasion.
            # Patches 20+ signals: canvas getImageData noise, audio fingerprint,
            # getBoundingClientRect, Function.toString, webdriver, chrome runtime, etc.
            # OPT-IN (PLAYWRIGHT_STEALTH=1). Default OFF: the deterministic per-account fingerprint
            # script above is the proven path, and playwright-stealth can OVERRIDE its canvas/WebGL
            # noise (breaking determinism) -- so it is gated for A/B rather than silently always-on
            # the moment the package is installed. Validate LIVE before making it default.
            if os.environ.get("PLAYWRIGHT_STEALTH", "0").strip() == "1" and not self._donut_lease:
                try:
                    from playwright_stealth import Stealth
                    Stealth().apply_stealth_sync(page)
                    print("[session] playwright-stealth applied (on top of the fallback fp script)")
                except ImportError:
                    print("[session] PLAYWRIGHT_STEALTH=1 but the package is not installed -- fallback only")
                except Exception as _se:
                    print(f"[session] playwright-stealth warning: {_se}")

            # Bootstrap HOME -> AUTH with a Referer, on domcontentloaded + the username field — NEVER
            # networkidle (this portal's WAF telemetry means the page never goes idle, so a networkidle
            # wait just burns its whole timeout; see browser_login for the full note). Same pattern used
            # for every browser session open here.
            if self._bootstrap_url:
                # Direct bootstrap (SOAX→ISP swap): the full warm jar is already injected, so land
                # straight on the target (Schedule.jsp) — skip the 2 redundant login-page navs. Referer
                # HOME so the request looks in-flow. No username-field wait (this isn't the login page).
                page.goto(HOME_URL, timeout=int(15000 * _nav_scale()), wait_until="domcontentloaded")
                page.goto(self._bootstrap_url, timeout=int(30000 * _nav_scale()),
                          wait_until="domcontentloaded", referer=HOME_URL)
                time.sleep(1)
            else:
                page.goto(HOME_URL, timeout=int(30000 * _nav_scale()), wait_until="domcontentloaded")
                page.goto(AUTH_URL, timeout=int(25000 * _nav_scale()), wait_until="domcontentloaded",
                          referer=HOME_URL)
                try:
                    page.wait_for_selector(
                        'input[name="username"], input#username, input[autocomplete="username"]',
                        timeout=int(15000 * _nav_scale()))
                except Exception:
                    pass
                time.sleep(2)

            # Same "page is navigating" race as every other page.content() call site
            # in this file — a 5th occurrence found 2026-06-18, here at the very start
            # of get_session() before login even begins. Same retry-loop fix applied.
            content = ""
            for _ci in range(6):
                try:
                    content = page.content() or ""
                    break
                except Exception:
                    time.sleep(1.5)
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
            challenge = "/ch/bd.js" in content
            raw_cookies = ctx.cookies()
            cookie_dict = {c["name"]: c["value"] for c in raw_cookies}
            cookie_names = list(cookie_dict.keys())
            print(f"[session] challenge={challenge}  cookies={cookie_names}")

            if challenge:
                raise RuntimeError("Bot challenge not bypassed - proxy IP flagged")

            # -- Set up primp client with cookies ------------------------------
            import primp
            primp_client = primp.Client(
                impersonate=IMPERSONATE,
                proxy=self.proxy,
                verify=False,
                follow_redirects=True,
                timeout=30,
            )
            # set_cookies() puts cookies in primp's jar (works for API endpoints).
            # The browser stays alive to handle verify_email() which requires the
            # same browser session (token URL has stricter fingerprint checks).
            if cookie_dict:
                try:
                    primp_client.set_cookies(AUTH_URL, cookie_dict)
                except Exception:
                    # Fallback: cookie header for all requests
                    self._cookie_header = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
                    print(f"[session] cookies -> header fallback: {cookie_names}")
            self._primp = primp_client
            print(f"[session] ready backend={_backend} in {time.time() - _t_launch:.1f}s")
            self._ready.set()

            # -- Event loop: wait for verify or close commands -----------------
            while True:
                cmd = self._cmd_q.get()
                if cmd is None or cmd.get("action") == self._CMD_CLOSE:
                    break
                # Every reply this iteration puts is stamped with this seq (_StampedResultQueue).
                self._current_seq = cmd.get("_seq", 0)

                if cmd.get("action") == self._CMD_VERIFY:
                    try:
                        result = self._do_browser_verify(page, ctx, cmd)
                        self._res_q.put(("ok", result))
                    except Exception as exc:
                        self._res_q.put(("err", str(exc)))

                elif cmd.get("action") == self._CMD_FETCH:
                    try:
                        result = self._do_browser_fetch(page, cmd)
                        self._res_q.put(("ok", result))
                    except Exception as exc:
                        self._res_q.put(("err", str(exc)))

                elif cmd.get("action") == self._CMD_DATADOME_UNBLOCK:
                    try:
                        result = self._do_datadome_unblock(page, cmd)
                        self._res_q.put(("ok", result))
                    except Exception as exc:
                        self._res_q.put(("err", str(exc)))

                elif cmd.get("action") == self._CMD_LOGIN:
                    try:
                        result = self._do_browser_login(page, cmd)
                        self._res_q.put(("ok", result))
                    except Exception as exc:
                        self._res_q.put(("err", str(exc)))

                elif cmd.get("action") == self._CMD_EVAL:
                    try:
                        result = page.evaluate(cmd["js"])
                        self._res_q.put(("ok", result))
                    except Exception as exc:
                        self._res_q.put(("err", str(exc)))

                elif cmd.get("action") == self._CMD_GET_ALL_COOKIES:
                    try:
                        self._res_q.put(("ok", ctx.cookies()))
                    except Exception as exc:
                        self._res_q.put(("err", str(exc)))

                elif cmd.get("action") == self._CMD_ADD_INIT:
                    # Register a script that Playwright runs at the START of every future navigation
                    # in this context (before the page's own scripts) -- the only place to install a
                    # fetch/XHR hook that must be present BEFORE a page-load-triggered request fires.
                    try:
                        ctx.add_init_script(cmd["js"])
                        self._res_q.put(("ok", True))
                    except Exception as exc:
                        self._res_q.put(("err", str(exc)))

                elif cmd.get("action") == self._CMD_NAV:
                    try:
                        # Sync primp cookies -> browser before navigating so the browser
                        # carries any session-cookie updates that primp acquired since login.
                        # SKIP this sync when the caller sets skip_cookie_sync=True — used for
                        # Questionario navigation to preserve the browser's post-login session
                        # (primp's alive-check GET may produce different cookies than the browser's).
                        if self._primp is not None and not cmd.get("skip_cookie_sync"):
                            try:
                                raw_c = self._primp.get_cookies(cmd["url"]) or {}
                                if raw_c:
                                    print(f"[session] nav cookie-sync: {list(raw_c.keys())}")
                                    ctx.add_cookies([
                                        {"name": k, "value": v, "url": cmd["url"]}
                                        for k, v in raw_c.items()
                                    ])
                                else:
                                    print(f"[session] nav cookie-sync: no cookies for {cmd['url'].split('/')[-1]}")
                            except Exception as _ce:
                                print(f"[session] nav cookie-sync error: {_ce}")
                        elif cmd.get("skip_cookie_sync"):
                            print(f"[session] nav cookie-sync: skipped (skip_cookie_sync=True) for {cmd['url'].split('/')[-1]}")
                        page.goto(cmd["url"], timeout=cmd.get("nav_timeout", 30000))
                        page.wait_for_load_state("networkidle", timeout=15000)
                        if not cmd.get("fast_nav"):
                            # DataDome's JS challenge fires asynchronously after networkidle.
                            # Wait for it to complete and the real page to load.
                            page.wait_for_timeout(4000)
                            # If DataDome is still showing (bd.js present), wait more.
                            if "/ch/bd.js" in (page.content() or ""):
                                page.wait_for_load_state("networkidle", timeout=10000)
                                page.wait_for_timeout(3000)
                        self._res_q.put(("ok", page.url))
                    except Exception as exc:
                        try:
                            self._res_q.put(("ok", page.url))
                        except Exception:
                            self._res_q.put(("err", str(exc)))

                elif cmd.get("action") == self._CMD_SCREENSHOT:
                    try:
                        _SCREENSHOTS_DIR.mkdir(exist_ok=True)
                        page.screenshot(path=cmd["path"], full_page=True)
                        self._res_q.put(("ok", cmd["path"]))
                    except Exception as exc:
                        self._res_q.put(("err", str(exc)))

                elif cmd.get("action") == self._CMD_NAV_SCREENSHOT:
                    try:
                        page.goto(cmd["url"], timeout=20000)
                        page.wait_for_load_state("networkidle", timeout=10000)
                        page.wait_for_timeout(2000)
                        _SCREENSHOTS_DIR.mkdir(exist_ok=True)
                        page.screenshot(path=cmd["path"], full_page=True)
                        self._res_q.put(("ok", cmd["path"]))
                    except Exception as exc:
                        self._res_q.put(("err", str(exc)))

                elif cmd.get("action") == self._CMD_FORM_POST:
                    # Real browser form POST via DOM form.submit(). Causes a genuine
                    # page navigation (not fetch()), so DataDome auto-solves the challenge.
                    try:
                        post_url  = cmd["url"]
                        post_data = cmd.get("data", {})
                        # Log the browser's current cookiesession1 (for diagnostics).
                        # DO NOT overwrite browser cookies with primp's — the browser session
                        # accumulated questionnaire state via XHR calls and primp's stale
                        # cookiesession1 would reset it to a session with no questionnaire state.
                        try:
                            _br_cookies = {c["name"]: c["value"] for c in ctx.cookies()}
                            _br_csid = _br_cookies.get("cookiesession1", "")[:12]
                            print(f"[session] form_post browser-csid={_br_csid}...  "
                                  f"cookies={list(_br_cookies.keys())}")
                            # Reverse-sync: update primp to match browser's current session.
                            if self._primp is not None and _br_cookies:
                                try:
                                    self._primp.set_cookies(post_url, _br_cookies)
                                except Exception:
                                    pass
                        except Exception as _ce:
                            print(f"[session] form_post browser-cookie read error: {_ce}")
                        fields_js = _json.dumps(post_data)
                        url_js    = _json.dumps(post_url)
                        try:
                            with page.expect_navigation(wait_until="load", timeout=45000):
                                page.evaluate(f"""() => {{
                                    const f = document.createElement('form');
                                    f.method = 'POST';
                                    f.action = {url_js};
                                    f.enctype = 'application/x-www-form-urlencoded';
                                    const fields = {fields_js};
                                    for (const [k, v] of Object.entries(fields)) {{
                                        const i = document.createElement('input');
                                        i.type = 'hidden'; i.name = k; i.value = String(v);
                                        f.appendChild(i);
                                    }}
                                    document.body.appendChild(f);
                                    f.submit();
                                }}""")
                        except Exception as _nav_e:
                            # expect_navigation/evaluate can itself raise the "page is
                            # navigating" race — the form submit DID trigger navigation;
                            # fall through to the content-retry loop instead of giving up.
                            print(f"[session] form_post: expect_navigation raced ({_nav_e}) - retrying content read")
                        _content = ""
                        for _ci in range(6):
                            try:
                                page.wait_for_timeout(1500)
                                _content = page.content() or ""
                                break
                            except Exception:
                                try:
                                    page.wait_for_load_state("networkidle", timeout=10000)
                                except Exception:
                                    pass
                        if "/ch/bd.js" in _content:
                            page.wait_for_load_state("networkidle", timeout=10000)
                            page.wait_for_timeout(3000)
                            _content = page.content() or ""
                        if not _content:
                            # All retries failed to read content while navigating. The POST
                            # likely already succeeded server-side; re-navigate to wherever the
                            # page actually landed and read fresh content instead of giving up.
                            try:
                                _landed_url = page.url
                                page.goto(_landed_url, wait_until="networkidle", timeout=20000)
                                _content = page.content() or ""
                                print(f"[session] form_post: re-navigated to landed url {_landed_url} -> len={len(_content)}")
                            except Exception as _re:
                                print(f"[session] form_post: re-navigate fallback failed: {_re}")
                        html = _content
                        print(f"[session] form_post -> {page.url}  len={len(html)}")
                        self._res_q.put(("ok", page.url, html))
                    except Exception as exc:
                        self._res_q.put(("err", str(exc), ""))

                elif cmd.get("action") == self._CMD_SUBMIT_PAGE_FORM:
                    # Submit an existing form already on the page (set up by questionnaire XHR steps).
                    # Does NOT inject a new form — uses whatever form action/fields the page has.
                    try:
                        target_action = cmd.get("action_contains", "Formulario")
                        # Log current form state for diagnostics
                        _diag_js = f"""(() => {{
                            const forms = Array.from(document.querySelectorAll('form'));
                            return forms.map(f => {{
                                const fd = new FormData(f);
                                const fields = {{}};
                                for (const [k, v] of fd.entries()) {{ fields[k] = v; }}
                                return {{action: f.action, method: f.method, nfields: Object.keys(fields).length, fieldNames: Object.keys(fields)}};
                            }});
                        }})()"""
                        _form_info = page.evaluate(_diag_js)
                        print(f"[session] submit_page_form: found {len(_form_info or [])} forms: {_form_info}")
                        # Find the form targeting Formulario (or first form if none found)
                        _submit_js = f"""(() => {{
                            const forms = Array.from(document.querySelectorAll('form'));
                            const f = forms.find(x => x.action && x.action.includes({_json.dumps(target_action)})) || forms[0];
                            if (!f) return 'no_form';
                            f.submit();
                            return 'submitted:' + f.action;
                        }})()"""
                        # Check form exists before starting expect_navigation
                        _check_js = f"""(() => {{
                            const forms = Array.from(document.querySelectorAll('form'));
                            const f = forms.find(x => x.action && x.action.includes({_json.dumps(target_action)})) || forms[0];
                            return f ? f.action : null;
                        }})()"""
                        _form_action = page.evaluate(_check_js)
                        print(f"[session] submit_page_form: target form action={_form_action}")
                        if not _form_action:
                            self._res_q.put(("err", "no_form_found", ""))
                        else:
                            try:
                                with page.expect_navigation(wait_until="load", timeout=45000):
                                    _submit_result = page.evaluate(_submit_js)
                                print(f"[session] submit_page_form submitted: {_submit_result}")
                            except Exception as _nav_e:
                                print(f"[session] submit_page_form: expect_navigation raced ({_nav_e}) - retrying content read")
                            _content = ""
                            for _ci in range(6):
                                try:
                                    page.wait_for_timeout(1500)
                                    _content = page.content() or ""
                                    break
                                except Exception:
                                    try:
                                        page.wait_for_load_state("networkidle", timeout=10000)
                                    except Exception:
                                        pass
                            if "/ch/bd.js" in _content:
                                page.wait_for_load_state("networkidle", timeout=10000)
                                page.wait_for_timeout(3000)
                                _content = page.content() or ""
                            html = _content
                            print(f"[session] submit_page_form -> {page.url}  len={len(html)}")
                            self._res_q.put(("ok", page.url, html))
                    except Exception as exc:
                        self._res_q.put(("err", str(exc), ""))

                elif cmd.get("action") == self._CMD_SUBMIT_FORM_BINARY:
                    # Same DOM form.submit() trick as submit_page_form (real navigation,
                    # bypasses DataDome) but for endpoints that respond with a binary
                    # payload (e.g. MostrarPdf's application/pdf) rather than HTML.
                    # page.content() can't be used here -- Chromium's built-in PDF viewer
                    # intercepts the response into its own viewer shell, not raw bytes.
                    #
                    # expect_response() + response.body() (the first approach tried here)
                    # is unreliable for a main-frame navigation TO a binary resource --
                    # confirmed live: "Protocol error (Network.getResponseBody): No
                    # resource with given identifier found". Chrome can evict the response
                    # from its network cache before Playwright's CDP call retrieves it,
                    # especially once the main frame has already navigated/repainted.
                    # page.route() interception avoids this: route.fetch() performs the
                    # request itself and returns a completed APIResponse with the body
                    # already downloaded, then route.fulfill() replays it to the browser
                    # so the real navigation still happens normally (same WAF-bypass
                    # characteristics -- one real request/response cycle either way).
                    try:
                        target_action = cmd.get("action_contains", "MostrarPdf")
                        _check_js = f"""(() => {{
                            const forms = Array.from(document.querySelectorAll('form'));
                            const f = forms.find(x => x.action && x.action.includes({_json.dumps(target_action)})) || forms[0];
                            return f ? f.action : null;
                        }})()"""
                        _form_action = page.evaluate(_check_js)
                        print(f"[session] submit_form_binary: target form action={_form_action}")
                        if not _form_action:
                            self._res_q.put(("err", "no_form_found"))
                        else:
                            _captured: dict = {}

                            def _handle_route(route, _a=target_action):
                                _resp = route.fetch()
                                _captured["status"]  = _resp.status
                                _captured["headers"] = _resp.headers
                                _captured["body"]    = _resp.body()
                                _captured["url"]     = route.request.url
                                route.fulfill(response=_resp)

                            _route_pattern = f"**/*{target_action}*"
                            page.route(_route_pattern, _handle_route)
                            try:
                                _submit_js = f"""(() => {{
                                    const forms = Array.from(document.querySelectorAll('form'));
                                    const f = forms.find(x => x.action && x.action.includes({_json.dumps(target_action)})) || forms[0];
                                    f.submit();
                                    return 'submitted:' + f.action;
                                }})()"""
                                page.evaluate(_submit_js)
                                for _ in range(20):  # up to ~10s for the route to fire
                                    if _captured:
                                        break
                                    page.wait_for_timeout(500)
                            finally:
                                page.unroute(_route_pattern, _handle_route)

                            if not _captured:
                                self._res_q.put(("err", "route_never_captured"))
                            else:
                                print(f"[session] submit_form_binary -> {_captured['url']}  "
                                      f"status={_captured['status']}  "
                                      f"content-type={_captured['headers'].get('content-type','')}  "
                                      f"len={len(_captured['body'])}")
                                self._res_q.put(("ok", {
                                    "status": _captured["status"],
                                    "content_type": _captured["headers"].get("content-type", ""),
                                    "url": _captured["url"],
                                    "body": _captured["body"],
                                }))
                    except Exception as exc:
                        self._res_q.put(("err", str(exc)))

                elif cmd.get("action") == self._CMD_CLOSE_BROWSER:
                    # Full browser shutdown after login - primp carries the session forward.
                    # The _run_loop exits; the outer finally block closes the browser.
                    self._res_q.put(("ok", None))
                    # Drain any commands queued after CLOSE_BROWSER so their callers
                    # don't block indefinitely waiting for a result that will never come.
                    while True:
                        try:
                            _leftover = self._cmd_q.get_nowait()
                            _lr = _leftover.get("_reply") or self._res_q
                            _lr.put(("err", "browser_closed"))
                        except queue.Empty:
                            break
                    break

                elif cmd.get("action") == self._CMD_SAVE_STATE:
                    try:
                        ctx.storage_state(path=cmd["path"])
                        # Write companion profile so the same fingerprint can be
                        # restored on next get_session_from_state() call.
                        if self._fp:
                            import json as _js
                            profile = {
                                "user_agent": self._fp[0],
                                "sec_ch_ua":  self._fp[1],
                                "viewport_w": self._fp[2],
                                "viewport_h": self._fp[3],
                                "platform":   self._fp[4],
                                "proxy":      self.proxy,
                                "saved_at":   time.time(),
                            }
                            p = Path(cmd["path"])
                            profile_path = p.parent / (p.stem + "_profile.json")
                            profile_path.write_text(_js.dumps(profile, indent=2), encoding="utf-8")
                        self._res_q.put(("ok", cmd["path"]))
                    except Exception as exc:
                        self._res_q.put(("err", str(exc)))

        except Exception as exc:
            self._error = exc
            self._ready.set()
        finally:
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
            try:
                if pw:
                    pw.stop()
            except Exception:
                pass
            # Over CDP, browser.close() only disconnects -- the Donut profile keeps running until
            # Donut is told to stop it.
            _donut.release(self._donut_lease)
            # Terminate a private Xvfb this session started, even if the browser LAUNCH failed (the
            # caller's close() never runs when __init__ raises, so this is the only cleanup for a
            # failed-launch headful worker -- otherwise every failed headful launch leaks a display).
            if self._xvfb_proc is not None:
                try:
                    self._xvfb_proc.terminate()
                except Exception:
                    pass
                self._xvfb_proc = None
            print(f"[session] browser closed")

    def _launch_donut(self, pw):
        """BROWSER_BACKEND=donut: start this session's Donut profile on our exit and attach to it.
        Runs on the worker thread. Returns (browser, ctx) like the Chromium launch does."""
        self._donut_lease = _donut.launch(self.proxy, self._fp_seed)
        browser = _donut.connect(pw, self._donut_lease, timeout_s=30.0 * _nav_scale())
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        # No engine fingerprint in this mode (Wayfern's is the only one), so SAVE_STATE writes no
        # companion fingerprint profile and a restore cannot force one onto the profile.
        self._fp = None
        if _donut._keep_state():
            # A kept profile carries last session's cookies. The portal's must not survive into a fresh
            # login (a stale Vistos_sid / cookiesession1 is exactly the state a new login must not see);
            # everything else the profile accumulated stays.
            try:
                ctx.clear_cookies(domain=urlparse(BASE).hostname or "")
            except Exception as _ke:
                print(f"[session] kept-profile portal-cookie clear failed: {_ke}")
        if self._storage_state:
            # A Donut profile's context already exists, so storage_state cannot be passed at
            # creation; the jar is what a restore needs (the portal keeps no state in localStorage).
            try:
                _st = _json.loads(Path(self._storage_state).read_text(encoding="utf-8"))
                ctx.add_cookies(_st.get("cookies") or [])
                print(f"[session] restoring session cookies from {self._storage_state}")
            except Exception as _re:
                print(f"[session] restore from {self._storage_state} failed: {_re}")
        return browser, ctx

    def _log_donut_fingerprint(self, page) -> None:
        """One line of what Wayfern presents, so every Donut session's log shows the device the
        portal sees -- the Chromium path's equivalent is the ANTIDETECT flags line."""
        try:
            fp = page.evaluate("""() => ({ua: navigator.userAgent, platform: navigator.platform,
                tz: Intl.DateTimeFormat().resolvedOptions().timeZone, langs: navigator.languages,
                webdriver: navigator.webdriver, cores: navigator.hardwareConcurrency,
                screen: screen.width + 'x' + screen.height})""")
            print(f"[session] donut fingerprint: {fp}")
        except Exception as _fe:
            print(f"[session] donut fingerprint read failed: {_fe}")

    _FP_ID_JS = """() => { let gl = '';
        try { const c = document.createElement('canvas'); const g = c.getContext('webgl');
              const e = g && g.getExtension('WEBGL_debug_renderer_info');
              gl = e ? g.getParameter(e.UNMASKED_RENDERER_WEBGL) : ''; } catch (_) {}
        return {ua: navigator.userAgent, platform: navigator.platform, webgl: gl,
                screen: screen.width + 'x' + screen.height, cores: navigator.hardwareConcurrency,
                mem: navigator.deviceMemory || 0, langs: (navigator.languages || []).join(',')}; }"""

    def identity_fingerprint(self) -> dict | None:
        """The device this session presents, as {hash, ...fields}: what registration records for a
        sub-account and a later login compares against, so "same device" is a fact, not an assumption.
        Exit-dependent fields (timezone, locale-by-geo) are deliberately NOT in the hash. One evaluate;
        None on any failure -- it must never be the reason a session fails."""
        try:
            fp = self.browser_eval(self._FP_ID_JS, timeout=20)
            if not isinstance(fp, dict) or not fp.get("ua"):
                return None
            key = _json.dumps({k: fp.get(k) for k in sorted(fp)}, sort_keys=True)
            fp["hash"] = hashlib.sha1(key.encode()).hexdigest()[:16]
            return fp
        except Exception as _fe:
            print(f"[session] identity fingerprint read failed: {_fe}")
            return None

    def _take_screenshot(self, page, username: str, stage: str) -> None:
        """Save a screenshot from within the browser worker thread (no queue)."""
        try:
            _SCREENSHOTS_DIR.mkdir(exist_ok=True)
            ts   = int(time.time())
            path = _SCREENSHOTS_DIR / f"{username}_{stage}_{ts}.png"
            page.screenshot(path=str(path), full_page=True)
            print(f"[session] screenshot: {path.name}")
        except Exception as e:
            print(f"[session] screenshot failed ({stage}): {e}")

    def _do_browser_verify(self, page, ctx, cmd: dict) -> dict:
        """Navigate to the token URL and submit the verification form in the browser."""
        token_url      = cmd["token_url"]
        insert_jsp     = cmd["insert_jsp"]
        verify_url     = cmd["verify_url"]
        token_input    = cmd["token_input"]
        token_search   = cmd["token_search"]
        lang           = cmd["lang"]

        print(f"[session] browser verify: nav -> {token_url[:80]}")
        page.goto(token_url, timeout=25000)
        page.wait_for_load_state("networkidle", timeout=15000)
        content = page.content()
        has_challenge = "/ch/bd.js" in content
        if has_challenge:
            raise RuntimeError("token URL returned challenge - cookie expired?")

        # Submit the verification via fetch() from within the browser context
        js = f"""
        async () => {{
            const resp = await fetch({_json.dumps(verify_url)}, {{
                method: "POST",
                headers: {{
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "*/*",
                }},
                body: new URLSearchParams({{
                    "language": {_json.dumps(lang)},
                    "token": {_json.dumps(token_input)},
                    "tokenSearchParams": {_json.dumps(token_search)},
                }}).toString(),
            }});
            const text = await resp.text();
            return {{ status: resp.status, body: text }};
        }}
        """
        result = page.evaluate(js)
        print(f"[session] browser verify status={result.get('status')}  "
              f"body={result.get('body','')[:100]}")
        return result

    def _do_browser_fetch(self, page, cmd: dict) -> dict:
        """Execute a fetch() from the browser context - passes DataDome TLS check.
        encode='form'      -> URLSearchParams (default, login/slots/formulario)
        encode='multipart' -> FormData with empty blobs for file_fields (ScheduleController)
        encode='none'      -> no body (GET requests)
        """
        url         = cmd["url"]
        method      = cmd.get("method", "POST")
        data        = cmd.get("data", {})
        headers     = cmd.get("headers", {})
        encode      = cmd.get("encode", "form")
        file_fields = cmd.get("file_fields", [])
        params      = cmd.get("params", {})

        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            url = url + ("&" if "?" in url else "?") + qs

        headers_js    = _json.dumps(headers)
        data_js       = _json.dumps(data)
        method_js     = _json.dumps(method)
        url_js        = _json.dumps(url)
        file_fields_js = _json.dumps(file_fields)

        if encode == "multipart":
            js = f"""
            async () => {{
                const fields = {data_js};
                const fileFields = {file_fields_js};
                const fd = new FormData();
                for (const [k, v] of Object.entries(fields)) {{
                    fd.append(k, v);
                }}
                for (const k of fileFields) {{
                    fd.append(k, new Blob([]), '');
                }}
                const extraHdrs = {headers_js};
                const hdrs = {{}};
                for (const [k, v] of Object.entries(extraHdrs)) {{
                    if (k.toLowerCase() !== 'content-type') hdrs[k] = v;
                }}
                const resp = await fetch({url_js}, {{
                    method: {method_js},
                    headers: hdrs,
                    body: fd,
                }});
                const text = await resp.text();
                return {{ status: resp.status, body: text }};
            }}
            """
        elif encode == "none" or method.upper() == "GET":
            js = f"""
            async () => {{
                const resp = await fetch({url_js}, {{
                    method: {method_js},
                    headers: {{
                        "Accept": "text/html,application/xhtml+xml,*/*",
                        ...{headers_js},
                    }},
                }});
                const text = await resp.text();
                return {{ status: resp.status, body: text }};
            }}
            """
        else:
            js = f"""
            async () => {{
                const fields = {data_js};
                const resp = await fetch({url_js}, {{
                    method: {method_js},
                    headers: {{
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "*/*",
                        ...{headers_js},
                    }},
                    body: new URLSearchParams(fields).toString(),
                }});
                const text = await resp.text();
                return {{ status: resp.status, body: text }};
            }}
            """

        result = page.evaluate(js)
        body = result.get("body", "")
        safe_body = body[:80].encode("utf-8", errors="replace").decode("utf-8", errors="replace")
        print(f"[session] browser_fetch {method} {url.split('?')[0].split('/')[-1]} "
              f"status={result.get('status')}  body={safe_body!r}")
        return {"status": result.get("status", 0), "body": body}

    def _solve_audio_challenge(self, page, twocaptcha_key: str) -> str:
        """
        When a reCAPTCHA image challenge is showing, switch to audio challenge,
        download the audio file, send to 2captcha audio endpoint, type the
        transcription, and submit. Returns the reCAPTCHA token on success or "".
        Token from real browser challenge interaction scores higher than injected tokens.
        """
        print(f"[session] audio_challenge: attempting")
        try:
            ch_frame = page.frame_locator(
                'iframe[title*="recaptcha challenge"], iframe[src*="bframe"]'
            ).first

            # Click audio button
            audio_btn = ch_frame.locator("#recaptcha-audio-button")
            if not audio_btn.is_visible(timeout=5000):
                print(f"[session] audio_challenge: audio button not visible")
                return ""
            audio_btn.click()
            page.wait_for_timeout(2000)

            # Check for "too many requests" error
            try:
                err_vis = ch_frame.locator(".rc-doscaptcha-body").is_visible(timeout=1000)
                if err_vis:
                    print(f"[session] audio_challenge: too-many-requests error")
                    return ""
            except Exception:
                pass

            # Get audio source URL
            audio_src = ""
            for sel in [
                "audio.rc-audiochallenge-audio-src",
                ".rc-audiochallenge-tdownload-link",
                "audio[src]",
            ]:
                try:
                    el = ch_frame.locator(sel).first
                    if el.count() > 0:
                        audio_src = el.get_attribute("src", timeout=3000) or \
                                    el.get_attribute("href", timeout=3000) or ""
                        if audio_src:
                            break
                except Exception:
                    pass

            if not audio_src:
                print(f"[session] audio_challenge: no audio source found")
                return ""
            print(f"[session] audio_challenge: audio_src={audio_src[:80]}")

            # Download audio via proxy
            import primp as _primp
            audio_client = _primp.Client(proxy=self.proxy, verify=False,
                                         follow_redirects=True, timeout=20)
            r = audio_client.get(audio_src)
            audio_bytes = r.content
            if not audio_bytes:
                print(f"[session] audio_challenge: empty audio response")
                return ""
            print(f"[session] audio_challenge: downloaded {len(audio_bytes)} bytes")

            # Submit to 2captcha audio solver
            if not twocaptcha_key:
                print(f"[session] audio_challenge: no 2captcha key - cannot solve audio")
                return ""
            import base64, requests as _req
            audio_b64 = base64.b64encode(audio_bytes).decode()
            submit_r = _req.post(
                "https://2captcha.com/in.php",
                data={"key": twocaptcha_key, "method": "audio",
                      "lang": "en", "body": audio_b64},
                timeout=30,
            )
            submit_text = submit_r.text.strip()
            if not submit_text.startswith("OK|"):
                print(f"[session] audio_challenge: submit error: {submit_text}")
                return ""
            task_id = submit_text.split("|", 1)[1]
            print(f"[session] audio_challenge: task_id={task_id}")

            # Poll for result
            transcription = ""
            for poll in range(30):
                time.sleep(5)
                poll_r = _req.get(
                    f"https://2captcha.com/res.php?key={twocaptcha_key}"
                    f"&action=get&id={task_id}",
                    timeout=15,
                )
                pt = poll_r.text.strip()
                if pt == "CAPCHA_NOT_READY":
                    continue
                if pt.startswith("OK|"):
                    transcription = pt.split("|", 1)[1].strip().lower()
                    print(f"[session] audio_challenge: transcription={transcription!r}")
                    break
                print(f"[session] audio_challenge: poll error: {pt}")
                return ""

            if not transcription:
                print(f"[session] audio_challenge: timed out waiting for transcription")
                return ""

            # Type transcription into audio input field and click verify
            audio_input = ch_frame.locator("#audio-response")
            audio_input.fill(transcription, timeout=5000)
            page.wait_for_timeout(500)
            verify_btn = ch_frame.locator("#recaptcha-verify-button")
            verify_btn.click(timeout=5000)
            page.wait_for_timeout(3000)

            # Check if token was generated (challenge solved)
            token = ""
            for _ in range(15):
                try:
                    token = page.evaluate("grecaptcha.enterprise.getResponse()") or ""
                    if token:
                        break
                except Exception:
                    pass
                page.wait_for_timeout(1000)

            if token:
                print(f"[session] audio_challenge: token obtained  len={len(token)}")
            else:
                print(f"[session] audio_challenge: no token after verify (wrong answer?)")
            return token

        except Exception as e:
            print(f"[session] audio_challenge: error: {e}")
            return ""

    def _do_browser_login(self, page, cmd: dict) -> dict:
        """
        Navigate fresh to AUTH_URL, type credentials to build reCAPTCHA behavioral
        signals, click the checkbox, then submit login via jQuery $.ajax.
        Falls back to an external CapSolver token if the image challenge appears.
        Returns {"status": int, "body": str}.
        """
        username          = cmd["username"]
        password          = cmd["password"]
        lang              = cmd.get("lang", "PT")
        solver_key        = cmd.get("solver_key", "")
        capsolver_keys    = cmd.get("capsolver_keys", [solver_key] if solver_key else [])
        anticaptcha_keys  = cmd.get("anticaptcha_keys", [])
        twocaptcha_keys   = cmd.get("twocaptcha_keys", [])
        capmonster_keys   = cmd.get("capmonster_keys", [])
        pre_token_holder  = cmd.get("pre_token", [])   # mutable list; worker's pre-solve thread writes here

        _proxy_short = (self.proxy or "none")[:60]
        print(f"[session] browser_login: user={username}  proxy={_proxy_short}")

        # Navigate to auth page - reuse existing page if already there (avoids WAF retrigger),
        # otherwise go HOME_URL -> AUTH_URL to match session init pattern.
        try:
            cur_url = page.url
        except Exception:
            cur_url = ""
        already_on_auth = AUTH_URL in cur_url or cur_url.endswith("/Authentication.jsp")
        # Check if form is clean and usable: username input present AND no challenge overlay
        form_ready = False
        if already_on_auth:
            try:
                form_ready = page.locator('input[name="username"]').count() > 0
            except Exception:
                pass
            if form_ready:
                try:
                    # Challenge iframe overlay blocks field clicks - must navigate fresh
                    ch_vis = page.locator(
                        'iframe[title*="recaptcha challenge"], iframe[src*="bframe"]'
                    ).is_visible(timeout=500)
                    if ch_vis:
                        form_ready = False
                        print(f"[session] browser_login: challenge overlay visible - navigating fresh")
                except Exception:
                    pass
        if form_ready:
            print(f"[session] browser_login: reusing existing AUTH_URL page  user={username}")
            time.sleep(random.uniform(0.5, 1.0))
        else:
            print(f"[session] browser_login: nav HOME_URL -> AUTH_URL  user={username}")
            # Bootstrap pattern (visit the entry page before the login page so the portal's own
            # filters/JS establish the session — Vistos_sid, load-balancer affinity — before we deep-link
            # to Authentication.jsp), plus a Referer so the login load reads as a click-through from HOME,
            # not a deep-link. Wait on domcontentloaded + the username field, NEVER networkidle: this
            # portal's WAF runs continuous background telemetry, so the page never reaches network-idle —
            # a networkidle wait just burns its whole timeout (×_nav_scale = ~45s of dead wait each). That
            # dead wait was the real cause behind our historical "nav-timeouts" that NAV_TIMEOUT_SCALE only
            # masked by tolerating a longer stall. (Confirmed from a ref-bot live capture on this portal.)
            page.goto(HOME_URL, timeout=int(30000 * _nav_scale()), wait_until="domcontentloaded")
            # Slow/overloaded-portal tolerance: the AUTH nav returns on domcontentloaded, but under load
            # the login FORM often hasn't rendered yet (or the server served a transient WAF/placeholder
            # page) — the username field simply never appears in one shot. Silently proceeding then clicks
            # a missing field and dies with "field not found: Timeout ...ms exceeded" (the 0-login failure
            # seen when the portal is slow but manual login works — a human just waits/reloads). So re-nav
            # AUTH_URL until the field is actually VISIBLE, up to LOGIN_FIELD_RETRIES times.
            _field_sel = ('input[name="username"], input#username, '
                          'input[autocomplete="username"]')
            try:
                _fld_retries = max(1, int(os.environ.get("LOGIN_FIELD_RETRIES", "3")))
            except (ValueError, TypeError):
                _fld_retries = 3
            _field_ok = False
            for _fa in range(1, _fld_retries + 1):
                page.goto(AUTH_URL, timeout=int(25000 * _nav_scale()),
                          wait_until="domcontentloaded", referer=HOME_URL)
                try:
                    page.wait_for_selector(_field_sel, timeout=int(15000 * _nav_scale()),
                                           state="visible")
                    _field_ok = True
                    break
                except Exception:
                    print(f"[session] login form not ready (attempt {_fa}/{_fld_retries}) — "
                          f"slow portal, re-navigating AUTH_URL  user={username}")
                    time.sleep(random.uniform(1.5, 3.0))
            if not _field_ok:
                print(f"[session] login form still not visible after {_fld_retries} attempts — "
                      f"proceeding, fill step will retry  user={username}")
            time.sleep(random.uniform(1.2, 2.0))

        # Debug: dump reCAPTCHA widget configuration to confirm action name
        try:
            rc_info = page.evaluate("""
            () => {
                const info = {};
                // data-action attributes on reCAPTCHA widget divs
                const divs = document.querySelectorAll('[data-sitekey],[data-action],[class*="recaptcha"]');
                info.widgets = Array.from(divs).map(el => ({
                    tag: el.tagName, id: el.id,
                    dataAction: el.getAttribute('data-action'),
                    dataSitekey: el.getAttribute('data-sitekey') || null,
                    dataCallback: el.getAttribute('data-callback'),
                }));
                // Look for grecaptcha render args in script content
                const scripts = Array.from(document.querySelectorAll('script:not([src])'));
                const hits = [];
                for (const s of scripts) {
                    const t = s.textContent;
                    const m = t.match(/action\\s*[:=]\\s*['"]([^'"]{1,40})['"]/g);
                    if (m) hits.push(...m);
                    const m2 = t.match(/grecaptcha[^;]{0,120}/g);
                    if (m2) hits.push(...m2.slice(0,5));
                }
                info.scriptHits = hits.slice(0, 20);
                return info;
            }
            """)
        except Exception as e:
            print(f"[session] rcaptcha_info error: {e}")

        # Type credentials into form fields - keystrokes build reCAPTCHA Enterprise
        # behavioral score (same pattern that makes registration CAPTCHA pass).
        # fill("") clears any residual value from previous retry before typing.
        try:
            uf = page.locator(
                'input[name="username"], input#username, '
                'input[autocomplete="username"], input[type="text"]:visible'
            ).first
            _human_mouse(page, uf)
            uf.click(timeout=int(5000 * _nav_scale()))
            uf.fill("")
            time.sleep(random.uniform(0.4, 0.8))
            uf.type(username, delay=random.randint(70, 150))
            time.sleep(random.uniform(0.4, 0.9))
        except Exception as e:
            print(f"[session] username field not found: {e}")

        try:
            pf = page.locator(
                'input[name="password"], input#password, input[type="password"]:visible'
            ).first
            _human_mouse(page, pf)
            pf.click(timeout=int(5000 * _nav_scale()))
            pf.fill("")
            time.sleep(random.uniform(0.3, 0.7))
            pf.type(password, delay=random.randint(70, 150))
            time.sleep(random.uniform(0.8, 1.5))
        except Exception as e:
            print(f"[session] password field not found: {e}")

        # Move mouse toward reCAPTCHA before clicking
        try:
            box = page.locator('iframe[title*="reCAPTCHA"]').first.bounding_box(timeout=5000)
            if box:
                page.mouse.move(
                    box["x"] + box["width"] * 0.3,
                    box["y"] + box["height"] * 0.5,
                    steps=random.randint(10, 20),
                )
                time.sleep(random.uniform(0.5, 2.0))
        except Exception:
            pass

        token = ""

        # The checkbox click + 25s one-click poll used to run here and was the DEFAULT
        # (skip_checkbox defaulted to False and ops_live.py never passed it). Removed 2026-08-20 on
        # the operator's call: our browsers are never trusted enough for the one-click pass, so the
        # poll always ran its full 25 seconds and then landed in the image challenge -- whose tokens
        # (~2400-2574 chars) are the low-scoring kind that the login endpoint rejects. It cost 25s of
        # every login attempt plus 3 sequential solver races, to arrive at a token worse than the one
        # a single silent injection produces. Now: always inject silently.
        print("[session] browser_login: silent token injection (checkbox path removed)")

        # External solver — ProxyLess (solver-cloud IPs have high reCAPTCHA Enterprise trust).
        # Audio challenge is intentionally skipped: .mp3 download via proxy degrades IP trust.
        # Pre-solved token (started in worker before browser launch) is used first if ready;
        # this eliminates the serial solver wait on successful pre-solve hits.
        if not token and pre_token_holder and pre_token_holder[0]:
            import solver as _solver
            # ALWAYS accepted. There used to be a `>= min_score` gate here, which discarded the
            # pre-solve and paid for a fresh solve -- at the threshold ops_live passed (50) that
            # fired on the overwhelming majority of tokens, since ours score 10-25. It threw away
            # the exact work the pre-solve exists to save, to buy a re-solve that scores the same.
            token = pre_token_holder[0]
            print(f"[session] pre-solved token accepted  len={len(token)}  "
                  f"score={_solver.score_token(token)}")

        if not token:
            # A/B lever: when LOGIN_ANTICAPTCHA_ONLY=1, drop capsolver from the reCAPTCHA RACE ONLY so
            # anti-captcha's token is actually submitted (otherwise a fast capsolver score>=min_score
            # token wins the race and cancels anti-captcha, so we never learn if anti-captcha's tokens
            # are accepted better by the portal's score-independent siteverify). DataDome solving below
            # still uses the full capsolver_keys — this only isolates the login reCAPTCHA provider.
            _login_ac_only = os.environ.get("LOGIN_ANTICAPTCHA_ONLY", "").strip() == "1" and bool(anticaptcha_keys)
            _login_cs_only = os.environ.get("LOGIN_CAPSOLVER_ONLY", "").strip() == "1" and bool(capsolver_keys)
            _rc_capsolver   = [] if _login_ac_only else capsolver_keys
            _rc_anticaptcha = [] if _login_cs_only else anticaptcha_keys
            any_keys = _rc_capsolver or _rc_anticaptcha or twocaptcha_keys or capmonster_keys
            if any_keys:
                import solver as _solver
                # ONE solve path now that the checkbox branch is gone: race_all (a single parallel
                # race) rather than race_best (3 sequential races). race_best only earned its cost on
                # the image-challenge tokens the removed path produced -- it ran all 3 races and
                # picked the shortest. A silently-injected token doesn't need that.
                #
                # RECAPTCHA_SOLVE_PROXY is honoured here, and falls BACK to proxyless if the
                # proxy-mode solve fails, because honouring the setting must never mean producing NO
                # token. Measured 2026-08-20, 5 solves/mode against LOGIN_EVISA:
                #
                #   soax/rotating proxied   2/5 solved (score 10), 2x PROXY_CONNECT_REFUSED,
                #                           1x CAPTCHA_SOLVE_FAILED
                #   soax/mobile   proxied   0/1 solved (CAPTCHA_SOLVE_FAILED)
                #   proxyless               5/5 solved, score 10
                #
                # Proxy-mode is not broken (an earlier note here claimed it was -- that was measured
                # only against mobile/webshare and was wrong for soax/rotating, the pool the 342
                # verified registrations ran on). It is simply WORSE: same score, ~60% fewer tokens.
                _rc_proxy = self.proxy if _recaptcha_solve_proxy() else None
                token = None
                if _rc_proxy:
                    print(f"[session] race_all (PROXY-based"
                          f"{', ANTICAPTCHA-ONLY' if _login_ac_only else ''})")
                    try:
                        token = _solver.race_all(
                            _rc_capsolver, _rc_anticaptcha, twocaptcha_keys, capmonster_keys,
                            "LOGIN_EVISA", proxy=_rc_proxy,
                        )
                    except Exception as e:
                        print(f"[session] PROXY-based solve failed ({e}) — falling back to proxyless")
                if not token:
                    print(f"[session] race_all (proxyless"
                          f"{', ANTICAPTCHA-ONLY' if _login_ac_only else ''})")
                    try:
                        token = _solver.race_all(
                            _rc_capsolver, _rc_anticaptcha, twocaptcha_keys, capmonster_keys,
                            "LOGIN_EVISA", proxy=None,
                        )
                    except Exception as e:
                        print(f"[session] race_all failed: {e}")
                if token:
                    print(f"[session] race_all token obtained  len={len(token)}  "
                          f"score={_solver.score_token(token)}")

        # Last-resort: read injected textarea value
        if not token:
            try:
                token = page.evaluate("document.getElementById('g-recaptcha-response').value") or ""
            except Exception:
                pass

        print(f"[session] browser_login: token_len={len(token)}")
        if not token:
            raise RuntimeError("reCAPTCHA: no token after all attempts")

        tok_js = _json.dumps(token)

        # Set the reCAPTCHA textarea value (makes getResponse() return token)
        page.evaluate(f"""
        (() => {{
            const _tok = {tok_js};
            for (const sel of ['#g-recaptcha-response-1', '#g-recaptcha-response']) {{
                const ta = document.querySelector(sel);
                if (!ta) continue;
                const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
                if (setter) setter.call(ta, _tok); else ta.value = _tok;
                ta.dispatchEvent(new Event('input',  {{bubbles: true}}));
                ta.dispatchEvent(new Event('change', {{bubbles: true}}));
            }}
        }})()
        """)
        print(f"[session] token set in textarea")

        # Override grecaptcha.enterprise.getResponse so doLogin() reads our token.
        # The internal reCAPTCHA widget state is separate from the textarea DOM value;
        # getResponse() reads widget state, not the textarea. Without this override
        # doLogin() sends an empty captchaResponse and the server returns type:error.
        page.evaluate(f"""
        (() => {{
            const _tok = {tok_js};
            const _patch = (obj) => {{
                if (!obj) return;
                const _orig = obj.getResponse;
                obj.getResponse = function(opt_widget_id) {{
                    const r = _orig ? _orig.call(this, opt_widget_id) : '';
                    return r || _tok;
                }};
            }};
            if (window.grecaptcha) {{
                _patch(window.grecaptcha);
                if (window.grecaptcha.enterprise) _patch(window.grecaptcha.enterprise);
            }}
        }})()
        """)
        print(f"[session] grecaptcha.getResponse patched to return injected token")

        # Call onCaptchaSuccess(token) — the portal's callback that may set server-side session state.
        # Without this, the server may reject the login because it never received the pre-validation call.
        try:
            _cb_src = page.evaluate(f"""
            (() => {{
                const _tok = {tok_js};
                const src = typeof window.onCaptchaSuccess === 'function'
                    ? window.onCaptchaSuccess.toString().slice(0, 300) : 'not defined';
                if (typeof window.onCaptchaSuccess === 'function') {{
                    window.onCaptchaSuccess(_tok);
                }}
                return src;
            }})()
            """)
            print(f"[session] onCaptchaSuccess called: {_cb_src!r:.300}")
            page.wait_for_timeout(1500)
        except Exception as _cbe:
            print(f"[session] onCaptchaSuccess call failed: {_cbe}")

        # Diagnostic: dump actual Authentication.jsp form fields before synthetic form submission.
        # This reveals any CSRF tokens or extra hidden fields we might be missing.
        try:
            _form_diag = page.evaluate("""
            (() => {
                const forms = Array.from(document.querySelectorAll('form'));
                return forms.map(f => {
                    const inputs = Array.from(f.elements).map(e => ({
                        name: e.name, type: e.type, value: e.value ? e.value.slice(0, 80) : ''
                    })).filter(e => e.name);
                    return {action: f.action, method: f.method, id: f.id, inputs};
                });
            })()
            """)
        except Exception as _fde:
            print(f"[session] form diag error: {_fde}")

        # Submit login via real DOM form.submit() — DataDome blocks fetch()/XHR POSTs
        # to /login even from within the browser, but treats real page navigations
        # (form.submit()) differently. After submission the browser lands at HOME_URL
        # (success) or Authentication.jsp (failure), so we read page.url directly.
        uname_js   = _json.dumps(username)
        passwd_js  = _json.dumps(password)
        lang_js    = _json.dumps(lang)
        login_url  = BASE + "/VistosOnline/login"
        login_url_js = _json.dumps(login_url)

        login_form_js = f"""
        () => {{
            const f = document.createElement('form');
            f.method = 'POST'; f.action = {login_url_js};
            f.enctype = 'application/x-www-form-urlencoded';
            for (const [k, v] of Object.entries({{
                'username': {uname_js}, 'password': {passwd_js},
                'language': {lang_js}, 'rgpd': 'Y', 'captchaResponse': {tok_js},
            }})) {{
                const i = document.createElement('input');
                i.type = 'hidden'; i.name = k; i.value = v;
                f.appendChild(i);
            }}
            document.body.appendChild(f);
            f.submit();
        }}
        """
        try:
            with page.expect_navigation(wait_until="load", timeout=45000):
                page.evaluate(login_form_js)
        except Exception as _nav_e:
            # expect_navigation/evaluate can itself raise the "page is navigating" race
            # (the form submit DID trigger navigation; the wait mechanism just choked
            # reading state mid-flight). Don't abort — fall through to the content-retry
            # loop below, which re-checks page state instead of giving up immediately.
            print(f"[session] login form submit: expect_navigation raced ({_nav_e}) - retrying content read")
        # A second JS-triggered redirect may start after networkidle; retry content() until stable.
        _login_content = ""
        for _ci in range(6):
            try:
                page.wait_for_timeout(1500)
                _login_content = page.content() or ""
                break
            except Exception:
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
        if "/ch/bd.js" in _login_content:
            page.wait_for_load_state("networkidle", timeout=25000)
            page.wait_for_timeout(3000)
            _login_content = page.content() or ""
        if not _login_content:
            try:
                _landed_url = page.url
                page.goto(_landed_url, wait_until="networkidle", timeout=20000)
                _login_content = page.content() or ""
                print(f"[session] login form submit: re-navigated to landed url {_landed_url} -> len={len(_login_content)}")
            except Exception as _re:
                print(f"[session] login form submit failed (all fallbacks exhausted): {_re}")
                return {"status": 0, "body": "", "captcha_token": token}
        final_url    = page.url
        html_content = _login_content
        has_challenge = "/ch/bd.js" in html_content

        # HAR-confirmed: POST /login -> 200 text/plain (68 bytes) on success, browser stays at
        # /VistosOnline/login. On failure the server 302s back to Authentication.jsp.
        # If we landed at /login with no challenge, navigate to AUTH_URL to complete the
        # redirect chain: Authentication.jsp -> 302 -> /VistosOnline -> /VistosOnline/
        _at_login_endpoint = final_url.rstrip("/").endswith("/VistosOnline/login")
        if _at_login_endpoint and not has_challenge and len(html_content) < 500:
            print(f"[session] /login 200 ok ({len(html_content)}b) — navigating to AUTH_URL to complete redirect")
            try:
                page.goto(AUTH_URL, timeout=20000)
                page.wait_for_load_state("networkidle", timeout=15000)
                page.wait_for_timeout(2000)
                final_url    = page.url
                html_content = page.content() or ""
                has_challenge = "/ch/bd.js" in html_content
            except Exception as _nav_e:
                print(f"[session] AUTH_URL nav after login failed: {_nav_e}")

        at_auth      = "Authentication.jsp" in final_url
        at_home      = final_url.rstrip("/") == HOME_URL.rstrip("/")
        status = 200
        print(f"[session] /login form POST -> {final_url}  at_home={at_home}  at_auth={at_auth}  challenge={has_challenge}  html_len={len(html_content)}")

        if at_home and not has_challenge:
            body = '{"type":"success","description":"form_nav_confirmed"}'
            self._take_screenshot(page, username, "login")
        elif has_challenge:
            _dd_result = self._try_datadome_bypass(
                page, html_content, final_url,
                capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                username, password, lang,
            )
            if _dd_result:
                return _dd_result
            body = '{"type":"error","description":"datadome_challenge_on_post"}'
        elif at_auth:
            # Extract the actual error message from Authentication.jsp
            try:
                _auth_error = page.evaluate(_PAGE_ERROR_JS)
                if _auth_error:
                    print(f"[session] auth-redirect error: {_auth_error!r:.300}")
                else:
                    _auth_snip = html_content[3500:4000].replace('\n', ' ').replace('\r', '')
                    print(f"[session] auth-redirect body@3500: {_auth_snip!r:.300}")
            except Exception as _ae:
                _auth_error = None
                _auth_snip = html_content[3500:4000].replace('\n', ' ').replace('\r', '')
                print(f"[session] auth-redirect body@3500: {_auth_snip!r:.300}")
            # Carry the message, not just the label. It was scraped, printed to stdout, and then
            # thrown away in favour of a fixed "login_failed_auth_redirect" -- so the reason reached
            # a log file nobody aggregates and never the event stream, the classifier, or the
            # operator. 554 of these said 'Por favor, confirma o reCAPTCHA.' and 8 said the portal
            # was refusing the request outright; those are different problems with the same label.
            if _auth_error:
                _safe_auth = str(_auth_error)[:200].replace('"', "'").replace("\\", "")
                body = f'{{"type":"error","description":"auth_redirect: {_safe_auth}"}}'
            else:
                body = '{"type":"error","description":"login_failed_auth_redirect"}'
        else:
            safe_url = final_url[:80].replace('"', '')
            # Read the page, don't just name the URL. This branch is the DOMINANT login rejection
            # (63% on 2026-08-21) and it recorded nothing but where we landed -- so every one of
            # those failures was indistinguishable from every other, and the portal's own
            # explanation went in the bin. See _PAGE_ERROR_JS.
            _page_error = None
            try:
                _page_error = page.evaluate(_PAGE_ERROR_JS)
            except Exception:
                pass
            if _page_error:
                print(f"[session] login-rejected error: {_page_error!r:.300}")
                _safe_msg = str(_page_error)[:200].replace('"', "'").replace("\\", "")
                body = f'{{"type":"error","description":"{_safe_msg}"}}'
            else:
                body = f'{{"type":"error","description":"unexpected_redirect_{safe_url}"}}'

        if body.startswith('{"type":"success'):
            # Sync browser cookies -> primp so keepalive GETs use the authenticated session.
            try:
                _post_cookies = {c["name"]: c["value"] for c in page.context.cookies()}
                if _post_cookies and self._primp is not None:
                    self._primp.set_cookies(AUTH_URL, _post_cookies)
                    print(f"[session] post-login primp sync: {list(_post_cookies.keys())}")
            except Exception as _se:
                print(f"[session] post-login primp sync failed: {_se}")

        return {"status": status, "body": body, "captcha_token": token}

    def _do_datadome_unblock(self, page, cmd: dict) -> bool:
        """
        Generic DataDome unblock for any endpoint blocked via fetch() (used by /slots
        and other non-login XHR calls). A fetch() response body containing the bd.js
        challenge HTML is never executed by the browser, so no challenge iframe ever
        appears in the DOM and the login-path's DOM-extraction trick has nothing to
        find. Fix: navigate the real page to challenge_url (a genuine GET navigation),
        which DOES execute bd.js and render the captcha-delivery iframe, then reuse
        the same capsolver DatadomeSliderTask + cookie-injection mechanism as login.
        Returns True if a datadome cookie was solved and injected, False otherwise.
        """
        import re as _re
        import solver as _solver

        challenge_url  = cmd["challenge_url"]
        capsolver_keys = cmd.get("capsolver_keys") or []

        try:
            page.goto(challenge_url, timeout=25000)
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(3000)
        except Exception as _ge:
            print(f"[session] datadome_unblock: nav to challenge_url failed: {_ge}")
            return False

        html_content = page.content() or ""
        if "/ch/bd.js" not in html_content:
            print("[session] datadome_unblock: challenge_url did not re-trigger a challenge "
                  "(already clean? or endpoint doesn't challenge on GET) — nothing to solve")
            return False

        captcha_url = None
        try:
            captcha_url = page.evaluate(
                "document.querySelector('iframe[src*=\"captcha-delivery.com\"]')?.src || null"
            )
        except Exception as _ee:
            print(f"[session] datadome_unblock: DOM extract failed: {_ee}")

        if not captcha_url:
            m = _re.search(
                r'https://geo\.captcha-delivery\.com/captcha/\?[^"\'<\s]+',
                html_content,
            )
            captcha_url = m.group(0) if m else None

        if not captcha_url:
            try:
                _iframes = page.evaluate(
                    "() => Array.from(document.querySelectorAll('iframe')).map(f => f.src || '(no-src)').slice(0,10)"
                )
                print(f"[session] datadome_unblock: iframes on page: {_iframes}")
            except Exception:
                pass
            print("[session] datadome_unblock: captchaUrl not found — cannot bypass")
            return False

        print(f"[session] datadome_unblock: captchaUrl len={len(captcha_url)}")

        try:
            user_agent = page.evaluate("navigator.userAgent") or ""
        except Exception:
            user_agent = ""

        cookie_str = _solver.solve_datadome_capsolver(
            capsolver_keys, captcha_url, challenge_url, user_agent, self.proxy
        )
        if not cookie_str:
            print("[session] datadome_unblock: solver returned no cookie")
            return False

        dd_value = cookie_str.removeprefix("datadome=")
        print(f"[session] datadome_unblock: injecting cookie  len={len(dd_value)}")
        try:
            page.context.add_cookies([{
                "name":   "datadome",
                "value":  dd_value,
                "domain": ".pedidodevistos.mne.gov.pt",
                "path":   "/",
            }])
        except Exception as _ce:
            print(f"[session] datadome_unblock: cookie inject failed: {_ce}")
            return False

        # Sync to primp too so the non-browser retry path (primp.post) also carries it.
        try:
            if self._primp is not None:
                _cookies_now = {c["name"]: c["value"] for c in page.context.cookies()}
                self._primp.set_cookies(challenge_url, _cookies_now)
        except Exception as _se:
            print(f"[session] datadome_unblock: primp cookie sync failed: {_se}")

        return True

    def _try_datadome_bypass(
        self, page, html_content: str, website_url: str,
        capsolver_keys: list, anticaptcha_keys: list,
        twocaptcha_keys: list, capmonster_keys: list,
        username: str, password: str, lang: str,
    ):
        """
        Called when DataDome challenges the login POST.
        Extracts the challenge URL, solves it via capsolver DatadomeSliderTask,
        injects the datadome cookie, then retries the login form once.
        Returns a result dict on success or None if bypass fails.
        """
        import re as _re
        import solver as _solver

        # Extract the captcha-delivery URL — bd.js creates an iframe after running
        captcha_url = None
        try:
            captcha_url = page.evaluate(
                "document.querySelector('iframe[src*=\"captcha-delivery.com\"]')?.src || null"
            )
        except Exception as _ee:
            print(f"[session] datadome: DOM extract failed: {_ee}")

        if not captcha_url:
            m = _re.search(
                r'https://geo\.captcha-delivery\.com/captcha/\?[^"\'<\s]+',
                html_content,
            )
            captcha_url = m.group(0) if m else None

        if not captcha_url:
            # Log page diagnostics to understand the challenge structure
            try:
                _iframes = page.evaluate("""
                () => Array.from(document.querySelectorAll('iframe')).map(f => f.src || f.srcdoc?.slice(0,80) || '(no-src)').slice(0,10)
                """)
                print(f"[session] datadome: iframes on page: {_iframes}")
                _scripts = page.evaluate("""
                () => Array.from(document.querySelectorAll('script[src]')).map(s => s.src).slice(0,10)
                """)
                print(f"[session] datadome: scripts on page: {_scripts}")
                print(f"[session] datadome: page.url={page.url}")
                print(f"[session] datadome: html snippet: {html_content[:500]!r}")
            except Exception as _de:
                print(f"[session] datadome: diag failed: {_de}")
            print("[session] datadome: captchaUrl not found — cannot bypass")
            return None

        print(f"[session] datadome: captchaUrl len={len(captcha_url)}")

        try:
            user_agent = page.evaluate("navigator.userAgent") or ""
        except Exception:
            user_agent = ""

        cookie_str = _solver.solve_datadome_capsolver(
            capsolver_keys, captcha_url, website_url, user_agent, self.proxy
        )
        if not cookie_str:
            print("[session] datadome: solver returned no cookie")
            return None

        dd_value = cookie_str.removeprefix("datadome=")
        print(f"[session] datadome: injecting cookie  len={len(dd_value)}")
        try:
            page.context.add_cookies([{
                "name":   "datadome",
                "value":  dd_value,
                "domain": ".pedidodevistos.mne.gov.pt",
                "path":   "/",
            }])
        except Exception as _ce:
            print(f"[session] datadome: cookie inject failed: {_ce}")
            return None

        # Navigate to AUTH_URL so the form is available for the retry
        try:
            page.goto(AUTH_URL, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=20000)
            page.wait_for_timeout(2000)
        except Exception as _ne:
            print(f"[session] datadome: AUTH_URL nav failed: {_ne}")
            return None

        # Fresh CAPTCHA token (previous one is consumed / expired)
        retry_token = ""
        print("[session] datadome: retry race_all")
        try:
            retry_token = _solver.race_all(
                capsolver_keys, anticaptcha_keys, twocaptcha_keys, capmonster_keys,
                "LOGIN_EVISA", proxy=None,
            )
            print(f"[session] datadome: retry token len={len(retry_token)}  score={_solver.score_token(retry_token)}")
        except Exception as _te:
            print(f"[session] datadome: retry race_all failed: {_te}")
            return None

        if not retry_token:
            return None

        # Resubmit login form with the new token
        _tok_js    = _json.dumps(retry_token)
        _uname_js  = _json.dumps(username)
        _passwd_js = _json.dumps(password)
        _lang_js   = _json.dumps(lang)
        _lurl_js   = _json.dumps(BASE + "/VistosOnline/login")

        _retry_form_js = f"""
        () => {{
            const f = document.createElement('form');
            f.method = 'POST'; f.action = {_lurl_js};
            f.enctype = 'application/x-www-form-urlencoded';
            for (const [k, v] of Object.entries({{
                'username': {_uname_js}, 'password': {_passwd_js},
                'language': {_lang_js}, 'rgpd': 'Y', 'captchaResponse': {_tok_js},
            }})) {{
                const i = document.createElement('input');
                i.type = 'hidden'; i.name = k; i.value = v;
                f.appendChild(i);
            }}
            document.body.appendChild(f);
            f.submit();
        }}
        """
        print("[session] datadome: retry login form submit")
        try:
            with page.expect_navigation(wait_until="load", timeout=45000):
                page.evaluate(_retry_form_js)
            _retry_content = ""
            for _ci in range(6):
                try:
                    page.wait_for_timeout(1500)
                    _retry_content = page.content() or ""
                    break
                except Exception:
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
            if "/ch/bd.js" in _retry_content:
                page.wait_for_load_state("networkidle", timeout=25000)
                page.wait_for_timeout(3000)
                _retry_content = page.content() or ""
        except Exception as _se:
            print(f"[session] datadome: retry form submit failed: {_se}")
            return None

        final_url2   = page.url
        html2        = _retry_content
        challenge2   = "/ch/bd.js" in html2

        _at_login2 = final_url2.rstrip("/").endswith("/VistosOnline/login")
        if _at_login2 and not challenge2 and len(html2) < 500:
            try:
                page.goto(AUTH_URL, timeout=20000)
                page.wait_for_load_state("networkidle", timeout=15000)
                page.wait_for_timeout(2000)
                final_url2 = page.url
                html2      = page.content() or ""
                challenge2 = "/ch/bd.js" in html2
            except Exception:
                pass

        at_home2 = final_url2.rstrip("/") == HOME_URL.rstrip("/")
        at_auth2 = "Authentication.jsp" in final_url2
        print(f"[session] datadome retry: {final_url2}  at_home={at_home2}  at_auth={at_auth2}  challenge={challenge2}  html_len={len(html2)}")

        if at_home2 and not challenge2:
            try:
                _post_cookies = {c["name"]: c["value"] for c in page.context.cookies()}
                if _post_cookies and self._primp is not None:
                    self._primp.set_cookies(AUTH_URL, _post_cookies)
                    print(f"[session] datadome: post-login primp sync: {list(_post_cookies.keys())}")
            except Exception as _se2:
                print(f"[session] datadome: primp sync failed: {_se2}")
            return {"status": 200, "body": '{"type":"success","description":"form_nav_confirmed"}', "captcha_token": retry_token}

        return None

    # -- Public interface ------------------------------------------------------

    @property
    def client(self):
        """The underlying primp.Client - used by session_store and batch_apply."""
        return self._primp

    def _hdrs(self, extra: dict) -> dict:
        """Merge extra headers; inject Cookie header if we have one."""
        h = dict(extra)
        if self._cookie_header and "Cookie" not in h:
            h["Cookie"] = self._cookie_header
        return h

    def get(self, url: str, headers: dict | None = None,
            params: dict | None = None,
            timeout: int = 20, follow_redirects: bool = True, **_) -> _FakeResponse:
        if self._primp is None:
            raise RuntimeError("Session not ready")
        r = self._primp.get(url, headers=self._hdrs(headers or {}),
                            params=params, timeout=timeout,
                            follow_redirects=follow_redirects)
        return _FakeResponse(r.status_code, r.text, str(r.url))

    def post(self, url: str, data: dict | None = None,
             headers: dict | None = None, params: dict | None = None,
             timeout: int = 30, follow_redirects: bool = True, **_) -> _FakeResponse:
        if self._primp is None:
            raise RuntimeError("Session not ready")
        r = self._primp.post(url, data=data, headers=self._hdrs(headers or {}),
                             params=params, timeout=timeout,
                             follow_redirects=follow_redirects)
        return _FakeResponse(r.status_code, r.text, str(r.url))

    # ── command/reply correlation (F17) ───────────────────────────────────────
    def _send(self, cmd: dict) -> int:
        """Queue a command and return its sequence number, for _await to match the reply against."""
        with self._seq_lock:
            self._next_seq += 1
            seq = self._next_seq
        cmd["_seq"] = seq
        self._cmd_q.put(cmd)
        return seq

    def _await(self, seq: int, timeout: float):
        """Wait for the reply to OUR command, discarding replies to calls that already gave up.

        Raises queue.Empty on timeout -- deliberately the same exception the bare _res_q.get() this
        replaces used to raise, so every call site keeps its own message and handling.

        `timeout` is the work's own budget; _QUEUE_MARGIN is added on top so the caller always
        outlives the worker thread. A reply carrying any other seq belongs to an abandoned earlier
        call and is dropped here rather than handed to the wrong caller.
        """
        deadline = time.monotonic() + timeout + _QUEUE_MARGIN
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise queue.Empty
            got, payload = self._res_q.get(timeout=remaining)
            if got == seq:
                return payload
            print(f"[session] discarded stale reply seq={got} while awaiting seq={seq} "
                  f"-- an earlier call had already timed out (F17 guard)")

    def _drain_replies(self) -> None:
        """Drop anything already queued. Belt-and-braces beside the seq check: it bounds the queue
        after a burst of abandoned calls. The seq check is the real guard, since a reply arriving
        AFTER a drain still has to match."""
        while True:
            try:
                self._res_q.get_nowait()
            except queue.Empty:
                return

    def browser_fetch(self, url: str, data: dict, method: str = "POST",
                      headers: dict | None = None, timeout: int = 30,
                      params: dict | None = None,
                      encode: str = "form",
                      file_fields: list | None = None) -> dict:
        """
        Execute a fetch() from the live browser context - bypasses DataDome TLS check.
        encode='form'      -> URLSearchParams body (default)
        encode='multipart' -> FormData body; pass file_fields=['foto','file1',...] for empty blobs
        encode='none'      -> no body (use for GET)
        params             -> appended to URL as query string
        Returns {"status": int, "body": str}.
        """
        timeout = int(timeout * _nav_scale())   # storm tolerance (NAV_TIMEOUT_SCALE); increase-only
        _seq = self._send({
            "action":      self._CMD_FETCH,
            "url":         url,
            "method":      method,
            "data":        data,
            "headers":     headers or {},
            "params":      params or {},
            "encode":      encode,
            "file_fields": file_fields or [],
        })
        try:
            status, result = self._await(_seq, timeout)
        except queue.Empty:
            raise RuntimeError(f"browser_fetch timed out after {timeout}s")
        if status == "err":
            raise RuntimeError(f"browser_fetch failed: {result}")
        return result

    def unblock_datadome(self, challenge_url: str, capsolver_keys: list,
                         timeout: int = 180) -> bool:
        """
        Solve a DataDome challenge for an arbitrary endpoint (e.g. /slots) that was
        blocked when called via fetch() (fetch responses never execute their JS, so
        bd.js never runs and no challenge iframe ever appears in the DOM). Navigates
        the real browser page to challenge_url so bd.js actually runs and renders the
        challenge iframe, solves it via capsolver DatadomeSliderTask, and injects the
        resulting datadome cookie into the browser context. Caller should retry its
        original request (browser_fetch/browser_form_post/...) immediately after this
        returns True — the cookie travels with the session for subsequent calls.
        Returns True on success, False if the bypass could not be completed.
        timeout default is 180s: nav/settle overhead (~43s) plus solve_datadome_capsolver's
        own internal poll timeout (120s default) can legitimately reach ~163s. A shorter
        outer timeout would abandon the call while the solver is still polling, and the
        late result would corrupt the next unrelated call's result-queue read.
        """
        timeout = int(timeout * _nav_scale())   # storm tolerance (NAV_TIMEOUT_SCALE); increase-only
        _seq = self._send({
            "action":        self._CMD_DATADOME_UNBLOCK,
            "challenge_url": challenge_url,
            "capsolver_keys": capsolver_keys,
        })
        try:
            status, result = self._await(_seq, timeout)
        except queue.Empty:
            print(f"[session] unblock_datadome timed out after {timeout}s")
            return False
        if status == "err":
            print(f"[session] unblock_datadome failed: {result}")
            return False
        return bool(result)

    def browser_login(self, username: str, password: str, lang: str = "PT",
                      solver_key: str = "", twocaptcha_key: str = "",
                      capsolver_keys: list | None = None,
                      anticaptcha_keys: list | None = None,
                      twocaptcha_keys: list | None = None,
                      capmonster_keys: list | None = None,
                      timeout: int = 300,
                      pre_token: list | None = None) -> dict:
        """
        Type credentials into the login form to build behavioral signals, click the
        reCAPTCHA token silently (no checkbox click). Races all solver services for token.
        pre_token: mutable list[str] — if [0] is non-empty when the token injection point
            is reached, it is used directly and the solver race is skipped entirely.
            Worker pre-solves in parallel with browser launch to eliminate serial wait.
        Returns {"status": int, "body": str}.
        """
        timeout = int(timeout * _nav_scale())   # storm tolerance (NAV_TIMEOUT_SCALE); increase-only
        _seq = self._send({
            "action":          self._CMD_LOGIN,
            "username":        username,
            "password":        password,
            "lang":            lang,
            "solver_key":      solver_key,
            "twocaptcha_key":  twocaptcha_key,
            "capsolver_keys":  capsolver_keys or ([solver_key] if solver_key else []),
            "anticaptcha_keys": anticaptcha_keys or [],
            "twocaptcha_keys": twocaptcha_keys or ([twocaptcha_key] if twocaptcha_key else []),
            "capmonster_keys": capmonster_keys or [],
            "pre_token":       pre_token or [],
        })
        try:
            status, result = self._await(_seq, timeout)
        except queue.Empty:
            raise RuntimeError(f"browser_login timed out after {timeout}s")
        if status == "err":
            raise RuntimeError(f"browser_login failed: {result}")
        return result

    def browser_add_init_script(self, js: str, timeout: int = 15) -> bool:
        """Register JS to run at the start of EVERY future navigation in this context (before the
        page's own scripts). Use for a fetch/XHR hook that must be in place before a page-load-
        triggered request fires (e.g. the portal's getTables handshake). Idempotency is the caller's
        responsibility -- Playwright STACKS init scripts, so add each one once per session."""
        _seq = self._send({"action": self._CMD_ADD_INIT, "js": js})
        try:
            status, result = self._await(_seq, timeout)
        except queue.Empty:
            raise RuntimeError(f"browser_add_init_script timed out after {timeout}s")
        if status == "err":
            raise RuntimeError(f"browser_add_init_script failed: {result}")
        return bool(result)

    def browser_eval(self, js: str, timeout: int = 30):
        """Execute arbitrary JS in the live browser page and return the result."""
        timeout = int(timeout * _nav_scale())   # storm tolerance (NAV_TIMEOUT_SCALE); increase-only
        _seq = self._send({"action": self._CMD_EVAL, "js": js})
        try:
            status, result = self._await(_seq, timeout)
        except queue.Empty:
            raise RuntimeError(f"browser_eval timed out after {timeout}s")
        if status == "err":
            raise RuntimeError(f"browser_eval failed: {result}")
        return result

    def browser_last_dialog(self) -> str | None:
        """The last JS alert()/confirm() message the portal raised on this page (or None). Captured
        by the page dialog handler in the worker thread; used to detect the 'já tem um pedido
        registado' (already-has-a-request) rejection, which the portal delivers as a JS alert."""
        return self._last_dialog

    def clear_last_dialog(self) -> None:
        self._last_dialog = None

    def browser_nav(self, url: str, timeout: int = 60, fast_nav: bool = False,
                    skip_cookie_sync: bool = False) -> str:
        """Navigate the browser page to url and return the final URL after load.
        fast_nav=True skips the DataDome 4-second post-load wait (safe for non-DataDome URLs).
        skip_cookie_sync=True preserves the browser's current cookies instead of overwriting with primp's.
        """
        timeout = int(timeout * _nav_scale())   # storm tolerance (NAV_TIMEOUT_SCALE); increase-only
        _seq = self._send({"action": self._CMD_NAV, "url": url,
                         "nav_timeout": timeout * 1000, "fast_nav": fast_nav,
                         "skip_cookie_sync": skip_cookie_sync})
        try:
            status, result = self._await(_seq, timeout)
        except queue.Empty:
            raise RuntimeError(f"browser_nav timed out after {timeout}s")
        if status == "err":
            raise RuntimeError(f"browser_nav failed: {result}")
        return result

    def browser_form_post(self, url: str, data: dict,
                          timeout: int = 100) -> tuple[str, str]:
        """Submit a form POST via browser DOM (form.submit()), causing a real page navigation.
        Syncs primp cookies first. Returns (final_url, html).
        timeout default is 100s, not a guess: the worker-thread side can legitimately take
        up to ~87s in its worst case (45s expect_navigation + 9s content-retry loop + 13s
        bd.js settle wait + 20s re-navigate fallback). A shorter outer timeout here doesn't
        just fail faster — it abandons the call while the worker thread is still running,
        and the eventual late result lands in the result queue where the NEXT unrelated
        call's .get() wrongly consumes it, corrupting that call's state. Always keep this
        margin above the real worst-case duration of the underlying _CMD_FORM_POST handler."""
        timeout = int(timeout * _nav_scale())   # storm tolerance (NAV_TIMEOUT_SCALE); increase-only
        _seq = self._send({"action": self._CMD_FORM_POST, "url": url, "data": data})
        try:
            result = self._await(_seq, timeout)
            if result[0] == "ok":
                _, final_url, html = result
                return final_url, html
            else:
                raise RuntimeError(f"browser_form_post failed: {result[1]}")
        except queue.Empty:
            raise RuntimeError(f"browser_form_post timed out after {timeout}s")

    def submit_page_form(self, action_contains: str = "Formulario",
                         timeout: int = 100) -> tuple[str, str]:
        """Submit the existing on-page form (populated by questionnaire XHR steps).
        Does NOT inject a new form. Returns (final_url, html).
        See browser_form_post's timeout docstring — same outer/inner timeout-margin reasoning."""
        timeout = int(timeout * _nav_scale())   # storm tolerance (NAV_TIMEOUT_SCALE); increase-only
        _seq = self._send({"action": self._CMD_SUBMIT_PAGE_FORM, "action_contains": action_contains})
        try:
            result = self._await(_seq, timeout)
            if result[0] == "ok":
                _, final_url, html = result
                return final_url, html
            else:
                raise RuntimeError(f"submit_page_form failed: {result[1]}")
        except queue.Empty:
            raise RuntimeError(f"submit_page_form timed out after {timeout}s")

    def submit_form_binary(self, action_contains: str = "MostrarPdf",
                           timeout: int = 45) -> dict:
        """Submit the existing on-page form via DOM form.submit() (real navigation,
        bypasses DataDome — same trick as submit_page_form) and return the RAW response
        bytes, for endpoints that respond with binary content (e.g. MostrarPdf's
        application/pdf) where page.content() would only return Chromium's built-in
        PDF-viewer shell, not the actual file.
        Returns {"status": int, "content_type": str, "url": str, "body": bytes}."""
        timeout = int(timeout * _nav_scale())   # storm tolerance (NAV_TIMEOUT_SCALE); increase-only
        _seq = self._send({"action": self._CMD_SUBMIT_FORM_BINARY, "action_contains": action_contains})
        try:
            status, result = self._await(_seq, timeout)
        except queue.Empty:
            raise RuntimeError(f"submit_form_binary timed out after {timeout}s")
        if status == "err":
            raise RuntimeError(f"submit_form_binary failed: {result}")
        return result

    def screenshot(self, username: str, stage: str, timeout: int = 15) -> str | None:
        """Take a screenshot during the login flow (browser still open). Returns path or None."""
        _SCREENSHOTS_DIR.mkdir(exist_ok=True)
        ts   = int(time.time())
        path = str(_SCREENSHOTS_DIR / f"{username}_{stage}_{ts}.png")
        _seq = self._send({"action": self._CMD_SCREENSHOT, "path": path})
        try:
            status, result = self._await(_seq, timeout)
            if status == "ok":
                print(f"[session] screenshot: {Path(result).name}")
                return result
            print(f"[session] screenshot failed ({stage}): {result}")
        except queue.Empty:
            print(f"[session] screenshot timed out ({stage})")
        return None

    def close_browser(self, timeout: int = 10) -> None:
        """Shut down the Playwright browser after login. primp carries the session forward."""
        _seq = self._send({"action": self._CMD_CLOSE_BROWSER})
        try:
            self._await(_seq, timeout)
            print("[session] browser closed - primp session active")
        except queue.Empty:
            # Graceful shutdown hung (worker stuck in a browser op) → force-kill the driver subtree so the
            # Chromium can't leak. primp (Rust, separate) is unaffected and carries the session forward.
            _kill_proc_tree(self._driver_pid)
            _donut.release(self._donut_lease)
            print("[session] browser close hung — force-killed driver tree (primp session active)")

    def save_state(self, path) -> None:
        """Export cookies + localStorage to a JSON file via Playwright storage_state."""
        _seq = self._send({"action": self._CMD_SAVE_STATE, "path": str(path)})
        try:
            status, result = self._await(_seq, 30)
        except queue.Empty:
            raise RuntimeError("save_state timed out after 30s")
        if status == "err":
            raise RuntimeError(f"save_state failed: {result}")
        print(f"[session] session state saved -> {result}")

    def verify_email(self, token_url: str, insert_jsp: str, verify_url: str,
                     token_input: str, token_search: str, lang: str,
                     timeout: int = 60) -> dict:
        """
        Use the live browser page to visit the token URL and submit verification.
        Returns {"status": int, "body": str} from the VerificarEmail POST.
        Blocks until done or timeout.
        """
        timeout = int(timeout * _nav_scale())   # storm tolerance (NAV_TIMEOUT_SCALE); increase-only
        _seq = self._send({
            "action":       self._CMD_VERIFY,
            "token_url":    token_url,
            "insert_jsp":   insert_jsp,
            "verify_url":   verify_url,
            "token_input":  token_input,
            "token_search": token_search,
            "lang":         lang,
        })
        try:
            status, result = self._await(_seq, timeout)
        except queue.Empty:
            raise RuntimeError(f"verify_email timed out after {timeout}s")
        if status == "err":
            raise RuntimeError(f"verify_email failed: {result}")
        return result

    def get_cookies(self, url: str) -> dict:
        if self._primp is None:
            return {}
        try:
            return self._primp.get_cookies(url)
        except Exception:
            return {}

    def get_all_cookies(self) -> list:
        """Full browser-context cookie jar (ALL domains, incl. httpOnly + any DataDome cookie) as
        Playwright cookie dicts. Unlike get_cookies(url) (primp, URL-scoped) this misses nothing —
        used by the SOAX→ISP migration probe to carry the complete warm jar to the new browser."""
        try:
            _seq = self._send({"action": self._CMD_GET_ALL_COOKIES})
            status, result = self._await(_seq, 30)
            return result if status == "ok" and result else []
        except Exception:
            return []

    def close(self):
        """Signal the browser worker to close and wait for it. If the worker is HUNG (a stuck browser op
        never processes _CMD_CLOSE → the graceful teardown never runs → Chromium leaks), force-kill the
        whole driver subtree so nothing leaks. This is the fix for the 0731 inotify-exhaustion collapse."""
        try:
            self._cmd_q.put({"action": self._CMD_CLOSE})
            self._thread.join(timeout=15)
        except Exception:
            pass
        if self._thread.is_alive():
            # graceful close timed out → worker is wedged in a hung browser op. SIGKILL the node+chromium
            # subtree; the wedged op then errors out and the thread unwinds.
            _kill_proc_tree(self._driver_pid)
            _donut.release(self._donut_lease)
        # Terminate the private Xvfb (if this session started one for headful) so it doesn't leak.
        if self._xvfb_proc is not None:
            try:
                self._xvfb_proc.terminate()
            except Exception:
                pass
            self._xvfb_proc = None


# -- External CAPTCHA solver (synchronous, for browser fallback) ---------------

def _capsolver_token_sync(api_key: str, sitekey: str, page_url: str,
                          proxy: str | None = None, timeout: int = 120,
                          cookies: str | None = None,
                          user_agent: str | None = None) -> str:
    """
    Call CapSolver to solve a reCAPTCHA v2 Enterprise checkbox in the background.
    Uses ReCaptchaV2EnterpriseTask (proxy-aware) or ProxyLess variant.
    Returns the gRecaptchaResponse token string, or raises on failure.

    cookies: browser cookie header string (e.g. "Vistos_sid=abc; cookiesession1=xyz").
             Passed to CapSolver so the token is generated inside the same server
             session - the Enterprise reCAPTCHA token will carry the session signals.
    """
    import urllib.request as _req
    import json as _j

    task: dict = {
        "type":       "ReCaptchaV2EnterpriseTask",
        "websiteURL": page_url,
        "websiteKey": sitekey,
    }
    if proxy:
        try:
            from urllib.parse import urlparse, unquote
            p = urlparse(proxy)
            scheme = (p.scheme or "http").lower()
            task["proxyType"]    = "socks5" if "socks" in scheme else "http"
            task["proxyAddress"] = p.hostname or ""
            task["proxyPort"]    = p.port or 80
            if p.username:
                task["proxyLogin"]    = p.username
                task["proxyPassword"] = unquote(p.password or "")
        except Exception:
            pass
    if cookies:
        task["cookies"] = cookies
    if user_agent:
        task["userAgent"] = user_agent

    def _post(url: str, body: dict) -> dict:
        data = _j.dumps(body).encode()
        r = _req.Request(url, data=data, headers={"Content-Type": "application/json"})
        return _j.loads(_req.urlopen(r, timeout=15).read())

    result = _post("https://api.capsolver.com/createTask",
                   {"clientKey": api_key, "task": task})
    task_id = result.get("taskId")
    if not task_id:
        raise RuntimeError(f"CapSolver createTask failed: {result}")

    get_body = {"clientKey": api_key, "taskId": task_id}
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(4)
        res = _post("https://api.capsolver.com/getTaskResult", get_body)
        if res.get("status") == "ready":
            return res["solution"]["gRecaptchaResponse"]
        if res.get("status") == "failed":
            raise RuntimeError(f"CapSolver task failed: {res}")
    raise RuntimeError("CapSolver: timed out waiting for result")


# -- Proxy URL parser ----------------------------------------------------------

def _parse_proxy(proxy_url: str | None) -> tuple[str | None, str | None, str | None]:
    """Split proxy URL into (server, username, password) for Playwright."""
    if not proxy_url:
        return None, None, None
    try:
        from urllib.parse import urlparse, unquote
        p = urlparse(proxy_url)
        server = f"{p.scheme or 'http'}://{p.hostname}:{p.port or 80}"
        return server, unquote(p.username or ""), unquote(p.password or "")
    except Exception:
        return proxy_url, None, None


# -- Public entry point --------------------------------------------------------

def get_session(proxy: str | None = None,
                inject_cookies: dict | None = None,
                headless: bool = True,
                storage_state: str | None = None,
                fp: tuple | None = None,
                inject_cookies_full: list | None = None,
                bootstrap_url: str | None = None,
                fp_seed: str | None = None) -> PlaywrightSession:
    """
    Launch a Chrome session. If inject_cookies is provided, the saved session
    cookies are pre-loaded before navigation - the WAF challenge is skipped.
    Pass headless=False to open a visible browser window (for manual inspection).
    Pass storage_state=path to restore a previously saved session (cookies + localStorage).
    Pass fp=(ua, sec_ch_ua, vp_w, vp_h, platform) to use a fixed fingerprint.
    """
    return PlaywrightSession(proxy, inject_cookies=inject_cookies,
                             headless=headless, storage_state=storage_state, fp=fp,
                             inject_cookies_full=inject_cookies_full,
                             bootstrap_url=bootstrap_url, fp_seed=fp_seed)


def get_session_from_state(state_path, headless: bool = False) -> PlaywrightSession:
    """
    Open a browser pre-loaded with a previously saved session state (from save_state()).
    No proxy needed - session cookies are embedded in the state file.
    headless=False by default so the user sees the browser window.
    Loads <state_stem>_profile.json alongside the state file to restore the original
    browser fingerprint (user-agent, viewport, platform) so the server sees the same
    fingerprint as during login.
    """
    state_path = Path(state_path)
    profile_path = state_path.parent / (state_path.stem + "_profile.json")
    fp = None
    proxy = None
    if profile_path.exists():
        try:
            prof = _json.loads(profile_path.read_text(encoding="utf-8"))
            fp    = (prof["user_agent"], prof["sec_ch_ua"],
                     prof["viewport_w"], prof["viewport_h"], prof["platform"])
            proxy = prof.get("proxy")
            print(f"[session] profile loaded from {profile_path.name}  ua=...{fp[0][55:85]}")
        except Exception as e:
            print(f"[session] profile load failed ({e}) - using random fingerprint")
    return PlaywrightSession(proxy=proxy, headless=headless,
                             storage_state=str(state_path), fp=fp)

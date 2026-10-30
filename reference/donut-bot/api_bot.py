"""
===================================================================================
 VISA APPOINTMENT BOT — PURE API VERSION v3 (dashboard-integrated)

 No browser needed. Direct HTTP requests only.

 This is a direct evolution of the prior-generation prototype
 (/home/yns/Documents/ptbot/visa_api_bot_v2.py) — its step-by-step site-flow logic is
 ported here largely verbatim (cross-validated against the freshest real HAR capture,
 2026-08-20, which independently confirmed every request shape is still accurate: the
 login body/headers, the QuestNextQuestion chain, the ScheduleController multipart
 submission, the SubmeterVistoCriaPDF booking body, and the getTables BD channel).

 The one structural change is architectural, not site-flow: accounts and proxies come
 from the visa-dashboard's machine API (the same one orchestrator.py and the browser
 extensions already use) instead of local CSV/txt files, and every step reports its
 progress back to that same API — so this bot's activity shows up live in the exact
 same Booking / BookingWorker / BookingAppointment views the browser-extension path
 already populates, and both paths can safely run concurrently against the same
 account/proxy pool (GET /api/account/claim is already atomic — SKIP LOCKED — so no
 coordination code is needed between the two bots).

 Flow:
   1. GET login page -> grab session cookies + run the getTables fingerprint check
   2. POST /VistosOnline/login -> authenticate (+ the Authentication.jsp nav POST that
      promotes the session into authenticated scope — HAR-verified, easy to miss)
   2.5. POST /VistosOnline/editProfile (fresh accounts only, non-fatal if skipped)
   3. GET /VistosOnline/QuestNextQuestion (chain) -> fill questionnaire via API
   4-5. Formulario -> ScheduleController (HYBRID — see below)
   6. POST /VistosOnline/slots -> get available appointments
   7. POST SubmeterVistoCriaPDF -> book slot + download PDF

 Steps 4-5 use a real (headless) browser, not httpx/curl_cffi. Confirmed 2026-09-03: a
 pure-HTTP ScheduleController submission always gets its 302 redirected straight to
 BD_problm.jsp — reproduced 4x across two consular posts and two Formulario-fill-timing
 profiles (65-90s and an exact 154s HAR-measured match), ruling out timing/proxy as the
 cause. A real Playwright browser (stealth-patched, handed the same session cookies)
 visiting Questionario for real triggers a genuine getTables fingerprint (the site's own
 bs_datefuncs.js executing natively) and a native <form method=POST> submission (matching
 how a real browser actually reaches Formulario, confirmed via the HAR's own GA4
 telemetry — not a GET) reliably clears ScheduleController and lands on the real
 Schedule.jsp. Steps 6-7 hand back off to httpx once past this checkpoint.

 Usage:
   python3 api_bot.py
   (config via .env — see .env.example)

 Requirements:
   pip install -r requirements.txt   (httpx, curl_cffi, playwright, playwright-stealth)
   playwright install chromium
===================================================================================
"""

import httpx
from curl_cffi import requests as curl_requests
from curl_cffi.requests import exceptions as curl_exceptions
import time
import itertools
import os
import sys
import json
import re
import math
import random
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode
from typing import Optional, Dict, List

from playwright.async_api import async_playwright, Browser, Error as PlaywrightError
from playwright_stealth import stealth_async


# =================================================================================
# CONFIGURATION
# =================================================================================
WORKING_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_URL = "https://pedidodevistos.mne.gov.pt"
SITE_KEY = "6LdOB9crAAAAADT4RFruc5sPmzLKIgvJVfL830d4"  # unchanged across every HAR, May-Aug 2026

# Real (non-empty, correctly-extensioned) sample uploads for the Formulario step — the
# server rejects empty files, and the client-side JS rejects a "foto" whose filename
# doesn't end in .jpg (a double extension like photo.jpg.jpeg trips a visible warning
# dialog, confirmed 2026-09-03).
SAMPLE_PHOTO_PATH = os.path.join(WORKING_DIR, "sample_photo.jpg")
SAMPLE_DOCUMENT_PATH = os.path.join(WORKING_DIR, "sample_attachment.pdf")

# Chrome-like headers that match what the real browser sends
BROWSER_HEADERS = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br, zstd',
    'Accept-Language': 'en-US,en;q=0.9',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive',
    'Pragma': 'no-cache',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'same-origin',
    'Sec-Fetch-User': '?1',
    'Upgrade-Insecure-Requests': '1',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
    'sec-ch-ua': '"Chromium";v="136", "Not-A.Brand";v="24", "Google Chrome";v="136"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
}

AJAX_HEADERS = {
    'Accept': '*/*',
    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
    'X-Requested-With': 'XMLHttpRequest',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-origin',
}

QUEST_HEADERS = {
    'Accept': '*/*',
    'Content-Type': 'text/plain',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-origin',
}

# ---------------------------------------------------------------------------------
# Load .env if one sits next to this file. In production systemd injects these via
# EnvironmentFile=, so anything already in the environment wins — this only fills gaps,
# which is what makes running the script directly behave the same as the service does.
# Without it the module docstring's "config via .env" is a lie and a direct run dies on
# a missing MACHINE_TOKEN with no hint that the file was never read.
# ---------------------------------------------------------------------------------
def _load_dotenv(path: str = os.path.join(WORKING_DIR, '.env')) -> None:
    try:
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, val = line.partition('=')
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except FileNotFoundError:
        pass
    except Exception as exc:  # a malformed .env must not take the service down
        print(f"warning: could not read {path}: {exc}", file=sys.stderr)


_load_dotenv()


# ---------------------------------------------------------------------------------
# Dashboard machine-API config (same secret/host the extensions + orchestrator use).
# ---------------------------------------------------------------------------------
NEXT_API_BASE = os.environ.get('NEXT_API_BASE', 'https://vmi3271966.contaboserver.net:8447').rstrip('/')
MACHINE_TOKEN = os.environ.get('MACHINE_TOKEN', '')
CONCURRENCY = int(os.environ.get('CONCURRENCY', '5'))
MAX_RETRIES_PER_ACCOUNT = int(os.environ.get('MAX_RETRIES_PER_ACCOUNT', '3'))
POLL_INTERVAL = float(os.environ.get('POLL_INTERVAL', '5'))
POSTO_ID_FALLBACK = os.environ.get('POSTO_ID', '5084')  # used only if /api/settings is unreachable
# Force a posto for THIS process, outranking the dashboard's own postoId. The dashboard
# setting is shared with the AdsPower/extension workers, so pointing this bot at a different
# consular post for a test must not be done by editing it — that would silently redirect the
# other path too. Empty (the default) keeps the normal behaviour: the dashboard decides.
POSTO_ID_OVERRIDE = os.environ.get('POSTO_ID_OVERRIDE', '').strip()
# A local ip:port:user:pass list to lease from instead of the dashboard pool.
PROXY_LIST_FILE = os.environ.get('PROXY_LIST_FILE', '').strip()
# Log in from a real Playwright page rather than the HTTP client (default on).
HYBRID_LOGIN = os.environ.get('HYBRID_LOGIN', '1').lower() not in ('0','false','no','')

# Donut Browser anti-detect
DONUT_API_URL = os.environ.get('DONUT_API_URL', 'http://127.0.0.1:10108').rstrip('/')
DONUT_API_TOKEN = os.environ.get('DONUT_API_TOKEN', '').strip()
DONUT_ENABLED = bool(DONUT_API_TOKEN)

# Click the page's own reCAPTCHA checkbox before falling back to a solver token.
CAPTCHA_CLICK_FIRST = os.environ.get('CAPTCHA_CLICK_FIRST', '1').lower() not in ('0','false','no','')
# Seconds to wait for the widget to issue its own token after the checkbox is clicked. The anchor
# iframe advertises anchor-ms=20000&execute-ms=30000, and the human in the 2032 HAR spent 38s
# working through image tiles before userverify — so 12s was never long enough to observe a pass.
CAPTCHA_WIDGET_WAIT_SECONDS = float(os.environ.get('CAPTCHA_WIDGET_WAIT_SECONDS', '60'))
# Refuse to substitute a solver token when the widget produces none. Every successful login in
# every HAR we hold was authenticated with a WIDGET-minted token: reload/replaceimage/userverify/
# clr — the in-page widget's own endpoints — immediately precede each POST /login that returned
# 200. A solver mints its token in the solver's browser, so those calls could never appear in our
# page's log. Without this flag a "click first" run silently becomes an injected-token run the
# moment a challenge appears, which is how the experiment kept not being the experiment.
CAPTCHA_WIDGET_ONLY = os.environ.get('CAPTCHA_WIDGET_ONLY', '0').lower() not in ('0','false','no','')
# Seconds to wait for the bot-detection beacon on the login page before typing credentials.
BD_BEACON_WAIT_SECONDS = int(os.environ.get('BD_BEACON_WAIT_SECONDS', '25'))
# Abort the attempt (and rotate the exit IP) when that beacon never fires.
BD_REQUIRE_BEACON = os.environ.get('BD_REQUIRE_BEACON', '0').lower() not in ('0','false','no','')
# Comma-separated loginUsernames this process may run. Empty = the whole pool.
ONLY_USERNAMES = {u.strip() for u in os.environ.get('ONLY_USERNAMES', '').split(',') if u.strip()}
# Applicant's address town on the Formulario (field f45). second_form_mapping_2.json hardcodes
# PRAIA, which contradicts the form's own pais_residencia whenever residence is not Cape Verde.
# Empty keeps whatever the mapping says.
RESIDENCE_ADDRESS = os.environ.get('RESIDENCE_ADDRESS', '').strip()
COUNTRY_OF_RESIDENCE = os.environ.get('COUNTRY_OF_RESIDENCE', 'CPV')
NATIONALITY = os.environ.get('NATIONALITY', 'CPV')
DURATION_OF_STAY_DAYS = int(os.environ.get('DURATION_OF_STAY_DAYS', '7'))
# Days from now to declare as the intended arrival. Must exceed this post's booking horizon —
# the Schedule calendar refuses any appointment later than this date.
INTENDED_ARRIVAL_DAYS = int(os.environ.get('INTENDED_ARRIVAL_DAYS', '150'))
LANGUAGE = os.environ.get('LANGUAGE', 'PT')

# ---------------------------------------------------------------------------------
# Slot-watch mode. Everything expensive in a run happens BEFORE the slots query: two
# captcha solves, editProfile, the 7-call questionnaire chain, and the Playwright
# Formulario leg, which pulls full page assets through the proxy. Tearing that session
# down after a single /slots query — which is what this bot used to do — spends
# megabytes to buy one instantaneous sample, and an instantaneous sample of an empty
# calendar tells us nothing about when the calendar fills. Holding the arrived session
# and re-querying costs one captcha plus a few KB per sample, so a release becomes
# something we can actually catch, and the sample log becomes a measurement of when
# slots open rather than a guess at it.
# ---------------------------------------------------------------------------------
# Go straight to Schedule.jsp in the browser after login instead of re-submitting the whole
# application. Schedule.jsp is reachable on its own — it is the page you land on to PICK or CHANGE
# an appointment date, and the calendar it draws is the availability answer. Our accounts already
# hold a registered application ("já tem um pedido registado com agendamento futuro"), so
# re-submitting the Formulario is rejected outright; this path is the one the site itself points
# at. Falls back to the full application flow when the direct page is not usable.
DIRECT_SCHEDULE = os.environ.get('DIRECT_SCHEDULE', '1').lower() not in ('0', 'false', 'no', '')
SLOT_WATCH = os.environ.get('SLOT_WATCH', '1').lower() not in ('0', 'false', 'no', '')
SLOT_POLL_INTERVAL = float(os.environ.get('SLOT_POLL_INTERVAL', '60'))
# How long a solved schedule captcha token is reused across /slots polls. The site's token
# lifetime is ~120s; renew a little before that.
CAPTCHA_TTL = float(os.environ.get('CAPTCHA_TTL', '100'))
SLOT_WATCH_MAX_MINUTES = float(os.environ.get('SLOT_WATCH_MAX_MINUTES', '0'))  # 0 = until the session dies
SLOT_POLL_CSV = os.path.join(WORKING_DIR, 'logs', 'slot_poll.csv')

FAKE_FINGERPRINT = os.environ.get('FAKE_FINGERPRINT', '0').lower() in ('1', 'true', 'yes')
CAPTCHA_SEND_ACTION = os.environ.get('CAPTCHA_SEND_ACTION', '0').lower() in ('1', 'true', 'yes')
_SOLVER_RETRIES = int(os.environ.get('SOLVER_RETRIES', '3'))
_SOLVER_BACKOFF = float(os.environ.get('SOLVER_BACKOFF', '4'))
CAPSOLVER_API_KEY = os.environ.get('CAPSOLVER_API_KEY', '')
_anti_raw = os.environ.get('ANTICAPTCHA_API_KEYS', '')
ANTICAPTCHA_API_KEYS = [k.strip() for k in _anti_raw.split(',') if k.strip()]

# =================================================================================
# LOGGING
# =================================================================================
from logging.handlers import RotatingFileHandler


def setup_logging():
    """Console (INFO+, compact) + rotating file (DEBUG+, detailed) — same shape as v2."""
    log_dir = os.path.join(WORKING_DIR, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'api_bot.log')

    _logger = logging.getLogger('VisaAPIv3')
    _logger.setLevel(logging.DEBUG)
    _logger.handlers.clear()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S'))
    _logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=10, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(funcName)-30s | L%(lineno)-4d | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))
    _logger.addHandler(file_handler)

    _logger.info('=' * 80)
    _logger.info('  VISA API BOT v3 (dashboard-integrated) — LOG SESSION STARTED')
    _logger.info(f'  Time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    _logger.info(f'  Dashboard: {NEXT_API_BASE}')
    _logger.info('=' * 80)
    return _logger


logger = setup_logging()
# ================================================================================
# SUPORTE A ARQUIVO LOCAL DE CONTAS (CREDS_FILE) - Versão Final à Prova de Falhas
# ================================================================================
CREDS_FILE = os.environ.get('CREDS_FILE', '').strip()
_local_accounts: List[dict] = []
_local_account_idx = 0

def _load_local_accounts():
    global _local_accounts
    if not CREDS_FILE or not os.path.exists(CREDS_FILE):
        return
    import csv
    # utf-8-sig remove AUTOMATICAMENTE o caractere invisível (BOM) gerado pelo Excel
    with open(CREDS_FILE, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        
        # Loga os cabeçalhos exatos que o Python está lendo para depuração
        if reader.fieldnames:
            logger.info(f"[Local] CSV Headers encontrados no arquivo: {reader.fieldnames}")
            
        for i, row in enumerate(reader):
            # Normaliza todas as chaves: remove espaços e converte para minúsculas
            # Assim, "\ufeffusername", "Email", " EMAIL " serão todos lidos como "username"
            clean_row = {k.strip().lower(): v for k, v in row.items() if k}
            
            username = (clean_row.get('username') or clean_row.get('loginusername') or 
                        clean_row.get('email') or clean_row.get('usuario') or clean_row.get('user') or 
                        clean_row.get('login') or f'local_user_{i}')
            
            password = (clean_row.get('password') or clean_row.get('loginpassword') or 
                        clean_row.get('senha') or clean_row.get('pass') or 'default_pass')
            
            passport = (clean_row.get('passportno') or clean_row.get('passport') or 
                        clean_row.get('passport_number') or clean_row.get('passaporte') or 'A000000')
            
            email = (clean_row.get('email') or clean_row.get('username') or clean_row.get('mail') or f'{username}@local.com')
            
            first_name = (clean_row.get('firstname') or clean_row.get('first_name') or 
                          clean_row.get('nome') or 'Local')
            
            last_name = (clean_row.get('lastname') or clean_row.get('last_name') or 
                         clean_row.get('sobrenome') or 'User')
            
            _local_accounts.append({
                'id': f"local_{i}",
                'loginUsername': str(username).strip(),
                'loginPassword': str(password).strip(),
                'passportNo': str(passport).strip(),
                'email': str(email).strip(),
                'firstName': str(first_name).strip(),
                'lastName': str(last_name).strip(),
            })
            
    logger.info(f"[Local] Loaded {len(_local_accounts)} accounts from {CREDS_FILE}")

# Chama a função imediatamente após defini-la
_load_local_accounts()


# =================================================================================
# CUSTOM EXCEPTIONS
# =================================================================================
class AlreadyBookedError(Exception):
    """This identity already holds an application with a future appointment.

    Permanent for this account, not transient: the site keys an applicant on the travel
    document, so retrying with the same passport can only be refused again. Burning the
    remaining attempts costs a captcha solve and a full browser pass each, for a verdict the
    server has already given.
    """


class CaptchaBlockedError(Exception):
    """Site blocked this account after too many failed reCAPTCHA attempts — retrying
    is pointless without a cooldown."""
    pass


# =================================================================================
# DASHBOARD MACHINE-API CLIENT
# Same x-socket-secret / endpoint shapes orchestrator.py and the extensions already
# use (src/lib/api-token.ts, src/app/api/account/*, src/app/api/proxy/*).
# =================================================================================
def _dash_request(method: str, path: str, json_body: Optional[dict] = None, timeout: float = 20.0):
    """Returns (status_code, parsed_json_or_{}). Never raises — best-effort by design,
    mirroring orchestrator.py's _request()/next_get() philosophy."""
    url = NEXT_API_BASE + path
    headers = {'x-socket-secret': MACHINE_TOKEN}
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.request(method, url, json=json_body, headers=headers)
        try:
            data = resp.json()
        except Exception:
            data = {}
        return resp.status_code, data
    except Exception as e:
        logger.warning(f"[Dashboard] {method} {path} error: {type(e).__name__}: {e}")
        return 0, {}


def claim_account() -> Optional[dict]:
    global _local_account_idx
    # 1. Se CREDS_FILE estiver configurado e houver contas locais, use-as primeiro
    if CREDS_FILE and _local_account_idx < len(_local_accounts):
        acc = _local_accounts[_local_account_idx]
        _local_account_idx += 1
        return acc
    
    # 2. Fallback para o dashboard (comportamento original)
    status, data = _dash_request('GET', '/api/account/claim')
    if status != 200:
        return None
    return data.get('account')


_FILE_PROXIES: List[dict] = []
_FILE_PROXY_N = itertools.count()
# file-pool proxy id -> time it was benched. The WAF's block is per-exit-IP, site-specific and
# TEMPORARY: a proxy that stops completing TLS to this origin still serves every other origin,
# and measured recovery is on the order of a minute. So bench, do not retire — a permanent set
# would eat a 93-proxy pool in under an hour and leave it empty while most of it was already well.
_FILE_BENCH: Dict[str, float] = {}
PROXY_BENCH_SECONDS = float(os.environ.get('PROXY_BENCH_SECONDS', '600'))


def _load_proxy_file(path: str) -> List[dict]:
    """Parse an ip:port:user:pass list (one per line) into proxy dicts."""
    out: List[dict] = []
    with open(path, encoding='utf-8') as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw or raw.startswith('#'):
                continue
            parts = raw.split(':')
            if len(parts) < 2:
                continue
            ip, port = parts[0], parts[1]
            user = parts[2] if len(parts) > 2 else None
            pw = parts[3] if len(parts) > 3 else None
            out.append({'id': f'file:{ip}:{port}', 'ip': ip, 'port': int(port),
                        'username': user, 'password': pw, 'method': 'HTTP', 'label': 'file'})
    return out


def get_unassigned_proxy() -> Optional[dict]:
    """GET /api/proxy/unassigned -> {id, ip, port, username, password, method, label} or None."""
    # PROXY_LIST_FILE takes this process off the shared dashboard pool. The dashboard is also
    # what the AdsPower workers lease from, so switching pools there would move them too, and
    # its 1000+ gateway entries would drown out a 100-proxy pool under test anyway.
    if PROXY_LIST_FILE:
        global _FILE_PROXIES
        if not _FILE_PROXIES:
            try:
                _FILE_PROXIES = _load_proxy_file(PROXY_LIST_FILE)
                logger.info(f"[Proxy] Loaded {len(_FILE_PROXIES)} proxies from {PROXY_LIST_FILE} "
                            f"(dashboard pool not used)")
            except Exception as e:
                logger.error(f"[Proxy] Could not read PROXY_LIST_FILE {PROXY_LIST_FILE}: {e}")
                return None
        now = time.time()
        live = [p for p in _FILE_PROXIES
                if now - _FILE_BENCH.get(p['id'], 0.0) > PROXY_BENCH_SECONDS]
        if not live:
            soonest = min(_FILE_BENCH.values()) if _FILE_BENCH else now
            logger.error(f"[Proxy] All {len(_FILE_PROXIES)} file proxies are benched; the first "
                         f"comes back in {PROXY_BENCH_SECONDS - (now - soonest):.0f}s")
            return None
        return live[next(_FILE_PROXY_N) % len(live)]
    # Diagnostic escape hatch. Both call sites already treat a None proxy as "go direct", so this
    # only has to withhold one. For the /slots discriminator the exit IP is not the variable under
    # test — the three probe requests share one session, so it is the differences BETWEEN them that
    # carry the signal — and a gateway returning 502 on every CONNECT blocks the experiment outright.
    if os.getenv('NO_PROXY_DIRECT') == '1':
        return None
    status, data = _dash_request('GET', '/api/proxy/unassigned')
    if status != 200:
        return None
    return data.get('proxy')


def report_event(account_id: str, event: str, detail: Optional[str] = None,
                  stage: Optional[str] = None, proxy_id: Optional[str] = None):
    """POST /api/account/event — best-effort, mirrors main-human/content.js's report().
    `stage` (a WorkerStatus value) is what drives the dashboard's auto-created-per-posto
    Booking/BookingWorker sync; `event == 'booking_success'` with a JSON
    {pdfUrl,date,period} detail is what creates a BookingAppointment — keep that shape
    EXACT, the server parses it."""
    if not account_id or not event:
        return
    body = {'accountId': account_id, 'event': str(event)[:80]}
    if detail is not None:
        body['detail'] = str(detail)[:2000]
    if stage:
        body['stage'] = stage
    if proxy_id:
        body['proxyId'] = proxy_id
    _dash_request('POST', '/api/account/event', json_body=body, timeout=15.0)


def report_status(account_id: str, work_status: str):
    """POST /api/account/status — LOGGED_IN / COMPLETED / NOT_LOGGED_IN, same 3 values
    background.js sends. NOT_LOGGED_IN also releases claimedAt server-side."""
    if not account_id:
        return
    _dash_request('POST', '/api/account/status',
                   json_body={'accountId': account_id, 'workStatus': work_status}, timeout=15.0)


def report_proxy_health(proxy_id: Optional[str], healthy: bool, detail: Optional[str] = None):
    """POST /api/proxy/health — only call this for a genuinely proxy-attributable outcome
    (connect/timeout/proxy-auth failure), never for a generic site rejection, matching the
    accuracy fix made in background.js's postProxyHealth earlier this session."""
    if not proxy_id:
        return
    # File-pool proxies have no dashboard row, so record the verdict in-process instead.
    # Without this a dead entry is handed straight back on the next lease and the same
    # transport error repeats until the account's retries are gone — which is exactly what
    # 46.203.172.7 did: three attempts, three TLS failures, one account burned.
    if proxy_id.startswith('file:'):
        if not healthy:
            _FILE_BENCH[proxy_id] = time.time()
            benched = sum(1 for t in _FILE_BENCH.values()
                          if time.time() - t <= PROXY_BENCH_SECONDS)
            logger.warning(f"[Proxy] {proxy_id} benched for {PROXY_BENCH_SECONDS:.0f}s "
                           f"({benched}/{len(_FILE_PROXIES)} out): {str(detail)[:80]}")
        return
    body = {'proxyId': proxy_id, 'healthy': bool(healthy)}
    if detail:
        body['detail'] = str(detail)[:2000]
    _dash_request('POST', '/api/proxy/health', json_body=body, timeout=15.0)


def get_settings() -> Optional[dict]:
    """GET /api/settings -> {enabled, profileSyncCount, refreshTimerSec, postoId, version,
    adsProfileCount}."""
    status, data = _dash_request('GET', '/api/settings')
    if status != 200:
        return None
    return data


# =================================================================================
# CAPTCHA SOLVER — CapSolver -> Anti-Captcha, proxyless (HAR/incident-validated: proxy
# IP-coherence is NOT required and a proxied attempt just ages the token toward its
# ~120s expiry for no benefit — see the hybrid bot's 2026-05-29 controlled probe).
# =================================================================================
async def solve_captcha(api_key: str, solver_type: str = "capsolver", action: Optional[str] = None,
                         website_url: Optional[str] = None, proxy: Optional[dict] = None) -> Optional[str]:
    """Solve reCAPTCHA Enterprise v2 (visible-challenge task type — HAR-confirmed by
    gRecaptchaResponse token length; a v3 score-only token gets rejected with {"data":{}}).

    IP BINDING (2026-09-20): when a proxy is given, the solver is asked to solve THROUGH that
    same proxy, so the token is minted from the same exit IP that redeems it. reCAPTCHA
    Enterprise scores the minting context, and a token minted on CapSolver's datacenter IP but
    presented by a session on a SOAX residential IP is a textbook mismatch — a valid-but-low-
    score token, which is exactly what a server answers with a defensive stub ({"data":{}} at
    /slots) rather than an outright rejection. A partner implementation built from a real
    successful-booking HAR binds the token this way; ours never did.
    """
    proxied = bool(proxy and proxy.get('ip') and proxy.get('port'))
    if solver_type == "capsolver":
        create_url = "https://api.capsolver.com/createTask"
        result_url = "https://api.capsolver.com/getTaskResult"
        task_type = "ReCaptchaV2EnterpriseTask" if proxied else "ReCaptchaV2EnterpriseTaskProxyLess"
    else:
        create_url = "https://api.anti-captcha.com/createTask"
        result_url = "https://api.anti-captcha.com/getTaskResult"
        task_type = "RecaptchaV2EnterpriseTask" if proxied else "RecaptchaV2EnterpriseTaskProxyless"

    url = website_url or f"{BASE_URL}/VistosOnline/Authentication.jsp"

    task = {"type": task_type, "websiteURL": url, "websiteKey": SITE_KEY}
    # The widget markup carries data-action (LOGIN_EVISA on Authentication.jsp, SCHEDULE_EVISA on
    # Schedule.jsp) and the HAR-derived partner implementation always forwards it. An earlier note
    # here argued the opposite from an older reference that sent none; the newer HAR-based one
    # wins. CAPTCHA_SEND_ACTION still forces it off for A/B testing.
    if action and CAPTCHA_SEND_ACTION:
        task["enterprisePayload"] = {"action": action}

    if proxied:
        task["proxyType"] = "http"
        task["proxyAddress"] = proxy['ip']
        task["proxyPort"] = int(proxy['port'])
        if proxy.get('username'):
            task["proxyLogin"] = proxy['username']
            # Pass the password through untouched. Semicolons are not padding here — on a SOAX
            # gateway the password IS the session selector ("wifi;fi;" = wifi pool, Finland,
            # empty session id), so stripping the trailing one silently asks for a different
            # exit than the session actually holds, which is precisely the mismatch that makes
            # an Enterprise assessment fail. Verified against CapSolver: "wifi;fi;" sent
            # verbatim solves in ~18s; there is nothing to trim.
            task["proxyPassword"] = proxy.get('password') or ''
        logger.debug(f"[Captcha] solving through {proxy['ip']}:{proxy['port']} (token IP-bound)")

    payload = {"clientKey": api_key, "task": task}

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            data = None
            for attempt in range(_SOLVER_RETRIES + 1):
                try:
                    resp = await client.post(create_url, json=payload)
                    data = resp.json()
                except (httpx.ReadTimeout, httpx.ConnectError, httpx.ConnectTimeout) as e:
                    if attempt < _SOLVER_RETRIES:
                        logger.warning(f"[Captcha] Network error creating task ({type(e).__name__}) — "
                                       f"retrying [{attempt + 1}/{_SOLVER_RETRIES}]")
                        await asyncio.sleep(_SOLVER_BACKOFF * (attempt + 1))
                        continue
                    logger.error(f"[Captcha] Network error creating task: {type(e).__name__}")
                    return None

                if data.get("errorId", 0) != 0:
                    desc = str(data.get('errorDescription', data))
                    # "the server system busy, please try again later" is the solver shedding
                    # load, not a verdict on our request — and one of those was aborting whole
                    # sessions that had already spent a login and an editProfile getting this
                    # far. Wait and ask again. A bad key or an empty balance is not retryable.
                    if attempt < _SOLVER_RETRIES and re.search(
                            r'busy|try again|rate.?limit|timeout|unavailable', desc, re.I):
                        wait = _SOLVER_BACKOFF * (attempt + 1)
                        logger.warning(f"[Captcha] {solver_type} busy ({desc[:70]}) — retrying in "
                                       f"{wait:.0f}s [{attempt + 1}/{_SOLVER_RETRIES}]")
                        await asyncio.sleep(wait)
                        continue
                    # Some proxy gateways (Webshare's p.webshare.io:80 among them) refuse
                    # connections from the solver's own egress, so the IP-bound task cannot be
                    # created at all. That is a property of the proxy vendor, not of our request
                    # — fall back to a proxyless solve rather than losing the whole session.
                    if proxied and re.search(r'proxy', desc, re.I):
                        logger.warning(f"[Captcha] Solver could not use our proxy ({desc[:80]}) — "
                                       f"retrying proxyless (token will NOT be IP-bound)")
                        task.pop("proxyType", None); task.pop("proxyAddress", None)
                        task.pop("proxyPort", None); task.pop("proxyLogin", None)
                        task.pop("proxyPassword", None)
                        task["type"] = ("ReCaptchaV2EnterpriseTaskProxyLess" if solver_type == "capsolver"
                                        else "RecaptchaV2EnterpriseTaskProxyless")
                        payload["task"] = task
                        continue
                    logger.error(f"[Captcha] Create task error: {desc}")
                    return None
                break

            if not data or data.get("errorId", 0) != 0:
                return None

            task_id = data.get("taskId")
            if not task_id:
                logger.error("[Captcha] No taskId returned")
                return None

            logger.info(f"[Captcha] Task created: {task_id}")

            consecutive_errors = 0
            for attempt in range(40):  # max ~2 minutes
                await asyncio.sleep(3)
                try:
                    resp = await client.post(result_url, json={"clientKey": api_key, "taskId": task_id})
                    result = resp.json()
                    consecutive_errors = 0
                except (httpx.ReadTimeout, httpx.ConnectError, httpx.ConnectTimeout) as e:
                    consecutive_errors += 1
                    logger.warning(f"[Captcha] Network error polling ({consecutive_errors}x): {type(e).__name__}")
                    if consecutive_errors >= 3:
                        logger.error("[Captcha] Too many consecutive network errors, giving up")
                        return None
                    continue

                status = result.get("status", "")
                if status == "ready":
                    token = result.get("solution", {}).get("gRecaptchaResponse", "")
                    if token:
                        logger.info(f"[Captcha] Solved! Token length: {len(token)}")
                        return token
                elif result.get("errorId", 0) != 0:
                    logger.error(f"[Captcha] Error: {result.get('errorDescription')}")
                    return None
    except Exception as e:
        logger.error(f"[Captcha] Unexpected error: {type(e).__name__}: {e}")
        return None

    logger.error("[Captcha] Timeout — no solution after 2 minutes")
    return None


# =================================================================================
# STAGE MAPPING — mirrors main-human/content.js's computeStage(), adapted to a linear
# step-bot (no "current page" concept, but an analogous "current step" one).
# =================================================================================
def stage_for(step_key: str) -> str:
    return {
        'login': 'LOGGING_IN',
        'editprofile': 'APPLYING',
        'questionnaire': 'APPLYING',
        'formulario': 'APPLYING',
        'slots_solving': 'POLLING',
        'slots_waiting': 'HOLDING',
        'booking': 'APPLYING',
        'completed': 'COMPLETED',
    }.get(step_key, 'IDLE')


class _InPageResponse:
    """Minimal httpx/curl_cffi-shaped response for a fetch() issued inside the live page."""

    class _Req:
        headers: Dict[str, str] = {}

    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text
        self.headers: Dict[str, str] = {}
        self.history: List[object] = []
        self.url = ""
        self.request = _InPageResponse._Req()

    def json(self):
        return json.loads(self.text)


# =================================================================================
# PROXY FAULT CLASSIFICATION + PROVIDER-LEVEL BREAKER
# A failed CONNECT is not automatically this proxy row's fault. Both outages so far were
# ACCOUNT-level and identical across every row: webshare returned `402 / X-Webshare-Reason:
# bandwidthlimit` on every username on 2026-09-05, and SOAX returned `422 "traffic of package
# is spent"` on 2026-09-04. httpx surfaces those as ProxyError with the status in the message.
# Reporting them as per-proxy health walks the entire pool (11k+ rows) into permanent
# retirement one row at a time, since Proxy.healthy is a latch only a human can clear.
# =================================================================================
# httpx raises several distinct types for what is one condition — the path through the
# proxy broke — and a residential gateway produces all of them routinely. A disconnect
# mid-handshake arrives as RemoteProtocolError, NOT ProxyError, so matching only on
# ProxyError lets the most common failure escape to the generic handler and get logged as
# an unexpected bug with a full traceback. Group them.
TRANSPORT_ERRORS = (
    httpx.ProxyError, httpx.ConnectError, httpx.ConnectTimeout,
    httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError,
    httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout,
    # self.session is now a curl_cffi Session (Chrome TLS impersonation) — its exceptions
    # don't subclass httpx's, so the same transport-fault condition needs its own names too.
    curl_exceptions.ProxyError, curl_exceptions.ConnectionError, curl_exceptions.ConnectTimeout,
    curl_exceptions.ReadTimeout, curl_exceptions.Timeout, curl_exceptions.CurlError,
)

# How many polls in a row may fail on transport before a watch session is abandoned.
WATCH_MAX_CONSECUTIVE_ERRORS = int(os.environ.get('WATCH_MAX_CONSECUTIVE_ERRORS', '5'))

_PROVIDER_ACCOUNT_STATUSES = ('402', '407', '422')  # payment/quota, proxy-auth, provider quota
_PROVIDER_FAULT_WINDOW_S = 60.0
_PROVIDER_FAULT_THRESHOLD = 3
_PROVIDER_HALT_SECONDS = 900.0
_provider_fault_times: List[float] = []
_provider_halted_until: float = 0.0


def classify_proxy_error(exc: Exception) -> str:
    """'account' = provider-wide quota/billing/auth fault (do NOT blame the row);
    'transport' = this specific proxy genuinely failed to carry the connection."""
    msg = str(exc)
    return 'account' if any(code in msg for code in _PROVIDER_ACCOUNT_STATUSES) else 'transport'


def note_provider_fault(detail: str) -> None:
    """Record an account-level proxy fault; trip a global halt once they cluster."""
    global _provider_halted_until
    now = time.time()
    _provider_fault_times.append(now)
    del _provider_fault_times[:-50]
    recent = [t for t in _provider_fault_times if now - t <= _PROVIDER_FAULT_WINDOW_S]
    if len(recent) >= _PROVIDER_FAULT_THRESHOLD and now >= _provider_halted_until:
        _provider_halted_until = now + _PROVIDER_HALT_SECONDS
        logger.error(
            f"[Provider] {len(recent)} account-level proxy faults in {_PROVIDER_FAULT_WINDOW_S:.0f}s "
            f"({detail}) — the proxy PLAN is out of quota/credit, not the individual proxies. "
            f"Halting all workers for {_PROVIDER_HALT_SECONDS/60:.0f} min. Top up the provider account."
        )


def provider_halt_remaining() -> float:
    return max(0.0, _provider_halted_until - time.time())


def block_reason(resp: httpx.Response) -> Optional[str]:
    """Identify a bot-detection deny using signals this origin actually emits.

    The standard indicators do not exist here: zero occurrences of Retry-After,
    cf-mitigated, cf-ray, X-RateLimit-*, or any 429 across 691 target-host HAR entries —
    the site is not behind Cloudflare/Akamai and strips its own Server header. What it
    DOES do is 302 the request into /VistosOnline/BD_problm.jsp and serve a 28.5KB
    bilingual block page with 403. Because the client follows redirects, that hop is only
    visible in resp.history, which is why the earlier debug captures recorded the block
    page's URL and not the URL actually requested."""
    for r in list(resp.history) + [resp]:
        loc = r.headers.get('location', '')
        if 'BD_problm' in loc or 'IsHuman' in loc:
            return f'bd-redirect:{loc}'
    if resp.status_code == 403:
        return 'http-403'
    if str(resp.url).find('BD_problm') != -1:
        return 'bd-landed'
    return None


def handle_proxy_exception(bot: 'VisaAPIBot', exc: Exception, where: str) -> None:
    """Single place that decides whether a connection failure is the proxy's fault."""
    kind = classify_proxy_error(exc)
    if kind == 'account':
        note_provider_fault(f"{where}: {type(exc).__name__}: {str(exc)[:120]}")
        logger.error(f"[{where}] Provider-account proxy fault ({exc}) — row NOT marked unhealthy")
    else:
        bot._report_proxy_health(False, f"{where}: {type(exc).__name__}")


# =================================================================================
# SHARED PLAYWRIGHT BROWSER (Formulario -> ScheduleController leg — see module docstring)
# One Chromium instance per process, reused across accounts/workers via fresh per-call
# incognito contexts — avoids paying this environment's ~15-25s cold-start cost more than
# once per process lifetime.
# =================================================================================
_playwright_ctx = None
_shared_browser: Optional[Browser] = None
_browser_lock = asyncio.Lock()



class DonutClient:
    def __init__(self, base_url, token):
        self.base = base_url.rstrip('/')
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    def _req(self, method, path, body=None, timeout=30):
        import urllib.request, json as _json
        url = self.base + path
        data = _json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode()
                return _json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Donut {method} {path} -> {e.code}: {e.read().decode()[:200]}") from e
    def create_proxy(self, name, host, port, user=None, pw=None):
        body = {"name": name, "proxy_settings": {"proxy_type": "http", "host": host, "port": int(port)}}
        if user: body["proxy_settings"]["username"] = user
        if pw: body["proxy_settings"]["password"] = pw
        res = self._req("POST", "/v1/proxies", body)
        return res.get("id") or (res.get("proxy") or {}).get("id")
    def create_profile(self, name, proxy_id=None):
        body = {"name": name, "browser": "wayfern", "version": "latest"}
        if proxy_id: body["proxy_id"] = proxy_id
        res = self._req("POST", "/v1/profiles", body)
        return (res.get("profile") or {}).get("id") or res.get("id")
    def launch(self, profile_id, headless=False):
        res = self._req("POST", f"/v1/profiles/{profile_id}/run", {"headless": headless}, timeout=60)
        port = res.get("remote_debugging_port")
        if not port: raise RuntimeError(f"no CDP port: {res}")
        return int(port)
    def kill(self, profile_id):
        try: self._req("POST", f"/v1/profiles/{profile_id}/kill", timeout=10)
        except Exception: pass

_donut = None
def get_donut():
    global _donut
    if not DONUT_ENABLED: return None
    if _donut is None: _donut = DonutClient(DONUT_API_URL, DONUT_API_TOKEN)
    return _donut

async def _get_shared_browser() -> Browser:
    global _playwright_ctx, _shared_browser
    async with _browser_lock:
        if _shared_browser is None or not _shared_browser.is_connected():
            _playwright_ctx = await async_playwright().start()
            # headless=False: the known-working reference bot (visa_api_bot.py) runs non-headless
            # and this origin's getTables fingerprint beacon (bs_datefuncs.js) never fired even
            # once across multiple headless runs here (confirmed via an explicit up-to-15s wait —
            # not a timing issue, it simply never triggers). A headless-specific check in that
            # script refusing to fire is the leading explanation. DISPLAY is already available on
            # this host (Xvfb), so no extra setup is needed.
            _shared_browser = await _playwright_ctx.chromium.launch(
                headless=False,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
        return _shared_browser


# =================================================================================
# MAIN BOT CLASS
# =================================================================================
class VisaAPIBot:
    """Pure API visa appointment bot — no browser needed."""

    def __init__(self, account: dict, proxy: Optional[dict] = None):
        self.account = account
        self.proxy = proxy
        self.session = None  # curl_cffi.requests.Session (Chrome TLS impersonation) once created

        self.language = LANGUAGE
        self.consular_post_id = str(POSTO_ID_OVERRIDE or get_settings_posto_id() or POSTO_ID_FALLBACK)
        self.nationality = NATIONALITY
        self.country_of_residence = COUNTRY_OF_RESIDENCE
        self.duration_of_stay = DURATION_OF_STAY_DAYS
        # Default intended arrival: ~3 weeks out, matching the HAR/config precedent of a
        # near-future placeholder date; a real appointment date is picked in Step 7 from
        # whatever the site actually offers.
        # The Schedule page enforces "The schedule date must be prior to date of arrival"
        # (validarDataChegada), comparing every offered day against the arrival date THIS form
        # submits. Lyon's earliest offered appointment sits ~32-35 days out and its calendar runs
        # more than a year ahead, so an arrival 21 days from now puts every bookable day past the
        # deadline and the calendar comes back with nothing selectable. Give the appointment room:
        # far beyond the booking horizon, still inside the 6-month window a short-stay application
        # is accepted in.
        self.intended_date = (datetime.now() + timedelta(days=INTENDED_ARRIVAL_DAYS)).strftime('%Y/%m/%d')

        self.capsolver_key = CAPSOLVER_API_KEY
        self.anticaptcha_keys = ANTICAPTCHA_API_KEYS

        # The visa product the questionnaire resolves to. step_fill_formulario builds the form
        # before it walks the questionnaire, so this has to exist from the start; the real values
        # overwrite it once the Questionario page exposes them.
        self._quest_hidden: Dict[str, str] = {
            'tipo_visto': 'C', 'tipo_visto_desc': 'VISTO DE CURTA DURAÇÃO',
            'class_visto': 'SCH', 'cod_estada': '10', 'id_visto_doc': '36',
        }

    # ---- dashboard reporting -----------------------------------------------------
    def _report(self, event: str, detail: Optional[str] = None, stage: Optional[str] = None):
        username = self.account.get('loginUsername', '?')
        # Uniform, greppable status line — every single thing reported to the dashboard is
        # ALSO visible locally in this exact same shape (`grep '\[STATUS\]' logs/api_bot.log`
        # or `tail -f` the console), independent of dashboard reachability.
        logger.info(f"[STATUS] {username} | stage={stage or '-'} | event={event} | detail={(detail or '')[:150]}")
        _update_live_status(self.account.get('id'), username, stage, event, detail)
        try:
            report_event(
                self.account.get('id'), event, detail=detail, stage=stage,
                proxy_id=self.proxy.get('id') if self.proxy else None,
            )
        except Exception:
            logger.debug(f"[Report] failed to report '{event}'", exc_info=True)

    def _report_proxy_health(self, healthy: bool, detail: Optional[str] = None):
        try:
            report_proxy_health(self.proxy.get('id') if self.proxy else None, healthy, detail)
        except Exception:
            logger.debug("[Report] failed to report proxy health", exc_info=True)

    # ---- session / proxy -----------------------------------------------------------
    def _proxy_url(self) -> Optional[str]:
        if not self.proxy or not self.proxy.get('ip') or not self.proxy.get('port'):
            return None
        ip = self.proxy['ip']
        port = self.proxy['port']
        user = self.proxy.get('username') or ''
        pwd = self.proxy.get('password') or ''
        if user:
            return f"http://{quote(user, safe='')}:{quote(pwd, safe='')}@{ip}:{port}"
        return f"http://{ip}:{port}"

    def _create_session(self):
        proxy_url = self._proxy_url()
        if proxy_url:
            logger.info(f"[Session] Using proxy: {self.proxy['ip']}:{self.proxy['port']}")
        # curl_cffi impersonates Chrome's TLS/JA3 handshake (BoringSSL) instead of httpx's
        # plain OpenSSL one. Confirmed 2026-09-18 against a known-working reference bot
        # (visa_api_bot.py) whose own author's comment names the exact mechanism: this
        # origin's DataDome layer (bs_datefuncs.js / "bd.js") distinguishes an httpx client
        # from the real Chromium that did the Formulario leg, and rather than blocking
        # outright it silently degrades the highest-value endpoint (/slots) to a canned
        # {"data":{}} stub instead of real inventory — which is exactly the symptom this
        # bot has been stuck on (0/317 real responses, ruled out as genuinely-empty by
        # disabling the separate FAKE_FINGERPRINT beacon, which made no difference).
        # chrome146, not chrome110: BROWSER_HEADERS announces Chrome/146 and sec-ch-ua v="146",
        # so impersonating 110's TLS/JA3 handed this origin a handshake and a set of headers that
        # name two different browsers. curl_cffi 0.15 ships a chrome146 target; use it so the
        # transport and what it claims to be agree.
        client = curl_requests.Session(
            impersonate="chrome136",
            proxies={"http": proxy_url, "https": proxy_url} if proxy_url else None,
            timeout=60,
            allow_redirects=True,
            headers=BROWSER_HEADERS,
        )
        return client

    def _prewarm(self, why: str) -> None:
        """Touch a tiny cached static asset so a live pooled connection exists before a
        latency-critical POST. 96 bytes with an ETag, and it is a file every real page load
        already requests, so it is both the cheapest and the least anomalous probe available
        on this origin. Never fatal — this is an optimisation, not a step."""
        try:
            t0 = time.monotonic()
            r = self.session.get(
                f"{BASE_URL}/VistosOnline/js/Company/jquery.mCustomPage.concat.min.js",
                headers={'Accept': '*/*', 'Sec-Fetch-Dest': 'script', 'Sec-Fetch-Mode': 'no-cors',
                         'Sec-Fetch-Site': 'same-origin'},
            )
            logger.debug(f"[Prewarm] {why}: {r.status_code} in {(time.monotonic()-t0)*1000:.0f}ms")
        except Exception as e:
            logger.debug(f"[Prewarm] {why} failed (non-fatal): {type(e).__name__}: {e}")

    async def _human_delay(self, min_s: float = 0.5, max_s: float = 2.0):
        """Sleep for a human-shaped interval.

        Uniform sampling (what this used to do) has a flat histogram and hard walls at both
        ends — over a few hundred requests that is itself a recognisable signature, since no
        human produces a rectangular distribution of pauses. Real inter-action gaps are
        right-skewed: mostly quick, occasionally much slower. Sample lognormal instead, with
        the median at the midpoint of the requested range.

        min_s/max_s are now a TYPICAL range rather than hard bounds: the floor is enforced
        (being too fast is the risk we are avoiding) but the upper tail is allowed to run
        past max_s, capped at 3x so a stray sample cannot stall a run.
        """
        median = max(min_s, (min_s + max_s) / 2.0)
        delay = random.lognormvariate(math.log(median), 0.35)
        await asyncio.sleep(min(max(delay, min_s), max_s * 3.0))

    # =========================================================================
    # STEP 1+2 (hybrid): Login inside a real browser from the very first request
    # =========================================================================

    async def _login_via_donut(self, donut, username, password):
        import time
        logger.info("[Donut] Starting login via Donut Browser...")
        profile_id = None
        try:
            proxy_id = None
            if self.proxy and self.proxy.get("ip"):
                pname = f"bot-{self.proxy['ip']}-{self.proxy.get('port')}-{int(time.time())}"
                proxy_id = donut.create_proxy(pname, self.proxy["ip"], self.proxy["port"],
                                              self.proxy.get("username"), self.proxy.get("password"))
                logger.info(f"[Donut] Proxy created: {proxy_id}")
            profile_id = donut.create_profile(f"visa-{username}-{int(time.time())}", proxy_id)
            logger.info(f"[Donut] Profile created: {profile_id}")
            port = donut.launch(profile_id, headless=False)
            logger.info(f"[Donut] CDP port: {port}")
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()

                last_alert_msg = None

                async def _handle_login_dialog(dialog):
                    nonlocal last_alert_msg
                    last_alert_msg = dialog.message
                    logger.warning(f"[Donut] alert(): {dialog.message!r}")
                    await dialog.accept()

                page.on("dialog", _handle_login_dialog)

                await page.goto("https://pedidodevistos.mne.gov.pt/VistosOnline/Authentication.jsp?language=PT",
                                wait_until="domcontentloaded", timeout=60000)
                logger.info(f"[Donut] Page: {page.url}")
                loc = page.locator("input[name='username']").first
                try:
                    await loc.wait_for(state="visible", timeout=45000)
                except Exception:
                    title = await page.title()
                    body = (await page.inner_text("body"))[:180]
                    logger.error(f"[Donut] Form missing. title={title!r} body={body!r}")
                    return False

                try:
                    accept_btn = page.locator("#allowAllCookiesBtn").first
                    if await accept_btn.count() == 0:
                        accept_btn = page.get_by_role("button", name="Aceitar todas").first
                    if await accept_btn.count() > 0 and await accept_btn.is_visible():
                        await accept_btn.click(timeout=3000)
                        logger.info("[Donut] Accepted cookie consent banner")
                        await asyncio.sleep(0.6)
                except Exception as cookie_exc:
                    logger.info(f"[Donut] Cookie banner not clicked: {type(cookie_exc).__name__}")

                await page.fill("input[name='username']", username)
                await page.fill("input[name='password']", password)
                logger.info("[Donut] Filled credentials")

                async def _widget_token():
                    return await page.evaluate(
                        "() => { try { const g = (window.grecaptcha && "
                        "window.grecaptcha.enterprise) || window.grecaptcha; "
                        "return (g && g.getResponse) ? String(g.getResponse() || '') : ''; } "
                        "catch (e) { return ''; } }")

                if CAPTCHA_CLICK_FIRST:
                    try:
                        anchor = page.frame_locator(
                            "iframe[src*='recaptcha/enterprise/anchor']").locator("#recaptcha-anchor")
                        await anchor.wait_for(state="visible", timeout=15000)
                        await anchor.click()
                        logger.info("[Donut] Clicked the reCAPTCHA checkbox")
                    except Exception as exc:
                        logger.info(f"[Donut] Checkbox not clicked ({type(exc).__name__}) "
                                    "— tick it in the Donut window")

                wait_s = CAPTCHA_WIDGET_WAIT_SECONDS
                logger.info(f"[Donut] Waiting up to {wait_s:.0f}s for the page widget to issue a token. "
                            "If an image challenge is open, solve it in the Donut window. "
                            "Submit stays disabled until then.")
                self._report('captcha_solving', 'donut widget', stage_for('login'))
                captcha_token = ''
                deadline = time.time() + wait_s
                while time.time() < deadline:
                    await asyncio.sleep(0.5)
                    captcha_token = await _widget_token()
                    if captcha_token:
                        break
                if not captcha_token:
                    logger.error(f"[Donut] Widget issued no token in {wait_s:.0f}s — "
                                 "not submitting and not calling the solver")
                    self._report('login_failed', 'widget-token-unavailable', stage_for('login'))
                    return False
                logger.info(f"[Donut] Widget token length: {len(captcha_token)}")
                self._report('captcha_solved', f'login widget len={len(captcha_token)}',
                             stage_for('login'))

                login_status = {'status': None}

                async def _log_login_resp(resp):
                    try:
                        if '/VistosOnline/login' not in resp.url or resp.request.method != 'POST':
                            return
                        login_status['status'] = resp.status
                        body = ''
                        try:
                            body = (await resp.text())[:300]
                        except Exception:
                            body = '<unreadable>'
                        logger.info(f"[Donut] POST /login -> HTTP {resp.status} body={body!r}")
                    except Exception:
                        pass

                page.on('response', _log_login_resp)
                logger.info("[Donut] Submitting login...")
                self._report('login_submitted', None, stage_for('login'))
                last_alert_msg = None
                try:
                    await page.click("#loginFormSubmitButton", timeout=5000)
                except Exception:
                    await page.keyboard.press("Enter")

                logged_in = False
                for i in range(1, 31):
                    await asyncio.sleep(1)
                    if page.is_closed():
                        break
                    try:
                        content_lower = (await page.content()).lower()
                        url_lower = page.url.lower()
                        if ("user-label" in content_lower or "terminar sessão" in content_lower or
                                "terminar sess&#227;o" in content_lower or "logout" in content_lower or
                                "pedidos" in url_lower or "authentication" not in url_lower):
                            logged_in = True
                            logger.info(f"[Donut] Login confirmed after {i}s")
                            break
                    except Exception:
                        continue
                    if last_alert_msg and 'erro' in last_alert_msg.lower():
                        logger.warning(f"[Donut] POST /login -> HTTP "
                                       f"{login_status.get('status', '?')} after {i}s; "
                                       f"page alert: {last_alert_msg!r}")
                        break

                cookies = await ctx.cookies()
                names = [c["name"] for c in cookies]
                logger.info(f"[Donut] Final URL={page.url} cookies={names}")
                if not logged_in:
                    logger.warning("[Donut] Login not confirmed — keeping this attempt failed")
                    self._report('login_failed',
                                 f'no confirmation: {last_alert_msg}'[:150], stage_for('login'))
                    return False

                self.session = self._create_session()
                for c in cookies:
                    self.session.cookies.set(
                        c["name"], c["value"],
                        domain=(c.get("domain") or '').lstrip('.'),
                        path=c.get("path") or '/',
                    )
                logger.info("[Donut] Login success — cookies synced")
                self._report('login_success', 'donut', stage_for('login'))
                return True
        except Exception as e:
            logger.error(f"[Donut] Error: {e}")
            return False
        finally:
            if profile_id:
                donut.kill(profile_id)

    async def step_hybrid_login(self) -> bool:
        """Login via a real Playwright page instead of httpx/curl_cffi.

        Every cheaper fix (curl_cffi TLS impersonation, disabling the fabricated getTables
        stub, actively waiting for the real one, headless vs non-headless) was tested and
        ruled out individually — the getTables fingerprint beacon (bs_datefuncs.js) never
        fires at all when the SESSION's cookies were born from a non-browser client (httpx/
        curl_cffi) at login, even once Formulario/Schedule.jsp switch to a real browser
        afterward. A known-working reference implementation (visa_api_bot.py) confirms this:
        it never touches httpx for login — the whole session starts inside a real browser.
        Ported here, adapted to this site's CURRENT login form (confirmed directly against
        a live fetch of Authentication.jsp 2026-09-18): input[name=username/password] and
        #loginFormSubmitButton are present on load, no click-through needed to reveal them.
        """
        logger.info("[Step 1+2] Hybrid login (real browser) starting...")
        username = self.account.get('loginUsername')
        password = self.account.get('loginPassword')
        donut = get_donut()
        if donut:
            return await self._login_via_donut(donut, username, password)

        try:
            browser = await _get_shared_browser()
        except Exception as e:
            logger.error(f"[Hybrid] Failed to launch Playwright browser: {e}")
            self._report('login_page_failed', f'playwright-launch-failed: {e}', stage_for('login'))
            return False

        proxy_cfg = None
        if self.proxy and self.proxy.get('ip') and self.proxy.get('port'):
            proxy_cfg = {"server": f"http://{self.proxy['ip']}:{self.proxy['port']}"}
            if self.proxy.get('username'):
                proxy_cfg["username"] = self.proxy['username']
                proxy_cfg["password"] = self.proxy.get('password') or ''

        context = await browser.new_context(locale="en-US", viewport=None, proxy=proxy_cfg)
        page = None
        try:
            await stealth_async(context)
            try:
                cdp = await context.new_cdp_session(await context.new_page())
                await cdp.send("Network.clearBrowserCache")
                await cdp.send("Network.clearBrowserCookies")
                await cdp.detach()
            except Exception:
                pass
            for p in context.pages:
                await p.close()
            page = await context.new_page()

            # AjaxSucceeded()/AjaxFailed() (confirmed via a live fetch of Authentication.jsp)
            # report the REAL reason for a rejected login via alert(resultObj.description) for
            # error/ReCaptchaError/secblock cases — and a genuine success does NOT navigate away,
            # it just calls location.reload() on the same URL. Without a dialog handler, that
            # alert() sits there blocking page JS silently (Playwright auto-dismisses dialogs it
            # has no handler for, but we'd never see WHY), which is very likely why failures here
            # have looked like a silent timeout instead of a legible error every time so far.
            last_alert_msg = None

            async def _handle_login_dialog(dialog):
                nonlocal last_alert_msg
                last_alert_msg = dialog.message
                logger.warning(f"[Hybrid] alert(): {dialog.message!r}")
                await dialog.accept()

            page.on("dialog", _handle_login_dialog)

            # Watch the site's bot-detection beacon on the LOGIN page.
            # bootstrap-datefunctions.min.js (the renamed bs_datefuncs.js) fetches
            # jquery.mCustomPage.concat.min.js and then POSTs an encrypted fingerprint to
            # /VistosOnline/js/jquery.getTables/<md5>. That POST is how this origin decides a
            # session is real, and the body our 403s carry is BD_problm.jsp — the same tier's
            # refusal page. In the one HAR of a login that worked, the beacon landed 23s BEFORE
            # the login POST. We have logged 1337 of these beacons — every one of them from the
            # Formulario page, never from here, because this is the first time we look.
            bd = {'loader': None, 'beacon': None, 'path': None}

            async def _log_bd(resp):
                try:
                    if 'mCustomPage.concat.min.js' in resp.url:
                        bd['loader'] = resp.status
                        logger.info(f"[Hybrid] BD loader fetched -> {resp.status}")
                    elif '/js/jquery.getTables/' in resp.url:
                        bd['beacon'] = resp.status
                        bd['path'] = resp.url.rsplit('/', 1)[-1]
                        try:
                            body = (await resp.text())[:120]
                        except Exception:
                            body = '<unreadable>'
                        logger.info(f"[Hybrid] BD BEACON {resp.status} "
                                    f"md5={bd['path']} body={body!r}")
                except Exception:
                    pass

            page.on('response', _log_bd)

            login_url = f"{BASE_URL}/VistosOnline/Authentication.jsp?language={self.language}"
            logger.info(f"[Hybrid] Navigating to {login_url}")
            await page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
            await self._human_delay(1.5, 3.0)

            # allowAllCookies() sets the real user_consent cookie (and loads GA) — the exact
            # cookie the old httpx flow used to fabricate via a guessed md5 hash, with a comment
            # noting the server checks for it before /slots. Every hybrid-login test so far left
            # this banner undismissed (visually confirmed via screenshot), so the real cookie was
            # never actually set either way. Click it for real now.
            try:
                accept_btn = page.locator("#allowAllCookiesBtn").first
                if await accept_btn.count() > 0 and await accept_btn.is_visible():
                    await self._human_delay(0.5, 1.2)
                    await accept_btn.click(timeout=3000)
                    logger.info("[Hybrid] Accepted cookie consent banner")
                    await self._human_delay(0.5, 1.0)
            except Exception as cookie_exc:
                logger.debug(f"[Hybrid] Cookie consent banner not handled: {cookie_exc}")

            # Give the bot-detection tier something to measure, then time to decide.
            # The reference implementation spends 35-90s on this page before submitting and
            # justifies it against the same script by name: "O loadBotd mede o tempo desde a
            # abertura do browser até à primeira acção / Bot: 0ms — Humano: 2-5s". We spend
            # 10-20s and submit. Scroll and move the pointer first so there is a signal at all.
            try:
                await page.mouse.move(random.randint(200, 800), random.randint(150, 500), steps=20)
                await asyncio.sleep(random.uniform(0.2, 0.5))
                await page.evaluate("window.scrollBy({top: 150, behavior: 'smooth'})")
                await asyncio.sleep(random.uniform(0.4, 0.9))
                await page.evaluate("window.scrollBy({top: -80, behavior: 'smooth'})")
                await asyncio.sleep(random.uniform(0.3, 0.8))
                await page.mouse.move(random.randint(200, 800), random.randint(150, 500), steps=15)
            except Exception:
                pass

            for _sec in range(BD_BEACON_WAIT_SECONDS):
                if bd['beacon'] is not None:
                    break
                await asyncio.sleep(1)
            try:
                cookie_names = [c['name'] for c in await context.cookies()]
            except Exception:
                cookie_names = []
            if bd['beacon'] is None:
                logger.warning(f"[Hybrid] BD beacon never fired on the login page in "
                               f"{BD_BEACON_WAIT_SECONDS}s (loader={bd['loader']}); "
                               f"cookies={cookie_names}")
                if BD_REQUIRE_BEACON:
                    self._report('login_failed', 'bd-beacon-absent', stage_for('login'))
                    self._report_proxy_health(False, 'BD beacon never fired on the login page')
                    return False
            else:
                logger.info(f"[Hybrid] BD beacon in hand before login; cookies={cookie_names}")

            # reCAPTCHA Enterprise loads async — let it finish before we touch the widget.
            await asyncio.sleep(random.uniform(2.0, 4.0))

            user_loc = page.locator("input[name='username']").first
            await user_loc.wait_for(state="visible", timeout=20000)
            await self._human_delay(0.5, 1.0)

            logger.info("[Hybrid] Filling credentials with human-shaped interaction...")
            for selector, value in [("input[name='username']", username), ("input[name='password']", password)]:
                loc = page.locator(selector).first
                box = await loc.bounding_box()
                if box:
                    target_x = box["x"] + box["width"] * random.uniform(0.35, 0.65)
                    target_y = box["y"] + box["height"] * random.uniform(0.35, 0.65)
                    steps = random.randint(15, 25)
                    for i in range(steps + 1):
                        t = i / steps
                        x = random.randint(250, 750) * (1 - t) + target_x * t
                        y = random.randint(250, 550) * (1 - t) + target_y * t
                        await page.mouse.move(x, y)
                        await asyncio.sleep(0.01 + t * 0.018)
                    await asyncio.sleep(random.uniform(0.12, 0.28))
                    await page.mouse.down()
                    await asyncio.sleep(random.uniform(0.07, 0.14))
                    await page.mouse.up()
                    await asyncio.sleep(random.uniform(0.18, 0.35))
                for char in value:
                    delay_ms = int(random.uniform(80, 200) + (random.uniform(250, 550) if random.random() < 0.10 else 0))
                    await page.keyboard.press(char, delay=delay_ms)
                await asyncio.sleep(random.uniform(0.5, 1.2))

            curr_url = page.url

            # Try the page's OWN checkbox before substituting a solver token.
            # POST /login answers 403 even from a real browser that holds the site's own consent
            # cookie, types like a person and passes the page's client-side captcha check — and
            # the check passes only because we overwrite grecaptcha.getResponse() with a token
            # minted somewhere else. The server then asks Google to assess that token, and the
            # assessment carries where it was solved. The reference session we are chasing does
            # not substitute anything: its screenshots show the real widget, an image challenge,
            # and a ticked box. On a clean ISP exit IP the checkbox frequently passes with no
            # challenge at all, and a token the widget itself issued is the one thing we have
            # never actually put in front of this endpoint.
            captcha_token = None
            if CAPTCHA_CLICK_FIRST:
                try:
                    anchor = page.frame_locator(
                        "iframe[src*='recaptcha/enterprise/anchor']").locator("#recaptcha-anchor")
                    await anchor.wait_for(state="visible", timeout=10000)
                    await anchor.click()
                    logger.info(f"[Hybrid] Clicked the reCAPTCHA checkbox — waiting up to "
                                f"{CAPTCHA_WIDGET_WAIT_SECONDS:.0f}s for the widget")
                    for _ in range(int(CAPTCHA_WIDGET_WAIT_SECONDS * 2)):
                        await asyncio.sleep(0.5)
                        n = await page.evaluate(
                            "() => { try { const g = (window.grecaptcha && "
                            "window.grecaptcha.enterprise) || window.grecaptcha; "
                            "return g && g.getResponse ? String(g.getResponse() || '').length : 0; } "
                            "catch (e) { return 0; } }")
                        if n and n > 0:
                            captcha_token = await page.evaluate(
                                "() => { const g = (window.grecaptcha && "
                                "window.grecaptcha.enterprise) || window.grecaptcha; "
                                "return g.getResponse(); }")
                            logger.info(f"[Hybrid] Widget issued its own token ({n} chars) — "
                                        f"no solver substitution needed")
                            break
                    if not captcha_token:
                        logger.info("[Hybrid] Checkbox did not resolve on its own "
                                    "(challenge shown or still pending) — falling back to the solver")
                        # Clicking the checkbox opens an image challenge, and that overlay sits on
                        # top of the page: a screenshot of this exact failure shows "Selecione
                        # todas as imagens com bicicletas" covering the login form, and the submit
                        # click below then lands on the overlay instead of the button — no POST
                        # /login is made at all, which is why the run reported "no-response-seen"
                        # with no alert. Dismiss the challenge before substituting a token.
                        try:
                            await page.evaluate(
                                "() => { const g = (window.grecaptcha && "
                                "window.grecaptcha.enterprise) || window.grecaptcha; "
                                "if (g && g.reset) g.reset(); }")
                            await asyncio.sleep(1.0)
                            logger.info("[Hybrid] Reset the widget to clear the challenge overlay")
                        except Exception as reset_exc:
                            logger.warning(f"[Hybrid] Could not reset the widget: "
                                           f"{type(reset_exc).__name__}")
                except Exception as exc:
                    logger.info(f"[Hybrid] Could not drive the checkbox "
                                f"({type(exc).__name__}) — falling back to the solver")

            widget_token = captcha_token          # non-empty only if the widget itself issued one
            if captcha_token:
                self._report('captcha_solved', f'login widget len={len(captcha_token)}',
                             stage_for('login'))
            elif CAPTCHA_WIDGET_ONLY:
                logger.error("[Hybrid] Widget issued no token and CAPTCHA_WIDGET_ONLY is set — "
                             "not substituting a solver token; rotating the exit IP instead")
                self._report('login_failed', 'widget-token-unavailable', stage_for('login'))
                self._report_proxy_health(False, 'reCAPTCHA widget would not pass on this IP')
                return False
            if not captcha_token:
                logger.info("[Hybrid] Solving reCAPTCHA...")
                self._report('captcha_solving', 'login', stage_for('login'))
            if not captcha_token and self.capsolver_key:
                captcha_token = await solve_captcha(self.capsolver_key, "capsolver", action="LOGIN_EVISA", website_url=curr_url, proxy=self.proxy)
            if not captcha_token:
                for key in self.anticaptcha_keys:
                    captcha_token = await solve_captcha(key, "anticaptcha", action="LOGIN_EVISA", website_url=curr_url, proxy=self.proxy)
                    if captcha_token:
                        break
            if not captcha_token:
                logger.error("[Hybrid] Could not solve CAPTCHA")
                self._report('captcha_failed', 'login', stage_for('login'))
                return False
            self._report('captcha_solved', f'login len={len(captcha_token)}', stage_for('login'))

            # The real submit handler (confirmed via a live fetch of Authentication.jsp) does:
            #   var captchaResponse = grecaptcha.enterprise.getResponse();
            #   if (captchaResponse === null || captchaResponse === '') { show error; return; }
            # — a direct function call, not a read of the hidden textarea. Google's own
            # enterprise.js loads asynchronously and can finish initializing (replacing
            # window.grecaptcha.enterprise wholesale) AFTER our injection runs, silently
            # wiping our override — the likely cause of this step's intermittent failures.
            # Wait for the real object to exist first, then inject, then verify the override
            # actually stuck by calling the exact function the site itself is about to call.
            # None of that applies when the widget issued the token itself: getResponse()
            # already returns it, the textarea already holds it, and the callbacks have already
            # run. Overriding a genuine widget response with a copy of itself would only replace
            # the real object with our stub, which is the thing we are trying to stop doing.
            if widget_token:
                logger.info("[Hybrid] Using the widget's own token — skipping injection")
            else:
                try:
                    await page.wait_for_function(
                        "() => window.grecaptcha && window.grecaptcha.enterprise",
                        timeout=8000,
                    )
                except Exception:
                    logger.warning("[Hybrid] grecaptcha.enterprise never appeared before injection")

                await page.evaluate(
                """
                (token) => {
                    if (!window.grecaptcha) window.grecaptcha = {};
                    const oR = () => token;
                    window.grecaptcha.getResponse = oR;
                    if (window.grecaptcha.enterprise) window.grecaptcha.enterprise.getResponse = oR;
                    else window.grecaptcha.enterprise = { getResponse: oR };
                    let t = document.querySelector('textarea[name="g-recaptcha-response"]');
                    if (!t) {
                        t = document.createElement('textarea');
                        t.name = 'g-recaptcha-response';
                        t.style.display = 'none';
                        document.forms[0] && document.forms[0].appendChild(t);
                    }
                    if (t) t.value = token;
                    try {
                        if (typeof ___grecaptcha_cfg !== 'undefined') {
                            const clients = ___grecaptcha_cfg.clients;
                            Object.keys(clients).forEach(k => {
                                const client = clients[k];
                                Object.keys(client).forEach(p => {
                                    if (client[p] && typeof client[p] === 'object') {
                                        Object.keys(client[p]).forEach(pp => {
                                            if (client[p][pp] && typeof client[p][pp] === 'object') {
                                                Object.keys(client[p][pp]).forEach(ppp => {
                                                    if (client[p][pp][ppp] && typeof client[p][pp][ppp].callback === 'function') {
                                                        client[p][pp][ppp].callback(token);
                                                    }
                                                });
                                            }
                                        });
                                    }
                                });
                            });
                        }
                    } catch (e) {}
                }
                """,
                    captcha_token,
                )
            await self._human_delay(1.0, 2.0)

            # Verify the override actually stuck — read back with the SAME call the site's own
            # submit handler makes, right before we click submit, and re-inject once if a late
            # async init clobbered it. Skipped for a widget token: there is no override to check,
            # and a mismatch there would only mean the widget refreshed its own response.
            verified_len = -1 if widget_token else await page.evaluate(
                "() => (window.grecaptcha && window.grecaptcha.enterprise && "
                "window.grecaptcha.enterprise.getResponse) ? "
                "String(window.grecaptcha.enterprise.getResponse() || '').length : -1"
            )
            logger.info(f"[Hybrid] grecaptcha.enterprise.getResponse() verified length: {verified_len} "
                        f"(token was {len(captcha_token)})")
            if not widget_token and verified_len != len(captcha_token):
                logger.warning("[Hybrid] Override did not stick (likely clobbered by async init) — re-injecting")
                await page.evaluate(
                    "(token) => { window.grecaptcha.enterprise.getResponse = () => token; }",
                    captcha_token,
                )
                verified_len = await page.evaluate(
                    "() => String(window.grecaptcha.enterprise.getResponse() || '').length"
                )
                logger.info(f"[Hybrid] After re-injection, verified length: {verified_len}")

            # Capture what /login actually answers. The page turns every non-429 failure into
            # the same generic alert ("Foi encontrado um erro ao executar a operação") via its
            # AjaxFailed handler, so the alert text alone cannot distinguish a 403 bot-block from
            # a 500 or a refused credential — and we have been reading that one string as if it
            # were the server's verdict.
            login_status = {}

            async def _log_login_resp(resp):
                try:
                    if '/VistosOnline/login' not in resp.url:
                        return
                    login_status['status'] = resp.status
                    login_status['url'] = resp.url
                    # The BODY, not just the status. A 403 here is served either by the site's
                    # bot-detection tier (BD_problm.jsp — "Não foi possível processar o pedido",
                    # which is what our captured 403s contain) or by something else entirely, and
                    # those two call for opposite fixes. We have recorded this status many times
                    # and never once looked at what came with it.
                    try:
                        body = await resp.text()
                    except Exception as exc:
                        body = f'<unreadable: {type(exc).__name__}>'
                    login_status['body'] = body
                    logger.info(f"[Hybrid] POST /login -> {resp.status} "
                                f"ct={resp.headers.get('content-type')!r} len={len(body)} "
                                f"body={body[:400]!r}")
                    if resp.status != 200:
                        try:
                            d = os.path.join(WORKING_DIR, 'debug_html')
                            os.makedirs(d, exist_ok=True)
                            p = os.path.join(
                                d, f"login_{resp.status}_{self.username}_{int(time.time())}.html")
                            with open(p, 'w', encoding='utf-8') as fh:
                                fh.write(body)
                            logger.info(f"[Hybrid] Saved the {resp.status} body -> {p}")
                        except Exception:
                            pass
                except Exception:
                    pass

            page.on('response', _log_login_resp)
            logger.info("[Hybrid] Submitting login...")
            self._report('login_submitted', None, stage_for('login'))
            submit_btn = page.locator(
                "#loginFormSubmitButton, #NewloginForm-d button[type='submit'], "
                "#loginForm-d button[type='submit'], button[type='submit']"
            ).first
            try:
                await submit_btn.wait_for(state="visible", timeout=10000)
                await submit_btn.click(force=True)
            except Exception:
                await page.keyboard.press("Enter")

            logger.info("[Hybrid] Waiting for login confirmation (up to 30s)...")
            # Only alerts raised from here on are the server's answer to THIS submit; anything
            # the page said earlier must not abort the wait.
            last_alert_msg = None
            logged_in = False
            for i in range(1, 31):
                await asyncio.sleep(1)
                if page.is_closed():
                    break
                try:
                    content_lower = (await page.content()).lower()
                    url_lower = page.url.lower()
                    if ("user-label" in content_lower or "terminar sessão" in content_lower or
                            "terminar sess&#227;o" in content_lower or "logout" in content_lower or
                            "pedidos" in url_lower or "authentication" not in url_lower):
                        logged_in = True
                        logger.info(f"[Hybrid] Login confirmed after {i}s")
                        break
                except Exception:
                    continue
                # The server answers a refused login with an alert, and it has already answered
                # by the time we see one. Sitting out the remaining seconds only slows the retry
                # that is actually going to succeed: this error is intermittent, not terminal —
                # every account it has hit has also logged in successfully at other times.
                if last_alert_msg and 'erro' in last_alert_msg.lower():
                    logger.warning(f"[Hybrid] POST /login -> HTTP "
                                   f"{login_status.get('status', '?')} after {i}s; page alert: "
                                   f"{last_alert_msg!r}")
                    break

            if not logged_in:
                logger.error(f"[Hybrid] Login not confirmed. POST /login answered HTTP "
                             f"{login_status.get('status', 'no-response-seen')}. Final URL: {page.url}. "
                             f"Last alert seen: {last_alert_msg!r}")
                try:
                    dump_dir = os.path.join(WORKING_DIR, 'debug_html')
                    os.makedirs(dump_dir, exist_ok=True)
                    dump_path = os.path.join(dump_dir, f"hybrid_login_fail_{username}_{int(time.time())}.html")
                    with open(dump_path, 'w', encoding='utf-8') as fh:
                        fh.write(await page.content())
                    shot_path = os.path.join(dump_dir, f"hybrid_login_fail_{username}_{int(time.time())}.png")
                    await page.screenshot(path=shot_path)
                    logger.error(f"[Hybrid] Dumped failure state -> {os.path.relpath(dump_path, WORKING_DIR)}, "
                                 f"{os.path.relpath(shot_path, WORKING_DIR)}")
                except Exception as dump_exc:
                    logger.warning(f"[Hybrid] Could not dump failure state: {dump_exc}")
                self._report('login_failed', f'no confirmation: {last_alert_msg}'[:150], stage_for('login'))
                return False

            logger.info(f"[Step 2] Login successful! Operação efetuada com sucesso.")
            self._report_proxy_health(True)
            self._report('login_success', 'Operação efetuada com sucesso.', stage_for('login'))

            # Hand the browser-born cookies to a fresh curl_cffi session — every later httpx-
            # style call (questionnaire, editProfile, /slots) rides on cookies that were
            # genuinely set by a real browser's own navigation, not fabricated/promoted by us.
            self.session = self._create_session()
            for c in await context.cookies():
                self.session.cookies.set(
                    c["name"], c["value"],
                    domain=(c.get("domain") or '').lstrip('.'),
                    path=c.get("path") or '/',
                )
            self._login_page_html = await page.content()
            logger.debug(f"[Hybrid] Session cookies after login: {dict(self.session.cookies)}")
            return True
        except Exception as e:
            logger.error(f"[Hybrid] Unexpected error: {type(e).__name__}: {e}")
            self._report('login_failed', f'hybrid error: {type(e).__name__}', stage_for('login'))
            return False
        finally:
            if page is not None:
                try:
                    await context.close()
                except Exception:
                    pass

    # =========================================================================
    # STEP 1: Load login page and get session cookies (unused — kept for reference,
    # superseded by step_hybrid_login above)
    # =========================================================================
    async def step_load_login_page(self) -> bool:
        logger.info("[Step 1] Loading login page...")
        self.session = self._create_session()

        url = f"{BASE_URL}/VistosOnline/Authentication.jsp?language={self.language}"
        try:
            resp = self.session.get(url)
        except (httpx.ProxyError, httpx.ConnectError, httpx.ConnectTimeout,
                curl_exceptions.ProxyError, curl_exceptions.ConnectionError,
                curl_exceptions.ConnectTimeout, curl_exceptions.CurlError) as e:
            logger.error(f"[Step 1] Proxy/connection error: {type(e).__name__}: {e}")
            handle_proxy_exception(self, e, 'Step 1')
            self._report('login_page_failed', f'proxy/connect error: {type(e).__name__}', stage_for('login'))
            return False
        except (httpx.ReadTimeout, curl_exceptions.ReadTimeout, curl_exceptions.Timeout):
            logger.error("[Step 1] Read timeout loading login page")
            self._report('login_page_failed', 'read timeout', stage_for('login'))
            return False

        if resp.status_code != 200:
            logger.error(f"[Step 1] Failed to load login page: {resp.status_code}")
            self._report('login_page_failed', f'http {resp.status_code}', stage_for('login'))
            return False

        # A successful, well-formed response through this proxy is itself a positive signal.
        self._report_proxy_health(True)

        self._login_page_html = resp.text
        cookies = dict(self.session.cookies)
        logger.info(f"[Step 1] Login page loaded. Cookies: {list(cookies.keys())}")
        logger.debug(f"[Step 1] Response status={resp.status_code} headers={dict(resp.headers)}")
        logger.debug(f"[Step 1] Cookie values: {cookies}")
        if 'Vistos_sid' not in cookies:
            logger.warning("[Step 1] Vistos_sid cookie not found!")

        # The cookies a real browser ends up with after clicking "Accept all", reproduced for the
        # HTTP leg. A capture of the partner's live session shows exactly three beyond the server's
        # own: user_consent=1, stats_ga, stats_ga_BRJX0W80X8 — and NOT _USER_CONSENT, which we used
        # to invent here from an md5 of Vistos_sid. The site never sets that name, so it was a
        # header only a bot would send, on every request including /slots, to an origin whose
        # reCAPTCHA Enterprise assessment is exactly what decides whether /slots answers at all.
        cookie_domain = 'pedidodevistos.mne.gov.pt'
        self.session.cookies.set('user_consent', '1', domain=cookie_domain, path='/')
        ga_client_id = f"{random.randint(100000000, 999999999)}.{int(time.time())}"
        ga_session = f"GS2.4.s{int(time.time())}$o1$g1$t{int(time.time())}$j0$l0$h0"
        self.session.cookies.set('stats_ga', f'GA1.4.{ga_client_id}', domain=cookie_domain, path='/')
        self.session.cookies.set('stats_ga_BRJX0W80X8', ga_session, domain=cookie_domain, path='/')

        # getTables fingerprint check — required before every successful step transition
        # per the roadmap (see _do_fingerprint_check's docstring).
        self._do_fingerprint_check(self._login_page_html, url)

        self._report('login_page_loaded', None, stage_for('login'))
        await self._human_delay(1.0, 2.0)
        return True

    # =========================================================================
    # STEP 2: Solve CAPTCHA and Login
    # =========================================================================
    async def step_login(self) -> bool:
        username = self.account.get('loginUsername')
        password = self.account.get('loginPassword')
        logger.info(f"[Step 2] Logging in as {username}...")

        login_page_url = f"{BASE_URL}/VistosOnline/Authentication.jsp?language={self.language}"
        self._do_fingerprint_check(getattr(self, '_login_page_html', ''), login_page_url)

        self._report('captcha_solving', 'login', stage_for('login'))
        captcha_token = None
        captcha_solved_at = None

        if self.capsolver_key:
            captcha_token = await solve_captcha(self.capsolver_key, "capsolver", action="LOGIN_EVISA",
                                                    website_url=f"{BASE_URL}/VistosOnline/Authentication.jsp?language={self.language}", proxy=self.proxy)
            if captcha_token:
                captcha_solved_at = time.time()

        if not captcha_token:
            for key in self.anticaptcha_keys:
                captcha_token = await solve_captcha(key, "anticaptcha", action="LOGIN_EVISA",
                                                        website_url=f"{BASE_URL}/VistosOnline/Authentication.jsp?language={self.language}", proxy=self.proxy)
                if captcha_token:
                    captcha_solved_at = time.time()
                    break

        if not captcha_token:
            logger.error("[Step 2] Could not solve CAPTCHA")
            self._report('captcha_failed', 'login', stage_for('login'))
            return False
        self._report('captcha_solved', f'login len={len(captcha_token)}', stage_for('login'))
        self._prewarm('before login POST')  # the solve above just idled the pool for 10-40s

        login_url = f"{BASE_URL}/VistosOnline/login"
        login_data = {
            'username': username, 'password': password,
            'language': self.language, 'rgpd': 'Y', 'captchaResponse': captcha_token,
        }
        # HAR evidence (2026-08-20 successful browser login): every other state-changing
        # POST in this flow sets Origin — /login was the one spot missing it.
        headers = {**AJAX_HEADERS, 'Origin': BASE_URL, 'Referer': login_page_url}

        self._report('login_submitted', None, stage_for('login'))
        resp = None
        for login_try in range(3):
            if captcha_solved_at and (time.time() - captcha_solved_at) > 110:
                logger.warning("[Step 2] Token too old — re-solving...")
                captcha_token = None
                if self.capsolver_key:
                    captcha_token = await solve_captcha(self.capsolver_key, "capsolver", action="LOGIN_EVISA",
                                                    website_url=f"{BASE_URL}/VistosOnline/Authentication.jsp?language={self.language}", proxy=self.proxy)
                if not captcha_token:
                    for key in self.anticaptcha_keys:
                        captcha_token = await solve_captcha(key, "anticaptcha", action="LOGIN_EVISA",
                                                        website_url=f"{BASE_URL}/VistosOnline/Authentication.jsp?language={self.language}", proxy=self.proxy)
                        if captcha_token:
                            break
                if not captcha_token:
                    logger.error("[Step 2] Could not re-solve CAPTCHA")
                    self._report('captcha_failed', 'login re-solve', stage_for('login'))
                    return False
                captcha_solved_at = time.time()
                login_data['captchaResponse'] = captcha_token

            try:
                resp = self.session.post(login_url, data=login_data, headers=headers)
                break
            except (httpx.ReadTimeout, curl_exceptions.ReadTimeout, curl_exceptions.Timeout):
                logger.warning(f"[Step 2] Login POST timed out (attempt {login_try + 1}/3)")
                if login_try < 2:
                    await asyncio.sleep(3)
                continue
            except (httpx.ConnectError, httpx.ProxyError,
                     curl_exceptions.ConnectionError, curl_exceptions.ProxyError,
                     curl_exceptions.CurlError) as e:
                logger.warning(f"[Step 2] Connection error: {e}")
                handle_proxy_exception(self, e, 'Step 2')
                self._report('login_failed', f'connect error: {type(e).__name__}', stage_for('login'))
                return False

        if resp is None:
            logger.error("[Step 2] Login POST timed out after 3 attempts")
            self._report('login_failed', 'timeout after 3 attempts', stage_for('login'))
            return False

        # Bot-detection deny check BEFORE status/JSON handling. A BD redirect can land on a
        # 200 page, in which case the JSON parse below fails and the run is misfiled as
        # "non-json response" rather than as a block — which is how a fingerprint problem
        # would masquerade as a credential problem in the stats.
        blocked = block_reason(resp)
        if blocked:
            # NOT a proxy verdict — do not report proxy health here. The origin serves the
            # same BD_problm.jsp 403 to an unproxied request from a clean machine, so it is a
            # judgement on request shape (fingerprint/headers/session), not on the exit IP.
            # report_proxy_health's own contract forbids calling it for a site rejection, and
            # Proxy.healthy is a permanent latch only a human can clear — so doing it here was
            # irreversibly retiring innocent rows on every WAF mood swing.
            # The proxy is not at fault, but it IS spent: every observed bd-block is followed
            # by the WAF dropping that exit IP's TLS handshakes outright, so the next attempt
            # down the same proxy fails on connect and burns a retry proving it. Retire it from
            # the local pool so the retry starts from a clean IP.
            logger.error(f"[Step 2] Bot-detection block ({blocked}) — retiring this exit IP")
            pid = (self.proxy or {}).get('id', '')
            if pid.startswith('file:'):
                _FILE_BENCH[pid] = time.time()
                logger.warning(f"[Proxy] {pid} benched after bd-block "
                               f"for {PROXY_BENCH_SECONDS:.0f}s")
            self._report('login_failed', f'bd-block: {blocked}'[:120], stage_for('login'))
            return False

        if resp.status_code != 200:
            logger.error(f"[Step 2] Login HTTP error: {resp.status_code}")
            self._report('login_failed', f'http {resp.status_code}', stage_for('login'))
            return False

        try:
            result = resp.json()
        except Exception:
            logger.error(f"[Step 2] Login response not JSON: {resp.text[:200]}")
            self._report('login_failed', 'non-json response', stage_for('login'))
            return False

        logger.debug(f"[Step 2] Full login response: {result}")
        if result.get('type') == 'success':
            logger.info(f"[Step 2] Login successful! {result.get('description', '')}")
            logger.debug(f"[Step 2] Session cookies after login: {dict(self.session.cookies)}")
            self._report_proxy_health(True)
            self._report('login_success', result.get('description', ''), stage_for('login'))
            await self._human_delay(1.0, 2.0)
            # PATCH (HAR evidence): the /login XHR alone does NOT promote the session into
            # authenticated /VistosOnline/* scope. Fire the nav-style POST the real browser
            # does, then follow its 302 — without this, everything downstream runs unauth.
            try:
                nav_headers = {
                    "Origin": BASE_URL,
                    "Referer": login_page_url,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "same-origin", "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1",
                    "User-Agent": self.session.headers.get("User-Agent", ""),
                }
                nav_resp = self.session.post(
                    f"{BASE_URL}/VistosOnline/Authentication.jsp",
                    data="lang=ENG&lang=PT", headers=nav_headers, allow_redirects=False,
                )
                logger.info(f"[Step 2] Authentication.jsp nav POST -> {nav_resp.status_code}")
                self.session.get(f"{BASE_URL}/VistosOnline/", headers={"Referer": nav_headers["Referer"]})
            except Exception as e:
                logger.warning(f"[Step 2] Authentication.jsp nav POST failed (non-fatal): {e}")
            return True
        else:
            error_type = result.get('type', 'unknown')
            error_desc = result.get('description', 'Unknown error')
            logger.error(f"[Step 2] Login failed: [{error_type}] {error_desc}")
            self._report('login_failed', f'[{error_type}] {error_desc}'[:200], stage_for('login'))
            if error_type.lower() in ('recaptchaerror', 'recaptcha_error'):
                raise CaptchaBlockedError(f"Account reCAPTCHA blocked: {error_desc}")
            if error_type.lower() == 'rgpderror':
                # The site's own AjaxSucceeded() answers this by opening the consent popup and
                # having the user tick #loginCheckbox1/2 before re-submitting. We already post
                # rgpd=Y (which is what that popup ultimately sends), so seeing this means the
                # server wants consent recorded against THIS account specifically — call it out
                # instead of burying it in the generic failure counter.
                logger.error("[Step 2] Server demands RGPD consent for this account — the posted "
                             "rgpd=Y was not accepted; this account needs its consent popup "
                             "completed once in a real browser")
                self._report('login_failed', 'rgpd-consent-required', stage_for('login'))
            if error_type.lower() == 'secblock':
                logger.error(f"[Step 2] Account security-blocked by the site: {error_desc}")
                self._report('login_failed', f'secblock: {error_desc}'[:150], stage_for('login'))
            return False

    # =========================================================================
    # STEP 2.5 (optional): Edit profile for fresh accounts
    # =========================================================================
    async def step_edit_profile(self) -> bool:
        logger.info("[Step 2.5] Attempting profile completion (fresh accounts only)...")
        profile_url = f"{BASE_URL}/VistosOnline/profile.jsp"

        try:
            profile_resp = self.session.get(profile_url, headers={'Referer': f"{BASE_URL}/VistosOnline/"})
            if profile_resp.status_code == 404:
                logger.info("[Step 2.5] profile.jsp not found — existing account, skipping")
                self._report('editprofile_skip', 'not fresh account', stage_for('editprofile'))
                return True
            if profile_resp.status_code != 200:
                logger.warning(f"[Step 2.5] profile.jsp returned {profile_resp.status_code} — skipping")
                self._report('editprofile_skip', f'http {profile_resp.status_code}', stage_for('editprofile'))
                return True
            logger.info("[Step 2.5] profile.jsp loaded — fresh account detected")

            # This page belongs to the account and is the only place the portal shows us the
            # identity it has on file. The Formulario is currently filled from a template whose
            # name and date of birth belong to nobody — paired with the account's real passport
            # number that produces "Erro na Validação da Identidade". Whatever is registered here
            # is by definition consistent with the passport, so read it and prefer it.
            try:
                ptxt = profile_resp.text
                d = os.path.join(WORKING_DIR, 'debug_html')
                os.makedirs(d, exist_ok=True)
                pth = os.path.join(d, f"profile_{self.account.get('loginUsername','?')}_{int(time.time())}.html")
                with open(pth, 'w', encoding='utf-8') as fh:
                    fh.write(ptxt)
                fields = {}
                for m in re.finditer(r'<input[^>]*name="([^"]+)"[^>]*>', ptxt):
                    tag = m.group(0)
                    if 'type="hidden"' in tag and 'captcha' in tag.lower():
                        continue
                    v = re.search(r'\svalue="([^"]*)"', tag)
                    if v and v.group(1).strip():
                        fields[m.group(1)] = v.group(1).strip()[:60]
                for m in re.finditer(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', ptxt, re.S):
                    sel = re.search(r'<option[^>]*selected[^>]*value="([^"]*)"', m.group(2))
                    if sel and sel.group(1).strip():
                        fields[m.group(1)] = sel.group(1).strip()[:60]
                self._profile_fields = fields
                logger.info(f"[Step 2.5] profile.jsp saved ({len(ptxt)}b) -> "
                            f"{os.path.relpath(pth, WORKING_DIR)} | prefilled={fields}")
            except Exception as exc:
                logger.warning(f"[Step 2.5] Could not inspect profile.jsp: {exc}")
        except Exception as e:
            logger.warning(f"[Step 2.5] Failed to load profile.jsp: {e} — continuing")
            return True

        await self._human_delay(0.5, 1.0)

        edit_captcha_token = None
        if self.capsolver_key:
            edit_captcha_token = await solve_captcha(self.capsolver_key, "capsolver", action="LOGIN_EVISA",
                                                    website_url=f"{BASE_URL}/VistosOnline/Authentication.jsp?language={self.language}", proxy=self.proxy)
        if not edit_captcha_token:
            for key in self.anticaptcha_keys:
                edit_captcha_token = await solve_captcha(key, "anticaptcha", action="LOGIN_EVISA",
                                                        website_url=f"{BASE_URL}/VistosOnline/Authentication.jsp?language={self.language}", proxy=self.proxy)
                if edit_captcha_token:
                    break

        if not edit_captcha_token:
            logger.warning("[Step 2.5] Could not solve CAPTCHA for profile edit — skipping")
            self._report('editprofile_skip', 'captcha solve failed', stage_for('editprofile'))
            return True

        # Real values when the claimed account carries them (matches the login extension's
        # traveldoc/email bridging); fall back to a non-validated placeholder otherwise
        # (the page doesn't actually validate these — v2's original finding).
        traveldoc = self.account.get('passportNo') or 'A123456'
        email = self.account.get('email') or 'placeholder@example.com'

        edit_url = f"{BASE_URL}/VistosOnline/editProfile"
        edit_data = {
            'traveldoc': traveldoc, 'email': email, 'token': '',
            'language': self.language, 'captchaResponse': edit_captcha_token,
        }
        headers = {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Origin': BASE_URL, 'Referer': profile_url,
            'Sec-Fetch-Dest': 'document', 'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-User': '?1',
            'Upgrade-Insecure-Requests': '1',
        }
        try:
            edit_resp = self.session.post(edit_url, data=edit_data, headers=headers, allow_redirects=False)
            logger.info(f"[Step 2.5] editProfile POST -> {edit_resp.status_code}")
            self._report('editprofile_done', f'http {edit_resp.status_code}', stage_for('editprofile'))
            return True
        except Exception as e:
            logger.warning(f"[Step 2.5] editProfile POST failed: {e} — continuing")
            return True

    # =========================================================================
    # STEP 3: Fill Questionnaire via API chain
    # =========================================================================
    async def step_questionnaire(self) -> bool:
        logger.info("[Step 3] Filling questionnaire...")
        quest_url = f"{BASE_URL}/VistosOnline/Questionario"
        resp = self.session.get(quest_url, headers={'Referer': f"{BASE_URL}/VistosOnline/"})
        if resp.status_code != 200:
            logger.error(f"[Step 3] Failed to load questionnaire: {resp.status_code}")
            self._report('quest_load_failed', f'http {resp.status_code}', stage_for('questionnaire'))
            return False

        await self._human_delay(1.0, 2.0)

        # Chain — HAR-verified 2026-08-20: 1 -> 21 -> 2 -> 3 -> [22 if FRA] -> 5 -> 6 -> 16.
        questions = [
            ("1", self.nationality), ("21", self.country_of_residence),
            ("2", "01"), ("3", "SCH"),
            ("5", "O"), ("6", "10"), ("16", "FAM_N"),
        ]
        if self.country_of_residence == "FRA":
            questions.insert(4, ("22", "N"))

        base_quest_url = f"{BASE_URL}/VistosOnline/QuestNextQuestion"
        pais_residencia, tipo_passaporte = "", ""
        headers = {**QUEST_HEADERS, 'Referer': quest_url}

        for id_pergunta, valor in questions:
            if id_pergunta == "21":
                pais_residencia = valor
            elif id_pergunta == "2":
                tipo_passaporte = valor

            params = {
                'id_pergunta': id_pergunta, 'valor': valor, 'lang': self.language,
                'nacionalidade': self.nationality,
                'pais_residencia': pais_residencia if id_pergunta != "1" else "",
                'tipo_passaporte': tipo_passaporte if id_pergunta not in ("1", "21") else "",
            }
            resp = self.session.get(base_quest_url, params=params, headers=headers)
            if resp.status_code != 200:
                logger.error(f"[Step 3] QuestNextQuestion failed (q{id_pergunta}): {resp.status_code}")
                self._report('quest_next_chain_step', f'id={id_pergunta} status={resp.status_code}', stage_for('questionnaire'))
                return False
            logger.info(f"[Step 3] Question {id_pergunta} = {valor}")
            logger.debug(f"[Step 3] Q{id_pergunta} params={params}")
            logger.debug(f"[Step 3] Q{id_pergunta} response ({len(resp.text)} chars): {resp.text[:300]}")
            self._report('quest_next_chain_step', f'id={id_pergunta} status={resp.status_code}', stage_for('questionnaire'))
            await self._human_delay(0.5, 1.5)

        final_text = resp.text
        if 'tipo_visto' in final_text:
            logger.info("[Step 3] Questionnaire complete — visa type determined")

            # The real markup is <input type="hidden" id="X" name="X" value="V">, so `value`
            # never follows `id` directly — an `\s+value=` pattern matches nothing and every
            # field silently takes its default. Four of those defaults happen to be right,
            # which is what hid this: only id_visto_doc differs (server says 9, default is 36),
            # so the Formulario was submitted under the wrong visa-document id with no error
            # anywhere. Allow the intervening attributes, and say which values were real.
            _fallbacks = {
                'tipo_visto': 'C',
                'tipo_visto_desc': 'VISTO DE CURTA DURAÇÃO',
                'class_visto': 'SCH',
                'cod_estada': '10',
                'id_visto_doc': '36',
            }
            _defaulted = []

            def _extract(field, default):
                m = re.search(rf'id="{field}"[^>]*?\svalue="([^"]*)"', final_text)
                if m:
                    return m.group(1)
                _defaulted.append(field)
                return default

            self._quest_hidden = {k: _extract(k, v) for k, v in _fallbacks.items()}
            logger.info(f"[Step 3] Visa determined by questionnaire: {self._quest_hidden}")
            if _defaulted:
                # Not fatal — the server may legitimately omit a field — but it must never be
                # invisible again: these values decide which visa product the form applies for.
                logger.warning(f"[Step 3] Could NOT extract from the questionnaire response, "
                               f"using defaults for: {_defaulted}")
            self._report('quest_next_chain_done', f'ok=true n={len(questions)}', stage_for('questionnaire'))
        else:
            logger.warning("[Step 3] Could not find visa type in response")
            self._quest_hidden = {'tipo_visto': 'C', 'class_visto': 'SCH', 'cod_estada': '10', 'id_visto_doc': '36'}
            self._report('quest_next_chain_done', f'ok=false n={len(questions)}', stage_for('questionnaire'))

        return True

    # =========================================================================
    # STEP 4: Submit questionnaire form -> navigate to Formulario
    # =========================================================================
    # =========================================================================
    # FINGERPRINT CHECK — getTables anti-bot validation (single channel).
    # Per July 2026 HAR reanalysis, /ch/v + /ch/bd.js were removed server-side; the
    # August 2026 HAR (freshest capture) confirms getTables is the sole surviving
    # channel, firing once per page navigation with a fresh random path + Salted__
    # AES-shaped payload. This construction is an explicit BEST-EFFORT MIMICRY (not a
    # real AES implementation of bs_datefuncs.js's fingerprinting) — treat the
    # presence/shape/timing as the load-bearing part, not the ciphertext content.
    # =========================================================================
    def _do_fingerprint_check(self, page_html: str, page_url: str):
        # The payload below is a fabrication, and comparing it to a real one from a session that
        # DID receive slots shows how far off it is:
        #     real : keys {t, fgp, fgpd}, t = 44 chars decoding to a valid 32-byte "Salted__"
        #            blob, fgp = 11,096 chars (the actual AES-encrypted browser fingerprint)
        #     ours : keys {t, fgp},       t = 22 chars that do not even base64-decode,
        #            fgp = 22 chars, fgpd missing entirely
        # This is the site's bot-detection channel and we fire it immediately before /slots. The
        # server answers 200 either way, so it looked harmless — but an obviously synthetic
        # fingerprint is worse than none, and a soft deny (200 with an empty calendar) is exactly
        # what a failed score would look like. Off by default; FAKE_FINGERPRINT=1 restores it.
        if not FAKE_FINGERPRINT:
            logger.debug("[FP] Skipping the fabricated getTables beacon (FAKE_FINGERPRINT=0)")
            return

        try:
            import hashlib, base64, secrets as _secrets

            url_seed = _secrets.token_hex(16)
            url_hash = hashlib.md5(f"{page_url}{url_seed}".encode()).hexdigest()
            tables_url = f"{BASE_URL}/VistosOnline/js/jquery.getTables/{url_hash}"

            # Read the jar directly rather than dict(self.session.cookies): the dict view
            # raises CookieConflict when two entries share a name across different
            # (domain, path) keys, and this whole function is wrapped in a bare except that
            # only logs — so any such conflict silently skips the beacon entirely. Sorting
            # the (name, value) pairs keeps the digest stable regardless of jar ordering.
            session_cookies = {}
            for _ck in self.session.cookies.jar:
                session_cookies.setdefault(_ck.name, _ck.value)
            cookie_str = json.dumps(session_cookies, sort_keys=True)
            fgp_seed = hashlib.sha256(cookie_str.encode()).digest()[:8]
            fgp_payload = base64.b64encode(fgp_seed).decode()
            t_seed = hashlib.sha256(f"{page_url}{url_seed}".encode()).digest()[:8]
            t_payload = base64.b64encode(t_seed).decode()

            tables_payload = {'t': f"U2FsdGVkX1{t_payload}", 'fgp': f"U2FsdGVkX1{fgp_payload}"}

            logger.debug(f"[FP] POST {url_hash} (getTables validation)")
            tables_resp = self.session.post(
                tables_url, json=tables_payload,
                headers={
                    'Accept': '*/*', 'Content-Type': 'application/json', 'Referer': page_url,
                    'Sec-Fetch-Dest': 'empty', 'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Site': 'same-origin',
                },
            )
            if tables_resp.status_code == 200:
                logger.debug(f"[FP] getTables success ({tables_resp.status_code})")
            else:
                logger.warning(f"[FP] getTables returned {tables_resp.status_code}")
        except Exception as e:
            logger.warning(f"[FP] Error: {e}")

    # =========================================================================
    # STEP 5: Fill and submit visa application form (multipart/form-data)
    # =========================================================================
    async def step_fill_formulario(self) -> bool:
        """Formulario -> ScheduleController, real-browser leg (see module docstring for why:
        a pure-HTTP submission here always gets redirected to BD_problm.jsp). Drives the same
        Questionario->Formulario->ScheduleController sequence a real user's browser does,
        inside a real (headless) Chromium tab handed this session's own cookies, then syncs
        the resulting session state back into self.session for the httpx-based steps after."""
        logger.info("[Step 4/5] Formulario (real-browser leg)...")

        quest_submit_fields = {
            'lang': self.language, 'nacionalidade': self.nationality,
            'pais_residencia': self.country_of_residence, 'tipo_passaporte': '01',
            'copia_pedido': 'null',
            'cb_pais_residencia': self.country_of_residence, 'cb_tipo_passaporte': '01',
            'cb_qt_dias': 'SCH', 'cb_trab_sazonal': 'O', 'cb_motivo_estada_sch': '10',
            'cb_viaja_reune_turismo': 'FAM_N',
            'tipo_visto': self._quest_hidden.get('tipo_visto', 'C'),
            'tipo_visto_desc': self._quest_hidden.get('tipo_visto_desc', 'VISTO DE CURTA DURAÇÃO'),
            'class_visto': self._quest_hidden.get('class_visto', 'SCH'),
            'cod_estada': self._quest_hidden.get('cod_estada', '10'),
            'id_visto_doc': self._quest_hidden.get('id_visto_doc', '36'),
        }
        if self.country_of_residence == "FRA":
            quest_submit_fields['cb_question_22'] = 'N'

        mapping_path = os.path.join(WORKING_DIR, "second_form_mapping_2.json")
        if not os.path.exists(mapping_path):
            logger.error("[Step 4/5] second_form_mapping_2.json not found")
            self._report('formulario_submit', 'mapping file missing', stage_for('formulario'))
            return False
        with open(mapping_path, 'r', encoding='utf-8') as f:
            mapping_data = json.load(f)

        file_field_names = {"foto", "file1", "file2", "file3", "file4"}
        form_fields = {}
        for key, cfg in mapping_data.items():
            if not isinstance(cfg, list) or len(cfg) < 2:
                continue
            name, value = cfg[0], cfg[1]
            if name in file_field_names:
                continue
            form_fields[name] = str(value) if value is not None else ""
        form_fields['f0sf1'] = self.consular_post_id
        form_fields['f30'] = self.intended_date.replace('-', '/')
        try:
            date_parts = self.intended_date.replace('-', '/').split('/')
            arrival = datetime(int(date_parts[0]), int(date_parts[1]), int(date_parts[2]))
            departure = arrival + timedelta(days=self.duration_of_stay)
            form_fields['f31'] = departure.strftime('%Y/%m/%d')
        except Exception:
            pass
        # Identity comes from the claimed account, not from the template. second_form_mapping_2.json
        # hardcodes one applicant (passport TRV187956, surname MARTINS), so every account was
        # submitting the SAME travel document — and this site keys an applicant on the document,
        # not on the login. Once any application existed under that number, every later submission
        # was refused with "Este requerente já tem um pedido registado com agendamento futuro",
        # the server re-rendered the form at a .../ScheduleController URL, and /slots then had no
        # application to answer for. The dashboard has carried a real per-account passportNo all
        # along; it was simply never read.
        identity = {
            'f14': self.account.get('passportNo'),   # passport number — the applicant's identity
            'f1': self.account.get('lastName'),      # surname
            'f3': self.account.get('firstName'),     # first name
            'f0': self.account.get('email'),         # applicant email
        }
        used, defaulted = {}, []
        for field, value in identity.items():
            if value:
                form_fields[field] = str(value)
                used[field] = str(value)
            else:
                defaulted.append(field)
        logger.info(f"[Step 4/5] Applicant identity from account: {used}"
                    + (f" | template fallback for {defaulted}" if defaulted else ""))
        if not self.account.get('passportNo'):
            logger.error("[Step 4/5] Account has no passportNo — falling back to the shared template "
                         "document, which this site will reject as an existing applicant.")

        form_fields['pais_residencia'] = self.country_of_residence
        if RESIDENCE_ADDRESS:
            # Keep the address consistent with pais_residencia. A form declaring residence in
            # France while giving a Cape Verde address is the kind of internal contradiction a
            # reviewer (or a validator) notices.
            form_fields['f45'] = RESIDENCE_ADDRESS
        form_fields['tipo_visto'] = self._quest_hidden.get('tipo_visto', 'C')
        form_fields['tipo_visto_desc'] = self._quest_hidden.get('tipo_visto_desc', 'VISTO DE CURTA DURAÇÃO')
        form_fields['class_visto'] = self._quest_hidden.get('class_visto', 'SCH')
        form_fields['cod_estada'] = self._quest_hidden.get('cod_estada', '10')
        form_fields['id_visto_doc'] = self._quest_hidden.get('id_visto_doc', '36')
        form_fields['tipo_passaporte'] = '01'
        form_fields['nacionalidade'] = self.nationality

        try:
            browser = await _get_shared_browser()
        except Exception as e:
            logger.error(f"[Step 4/5] Failed to launch Playwright browser: {e}")
            self._report('formulario_submit', json.dumps({'ok': False, 'reason': 'playwright-launch-failed'}), stage_for('formulario'))
            return False

        proxy_cfg = None
        if self.proxy and self.proxy.get('ip') and self.proxy.get('port'):
            proxy_cfg = {"server": f"http://{self.proxy['ip']}:{self.proxy['port']}"}
            if self.proxy.get('username'):
                proxy_cfg["username"] = self.proxy['username']
                proxy_cfg["password"] = self.proxy.get('password') or ''

        # No user_agent override. Playwright's option rewrites the UA string but does NOT
        # regenerate the Sec-CH-UA client hints, so claiming Chrome/139 out of a Chromium 147
        # binary shipped every request with a UA and a set of client hints that disagree — a
        # contradiction no real browser produces, to the one origin whose risk assessment decides
        # whether /slots answers. Let Chromium describe itself. Same reason for viewport=None:
        # a pinned 1366x768 that does not match the real window is another free signal.
        context = await browser.new_context(
            locale="en-US", viewport=None, proxy=proxy_cfg,
        )
        try:
            await stealth_async(context)

            cookie_objs = [
                {"name": name, "value": value, "domain": "pedidodevistos.mne.gov.pt", "path": "/"}
                for name, value in dict(self.session.cookies).items()
            ]
            await context.add_cookies(cookie_objs)

            page = await context.new_page()

            # Record every request this page makes. The successful capture fired a genuine
            # getTables beacon from Schedule.jsp carrying an 11,096-char fingerprint; our session
            # fires none at all, and no capture we hold contains the script that builds it. So the
            # question is whether the site even serves us that machinery — this answers it.
            self._page_requests = []

            def _log_req(req):
                try:
                    if 'pedidodevistos' in req.url or 'getTables' in req.url:
                        self._page_requests.append((req.method, req.url.split('/VistosOnline')[-1][:110]))
                except Exception:
                    pass

            page.on('request', _log_req)

            # The request firing is not the signal that matters — the server's ANSWER is. A
            # partner implementation built from a real successful-booking HAR treats
            # {"success":"true"} coming back from getTables as the WAF handshake that has to
            # land before /slots or /SubmeterVistoCriaPDF will return anything real. Watch the
            # response, not just the request.
            self._tables_ok = False

            async def _log_getTables_resp(resp):
                if 'getTables' not in resp.url:
                    return
                # Read the body defensively but LOUDLY: a swallowed failure here is
                # indistinguishable from a rejected handshake, and that difference is the whole
                # question. Report the status even when the body cannot be retrieved.
                try:
                    body = await resp.text()
                except Exception as exc:
                    logger.warning(f"[Step 4/5] getTables answered {resp.status} but its body was "
                                   f"unreadable ({type(exc).__name__}: {exc}) — success UNKNOWN")
                    return
                ok = '"success":"true"' in body or '"success": "true"' in body
                if ok:
                    self._tables_ok = True
                logger.info(f"[Step 4/5] getTables answered {resp.status} success={ok} "
                            f"len={len(body)} body={body[:200]!r}")

            page.on('response', _log_getTables_resp)
            alert_count = 0
            dialog_complete = asyncio.Event()

            async def handle_dialog(dialog):
                nonlocal alert_count
                alert_count += 1
                logger.debug(f"[Step 4/5] Dialog #{alert_count}: {dialog.type} {dialog.message[:150]!r}")
                await dialog.accept()
                if alert_count >= 2:
                    dialog_complete.set()

            page.on("dialog", handle_dialog)

            quest_page_url = f"{BASE_URL}/VistosOnline/Questionario"

            # A cold GET straight to Formulario (with or without a Referer) always lands on a
            # different "voltar()" app-state page — the real flow never GETs it. Questionario's
            # own page does a native <form method=POST> submit (confirmed via this HAR's own
            # GA4 telemetry: en=form_submit, ep.form_id=questForm, ep.form_destination=.../
            # Formulario). Replicate for real: load the real Questionario page (a normal GET),
            # then inject+submit a hidden form — a genuine top-level POST navigation with
            # correct Referer/Origin set by the browser itself, and (critically) this is what
            # lets bs_datefuncs.js fire a REAL getTables fingerprint the server actually trusts.
            await page.goto(quest_page_url, wait_until="domcontentloaded", timeout=45000)

            # Answer the questionnaire IN THE PAGE, by selecting the real dropdowns, instead of
            # injecting a synthetic hidden form. Each <select> carries onchange="goNext(N,value)",
            # so selecting fires the site's OWN /QuestNextQuestion XHR from this browser session
            # and the next question is appended to the DOM. Only once the last one is answered
            # does the page reveal #btnContinue and set the tipo_visto hidden fields itself; then
            # its own questForm posts to Formulario.
            #
            # Why this matters: the previous hidden-form injection meant the browser that goes on
            # to submit the Formulario and land on Schedule.jsp had never answered a single
            # question in-page — the chain had been walked earlier over a different HTTP client.
            # The partner's working implementation drives the real dropdowns, and his /slots
            # returns a real 309-date array where ours returned the {"data":{}} wrapper.
            # Q1 (nationality) is auto-filled and disabled by the page itself — never touch it.
            # The question chain is NOT a fixed sequence — it branches on the residence
            # country. Under FRA it runs 21->2->3->22->5->6->16; under BEL questions 5 and 6
            # never appear at all. So drive it adaptively: after each answer, look at which
            # cb_question_* select the page has actually revealed and answer that one, until
            # the page itself decides the questionnaire is done (#btnContinue appears).
            answer_for = {
                "21": self.country_of_residence,
                "2": "01",          # passport type
                "3": "SCH",         # Schengen short stay
                "22": "N",          # residence card / national visa (FRA branch)
                "5": "O",           # travel purpose
                "6": "10",          # length of stay
                "16": "FAM_N",
                "17": "FAM_N",
            }

            answered: List[str] = []
            for _ in range(14):
                if await page.locator("#btnContinue").first.is_visible():
                    break
                pending = await page.evaluate(
                    """
                    (done) => Array.from(document.querySelectorAll('select[id^="cb_question_"]'))
                        .filter(el => !el.disabled && el.offsetParent !== null)
                        .map(el => el.id.replace('cb_question_',''))
                        .filter(id => !done.includes(id))
                    """,
                    answered,
                )
                if not pending:
                    await asyncio.sleep(1.5)
                    continue
                qid = pending[0]
                value = answer_for.get(qid)
                if value is None:
                    opts = await page.evaluate(
                        "(id) => Array.from(document.getElementById('cb_question_'+id).options)"
                        ".map(o => o.value + '=' + (o.text||'').trim().slice(0,40))", qid)
                    logger.error(f"[Step 4/5] Unknown question Q{qid} for residence "
                                 f"{self.country_of_residence} — options: {opts[:20]}")
                    self._report('formulario_submit', json.dumps(
                        {'ok': False, 'reason': 'unknown-question', 'qid': qid}), stage_for('formulario'))
                    return False
                try:
                    await page.locator(f"select#cb_question_{qid}").first.select_option(value)
                except Exception as exc:
                    logger.error(f"[Step 4/5] Could not answer Q{qid}={value}: "
                                 f"{type(exc).__name__}: {str(exc)[:120]}")
                    self._report('formulario_submit', json.dumps(
                        {'ok': False, 'reason': 'question-not-answerable', 'qid': qid}), stage_for('formulario'))
                    return False
                answered.append(qid)
                logger.info(f"[Step 4/5] Questionario UI: Q{qid} = {value}")
                await self._human_delay(1.5, 3.0)

            logger.info(f"[Step 4/5] Questionario UI answered in-page: {answered}")

            # The page only reveals #btnContinue once it has determined tipo_visto itself.
            try:
                await page.locator("#btnContinue").first.wait_for(state="visible", timeout=30000)
            except Exception:
                logger.error("[Step 4/5] #btnContinue never became visible — the page did not "
                             "accept the questionnaire as complete")
                self._report('formulario_submit', json.dumps(
                    {'ok': False, 'reason': 'btncontinue-not-visible'}), stage_for('formulario'))
                return False

            # By now the page also holds the visa product the answers resolved to. Read it from
            # the DOM. These five values used to come from step_questionnaire()'s HTTP walk of
            # the same chain; now that the browser is the only client that answers it, the
            # browser is also where the answer has to be read — otherwise the form is submitted
            # against defaults that may not match what was actually asked. Failing to read them
            # is not fatal (the defaults still apply), so it must not abort the run.
            try:
                dom_hidden = await page.evaluate(
                    """() => {
                        const out = {};
                        for (const k of ['tipo_visto','tipo_visto_desc','class_visto',
                                         'cod_estada','id_visto_doc']) {
                            const el = document.querySelector('[name="'+k+'"]') ||
                                       document.getElementById(k);
                            if (el && el.value) out[k] = el.value;
                        }
                        return out;
                    }"""
                )
                if dom_hidden:
                    self._quest_hidden.update(dom_hidden)
                    for k, v in dom_hidden.items():
                        form_fields[k] = v
                    logger.info(f"[Step 4/5] Visa product read from Questionario DOM: {dom_hidden}")
                else:
                    logger.warning("[Step 4/5] Questionario exposed no visa-type fields — "
                                   f"submitting with defaults {self._quest_hidden}")
            except Exception as exc:
                logger.warning(f"[Step 4/5] Could not read the visa product from the DOM "
                               f"({type(exc).__name__}) — using defaults {self._quest_hidden}")

            async with page.expect_navigation(wait_until="domcontentloaded", timeout=45000):
                await page.locator("#btnContinue").first.click()

            if "Formulario" not in page.url:
                logger.error(f"[Step 4/5] Unexpected page after Questionario submit: {page.url}")
                self._report('formulario_submit', json.dumps({'ok': False, 'reason': 'unexpected-page-after-quest-submit'}), stage_for('formulario'))
                return False

            try:
                await page.wait_for_selector("#vistoForm", timeout=15000)
            except Exception:
                logger.error(f"[Step 4/5] #vistoForm not found. Title: {await page.title()}")
                self._report('formulario_submit', json.dumps({'ok': False, 'reason': 'vistoForm-not-found'}), stage_for('formulario'))
                return False

            # Capture the form exactly as the server renders it, BEFORE we touch anything. Every
            # dump we have is post-fill, so we have never been able to tell which values the
            # portal supplies for this applicant and which it expects from us — which is the whole
            # question behind "Erro na Validação da Identidade".
            try:
                pristine = await page.content()
                d = os.path.join(WORKING_DIR, 'debug_html')
                os.makedirs(d, exist_ok=True)
                pth = os.path.join(d, f"formulario_pristine_{self.account.get('loginUsername','?')}_{int(time.time())}.html")
                with open(pth, 'w', encoding='utf-8') as fh:
                    fh.write(pristine)
                prefill = await page.evaluate(
                    r"""
                    () => {
                        const out = {};
                        document.querySelectorAll('#vistoForm [name]').forEach(el => {
                            if (el.type === 'file' || el.type === 'button' || el.type === 'submit') return;
                            let v = '';
                            if (el.tagName === 'SELECT') {
                                const o = el.options[el.selectedIndex];
                                v = o ? o.value : '';
                            } else if (el.type === 'checkbox' || el.type === 'radio') {
                                if (!el.checked) return;
                                v = el.value;
                            } else { v = el.value; }
                            if (v !== '' && v !== '-1') {
                                out[el.name] = {v: String(v).slice(0, 40),
                                                ro: !!(el.readOnly || el.disabled),
                                                req: (el.className || '').includes('obrigatorio')};
                            }
                        });
                        return out;
                    }
                    """
                )
                logger.info(f"[Step 4/5] Pristine Formulario saved ({len(pristine)}b) -> "
                            f"{os.path.relpath(pth, WORKING_DIR)}")
                logger.info(f"[Step 4/5] Server pre-filled {len(prefill)} fields: "
                            f"{ {k: v['v'] for k, v in prefill.items()} }")
                self._prefill = prefill
            except Exception as exc:
                logger.warning(f"[Step 4/5] Could not capture the pristine form: {exc}")

            # Derive the writable identity fields from what the server already knows about this
            # applicant, instead of shipping the template's constants. The portal fills surname,
            # given names, birth date, sex and passport itself (readonly) and leaves surname-at-
            # birth, place/country of birth, original nationality and the passport issuer/issue
            # date for us — and the template answers those with another person's surname and a
            # literal "+" for place of birth. A real passport carrying that is precisely what
            # "Erro na Validação da Identidade" rejects.
            def _server(field: str) -> str:
                info = (getattr(self, '_prefill', None) or {}).get(field) or {}
                return str(info.get('v') or '')

            derived = {}
            srv_surname = _server('f1')
            if srv_surname:
                derived['f2'] = srv_surname          # apelido de solteiro: defaults to the surname
            srv_nat = _server('f7sf1') or self.nationality
            if srv_nat:
                derived['f6sf1'] = srv_nat           # país de nascimento
                derived['f8'] = srv_nat              # nacionalidade origem
                derived['f15'] = srv_nat             # emitido por
            if derived:
                form_fields.update(derived)
                logger.info(f"[Step 4/5] Identity derived from the server's own values: {derived}")

            set_result = await page.evaluate(
                """
                (fields) => {
                    const results = {set: 0, missing: [], rejected: [], serverFilled: []};
                    for (const [name, value] of Object.entries(fields)) {
                        const el = document.querySelector(`[name="${name}"]`) || document.getElementById(name);
                        if (!el) { results.missing.push(name); continue; }
                        // Server-authoritative fields. The portal pre-fills the applicant's
                        // identity here from the account's own registration — surname, given
                        // name, date of birth, sex, passport, email — and marks them readonly.
                        // Overwriting them with the template's placeholder identity is what
                        // produced "Erro na Validação da Identidade": a real passport number
                        // paired with a name and birth date belonging to nobody. Leave them.
                        if (el.readOnly || el.disabled || el.getAttribute('aria-disabled') === 'true') {
                            results.serverFilled.push(name + '=' + String(el.value).slice(0, 40));
                            continue;
                        }
                        el.value = value;
                        // A <select> silently ignores a value matching no <option> and keeps
                        // whatever was selected before. The consular-post select f0sf1 ships
                        // with an option already selected, so an unnoticed rejection here means
                        // applying to a DIFFERENT consulate while every counter still reads
                        // success. Read the value back; only count it if it actually stuck.
                        if (el.value !== String(value)) {
                            results.rejected.push({
                                name: name,
                                wanted: String(value),
                                got: el.value,
                                options: el.options ? Array.from(el.options).map(function (o) {
                                    return o.value + '=' + (o.text || '').trim().slice(0, 40);
                                }).slice(0, 40) : null,
                            });
                            continue;
                        }
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        if (el.onchange) { try { el.onchange(); } catch(e) {} }
                        results.set++;
                    }
                    return results;
                }
                """,
                form_fields,
            )
            server_filled = set_result.get('serverFilled') or []
            if server_filled:
                logger.info(f"[Step 4/5] Left {len(server_filled)} server-filled identity fields "
                            f"untouched: {server_filled}")
            rejected = set_result.get('rejected') or []
            logger.info(f"[Step 4/5] Filled {set_result['set']}/{len(form_fields)} fields "
                        f"({len(set_result['missing'])} missing, {len(rejected)} rejected)")
            try:
                shot_dir = os.path.join(WORKING_DIR, 'debug_html')
                os.makedirs(shot_dir, exist_ok=True)
                shot_path = os.path.join(shot_dir, f"formulario_filled_{self.account.get('loginUsername','?')}_{int(time.time())}.png")
                await page.screenshot(path=shot_path, full_page=True)
                logger.info(f"[Step 4/5] Filled-form screenshot -> {os.path.relpath(shot_path, WORKING_DIR)}")
            except Exception as shot_exc:
                logger.warning(f"[Step 4/5] Could not screenshot filled form: {shot_exc}")
            for r in rejected:
                logger.warning(f"[Step 4/5] Field {r['name']} REJECTED the value: "
                               f"wanted={r['wanted']!r} got={r['got']!r}")
                if r.get('options'):
                    logger.warning(f"[Step 4/5]   available options: {r['options']}")
            # The consular post is the one field whose silent rejection changes WHICH consulate
            # the application goes to, so it is the one worth refusing to continue on. Everything
            # else is logged and left to the server to validate.
            posto_rejected = next((r for r in rejected if r['name'] == 'f0sf1'), None)
            if posto_rejected:
                logger.error(f"[Step 4/5] Consular post {posto_rejected['wanted']} is NOT offered to this "
                             f"applicant — the form kept {posto_rejected['got']!r}. Aborting rather than "
                             f"silently applying to the wrong consulate.")
                self._report('formulario_submit', json.dumps({
                    'ok': False, 'reason': 'posto-not-offered',
                    'wanted': posto_rejected['wanted'], 'got': posto_rejected['got'],
                }), stage_for('formulario'))
                return False
            self._report('formulario_submit', json.dumps({'fields': set_result['set']}), stage_for('formulario'))

            for fname, path in [
                ("foto", SAMPLE_PHOTO_PATH), ("file1", SAMPLE_DOCUMENT_PATH), ("file2", SAMPLE_DOCUMENT_PATH),
                ("file3", SAMPLE_DOCUMENT_PATH), ("file4", SAMPLE_DOCUMENT_PATH),
            ]:
                try:
                    locator = page.locator(f'input[name="{fname}"]')
                    if await locator.count() > 0:
                        await locator.set_input_files(path)
                except Exception as e:
                    logger.warning(f"[Step 4/5] Failed to set file input {fname}: {e}")

            await page.wait_for_timeout(1000)

            # Wait for the getTables fingerprint POST HERE, on the Formulario page, before
            # submitting — this is where the partner's working implementation waits for it
            # ("[Browser-Step5] À espera do fingerprint POST (getTables)" -> "detectado", then
            # "Submetendo Formulario..."). We had only ever looked for it later, on Schedule.jsp,
            # where it frequently never fires at all.
            pre_beacons = [u for m, u in getattr(self, '_page_requests', []) if 'getTables' in u]
            if not pre_beacons:
                logger.info("[Step 4/5] Waiting up to 20s for the getTables fingerprint on the "
                            "Formulario page before submitting...")
                for _ in range(40):
                    await asyncio.sleep(0.5)
                    pre_beacons = [u for m, u in getattr(self, '_page_requests', []) if 'getTables' in u]
                    if pre_beacons:
                        break
            if pre_beacons:
                logger.info(f"[Step 4/5] getTables fired on the Formulario page: {pre_beacons}")
                await asyncio.sleep(2)  # let the server record it before we submit
            else:
                logger.warning("[Step 4/5] getTables never fired on the Formulario page either")

            btn = page.locator("#btnSubmit")
            if await btn.count() == 0:
                logger.error("[Step 4/5] #btnSubmit not found")
                self._report('formulario_submit', json.dumps({'ok': False, 'reason': 'btnSubmit-not-found'}), stage_for('formulario'))
                return False

            await btn.click(delay=150)
            try:
                await asyncio.wait_for(dialog_complete.wait(), timeout=8.0)
            except asyncio.TimeoutError:
                logger.warning(f"[Step 4/5] Only {alert_count}/2 confirm dialogs seen")

            try:
                await page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass
            await page.wait_for_timeout(1500)

            final_url = page.url
            html = await page.content()
            posto_match = re.search(r'posto_id=(\d+)', final_url)
            self._posto_id = posto_match.group(1) if posto_match else self.consular_post_id

            # Judge by CONTENT, not by URL. When the server rejects the application it re-renders
            # the Formulario and the response URL is still .../ScheduleController?posto_id=N — so a
            # substring test on the URL passes while we are actually looking at the form again,
            # complete with its error box. That misread is why every run reported "Reached
            # Schedule" and then got {"data":{}} from /slots: there was no application to schedule.
            # A genuine Schedule.jsp always carries the captcha widget and the calendar container.
            is_real_schedule = ('calendarDiv' in html) and ('captchaDiv' in html)
            already_booked = 'já tem um pedido registado' in html or 'ja tem um pedido registado' in html

            def _dump(tag: str) -> str:
                """Persist whatever we landed on. Not having an artifact for the failure paths is
                what let a rejected application read as success for months."""
                try:
                    d = os.path.join(WORKING_DIR, 'debug_html')
                    os.makedirs(d, exist_ok=True)
                    path = os.path.join(
                        d, f"{tag}_{self._posto_id}_{self.account.get('loginUsername','?')}_{int(time.time())}.html")
                    with open(path, 'w', encoding='utf-8') as fh:
                        fh.write(html)
                    return os.path.relpath(path, WORKING_DIR)
                except Exception as exc:
                    return f"(save failed: {exc})"

            if already_booked:
                logger.error("[Step 4/5] REJECTED — this applicant already holds a registered "
                             "application with a future appointment. The site's own message: "
                             "'pode alterar a data do agendamento do pedido anterior'. A new "
                             "application cannot be created for this account.")
                logger.error(f"[Step 4/5] Page saved -> {_dump('rejected_alreadybooked')}")
                self._report('formulario_submit', json.dumps({'ok': False, 'reason': 'already-has-appointment'}),
                             stage_for('formulario'))
                raise AlreadyBookedError(self.account.get('loginUsername', '?'))

            if not is_real_schedule:
                missing = [m for m in ('calendarDiv', 'captchaDiv') if m not in html]
                logger.error(f"[Step 4/5] Landed on {final_url} but this is NOT a Schedule page "
                             f"(missing {missing}, {len(html)}b). The application was not accepted. "
                             f"Page saved -> {_dump('notschedule')}")
                # Surface whatever the page says, so the reason is in the log even before anyone
                # opens the artifact.
                for _m in re.finditer(r'class="texto_erro"[^>]*>([^<]{10,300})', html):
                    logger.error(f"[Step 4/5]   site says: {_m.group(1).strip()[:250]}")
                self._report('formulario_submit', json.dumps({'ok': False, 'reason': 'not-schedule-page',
                                                              'missing': missing, 'bytes': len(html)}),
                             stage_for('formulario'))
                return False

            if "schedule" not in final_url.lower():
                logger.error(f"[Step 4/5] Did not reach Schedule page. final_url={final_url}")
                debug_dir = os.path.join(WORKING_DIR, 'debug_html')
                os.makedirs(debug_dir, exist_ok=True)
                debug_path = os.path.join(debug_dir, f'formulario_pw_fail_{int(time.time())}.html')
                with open(debug_path, 'w', encoding='utf-8') as f:
                    f.write(f"URL: {final_url}\n\n{html}")
                self._report('formulario_submit', json.dumps({'ok': False, 'reason': 'schedule-not-reached', 'url': final_url}), stage_for('formulario'))
                return False

            self._schedule_html = html

            # Rebuild the httpx jar from Playwright's, preserving each cookie's real domain
            # and path. Do NOT use a bare cookies.set(name, value) loop: httpx defaults that
            # to domain='' path='/', http.cookiejar keys on (domain, path, name), and the
            # server issued Vistos_sid for (pedidodevistos.mne.gov.pt, /VistosOnline) — so a
            # bare set ADDS a second entry instead of updating. Every later request then sends
            # `Vistos_sid=…; Vistos_sid=…`, and dict(self.session.cookies) raises CookieConflict
            # inside _do_fingerprint_check, silently skipping the getTables beacon for the slots
            # step (confirmed 2026-09-03/04 in logs/users/{mariajes13316,ariela51341,diego91129}
            # .log, each immediately before a slots call recorded as "no slots available").
            pw_cookies = await context.cookies()
            self.session.cookies.clear()
            for c in pw_cookies:
                self.session.cookies.set(
                    c['name'], c['value'],
                    domain=(c.get('domain') or '').lstrip('.'),
                    path=c.get('path') or '/',
                )
            jar_names = [ck.name for ck in self.session.cookies.jar]
            if len(jar_names) != len(set(jar_names)):
                logger.error(f"[Step 4/5] Duplicate cookies after handoff — getTables will break: {jar_names}")
            logger.debug(f"[Step 4/5] Cookie jar rebuilt from Playwright: {jar_names}")

            # Log the URL itself: self._posto_id falls back to the CONFIGURED id when the URL
            # carries no posto_id, so it can echo what we asked for rather than what we got.
            # The URL is the only server-side evidence of which post we actually landed on.
            logger.info(f"[Step 4/5] Reached Schedule page via real browser. Posto ID: {self._posto_id} "
                        f"({'from URL' if posto_match else 'FALLBACK — url had no posto_id'}) | url={final_url}")

            # Save the page we actually landed on, every time — not only on failure. The failure
            # branch below already dumps, which means the one case we most need to inspect (a
            # landing that LOOKS fine but whose /slots answers with a wrapper the site's own JS
            # cannot parse) is the only case that leaves no artifact behind.
            try:
                dump_dir = os.path.join(WORKING_DIR, 'debug_html')
                os.makedirs(dump_dir, exist_ok=True)
                dump_path = os.path.join(
                    dump_dir, f"schedule_{self._posto_id}_{self.account.get('loginUsername','?')}_{int(time.time())}.html")
                with open(dump_path, 'w', encoding='utf-8') as fh:
                    fh.write(html)
                markers = {m: (m in html) for m in
                           ('captchaDiv', 'calendarDiv', 'vistoForm', 'inputPeriodos',
                            'cmbPeriodo', 'getSlots', 'data-sitekey')}
                logger.info(f"[Step 4/5] Schedule page saved ({len(html)}b) -> "
                            f"{os.path.relpath(dump_path, WORKING_DIR)} | markers={markers}")
            except Exception as exc:
                logger.warning(f"[Step 4/5] Could not save Schedule page: {exc}")
            # Hand the live page to the slots step instead of discarding it. A pure-HTTP /slots
            # is answered by this origin's edge tier, not by the application: it comes back with a
            # DOUBLED 'application/octet-stream, application/json' Content-Type and none of the
            # security headers (CSP/HSTS/Referrer-Policy/X-Content-Type-Options/X-XSS-Protection)
            # that every genuine app response carries, wrapping a {"data":{}} body that the site's
            # own handler could not even parse — it does data.find(...) on an array. This is the
            # same class of block that forced the Formulario leg into a real browser, and /slots
            # was the last valuable request still leaving from outside one.
            self._pw_context = context
            self._pw_page = page
            reqs = getattr(self, '_page_requests', [])
            scripts = [u for m, u in reqs if '.js' in u]
            beacons = [u for m, u in reqs if 'getTables' in u]
            logger.info(f"[Step 4/5] Page made {len(reqs)} same-origin requests, {len(scripts)} scripts, "
                        f"{len(beacons)} getTables beacons")
            logger.info(f"[Step 4/5] Scripts the site served us: {sorted(set(scripts))}")

            # A known-working reference implementation (visa_api_bot.py, 2026-09) documents this
            # exact failure mode: solving the slots CAPTCHA immediately on arrival, without waiting
            # for Schedule.jsp's own getTables fingerprint POST to actually fire, means the site has
            # no fresh fingerprint on file for this session when /slots is called — so it answers
            # with the safe {"data":{}} stub regardless of real availability, every time (confirmed
            # empirically: 0/0 real responses across curl_cffi + FAKE_FINGERPRINT=0 + 10 concurrent
            # accounts, all landing here with 0 beacons recorded). page.on('request') above has been
            # listening since before this navigation started, so if the beacon already fired it's
            # already in `reqs` — only actively wait when it hasn't.
            if not beacons:
                logger.info("[Step 4/5] No getTables beacon yet — waiting up to 15s for the real "
                            "fingerprint to fire before proceeding to slots...")
                for _ in range(30):
                    await asyncio.sleep(0.5)
                    reqs = getattr(self, '_page_requests', [])
                    beacons = [u for m, u in reqs if 'getTables' in u]
                    if beacons:
                        break

            # Landing on Schedule.jsp via the ScheduleController POST/redirect does not reliably
            # run the script that fires the beacon. The partner implementation's fix is simply to
            # navigate to Schedule.jsp again — a plain GET does load and run it. We are already
            # authenticated with a submitted application at this point, so this is no longer the
            # "cold" direct navigation the module docstring warns gets bounced to /VistosOnline/.
            # Gate on the beacon ALONE, not on _tables_ok: Playwright cannot read the getTables
            # response body over CDP ("No resource with given identifier found"), so _tables_ok
            # is never True and gating on it re-navigated on every single run — which destroyed
            # the page's execution context mid-/slots call ("Execution context was destroyed").
            if not beacons:
                schedule_url = f"{BASE_URL}/VistosOnline/Schedule.jsp?posto_id={self._posto_id}"
                logger.warning(f"[Step 4/5] getTables handshake not confirmed "
                               f"(beacons={len(beacons)} success={self._tables_ok}) — re-navigating "
                               f"{schedule_url} to force it")
                try:
                    await page.goto(schedule_url, wait_until="domcontentloaded", timeout=30000)
                    for _ in range(20):
                        await asyncio.sleep(0.5)
                        reqs = getattr(self, '_page_requests', [])
                        beacons = [u for m, u in reqs if 'getTables' in u]
                        if self._tables_ok:
                            break
                    html = await page.content()
                    self._schedule_html = html
                except Exception as exc:
                    logger.warning(f"[Step 4/5] Re-navigation to Schedule.jsp failed: {exc}")

            if self._tables_ok:
                logger.info(f"[Step 4/5] getTables WAF handshake CONFIRMED (success=true): {beacons}")
            elif beacons:
                logger.warning(f"[Step 4/5] getTables fired but never answered success=true: {beacons}")
            else:
                logger.warning("[Step 4/5] getTables beacon still did not fire even after re-navigation "
                                "— proceeding anyway, but /slots is likely to answer with the stub")
            self._report('formulario_submit', json.dumps({'ok': True, 'via': 'playwright'}), stage_for('formulario'))
            return True
        finally:
            if getattr(self, '_pw_context', None) is not context:
                await context.close()

    # =========================================================================
    # CAPTCHA PRE-SOLVER — runs in parallel with Step 5, since tokens age ~120s
    # =========================================================================
    async def _solve_captcha_for_slots(self) -> Optional[dict]:
        schedule_url = f"{BASE_URL}/VistosOnline/Schedule.jsp?posto_id={self.consular_post_id}"
        schedule_action = "SCHEDULE_EVISA"  # HAR-confirmed from Schedule.jsp's widget markup
        try:
            if self.capsolver_key:
                token = await solve_captcha(self.capsolver_key, "capsolver", action=schedule_action, website_url=schedule_url, proxy=self.proxy)
                if token:
                    return {"token": token, "solved_at": time.time()}
            for key in self.anticaptcha_keys:
                token = await solve_captcha(key, "anticaptcha", action=schedule_action, website_url=schedule_url, proxy=self.proxy)
                if token:
                    return {"token": token, "solved_at": time.time()}
            logger.warning("[Pre-solve] All CAPTCHA solvers failed")
            return None
        except asyncio.CancelledError:
            return None
        except Exception as e:
            logger.error(f"[Pre-solve] Error: {e}")
            return None

    # =========================================================================
    # STEP 6: Solve schedule CAPTCHA and get appointment slots
    # =========================================================================
    async def step_get_slots(self, pre_solved_captcha: Optional[str] = None) -> Optional[list]:
        logger.info("[Step 6] Getting appointment slots...")
        # Reset per call: the watcher reads this right after we return to decide whether an
        # empty result means "no slots right now" (keep polling) or "this session is gone"
        # (stop and let the outer retry rebuild it). Those look identical from the outside.
        self._session_dead: Optional[str] = None
        posto_id = getattr(self, '_posto_id', self.consular_post_id)
        schedule_url = f"{BASE_URL}/VistosOnline/Schedule.jsp?posto_id={posto_id}"
        schedule_action = "SCHEDULE_EVISA"

        self._report('captcha_solving', 'schedule', stage_for('slots_solving'))
        if pre_solved_captcha:
            captcha_token = pre_solved_captcha
            self._slots_token, self._slots_token_ts = captcha_token, time.time()
        else:
            # Reuse a recent token instead of buying one per poll. The site's token is good for
            # ~120s; the partner implementation renews at 100s and polls every 10s in between,
            # which is what makes aggressive polling affordable at all — a solve per poll is why
            # this loop used to have to sit at 60s and still cost a captcha every time.
            cached = getattr(self, '_slots_token', None)
            cached_age = time.time() - getattr(self, '_slots_token_ts', 0.0)
            if cached and cached_age < CAPTCHA_TTL:
                captcha_token = cached
                logger.info(f"[Step 6] Reusing schedule captcha token ({cached_age:.0f}s old, "
                            f"TTL {CAPTCHA_TTL}s)")
            else:
                captcha_token = None
                if self.capsolver_key:
                    captcha_token = await solve_captcha(self.capsolver_key, "capsolver", action=schedule_action, website_url=schedule_url, proxy=self.proxy)
                if not captcha_token:
                    for key in self.anticaptcha_keys:
                        captcha_token = await solve_captcha(key, "anticaptcha", action=schedule_action, website_url=schedule_url, proxy=self.proxy)
                        if captcha_token:
                            break
                if captcha_token:
                    self._slots_token, self._slots_token_ts = captcha_token, time.time()

        if not captcha_token:
            logger.error("[Step 6] Could not solve schedule CAPTCHA")
            self._report('captcha_failed', 'schedule', stage_for('slots_waiting'))
            return None
        self._report('captcha_solved', f'schedule len={len(captcha_token)}', stage_for('slots_solving'))
        self._prewarm('before slots POST')  # same idle problem as login, and this POST is the prize

        self._do_fingerprint_check(getattr(self, '_schedule_html', ''), schedule_url)

        slots_url = f"{BASE_URL}/VistosOnline/slots"
        slots_data = {'posto_id': posto_id, 'captcha': captcha_token}
        headers = {
            'Accept': '*/*', 'Content-Type': 'application/x-www-form-urlencoded',
            'Origin': BASE_URL, 'Referer': f"{BASE_URL}/VistosOnline/Schedule.jsp?posto_id={posto_id}",
            # A JSP is free to branch on this, and this endpoint evidently does: the partner's
            # implementation sends it and gets a bare array of 309 dates for posto 3059, while
            # ours omitted it (on the strength of one old capture) and got the {"data":{}}
            # wrapper every single time for the same post, same residence, same endpoint.
            'X-Requested-With': 'XMLHttpRequest',
            'Sec-Fetch-Dest': 'empty', 'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Site': 'same-origin',
        }
        logger.debug(f"[Step 6] Slots request URL={slots_url}?posto_id={posto_id} captcha_len={len(captcha_token)}")

        # Primary path: from inside the live page. Kept alongside the httpx call for now so each
        # run produces a direct A/B on the same token, in the same second, over the same session —
        # that comparison is the evidence for whether the edge tier is judging the transport.
        browser = await self._slots_via_browser(captcha_token)
        if browser is not None:
            logger.info(f"[Step 6] In-page fetch: HTTP {browser.get('status')} "
                        f"ct={browser.get('ct')!r} len={len(browser.get('body') or '')}")
            logger.debug(f"[Step 6] In-page body: {(browser.get('body') or '')[:300]}")

        # One-shot discriminator. Every /slots answer we have ever recorded — 362 of them, across
        # three postos, three residence countries, ten accounts, both TLS stacks and both token
        # provenances — is the same 12 bytes. A calendar varies; a constant is emitted before any
        # calendar is consulted. And the shape is one the site's own front-end cannot consume:
        # handleServerResponse() does `data = JSON.parse(...)` then `data.find(d => d.date === k)`,
        # so a real answer is a bare ARRAY. So ask what the server is branching on: send it a
        # deliberately bad token, then no token at all. If those answer differently, the captcha is
        # being evaluated and we are past it; if they answer the same 12 bytes, the request never
        # reaches captcha evaluation and the precondition that fails is upstream of it.
        page = getattr(self, '_pw_page', None)
        # Off by default. This answered its question — /slots ignores nothing; it was our token
        # that was being refused — and it is no longer free to run: the bad-token probe draws a
        # 403 HTML page from the bot-detection tier, and the requests that follow it in the same
        # page came back "Failed to fetch". Firing that at a session that has just been handed a
        # real calendar is a bad trade. Set SLOTS_PROBE=1 to bring it back deliberately.
        if (os.getenv('SLOTS_PROBE', '0') == '1' and not getattr(type(self), '_probe_done', False)
                and page is not None and not page.is_closed()):
            type(self)._probe_done = True
            for label, body in (
                ('bad-token', f"posto_id={posto_id}&captcha=NOT_A_REAL_TOKEN"),
                ('no-captcha', f"posto_id={posto_id}"),
                ('bogus-posto', f"posto_id=99999&captcha={quote(captcha_token)}"),
            ):
                try:
                    r = await page.evaluate(
                        """async ([url, body]) => {
                            const r = await fetch(url, {method: 'POST', credentials: 'include',
                                headers: {'Content-Type': 'application/x-www-form-urlencoded',
                                          'X-Requested-With': 'XMLHttpRequest'}, body});
                            return {status: r.status, body: (await r.text()).slice(0, 300)};
                        }""",
                        [f"/VistosOnline/slots?posto_id={posto_id}", body],
                    )
                    logger.info(f"[Probe] {label:<12} HTTP {r['status']} body={r['body']!r}")
                except Exception as e:
                    logger.warning(f"[Probe] {label} failed: {e}")

            # Two hypotheses survive the parameter probe: (A) the servlet runs but bails on missing
            # session state, or (B) an edge tier answers /slots itself and the servlet never runs.
            # Response headers separate them — Tomcat and a WAF appliance do not describe themselves
            # the same way — and so does a path that does not exist: under (B) a canned body comes
            # back for anything matching the rule, under (A) the container 404s.
            for label, url, method, body in (
                ('slots-GET', f"/VistosOnline/slots?posto_id={posto_id}", 'GET', None),
                ('no-such-path', "/VistosOnline/slotsXYZNOPE", 'POST', 'posto_id=2032'),
                ('Pedidos', "/VistosOnline/Pedidos", 'GET', None),
            ):
                try:
                    r = await page.evaluate(
                        """async ([url, method, body]) => {
                            const init = {method, credentials: 'include'};
                            if (body !== null) {
                                init.body = body;
                                init.headers = {'Content-Type': 'application/x-www-form-urlencoded'};
                            }
                            const r = await fetch(url, init);
                            const t = await r.text();
                            const h = {}; r.headers.forEach((v, k) => h[k] = v);
                            return {status: r.status, len: t.length, headers: h,
                                    body: t.slice(0, 200)};
                        }""",
                        [url, method, body],
                    )
                    logger.info(f"[Probe] {label:<12} HTTP {r['status']} len={r['len']} "
                                f"headers={r['headers']}")
                    if label != 'Pedidos':
                        logger.info(f"[Probe] {label:<12} body={r['body']!r}")
                except Exception as e:
                    logger.warning(f"[Probe] {label} failed: {e}")

            # Save the applications list in full — "does this account hold a registered
            # application?" is worth answering from the bytes, not from a stripped snippet.
            try:
                html = await page.evaluate(
                    "async () => (await fetch('/VistosOnline/Pedidos', {credentials:'include'})).text()"
                )
                d = os.path.join(WORKING_DIR, 'debug_html')
                os.makedirs(d, exist_ok=True)
                p = os.path.join(d, f"pedidos_{self.username}_{int(time.time())}.html")
                with open(p, 'w', encoding='utf-8') as fh:
                    fh.write(html)
                logger.info(f"[Probe] Pedidos saved -> {p}")
            except Exception as e:
                logger.warning(f"[Probe] Pedidos save failed: {e}")

        # A reCAPTCHA token is SINGLE-USE. The in-page call above already redeemed it, so firing
        # this second request with the same token guarantees a spent-token rejection — and its
        # answer is what the rest of this function then judges. Only fall back to it when the
        # in-page path produced nothing at all.
        if browser is not None and browser.get('status') == 200:
            resp = _InPageResponse(browser.get('status'), browser.get('body') or '')
        else:
            resp = self.session.post(slots_url, data=slots_data, params={'posto_id': posto_id}, headers=headers)
        # The exact Cookie line we sent and the response headers we got back. A 200 carrying a
        # shape the site's own front-end cannot parse is a server-side refusal of some kind, and
        # these two lines are what let us compare our session against a captured browser one.
        logger.debug(f"[Step 6] Cookie sent: {resp.request.headers.get('cookie', '(none)')}")
        logger.debug(f"[Step 6] Slots response headers: {dict(resp.headers)}")
        logger.info(f"[Step 6] Slots API response (httpx): HTTP {resp.status_code} ({len(resp.text)} chars)")

        # Prefer the in-page answer when we have one: it is the only request path this origin
        # serves from the application tier.
        raw = resp.text
        source = 'httpx'
        if browser is not None and browser.get('status') == 200:
            raw = browser.get('body') or ''
            source = 'browser'
            if raw.lstrip().startswith('[') and not resp.text.lstrip().startswith('['):
                logger.warning("[Step 6] TRANSPORT CONFIRMED: the in-page fetch got a slot ARRAY "
                               "while httpx got %r on the same token — /slots must stay in the browser",
                               resp.text[:40])
        logger.info(f"[Step 6] Using {source} response ({len(raw)} chars)")
        logger.debug(f"[Step 6] Full raw response: {raw[:1000]}")

        status = browser.get('status') if source == 'browser' else resp.status_code
        if status != 200:
            logger.error(f"[Step 6] Slots API failed ({source}): {status}")
            # 403 is the BD perimeter deny and a 3xx means we were bounced out of the
            # authenticated area. Neither is "the calendar is empty" — both end the session.
            if status == 403 or 300 <= status < 400:
                self._session_dead = f'http-{status}'
            # Whatever else went wrong, do not keep replaying the same token at it.
            self._slots_token = None
            self._report('booking_no_availability', f'http {status}', stage_for('slots_waiting'))
            return None

        try:
            result = json.loads(raw)
        except Exception:
            logger.error("[Step 6] Slots response not JSON")
            # /slots answers JSON to a live session; HTML here means the site served a page
            # instead — the login screen or an error page — so the session is finished.
            self._session_dead = 'non-json-response'
            self._slots_token = None
            logger.debug(f"[Step 6] Non-JSON body head: {raw[:200]!r}")
            return None

        if isinstance(result, dict):
            if result.get('type') == 'error':
                desc = str(result.get('description', ''))
                logger.error(f"[Step 6] Slots API error: {desc or 'unknown'}")
                # The site reports a lapsed session as a normal JSON error, so match the
                # wording rather than treating every error as a transient empty result.
                if re.search(r'sess|autentic|login|expir|credenc', desc, re.I):
                    self._session_dead = f'site-error:{desc[:60]}'
                self._report('booking_no_availability', f"error: {desc}"[:120], stage_for('slots_waiting'))
                return None
            if 'data' in result:
                result = result['data']
                if isinstance(result, dict) and not result:
                    # NOT an empty calendar. {"data":{}} is this server's answer to an invalid or
                    # already-spent captcha, confirmed three ways: (1) the site's own front-end
                    # does data = JSON.parse(responseText) then data.find(d => d.date === k), so a
                    # real answer is a bare ARRAY and this shape would throw; (2) a capture of the
                    # partner's live session shows the same posto returning a real 16-date array
                    # with a good token and exactly these 12 bytes one minute later when he re-fired
                    # it with captcha=null; (3) his own handler calls it "ARMADILHA" (trap) and
                    # reacts by discarding the token and re-solving immediately.
                    # So do not report no-availability and sleep a full poll interval — that turns
                    # one bad token into 25s of blindness. Drop the token and come back fast.
                    logger.warning("[Step 6] {\"data\":{}} — captcha rejected (invalid/spent), "
                                   "not an empty calendar. Discarding token and re-solving.")
                    self._slots_token = None
                    self._captcha_rejected = True
                    self._report('booking_no_availability', 'captcha-rejected', stage_for('slots_waiting'))
                    return []
            elif not result:
                self._report('booking_no_availability', 'empty', stage_for('slots_waiting'))
                return []
            else:
                logger.warning(f"[Step 6] Unknown dict response format. Keys: {list(result.keys())}")
                return []

        if not isinstance(result, list) or not result:
            logger.warning("[Step 6] No appointment slots available")
            self._report('booking_no_availability', f'n={len(result) if isinstance(result, list) else 0}', stage_for('slots_waiting'))
            return []

        # Summarise the way the calendar is actually read: the earliest selectable day, how far
        # out it sits, and which months carry anything. A bare count hides the one number that
        # matters — this post offers nothing for roughly the first month, so an empty September
        # means "too soon", not "closed".
        try:
            days = sorted(d.get('date') for d in result if isinstance(d, dict) and d.get('date'))
            first = datetime.strptime(days[0], '%Y-%m-%d').date() if days else None
            lead = (first - datetime.now().date()).days if first else None
            months = sorted({d[:7] for d in days})
            logger.info(f"[Step 6] SLOTS OPEN — {len(result)} selectable dates, earliest {days[0]} "
                        f"(+{lead}d), months {months[:6]}{'...' if len(months) > 6 else ''}")
        except Exception:
            logger.info(f"[Step 6] {len(result)} dates with slots available")
        self._report('slots_available', f'n={len(result)}', stage_for('slots_solving'))
        return result

    async def _slots_via_browser(self, captcha_token: str) -> Optional[dict]:
        """Ask for slots the way the page itself does.

        The page mints its own token with grecaptcha.enterprise.execute() and hands it to
        getSlots(). Everywhere else we had substituted our own artifact for the page's, the site
        rejected it — the Formulario needed a real browser submit, and a solver token is the last
        remaining substitution on this path. So try the page's native token first and fall back to
        the solver's, logging which one answered: the pair is the evidence for whether the token's
        provenance is what /slots is judging.
        """
        page = getattr(self, '_pw_page', None)
        if page is None or page.is_closed():
            return None
        posto_id = getattr(self, '_posto_id', self.consular_post_id)

        native = None
        try:
            probe = await page.evaluate(
                """
                async ([sitekey, action]) => {
                    const out = {
                        hasGrecaptcha: typeof window.grecaptcha !== 'undefined',
                        hasEnterprise: !!(window.grecaptcha && window.grecaptcha.enterprise),
                        scripts: Array.from(document.querySelectorAll('script[src]'))
                            .map(s => s.src).filter(s => /recaptcha|gstatic|google/i.test(s)).slice(0, 5),
                        widgets: document.querySelectorAll('[data-sitekey]').length,
                        token: null, error: null,
                    };
                    const g = (window.grecaptcha && window.grecaptcha.enterprise) || window.grecaptcha;
                    if (!g || !g.execute) { out.error = 'grecaptcha.execute unavailable'; return out; }
                    try {
                        await new Promise(res => (g.ready ? g.ready(res) : res()));
                        out.token = await g.execute(sitekey, {action});
                    } catch (e) { out.error = String((e && e.message) || e).slice(0, 200); }
                    return out;
                }
                """,
                [SITE_KEY, "SCHEDULE_EVISA"],
            )
            # Say what the page actually has. The previous version returned a bare null when
            # grecaptcha was missing and logged nothing, so the one experiment that mattered —
            # does a page-minted token behave differently from a solver's — silently never ran.
            logger.info(f"[Step 6] grecaptcha on page: present={probe.get('hasGrecaptcha')} "
                        f"enterprise={probe.get('hasEnterprise')} widgets={probe.get('widgets')} "
                        f"scripts={probe.get('scripts')}")
            if probe.get('error'):
                logger.warning(f"[Step 6] Page could not mint a token: {probe['error']}")
            native = probe.get('token')
        except Exception as exc:
            logger.warning(f"[Step 6] grecaptcha probe failed: {type(exc).__name__}: {str(exc)[:140]}")

        # Call the page's OWN getSlots(), not a fetch of our own.
        #
        # Schedule.jsp creates one XMLHttpRequest at load time (`var xmlhttp = new getXMLObject()`)
        # and getSlots(posto, token) issues the request through it; handleServerResponse() then
        # parks the parsed result in the page-global `data` and reveals the calendar. A request we
        # build ourselves — fetch or a fresh XHR — does not carry that context, and this origin
        # answers it differently: a working reference implementation records getting Schedule.jsp's
        # own HTML back instead of JSON, and we have been getting a {"data":{}} wrapper that the
        # array-shaped `data` the page expects never looks like. So hand the token to the page and
        # read what its own plumbing produces.
        # Solver token only. `native` comes from grecaptcha.enterprise.execute(SITE_KEY, {action}),
        # which is the score-flow API — but this page renders a VISIBLE v2 checkbox widget
        # (data-callback="onCaptchaSuccess", anchor iframe size=normal, 304x78), and execute() on
        # one of those is refused: 373 "Invalid site key or not loaded in api.js" in our own log.
        # So `native` has never once been a token, and preferring it only ever cost a branch.
        token = captcha_token
        attempts = [('solver-token', captcha_token)]

        # Read the transport, not the page global. Schedule.jsp parks the parsed result in a
        # page variable whose NAME VARIES BY POSTO — `data` on 2032, `tdata` on 3109 (both seen
        # in captures of handleServerResponse) — so polling for `data` alone silently misses a
        # real answer on any post that uses the other name. `xmlhttp.responseText` is the same
        # object the site's own handler reads and is named identically everywhere.
        result = await page.evaluate(
            """
            ([posto, token]) => new Promise((resolve) => {
                if (typeof getSlots !== 'function') {
                    resolve({err: 'no getSlots on page'}); return;
                }
                if (typeof xmlhttp === 'undefined' || !xmlhttp) {
                    resolve({err: 'no xmlhttp on page'}); return;
                }
                try { getSlots(posto, token); }
                catch (e) { resolve({err: 'getSlots threw: ' + (e && e.message || e)}); return; }
                let n = 0;
                const iv = setInterval(() => {
                    n++;
                    if (xmlhttp.readyState === 4) {
                        clearInterval(iv);
                        const text = xmlhttp.responseText || '';
                        let parsed = null;
                        try { parsed = JSON.parse(text); } catch (e) {}
                        resolve({err: null, status: xmlhttp.status, text: text,
                                 isArray: Array.isArray(parsed),
                                 count: Array.isArray(parsed) ? parsed.length : 0});
                    } else if (n >= 40) {           // 40 x 500ms = 20s
                        clearInterval(iv);
                        resolve({err: 'xmlhttp never reached readyState 4',
                                 sawState: xmlhttp.readyState});
                    }
                }, 500);
            })
            """,
            [str(posto_id), token],
        )

        if result.get('err'):
            logger.warning(f"[Step 6] Native getSlots did not deliver: {result['err']}"
                           + (f" (readyState = {result.get('sawState')})" if result.get('sawState') else ""))
        else:
            body = result.get('text') or ''
            logger.info(f"[Step 6] Native getSlots -> HTTP {result.get('status')} "
                        f"len={len(body)} array={result.get('isArray')} head={body[:60]!r}")
            # The page's own XHR has now SPENT this token — reCAPTCHA tokens are single-use, and
            # Schedule.jsp's handleServerResponse ends with an unconditional
            # grecaptcha.enterprise.reset(). Re-firing the same token below could only ever be
            # refused, and its refusal ({"data":{}}) would then be logged as if it were the
            # server's verdict on availability. So this answer is the answer, whatever it says.
            return {'status': result.get('status') or 200, 'ct': 'native', 'body': body}

        # Only reached when the native call never ran at all, so the token is still unspent.
        last = None
        for label, tok in attempts:
            try:
                res = await page.evaluate(
                    """
                    async ([posto, token]) => {
                        const r = await fetch('/VistosOnline/slots?posto_id=' + encodeURIComponent(posto), {
                            method: 'POST',
                            headers: {'Content-Type': 'application/x-www-form-urlencoded',
                                      'X-Requested-With': 'XMLHttpRequest'},
                            body: 'posto_id=' + encodeURIComponent(posto) + '&captcha=' + encodeURIComponent(token),
                            credentials: 'same-origin',
                        });
                        return {status: r.status, ct: r.headers.get('content-type'), body: await r.text()};
                    }
                    """,
                    [str(posto_id), tok],
                )
            except Exception as exc:
                logger.warning(f"[Step 6] Fallback fetch ({label}) failed: {type(exc).__name__}: {exc}")
                continue
            body = (res or {}).get('body') or ''
            logger.info(f"[Step 6] fallback {label}: HTTP {res.get('status')} len={len(body)} head={body[:60]!r}")
            last = res
            if body.lstrip().startswith('['):
                return res
        return last

    # =========================================================================
    # STEP 6b: Hold the arrived session and watch for a release
    # =========================================================================
    async def _watch_for_slots(self, first_token: Optional[str] = None) -> Optional[list]:
        """Stay on Schedule.jsp and re-query /slots on a jittered cadence until something opens.

        By the time we get here every input /slots needs is already instance state: the
        cookie jar on self.session, self._schedule_html for the getTables beacon, and
        self._posto_id. _do_fingerprint_check draws a fresh secrets.token_hex(16) seed on
        every call, so re-firing the beacon is a new sample rather than a replay of a stale
        one. A repeat query therefore costs one captcha solve and a few KB, against the
        megabytes the Playwright leg spends to get a session here in the first place.

        Returns the slot list on a hit, [] if the configured window elapsed with nothing,
        and None if the session died — the caller treats None as "rebuild and come back",
        which is why step_get_slots distinguishes those two empty-looking cases.
        """
        posto = getattr(self, '_posto_id', self.consular_post_id)
        started = time.time()
        deadline = started + SLOT_WATCH_MAX_MINUTES * 60 if SLOT_WATCH_MAX_MINUTES > 0 else None
        poll = 0
        last_heartbeat = started

        logger.info(
            f"[Watch] Armed on posto {posto} — polling /slots every ~{SLOT_POLL_INTERVAL:.0f}s"
            + ("" if deadline is None else f" for up to {SLOT_WATCH_MAX_MINUTES:.0f} min")
            + f"; samples -> {os.path.relpath(SLOT_POLL_CSV, WORKING_DIR)}"
        )

        consecutive_errors = 0

        while True:
            poll += 1
            t0 = time.time()
            # The pre-solved token was fetched while Formulario was still submitting, so it
            # is only fresh enough for the first poll; every later one solves its own.
            try:
                slots = await self.step_get_slots(pre_solved_captcha=first_token if poll == 1 else None)
            except TRANSPORT_ERRORS as exc:
                # A residential exit node dropping one request is not a reason to discard a
                # session that cost two captchas and the entire Playwright leg to establish.
                # The session itself is almost certainly still valid server-side, so ride the
                # hiccup out and only abandon it once the path is repeatedly unusable.
                consecutive_errors += 1
                logger.warning(
                    f"[Watch] poll #{poll} transport error ({type(exc).__name__}: {str(exc)[:80]}) "
                    f"— {consecutive_errors}/{WATCH_MAX_CONSECUTIVE_ERRORS} in a row, session kept")
                self._record_poll(poll, f'XPORT:{type(exc).__name__}', 0, int((time.time() - t0) * 1000))
                if consecutive_errors >= WATCH_MAX_CONSECUTIVE_ERRORS:
                    logger.error(f"[Watch] {consecutive_errors} consecutive transport errors — "
                                 f"path through this proxy is unusable, abandoning session")
                    return None
                await self._human_delay(SLOT_POLL_INTERVAL * 0.3, SLOT_POLL_INTERVAL * 0.6)
                continue
            consecutive_errors = 0
            elapsed_ms = int((time.time() - t0) * 1000)
            watched_min = (time.time() - started) / 60

            if slots:
                logger.info(f"[Watch] SLOTS FOUND on poll #{poll} after {watched_min:.1f} min "
                            f"— {len(slots)} date(s) available")
                self._record_poll(poll, 'FOUND', len(slots), elapsed_ms)
                return slots

            if self._session_dead:
                logger.warning(f"[Watch] Session unusable on poll #{poll} after {watched_min:.1f} min "
                               f"({self._session_dead}) — ending watch so the outer retry rebuilds it")
                self._record_poll(poll, f'DEAD:{self._session_dead}', 0, elapsed_ms)
                return None

            self._record_poll(poll, 'EMPTY' if slots is not None else 'ERROR', 0, elapsed_ms)
            logger.info(f"[Watch] poll #{poll} @ {datetime.now().astimezone():%H:%M:%S %Z} — "
                        f"no slots ({watched_min:.1f} min watched)")

            if deadline and time.time() >= deadline:
                logger.info(f"[Watch] Window elapsed after {poll} polls — nothing opened")
                return []

            # Hold the dashboard claim. /api/account/claim frees any account whose lastEventAt
            # is older than 15 minutes, so a silent watcher would eventually have its own
            # account claimed out from under it. Report well inside that window, but not once
            # per poll — at this cadence that would be ~1400 Event rows a day per account.
            if time.time() - last_heartbeat > 480:
                self._report('booking_no_availability',
                             f'watching: {poll} polls / {watched_min:.0f} min, calendar empty',
                             stage_for('slots_waiting'))
                last_heartbeat = time.time()

            # A rejected captcha is not an empty calendar, so it must not cost an empty
            # calendar's wait. Come straight back with a fresh token instead of staying blind
            # for a full interval — during which the post may open and close again.
            if getattr(self, '_captcha_rejected', False):
                self._captcha_rejected = False
                await self._human_delay(1.5, 3.0)
                continue

            # Jittered rather than on a grid: requests landing on an exact 60.000s cadence
            # are a cleaner tell than anything in their contents.
            await self._human_delay(SLOT_POLL_INTERVAL * 0.85, SLOT_POLL_INTERVAL * 1.25)

    def _record_poll(self, poll: int, result: str, n_dates: int, elapsed_ms: int):
        """Append one observation to the slot-poll dataset.

        This file is the actual point of watch mode: a per-sample record of what /slots
        returned and exactly when, so "when do slots open" becomes something measured from
        our own observations. Timestamps are written in BOTH UTC and this host's local zone
        because the host runs Europe/Berlin while the consular post and the site do not —
        reading the resulting histogram in the wrong zone is the easiest way to land on a
        window that is hours off.
        """
        try:
            os.makedirs(os.path.dirname(SLOT_POLL_CSV), exist_ok=True)
            is_new = not os.path.exists(SLOT_POLL_CSV)
            now_utc = datetime.now(timezone.utc)
            now_local = now_utc.astimezone()
            with open(SLOT_POLL_CSV, 'a', encoding='utf-8') as fh:
                if is_new:
                    fh.write('utc_iso,local_iso,local_tz,weekday_utc,hour_utc,minute_utc,'
                             'username,posto,poll_n,result,n_dates,elapsed_ms\n')
                fh.write(
                    f"{now_utc.isoformat(timespec='seconds')},"
                    f"{now_local.isoformat(timespec='seconds')},"
                    f"{now_local.tzname()},"
                    f"{now_utc.strftime('%a')},{now_utc.hour},{now_utc.minute},"
                    f"{self.account.get('loginUsername', '?')},"
                    f"{getattr(self, '_posto_id', self.consular_post_id)},"
                    f"{poll},{result},{n_dates},{elapsed_ms}\n"
                )
        except Exception as e:
            logger.warning(f"[Watch] Could not record poll sample: {e}")

    # =========================================================================
    # STEP 7: Select appointment and confirm
    # =========================================================================
    async def step_book_appointment(self, slots: list) -> bool:
        logger.info("[Step 7] Booking appointment...")
        if not slots:
            return False

        today = datetime.now().date()
        valid_slots = []
        for entry in slots:
            try:
                slot_date = datetime.strptime(entry['date'], '%Y-%m-%d').date()
                if slot_date >= today and entry.get('periods'):
                    valid_slots.append(entry)
            except (KeyError, ValueError):
                continue

        if not valid_slots:
            logger.warning("[Step 7] No valid future slots with available periods")
            self._report('booking_no_availability', 'no-valid-future-slots', stage_for('slots_waiting'))
            return False

        valid_slots.sort(key=lambda x: x['date'])
        selected_entry = valid_slots[0]
        selected_date_raw = selected_entry['date']
        selected_date_slash = selected_date_raw.replace('-', '/')
        selected_period = selected_entry['periods'][0]
        period_id = selected_period['id']
        period_desc = selected_period.get('description', '')

        logger.info(f"[Step 7] Selected: {selected_date_slash} period {period_id} ({period_desc})")
        self._report('date_selected', selected_date_slash, stage_for('booking'))
        self._report('period_selected', str(period_id), stage_for('booking'))

        posto_id = getattr(self, '_posto_id', self.consular_post_id)
        book_url = f"{BASE_URL}/VistosOnline/SubmeterVistoCriaPDF"
        book_data = {
            'lang': self.language, 'txtHuman': '', 'back': '',
            'f_date_c': selected_date_slash, 'cmbPeriodo': str(period_id),
        }
        headers = {
            'Accept': '*/*', 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'Origin': BASE_URL, 'Referer': f"{BASE_URL}/VistosOnline/Schedule.jsp?posto_id={posto_id}",
            # The one booking capture we have sends this, and a JSP is free to branch on it. This
            # request runs once, at the only moment that matters, so it matches the known-good
            # capture exactly. (/slots sends it too now — see the note there.)
            'X-Requested-With': 'XMLHttpRequest',
            'Sec-Fetch-Dest': 'empty', 'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Site': 'same-origin',
        }

        self._report('booking_submit', selected_date_slash, stage_for('booking'))
        logger.debug(f"[Step 7a] Book URL={book_url}?posto_id={posto_id} body={book_data}")

        # Prefer the live page: the booking POST then rides the exact session the WAF just
        # handshook with (same cookies, same TLS, same JS context), which is how the partner's
        # HAR-derived implementation issues it. Fall back to the session client if no page.
        resp = None
        page = getattr(self, '_pw_page', None)
        if page is not None and not page.is_closed():
            try:
                body = urlencode(book_data)
                inpage = await page.evaluate(
                    """
                    async ([posto, body]) => {
                        const r = await fetch('/VistosOnline/SubmeterVistoCriaPDF?posto_id=' +
                                              encodeURIComponent(posto), {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                                'X-Requested-With': 'XMLHttpRequest',
                            },
                            body: body,
                            credentials: 'same-origin',
                        });
                        return {status: r.status, body: await r.text()};
                    }
                    """,
                    [str(posto_id), body],
                )
                logger.info(f"[Step 7a] Booking via live page: HTTP {inpage.get('status')} "
                            f"({len(inpage.get('body') or '')} chars)")
                resp = _InPageResponse(inpage.get('status', 0), inpage.get('body') or '')
            except Exception as exc:
                logger.warning(f"[Step 7a] In-page booking failed ({type(exc).__name__}: {exc}) "
                               f"— falling back to the session client")

        if resp is None:
            resp = self.session.post(book_url, data=book_data, headers=headers, params={'posto_id': posto_id})
        logger.debug(f"[Step 7a] Response status={resp.status_code} headers={dict(resp.headers)}")
        logger.debug(f"[Step 7a] Response body ({len(resp.text)} chars): {resp.text[:500]}")

        if resp.status_code == 403:
            # Site-side verdict, not a proxy fault — see the note in step_login. Reporting
            # proxy health here permanently retired good rows for a block the origin would
            # have issued to any client with this request shape.
            logger.error("[Step 7a] 403 Forbidden — site-side block (BD/WAF), proxy not blamed")
            self._report('booking_failed', 'http 403', stage_for('booking'))
            return False
        if resp.status_code != 200:
            logger.error(f"[Step 7a] Booking POST failed: {resp.status_code}")
            self._report('booking_failed', f'http {resp.status_code}', stage_for('booking'))
            return False

        resp_text = resp.text
        # The success page is a self-submitting redirect to the PDF: it defines redireccionar(),
        # arms setTimeout(...,1500) and submits frm_1. 'MostrarPdf' does appear, but only inside a
        # commented-out window.open line — matching on that alone would break the moment someone
        # tidies the comment. Accept any of the three real markers.
        markers = [m for m in ('redireccionar', 'frm_1', 'MostrarPdf', 'dirija-se ao consulado',
                               'Imprima o formul') if m in resp_text]
        booking_confirmed = bool(markers)

        # The server states the booked appointment in words: "Imprima o formulário e dirija-se ao
        # consulado escolhido no dia 2026/09/21 11-12". That sentence is the strongest confirmation
        # available — it is the server telling us the date and period it actually reserved, rather
        # than us echoing back what we asked for. Read it and check it against our request.
        confirmed_date = confirmed_period = None
        m = re.search(r'no dia\s+(\d{4}/\d{2}/\d{2})\s*([\d\-]+)?', resp_text)
        if m:
            confirmed_date, confirmed_period = m.group(1), (m.group(2) or '').strip()
            logger.info(f"[Step 7a] Server confirms the appointment: {confirmed_date} {confirmed_period}")
            if confirmed_date != selected_date_slash:
                logger.warning(f"[Step 7a] Server booked {confirmed_date}, we asked for "
                               f"{selected_date_slash} — reporting the server's answer")

        if booking_confirmed:
            logger.info(f"[Step 7a] Booking confirmed! Server is redirecting to the PDF "
                        f"(markers: {markers})")
        elif 'erro' in resp_text.lower() or 'error' in resp_text.lower():
            logger.error("[Step 7a] Booking returned error page")
            self._report('booking_failed', 'error page', stage_for('booking'))
            return False
        else:
            logger.warning("[Step 7a] Unexpected response (no MostrarPdf found). Trying PDF anyway...")

        await self._human_delay(1.5, 2.5)  # matches the site's own setTimeout(1500)

        pdf_url = f"{BASE_URL}/VistosOnline/MostrarPdf"
        pdf_headers = {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            # frm_1 lives on the SubmeterVistoCriaPDF response, so that page is the referrer
            # the browser would send — not Schedule.jsp.
            'Referer': f"{BASE_URL}/VistosOnline/SubmeterVistoCriaPDF?posto_id={posto_id}",
            'Sec-Fetch-Dest': 'document', 'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-User': '?1',
            'Upgrade-Insecure-Requests': '1',
        }
        pdf_resp = self.session.get(pdf_url, headers=pdf_headers)
        logger.debug(f"[Step 7b] PDF response status={pdf_resp.status_code} headers={dict(pdf_resp.headers)} "
                     f"size={len(pdf_resp.content)}b first20={pdf_resp.content[:20]}")
        if pdf_resp.status_code != 200:
            logger.error(f"[Step 7b] PDF download failed: {pdf_resp.status_code}")
            if booking_confirmed:
                self._report('booking_success', json.dumps({'pdfUrl': None, 'date': confirmed_date or selected_date_slash,
                                'period': confirmed_period or period_id}), stage_for('completed'))
                return True
            self._report('booking_failed', 'pdf download failed', stage_for('booking'))
            return False

        content_type = pdf_resp.headers.get('content-type', '').lower()
        content = pdf_resp.content
        is_pdf = ('pdf' in content_type or 'octet-stream' in content_type or content[:5] == b'%PDF-')

        if is_pdf and len(content) > 100:
            pdf_dir = os.path.join(WORKING_DIR, 'PDFs')
            os.makedirs(pdf_dir, exist_ok=True)
            pdf_path = os.path.join(pdf_dir, f'appointment_{selected_date_raw}_p{period_id}.pdf')
            with open(pdf_path, 'wb') as f:
                f.write(content)
            logger.info(f"[Step 7b] PDF saved: {pdf_path} ({len(content)} bytes)")
            # pdfUrl is intentionally the real MostrarPdf endpoint (same shape/host the
            # dashboard's pdfUrl allowlist accepts — see route.ts sanitizePdfUrl), not a
            # local file path, matching what main-human's booking_success detail carries.
            self._report(
                'booking_success',
                json.dumps({'pdfUrl': pdf_url, 'date': confirmed_date or selected_date_slash,
                            'period': confirmed_period or period_id}),
                stage_for('completed'),
            )
            return True
        else:
            logger.warning(f"[Step 7b] Response doesn't look like a PDF. Content-Type: {content_type}, Size: {len(content)}")
            if booking_confirmed:
                self._report('booking_success', json.dumps({'pdfUrl': None, 'date': selected_date_slash, 'period': period_id}), stage_for('completed'))
                return True
            self._report('booking_failed', 'not-a-pdf response', stage_for('booking'))
            return False

    # =========================================================================
    # FULL FLOW
    # =========================================================================
    async def run(self) -> bool:
        username = self.account.get('loginUsername')
        logger.info(f"\n{'='*60}\n  VISA API BOT v3 — Starting for {username}\n"
                    f"  Consulate: {self.consular_post_id}\n{'='*60}\n")
        flow_start = time.time()
        step_times: Dict[str, float] = {}

        try:
            # Log in from a real browser when HYBRID_LOGIN is on. The HTTP path sends a payload
            # byte-identical to the one a working reference session sends (username, password,
            # language, rgpd=Y, captchaResponse — verified against a capture of the partner's
            # live login XHR), and still draws http-403 bd-blocks on the POST while the GET of
            # the same page succeeds. What it cannot reproduce is the client-side half: this
            # origin's bot-detection sets its cookie from JavaScript, and the partner's console
            # names the consequence directly — "Nenhum cookie DataDome encontrado. O proxy pode
            # ser banido." A curl_cffi client runs no JS, so it never holds that cookie. The
            # browser does, and step_hybrid_login hands its cookies back to self.session.
            t = time.time()
            logged_in = False
            if HYBRID_LOGIN:
                try:
                    logged_in = await self.step_hybrid_login()
                except Exception as exc:
                    logger.warning(f"[Login] Hybrid login raised "
                                   f"{type(exc).__name__}: {str(exc)[:120]} — falling back to HTTP")
                if not logged_in:
                    logger.warning("[Login] Hybrid login did not succeed — falling back to HTTP")
            if not logged_in:
                if not await self.step_load_login_page():
                    return False
                step_times['Step1_LoginPage'] = round(time.time() - t, 2)
                t = time.time()
                if not await self.step_login():
                    return False
            step_times['Step2_Login+CAPTCHA'] = round(time.time() - t, 2)
            report_status(self.account.get('id'), 'LOGGED_IN')

            # step_edit_profile() is deliberately NOT called. It POSTed /VistosOnline/editProfile
            # with traveldoc = the dashboard's passportNo on every run of every account, while the
            # server already holds a registered document (profile dumps show e.g. traveldoc
            # "PA459768+", every other field disabled) — so whenever the two disagreed it rewrote
            # the account's travel document, which the Formulario then echoes back as readonly f14.
            # It also spent one captcha per run. The partner's working flow has no editProfile,
            # no profile.jsp and no /Pedidos step at all: the application is created fresh each run
            # by Formulario -> ScheduleController. Kept as a method for reference only.

            # Preferred path: open Schedule.jsp directly. These accounts already hold a
            # registered application, so re-submitting the Formulario is refused outright
            # ("já tem um pedido registado com agendamento futuro") and the server hands back the
            # form again at a .../ScheduleController URL — which older code read as success.
            # Schedule.jsp is where the site itself says to go to set or change the date.
            # The Schedule page cannot be entered cold (a direct navigation is redirected to
            # /VistosOnline/), and Pedidos listed no existing application on any account we tried,
            # so the shortcut that used to sit here only ever cost ~70s per session before falling
            # through. Now that the applicant-identity fix lets the application be accepted, the
            # normal flow is the flow.
            captcha_task = asyncio.create_task(self._solve_captcha_for_slots())

            # step_questionnaire() is deliberately NOT called. It walked the whole
            # QuestNextQuestion chain over curl_cffi, and step_fill_formulario() then walks the
            # SAME chain again inside the browser — two answer trails for one application, laid
            # down by two clients with different TLS and header fingerprints against a server that
            # accumulates them per session. The browser pass is the one whose result we use, so
            # the HTTP pass was pure risk. Kept as a method for reference only.
            t = time.time()
            if not await self.step_fill_formulario():
                captcha_task.cancel()
                return False
            step_times['Step4-5_Formulario'] = round(time.time() - t, 2)

            t = time.time()
            pre_solved_token = await captcha_task
            first_token = None
            # Deliberately NOT reusing the pre-solved token. It is minted in parallel with the
            # Formulario leg, so by the time /slots runs it is typically 60-120s old — against a
            # ~2min reCAPTCHA lifetime the server itself validates. The partner's working run
            # mints its schedule token AFTER landing on Schedule.jsp and uses it ~9s later.
            # Solving fresh costs one captcha; presenting a stale one may cost the whole answer.
            if pre_solved_token:
                age = time.time() - pre_solved_token['solved_at']
                logger.info(f"[Step 6] Discarding pre-solved token ({age:.0f}s old) — minting "
                            f"fresh on Schedule.jsp instead")

            if SLOT_WATCH:
                slots = await self._watch_for_slots(first_token=first_token)
            else:
                slots = await self.step_get_slots(pre_solved_captcha=first_token)
            if slots is None:
                return False
            step_times['Step6_Slots+CAPTCHA'] = round(time.time() - t, 2)

            if slots:
                t = time.time()
                result = await self.step_book_appointment(slots)
                step_times['Step7_Booking'] = round(time.time() - t, 2)
                total = round(time.time() - flow_start, 2)
                logger.info(f"\nTIMING for {username} — Total: {total}s")
                for step_name, duration in step_times.items():
                    logger.info(f"  {step_name}: {duration}s")
                if result:
                    report_status(self.account.get('id'), 'COMPLETED')
                return result
            else:
                logger.warning("No slots available. Will need to retry when they open.")
                return False

        except (CaptchaBlockedError, AlreadyBookedError):
            raise
        except (httpx.ProxyError, httpx.ConnectError, httpx.ConnectTimeout,
                curl_exceptions.ProxyError, curl_exceptions.ConnectionError,
                curl_exceptions.ConnectTimeout) as e:
            logger.error(f"Proxy error for {username}: {type(e).__name__}: {e}")
            self._report_proxy_health(False, f"proxy error: {type(e).__name__}")
            return False
        except TRANSPORT_ERRORS as e:
            # Timeouts and mid-flight disconnects: the path broke, but unlike a failure to
            # connect at all this does not identify the exit node as bad. Proxy.healthy is a
            # latch a human has to clear in the dashboard, so do NOT set it here — a few
            # seconds of packet loss must not permanently retire a working row.
            logger.error(f"Transport error for {username}: {type(e).__name__}: {str(e)[:120]}")
            return False
        except PlaywrightError as e:
            # The Formulario leg drives a real browser through a residential proxy, so a slow
            # page load or a navigation racing an evaluate is an operational failure, not a
            # defect. logger.exception buried these under stack traces that read like crashes.
            logger.error(f"Browser leg failed for {username}: {type(e).__name__}: "
                         f"{str(e).splitlines()[0][:160]}")
            return False
        except Exception as e:
            logger.exception(f"Unexpected error for {username}: {e}")
            return False
        finally:
            ctx = getattr(self, '_pw_context', None)
            if ctx is not None:
                try:
                    await ctx.close()
                except Exception:
                    pass
                self._pw_context = None
                self._pw_page = None
            if self.session:
                self.session.close()


# =================================================================================
# SETTINGS CACHE — posto_id from the dashboard, refreshed once per claim cycle so a
# dashboard-side change propagates without redeploying this bot.
# =================================================================================
_settings_cache: dict = {}


def refresh_settings_cache():
    global _settings_cache
    s = get_settings()
    if s:
        _settings_cache = s


def get_settings_posto_id():
    return _settings_cache.get('postoId')


# =================================================================================
# LIVE STATUS TABLE — in-memory, updated on every single dashboard report so the
# local process (console / journalctl -f / logs/api_bot.log) always has an
# up-to-the-second view of every in-flight account's current stage, independent of
# and in addition to the dashboard's own BookingWorker view. main()'s loop prints
# this as a compact table every cycle.
# =================================================================================
_live_status: Dict[str, dict] = {}  # account_id -> {username, stage, event, detail, updated_at}


def _update_live_status(account_id: Optional[str], username: str, stage: Optional[str],
                         event: str, detail: Optional[str] = None):
    if not account_id:
        return
    _live_status[account_id] = {
        'username': username,
        'stage': stage or '-',
        'event': event,
        'detail': (detail or '')[:100],
        'updated_at': time.time(),
    }


def _clear_live_status(account_id: Optional[str]):
    if account_id:
        _live_status.pop(account_id, None)


def format_status_table() -> str:
    """A compact, human-scannable snapshot of every currently-tracked account."""
    if not _live_status:
        return "  (no accounts currently in flight)"
    now = time.time()
    rows = sorted(_live_status.values(), key=lambda r: str(r.get('username') or 'UNKNOWN'))
    lines = []
    for r in rows:
        age = now - r['updated_at']
        username = str(r.get('username') or 'UNKNOWN')
        stage = str(r.get('stage') or '-')
        event = str(r.get('event') or '-')
        detail = str(r.get('detail') or '')[:100]
        lines.append(
            f"  {username:<18} | {stage:<11} | {event:<28} | "
            f"{age:5.1f}s ago | {detail}"
        )
    return "\n".join(lines)


# =================================================================================
# ENTRY POINT — dashboard-polling concurrent processing
# =================================================================================
async def process_single_account(account: dict, semaphore: asyncio.Semaphore, worker_id: int) -> bool:
    """Process one claimed account with retries, one dedicated proxy for the whole run."""
    async with semaphore:
        username = account.get('loginUsername', '?')

        user_log_dir = os.path.join(WORKING_DIR, 'logs', 'users')
        os.makedirs(user_log_dir, exist_ok=True)
        user_handler = logging.FileHandler(os.path.join(user_log_dir, f'{username}.log'), mode='a', encoding='utf-8')
        user_handler.setLevel(logging.DEBUG)
        user_handler.setFormatter(logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(funcName)-30s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
        logger.addHandler(user_handler)

        proxy = get_unassigned_proxy()
        proxy_display = f"{proxy['ip']}:{proxy['port']}" if proxy else 'NONE'
        if not proxy:
            logger.warning(f"[Worker-{worker_id}][{username}] No unassigned proxy available — running proxyless")
            report_event(account.get('id'), 'no_proxy_available', 'proxy pool exhausted, running proxyless', 'IDLE')
        logger.info(f"[Worker-{worker_id}] Starting: {username} | Proxy: {proxy_display}")
        _update_live_status(account.get('id'), username, 'IDLE', 'worker_started', f'proxy={proxy_display}')
        report_event(account.get('id'), 'worker_started', f'worker={worker_id} proxy={proxy_display}', 'IDLE',
                     proxy_id=proxy.get('id') if proxy else None)

        success = False
        blocked = False
        already_booked = False
        for attempt in range(MAX_RETRIES_PER_ACCOUNT):
            # One dedicated proxy per run — unless it died. Retrying an account down a proxy
            # that just failed at the transport layer spends the whole retry budget proving the
            # same thing three times.
            if proxy and time.time() - _FILE_BENCH.get(proxy.get('id'), 0.0) <= PROXY_BENCH_SECONDS:
                replacement = get_unassigned_proxy()
                if replacement and replacement.get('id') != proxy.get('id'):
                    logger.info(f"[Worker-{worker_id}][{username}] Proxy {proxy_display} benched — "
                                f"switching to {replacement['ip']}:{replacement['port']}")
                    proxy = replacement
                    proxy_display = f"{proxy['ip']}:{proxy['port']}"
            logger.info(f"[Worker-{worker_id}][{username}] Attempt {attempt + 1}/{MAX_RETRIES_PER_ACCOUNT} | Proxy: {proxy_display}")
            report_event(account.get('id'), 'attempt_start', f'{attempt + 1}/{MAX_RETRIES_PER_ACCOUNT}', 'LOGGING_IN',
                         proxy_id=proxy.get('id') if proxy else None)
            try:
                bot = VisaAPIBot(account, proxy)
                result = await bot.run()
                if result:
                    success = True
                    break
                if attempt < MAX_RETRIES_PER_ACCOUNT - 1:
                    wait = random.uniform(3, 8) * (attempt + 1)
                    logger.info(f"[Worker-{worker_id}][{username}] Retrying in {wait:.1f}s...")
                    _update_live_status(account.get('id'), username, 'IDLE', 'attempt_retry_wait', f'{wait:.1f}s')
                    await asyncio.sleep(wait)
            except AlreadyBookedError:
                # The identity already has what this bot exists to obtain. Take it out of the
                # pool rather than letting it be claimed again every cycle.
                logger.warning(f"[Worker-{worker_id}][{username}] This identity already holds an "
                               f"appointment — retiring it from the pool, no retries")
                report_status(account.get('id'), 'COMPLETED')
                report_event(account.get('id'), 'already_has_appointment',
                             'server refused a new application: existing one has a future appointment',
                             'COMPLETED')
                already_booked = True
                break
            except CaptchaBlockedError as e:
                logger.error(f"[Worker-{worker_id}][{username}] CAPTCHA BLOCKED — stopping retries: {e}")
                blocked = True
                break
            except Exception as e:
                logger.error(f"[Worker-{worker_id}][{username}] Error: {e}")
                if attempt < MAX_RETRIES_PER_ACCOUNT - 1:
                    await asyncio.sleep(5)

        if success:
            logger.info(f"[Worker-{worker_id}] {username} — APPOINTMENT BOOKED!")
        elif already_booked:
            logger.info(f"[Worker-{worker_id}] {username} — already had an appointment, retired")
        elif blocked:
            # Release the claim here too. Without this the account keeps claimedAt set
            # forever — /api/account/claim hard-requires claimedAt IS NULL, so a
            # reCAPTCHA-blocked account was permanently removed from the pool by a condition
            # that is transient. The event below still stamps lastEventAt, so the claim
            # route's 15-minute double-claim guard gives it a natural cooldown.
            report_status(account.get('id'), 'NOT_LOGGED_IN')
            report_event(account.get('id'), 'login_captcha_blocked', 'recaptcha block limit reached', 'FAILED')
            logger.info(f"[Worker-{worker_id}] {username} — blocked (reCAPTCHA limit), claim released")
        else:
            # Release the claim so it can be retried later (by this bot or the other path).
            report_status(account.get('id'), 'NOT_LOGGED_IN')
            report_event(account.get('id'), 'worker_exhausted', f'{MAX_RETRIES_PER_ACCOUNT} attempts, all failed', 'STOPPED')
            logger.info(f"[Worker-{worker_id}] {username} — failed all {MAX_RETRIES_PER_ACCOUNT} attempts")

        _clear_live_status(account.get('id'))
        logger.removeHandler(user_handler)
        user_handler.close()
        return success


async def main():
    LOCAL_MODE = bool(CREDS_FILE and _local_accounts)
    if not MACHINE_TOKEN and not LOCAL_MODE:
        logger.error("MACHINE_TOKEN is required (= the dashboard's SOCKET_INTERNAL_SECRET). Exiting.")
        sys.exit(1)
    if LOCAL_MODE:
        logger.info("\\n" + "="*60 + "\\n  LOCAL MODE — Donut will be used if configured\\n" + "="*60 + "\\n")

    logger.info(f"\n{'='*60}\n  VISA API BOT v3 — DASHBOARD-INTEGRATED MODE\n"
                f"  Dashboard: {NEXT_API_BASE}\n  Concurrency: {CONCURRENCY}\n"
                f"  Nationality: {NATIONALITY} | Residence: {COUNTRY_OF_RESIDENCE}"
                f"{' | Address: ' + RESIDENCE_ADDRESS if RESIDENCE_ADDRESS else ''}\n"
                f"  Posto: {POSTO_ID_OVERRIDE + ' (LOCAL OVERRIDE — dashboard ignored)' if POSTO_ID_OVERRIDE else 'from dashboard'}\n"
                f"  Slot watch: {'on, ~%.0fs' % SLOT_POLL_INTERVAL if SLOT_WATCH else 'off'}\n{'='*60}\n")

    _rejected_ids: set = set()   # accounts ONLY_USERNAMES has already turned away
    semaphore = asyncio.Semaphore(CONCURRENCY)
    in_flight: List[asyncio.Task] = []
    cycle = 0
    claimed_none_streak = 0

    while True:
        cycle += 1
        if not LOCAL_MODE:
            refresh_settings_cache()
            settings = _settings_cache
            if not settings or not settings.get('enabled'):
                logger.info(f"[Cycle {cycle}] Dashboard automation disabled or unreachable — idling")
                await asyncio.sleep(POLL_INTERVAL * 4)
                continue
        else:
            settings = {"enabled": True}

        # Drop finished tasks from the in-flight list.
        in_flight = [t for t in in_flight if not t.done()]

        # Provider-level halt: the proxy PLAN is out of quota/credit, so every account we
        # claim would burn its retry budget on 402/422 and be released 15 minutes cold for
        # nothing. Stop claiming until a human tops the plan up.
        halt_left = provider_halt_remaining()
        if halt_left > 0:
            if not in_flight:
                logger.warning(f"[Cycle {cycle}] Provider halt active — {halt_left/60:.1f} min left. "
                               f"Not claiming accounts (proxy plan out of quota).")
                await asyncio.sleep(min(POLL_INTERVAL * 4, halt_left))
                continue

        # Top up to CONCURRENCY by claiming fresh accounts.
        claimed_this_cycle = 0
        # ONLY_USERNAMES pins a run to named accounts. The pool mixes adults with 11 identities
        # registered on the site as children — profile.jsp reports bdays from 2015 to 2023 — and
        # the reference implementation rejects an application outright on "inferior a 15 anos".
        # A result from a randomly claimed account cannot distinguish "our changes did not work"
        # from "this applicant is nine years old".
        # The skip budget is what keeps this a filter rather than a spin: claim_account() hands
        # back whatever the pool offers, so without a bound a cycle in which no wanted account is
        # ever offered loops forever, re-claiming and releasing as fast as the dashboard answers.
        # A skip still costs a claim and a release, so the budget has to be small: at 40 per
        # ~20s cycle this filter generated ~120 dashboard round-trips a minute purely to look for
        # a name. Keep it to a handful, and remember across cycles which ids were already
        # rejected so the same unwanted accounts are not re-claimed forever.
        skips_left = 5 if ONLY_USERNAMES else 0
        while len(in_flight) < CONCURRENCY:
            account = claim_account()
            if not account:
                break
            if ONLY_USERNAMES and account.get('loginUsername', '') not in ONLY_USERNAMES:
                report_status(account.get('id'), 'NOT_LOGGED_IN')
                _rejected_ids.add(account.get('id'))
                skips_left -= 1
                if skips_left <= 0:
                    logger.info(f"[Cycle {cycle}] None of ONLY_USERNAMES offered in 5 claims "
                                f"({len(_rejected_ids)} skipped so far) — waiting for the next cycle")
                    break
                await asyncio.sleep(0.5)
                continue
            claimed_this_cycle += 1
            worker_id = len(in_flight) + 1
            task = asyncio.create_task(process_single_account(account, semaphore, worker_id))
            in_flight.append(task)
            await asyncio.sleep(random.uniform(0.5, 1.5))  # stagger launches

        if claimed_this_cycle:
            claimed_none_streak = 0
            logger.info(f"[Cycle {cycle}] Claimed {claimed_this_cycle} fresh account(s)")
        elif not in_flight:
            # Nothing running AND nothing to claim — genuinely idle. Report this clearly but
            # not on every single poll (that would spam the log at POLL_INTERVAL cadence).
            claimed_none_streak += 1
            if claimed_none_streak == 1 or claimed_none_streak % 12 == 0:
                logger.info(f"[Cycle {cycle}] No unclaimed accounts available — idling "
                            f"(checked {claimed_none_streak}x since last activity)")

        # Realtime status snapshot — every in-flight account's current stage, updated by
        # every single _report() call (see VisaAPIBot._report / format_status_table).
        if in_flight:
            logger.info(f"[Cycle {cycle}] {len(in_flight)} account(s) in flight:\n{format_status_table()}")

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted; exiting")

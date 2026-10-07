"""LiveEngineOps — the live-browser EngineOps implementation, backed by libs/engine/vendor's proven
PlaywrightSession (113 confirmed real bookings, the anti-detection moat -- never modify that module,
only call into it).

Mirrors apps/worker/ops_sim.py's shape and constructor-injected-determinism philosophy, but talks to
the real portal. Constructor signature confirmed verbatim by the intact recovery probe
(_recovery/storm-recovery/source-fragments/probe_getperiodos.py, which imports and calls this exact
class). URLs, payload shapes, CSRF extraction, and the booking-confirmation classification below are
ported from storm-auto's proven app/core/batch_apply.py (confirmed read in full for the relevant
sections) -- the parts specific to storm-hub's architecture (checkpoint-first login, the confirmed
booking bug fixes, event-rate-safe polling) are new, built on top of that proven foundation.

Every method here is LIVE-UNVERIFIED in the sense the project's own hard constraint defines: only a
real, user-run booking against the live portal can confirm it end-to-end (see Phase E, tier 2 -- this
plan never attempts that autonomously). What CAN be said with confidence: every URL, payload field, and
classification rule below traces to either a confirmed-working reference implementation or an intact
diagnostic script, not invention.

Every get_session() call site passes fp_seed=<the sub-account's portal username> (storm-auto's own
convention, e.g. app/worker.py's `get_session(login_proxy, fp_seed=_user)`), so one account always
presents the SAME browser fingerprint across fresh logins, checkpoint restores, and hold-swaps.
Regression this closes: fp_seed was previously never passed anywhere, so a checkpoint restore
injected an authenticated cookie jar into a browser with a brand-new RANDOM fingerprint every time --
a fingerprint mismatch against the identity that earned those cookies is a first-order detection
signal, on exactly the paths (respawn, keepalive-restore, hold-swap) this module exists to make
robust.
"""
from __future__ import annotations

from libs.domain.enums import NOT_BOOKABLE_PREFIX

import json
import random
import re
import time
from enum import Enum
from urllib.parse import urlencode
from datetime import date

from libs.bus.portal_liveness import PortalLiveness
from libs.bus.semaphore import RedisSemaphore
from libs.engine import checkpoint as ckpt
from libs.engine.config import EngineConfig
from libs.engine.portal_health import PortalHealth, classify_failure
from libs.engine.discovery.gettime import (
    GettimeStatus,
    classify_gettime_body,
    parse_special_days,
    special_days_from_json,
    special_days_to_json,
)
from libs.engine.discovery.periodos import parse_periodos_ocupados
from libs.engine.payload import booking_payload, schedule_controller_payload
from libs.engine.phases import EngineOps
from libs.engine.vendor import get_session

from .bus_signals import INACTIVATE, TIMEOUT

BASE = "https://pedidodevistos.mne.gov.pt/VistosOnline"
_PORTAL_ORIGIN = "https://pedidodevistos.mne.gov.pt"
HOME_URL = BASE + "/"
AUTH_URL = BASE + "/Authentication.jsp"
QUEST_URL = BASE + "/Questionario"
QUEST_NEXT = BASE + "/QuestNextQuestion"
FORMULARIO = BASE + "/Formulario"
SCHED_CTRL = BASE + "/ScheduleController"
SCHED_JSP = BASE + "/Schedule.jsp"
GETTIME_URL = BASE + "/gettime"
GETPERIODOS_URL = BASE + "/getPeriodosOcupados"
SLOTS_URL = BASE + "/slots"
# Substring the in-page interceptor matches a request URL against. Deliberately the PATH, not the
# full URL: the page issues /slots with a posto_id query string, and may issue it relative.
_SLOTS_PATH_MARK = "/slots"


# What /slots actually answered, as four outcomes that need four DIFFERENT responses. Every one of
# these used to be `failed`, which routed all of them into the re-warm ladder -- a treatment that
# fixes none of them. MEASURED on the 2026-09-02 17:15 run of "Test 0901", where two scouts polled
# the same endpoint seconds apart for the same dates:
#   * scout 88d2046d alternated strictly reject/success/reject/success, successes 99s and 117s
#     apart -> the endpoint grants roughly ONE real response per ~100s per session and answers
#     anything sooner with {"error": ReCaptchaError"}. Nothing is wrong with the session.
#   * scout 28b8fe7f returned {"data":{}} SIX times out of six and never once got data, having also
#     gone 8-for-8 the same way earlier that day. Its form state was fine -- it re-warmed
#     successfully four times -- and a re-warm never once helped. In the 04:22 run the same worker
#     recovered the instant it re-logged in.
# The two are told apart by the response, not by guesswork: the empty answer carries a doubled
# content-type (application/octet-stream, application/js) that the real one never has, the rate
# limit comes back as application/json, and a real array arrives with no content-type at all.
SLOTS_OK = "ok"
SLOTS_RATE_LIMITED = "rate_limited"    # token refused; wait, do NOT touch the session
SLOTS_SESSION_BURNED = "session_burned"  # 200 + {"data":{}}; only a fresh LOGIN clears it
SLOTS_BLOCKED = "blocked"              # 4xx/HTML; this exit is done
SLOTS_UNKNOWN = "unknown"


def _classify_slots_response(res: dict) -> str:
    """Read the intercepted response first and the page residue only as a fallback.

    Order matters: the block check comes before the body checks because a 403 can carry any body,
    and ReCaptchaError before the empty-object check because a rejected token is a statement about
    the REQUEST while the empty object is a statement about the SESSION.
    """
    probe = res.get("probe") if isinstance(res, dict) else None
    body = ""
    status = None
    if isinstance(probe, dict):
        status = probe.get("status")
        body = str(probe.get("body") or "")
    if not body:
        body = str(res.get("raw") or "")          # residue fallback when nothing was captured
    try:
        if status is not None and int(status) >= 400:
            return SLOTS_BLOCKED
    except (TypeError, ValueError):
        pass
    if "ReCaptchaError" in body or "recaptcha" in body.lower():
        return SLOTS_RATE_LIMITED
    if '"data"' in body.replace(" ", ""):
        # {"data":{}} -- a 200 whose body announces no payload at all.
        return SLOTS_SESSION_BURNED
    if res.get("ok"):
        return SLOTS_OK
    return SLOTS_UNKNOWN


def _fmt_slots_probe(probe) -> str:
    """Render the intercepted /slots response for the log in one line.

    This exists because `{"data":{}}` in window.data is not a diagnosis. A 200 carrying an empty
    object, a 429, a 403, a DataDome interstitial and a session redirect all leave the same residue
    there, and they call for opposite responses -- wait, rotate the exit, re-login. MEASURED on the
    2026-09-01 23:18 run of "Test 0901": 206 of 214 delivered tokens produced that residue, and
    nothing recorded which of those five it was. The body head is included (600 chars, captured
    in-page) because for the HTML answers -- the interstitial and the login redirect -- the status
    code alone is frequently 200 and only the body distinguishes them.
    """
    if not isinstance(probe, dict):
        # Not a failure in itself: the page may have answered from cache, or the request may have
        # been issued before our hook existed. Distinguished from a captured error so the two are
        # never conflated in the logs.
        return "<no response captured>"
    if probe.get("err"):
        return f"<network error via {probe.get('via')}: {str(probe['err'])[:120]}>"
    body = str(probe.get("body") or "")
    return (f"HTTP {probe.get('status')} via {probe.get('via')} "
            f"ct={str(probe.get('ct') or '')[:40]!r} len={probe.get('len')} "
            f"body={body[:220]!r}")
SUBMIT_URL = BASE + "/SubmeterVistoCriaPDF"

_CSRF_RE = re.compile(
    r'name=["\']__RequestVerificationToken["\'][^>]*value=["\']([^"\']+)["\']', re.I
)
_CSRF_RE_ALT = re.compile(
    r'value=["\']([^"\']+)["\'][^>]*name=["\']__RequestVerificationToken["\']', re.I
)

# Known error/bounce markers, ported verbatim from the confirmed classification (storm-auto
# batch_apply.py apply_book) -- see do_apply()'s docstring for the full reasoning.
_ERROR_KEYWORDS = ("indisponivel", "unavailable", "bd_problm")
# storm-auto's confirmed two-flavour bounce, read verbatim from batch_apply.apply_book (the code
# behind 113 real bookings). BOTH flavours are "bounced_back"; what separates them is WHERE:
#   (a) back to the schedule/questionario FORM (questionario / getslots / captchadiv / auth page)
#       = the slot was taken, or the submit was rejected mid-flight. Session is FINE.
#       -> "no_slot": soft-release and try a different slot.
#   (b) all the way out to the logged-in HOME dashboard = the booking form-state expired. The slot
#       is NOT gone, our context is. -> "form_stale": release without penalty and RE-WARM.
#
# I had this exactly backwards: I classified `questionario` as form_stale, which is flavour (a) in
# storm-auto. That is why worker e5dfc1b1 clung to one slot and paid five full login+warmup cycles
# on it instead of soft-releasing and moving to the next slot.
_BOUNCE_KEYWORDS = ("questionario", "getslots", "captchadiv", "authentication.jsp")
# Flavour (b) is identified positively -- by the home dashboard's own nav text, or by the portal
# having redirected us to the bare site root -- AND by the absence of any schedule marker. Never by
# a single keyword.
#
# storm-auto matches only the PORTUGUESE nav text. This PT-version branch now sends lang=PT through
# the WHOLE flow (home-lang POST, ScheduleController, and the SubmeterVistoCriaPDF submit -- confirmed
# against the completed-booking HAR, post id 3059 france), so the dashboard renders Portuguese and
# these markers actually match -- which is the whole point of moving off ENG. The site-root fallback
# below is KEPT as belt-and-suspenders: on the old ENG flow, confirmed live 2026-08-14, 20 of 24
# bounces landed on the bare /VistosOnline/ root and only 6 matched the Portuguese markers, so a page
# the portal served AT its own root is still accepted as the dashboard whatever language it rendered.
#
# So the final URL is accepted as equivalent evidence: a page the portal served AT its own site root
# IS the dashboard, whatever language it rendered in. The schedule-marker veto below stays absolute
# in both cases -- a page carrying cmbPeriodo/getSlots is the schedule form, not the dashboard, and
# must remain no_slot however it was reached.
# The scheduling page's own markers, verbatim from storm-auto's _sched_markers. Their ABSENCE from
# Schedule.jsp means ScheduleController was silently rejected and there is no form state to book
# against -- see _run_warmup_steps.
_SCHED_PAGE_MARKERS = ("getSlots", "captchaDiv", "SubmeterVistoCriaPDF", "f_date_c", "cmbPeriodo")

# The only values do_discover accepts for DISCOVERY_SOURCE. Lives here rather than in the settings
# catalog because that framework has no enum type -- so the dashboard renders the setting as free
# text and the worker is the only place that can reject a typo. See the setting's own note.
_DISCOVERY_SOURCES = frozenset({"calendar", "slots", "both"})
# The portal's own "come back later" page. It is served in either language and is NOT about the
# slot, the session or the network -- the portal simply declined to process the request.
#
# CONFIRMED 2026-08-23 by size correlation across three different endpoints in one run:
#   28527B  the login auth-redirect, carrying this exact text
#   28506B  a Schedule.jsp GET ("200 but no scheduling markers")
#   28529B  a SubmeterVistoCriaPDF submit response (final_url WAS the submit endpoint)
# The same refusal page answers login attempts and booking submits alike.
_PORTAL_BUSY_MARKERS = (
    "não foi possível de momento concretizar o seu pedido",
    "nao foi possivel de momento concretizar o seu pedido",
    "we were unable to complete your request at this time",
)

# The portal's MAINTENANCE / 'Serviço Indisponível' page. SPLIT OUT of _PORTAL_BUSY_MARKERS
# 2026-09-23: it was lumped in as "same class as the refusals above", but it is NOT the same class.
# The 'try again later' throttle is ambiguous -- an account-level block returns it from every exit,
# so it is per-account evidence as often as fleet-wide (CONFIRMED 2026-09-01: 11 throttle pages
# coexisted with 4 successful logins in one bucket). The maintenance page is unambiguous: the portal
# is explicitly down for EVERYONE, saying nothing about our session, proxy or account. Rolling exits
# or rotating sub-accounts against it only burns the pool (booking "Test 2032": 154 refusals across
# 13 sub-accounts and 99 rotations). So the two route differently -- maintenance marks the portal
# DOWN on the dashboard (PortalLiveness.record_down) and backs THIS worker off for a cooldown, while
# the throttle is handled purely per-worker (roll exit) and never touches liveness. Accent-stripped
# variants included because the portal serves both.
_MAINTENANCE_MARKERS = (
    "serviço em manutenção",
    "servico em manutencao",
    "service under maintenance",
    "serviço indisponível",
    "servico indisponivel",
    "serviço indisponível / unavailable",
    "service unavailable. please visit again later",
)


# The portal's permanent refusal: this applicant already holds a future appointment.
_ALREADY_BOOKED_MARKERS = (
    "already has an application registered",
    "already has an application",
    "já tem um pedido registado",
    "ja tem um pedido registado",
)


def _classify_gettime_page(html: str) -> str:
    """Name the page gettime answered with, so a rejection is actionable rather than a mystery."""
    if _is_challenge_body(html):
        return "waf_challenge"
    if _is_maintenance(html):
        return "portal_maintenance"
    if _is_portal_busy(html):
        return "portal_try_again_later"
    low = (html or "").lower()
    if "authentication.jsp" in low or "/vistosonline/login" in low:   # quote-agnostic
        return "auth_redirect"
    return "unrecognised"


# The portal's reCAPTCHA verdict, and its own statement of the penalty:
#
#   "o sistema apenas permite falhar o processo de ReCaptcha 2 vezes. Se tentar executar este
#    processo mais vezes no periodo de tempo definido o seu acesso a plataforma de E-VISA sera
#    bloqueado"
#
# Two failures allowed; more within the window and PLATFORM ACCESS IS BLOCKED. The registrar has
# respected this since 2026-08-21; the login path did not, and spent its full sticky budget walking
# an account straight to the edge of that limit. Measured 2026-08-23: 34 of these.
_RECAPTCHA_VERDICT_MARKERS = (
    "por favor, confirma o recaptcha",
    "confirma o recaptcha",
    "recaptchaerror",
    "apenas permite falhar o processo de recaptcha",
    "tentativas",
)


def _is_recaptcha_verdict(reason: str) -> bool:
    """True when the portal rejected our CAPTCHA specifically (not the credentials, not the WAF)."""
    low = (reason or "").lower()
    return any(m in low for m in _RECAPTCHA_VERDICT_MARKERS)


def _is_portal_busy(html: str) -> bool:
    """True when the portal answered with its 'try again later' login throttle (AMBIGUOUS: this is
    per-account as often as fleet-wide -- see _MAINTENANCE_MARKERS). Not the maintenance page."""
    low = (html or "").lower()
    return any(m in low for m in _PORTAL_BUSY_MARKERS)


def _is_maintenance(html: str) -> bool:
    """True when the portal served its explicit maintenance / 'Serviço Indisponível' page -- down
    for everyone, a strong fleet-wide signal."""
    low = (html or "").lower()
    return any(m in low for m in _MAINTENANCE_MARKERS)


def _is_portal_refusal(html: str) -> bool:
    """True when the portal declined to serve us for EITHER reason (throttle or maintenance). Use
    this where the caller only needs 'the portal is not serving right now' and does not route the
    two differently -- keepalive/warmup/gettime classification. The login path, which DOES route
    them differently, calls _is_maintenance() and _is_portal_busy() separately."""
    return _is_portal_busy(html) or _is_maintenance(html)
# Fingerprints of an interstitial served INSTEAD of the page we asked for. bd.js is DataDome's
# loader; the noindex/nofollow meta plus an empty styled body is its challenge shell.
_CHALLENGE_MARKERS = ("/ch/bd.js", "captcha-delivery", "datadome",
                      'name="robots" content="noindex,nofollow"')


def _is_challenge_body(html: str) -> bool:
    """True when a response is a bot-challenge interstitial rather than the requested page."""
    low = (html or "").lower()
    return any(m.lower() in low for m in _CHALLENGE_MARKERS)
_HOME_MARKERS = ("solicitar pedido de visto", "consultar pedidos")
_SCHEDULE_MARKERS = ("getperiodosocupados", "cmbperiodo", "getslots")


def _is_home_url(final_url: str) -> bool:
    """True only for the portal's bare site root (…/VistosOnline or …/VistosOnline/), never for a
    deeper path -- SubmeterVistoCriaPDF, Schedule.jsp and friends all say something different."""
    path = (final_url or "").split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return path.endswith("/VistosOnline")


class SessionState(str, Enum):
    """What a keepalive probe's response actually says about the session -- not "did the
    navigation raise" (that's a transport failure and stays a separate, pre-existing branch in
    _do_keepalive) but "is the page we got back proof of a live session, proof of a dead one,
    evidence the portal is refusing everyone, or none of the above."

    docs/engineering/FAILURE_MODES.md F21-F26 are six symptoms of one cause: this evidence was
    already being fetched (the page IS read, or -- pre-warm -- was one `browser_eval` away from
    being read) and then discarded, so a logged-out home page read as healthy (F23), a lapsed
    Schedule.jsp read as "form-state lapsed" whether the real cause was a lapsed form OR a dead
    session (F24), a busy portal read the same way (F25), and no response ever had a name for
    "none of the above, we don't actually know" (F26)."""
    AUTHENTICATED = "authenticated"
    SESSION_EXPIRED = "session_expired"
    PORTAL_ERROR = "portal_error"
    UNKNOWN = "unknown"


def classify_session_state(page: str, final_url: str, *, warmed: bool) -> SessionState:
    """Classify a keepalive probe's response into a SessionState.

    `warmed` selects which page was actually requested -- Schedule.jsp once posto_pdf is set,
    the bare home page before that -- since the positive evidence for "authenticated" differs
    per page and the same page content must not be judged the same way in both cases.

    Order matters. sessionLost.jsp/Authentication.jsp (an explicit expiry signal, F22) and a
    portal-busy "try again later" page (F25) are both checked BEFORE either page-specific marker
    check, so a busy or genuinely-expired portal is never misread as a lapsed form just because it
    also happens to be missing the markers a healthy page would carry.

    Absence of a positive marker is UNKNOWN, never promoted to SESSION_EXPIRED on its own (F26) --
    the one exception is the home page's own measured discriminator (CONFIRMED_FACTS.md §24):
    the logged-out home carries the public menu item but not the auth-gated one, which IS a
    positive (if page-specific) signal of expiry, not a mere absence.
    """
    url_low = (final_url or "").lower()
    if "sessionlost" in url_low or "authentication.jsp" in url_low:
        return SessionState.SESSION_EXPIRED
    if _is_portal_refusal(page):
        return SessionState.PORTAL_ERROR
    # A WAF/bot-challenge interstitial served INSTEAD of the page is the portal declining our
    # request, NOT a dead session -- exactly like _is_portal_busy above (the apply/warmup paths
    # already treat it that way via _is_challenge_body; the keepalive was the one classifier blind
    # to it, so a challenge fell through to UNKNOWN and the worker sat in limbo). Back off, don't
    # re-authenticate: re-logging in cannot pass a WAF that is blocking the exit/session.
    if _is_challenge_body(page):
        return SessionState.PORTAL_ERROR
    if warmed:
        if any(m in page for m in _SCHED_PAGE_MARKERS):
            return SessionState.AUTHENTICATED
        return SessionState.UNKNOWN
    low = (page or "").lower()
    has_public = _HOME_MARKERS[0] in low        # "solicitar pedido de visto" -- present either way
    has_auth_gated = _HOME_MARKERS[1] in low    # "consultar pedidos" -- present only when authenticated
    if has_public and has_auth_gated:
        return SessionState.AUTHENTICATED
    if has_public and not has_auth_gated:
        return SessionState.SESSION_EXPIRED
    return SessionState.UNKNOWN

# storm-auto bounds its equivalent restore<->re-warm chain with MAX_WARMUP_RESTORES=3 ("a session
# whose re-warm keeps bouncing to home can't churn fresh proxies forever"). Mirrored here as the
# keepalive-restore retry budget once a worker enters the "session is dead" state.
_MAX_KEEPALIVE_RESTORE_ATTEMPTS = 3

# storm-auto's QUEST_STEP_RETRIES (default 3): re-attempt a questionnaire step that doesn't return
# 200, "like a manual page refresh". Not a settings-catalog knob -- it is a retry shape, and the
# proven value is the one that matters.
_QUEST_STEP_RETRIES = 3


def _proxy_label(conn_url: str | None) -> str:
    """host:port of a proxy, credentials stripped -- safe to log.

    Every login attempt records which proxy it used. Without it a failure cannot be attributed to a
    proxy vs an account vs the portal, which is exactly the ambiguity behind "why is one role's
    login rate worse" -- the roles run identical code with identical accounts, so proxy draw is the
    remaining variable and it was unobservable.

    NOTE the endpoint alone is NOT an identity. Every webshare credential we own points at the same
    gateway, so this used to print `proxy=p.webshare.io:80` for every worker in every run -- exactly
    the attribution the docstring above claims it provides, and it provided none of it. The sticky
    SESSION ID is what says which exit a worker is actually on (hold it to keep the exit, change it
    to get another), so it belongs in the label."""
    if not conn_url:
        return "proxy=none"
    label = "proxy=" + conn_url.rsplit("@", 1)[-1]
    try:
        from libs.proxies.lease import session_id
        sid = session_id(conn_url)
    except Exception:
        sid = ""
    return f"{label}#{sid}" if sid else label


def _submit_bounce_reason(html: str) -> str:
    """Why _classify_submit_response rejected a SubmeterVistoCriaPDF response.

    Kept separate from the classifier so that function's booked/bounced contract (and its tests)
    stay untouched -- this only explains the verdict, and is only ever called on the failure path."""
    low = (html or "").lower()
    if not low.strip():
        return "empty response body"
    hits = [k for k in _BOUNCE_KEYWORDS if k in low]
    errs = [k for k in _ERROR_KEYWORDS if k in low]
    if errs:
        return f"portal error marker(s) {errs}"
    if hits:
        # Which flavour of bounce this is decides the whole recovery (re-warm vs try another slot),
        # and it hinges on markers we otherwise never see. Confirmed live 2026-08-14: 20 of 24
        # bounces landed on the bare /VistosOnline/ root -- the home dashboard -- but only 6 were
        # detected as such, and with nothing logged there was no way to tell why the other 14
        # weren't. Spell out both halves of the home-dashboard test on every bounce.
        home = [m for m in _HOME_MARKERS if m in low]
        sched = [m for m in _SCHEDULE_MARKERS if m in low]
        title = re.search(r"<title[^>]*>(.*?)</title>", low, re.S)
        return (f"bounced back to {hits} (session/form state lost, or the slot was taken) "
                f"| home_markers={home or 'none'} schedule_markers={sched or 'none'} "
                f"| {len(html)}B title={(title.group(1).strip()[:60] if title else '?')!r}")
    if "mostrarpdf" not in low:
        return f"no MostrarPdf confirmation form; {len(html)}B response, head={html[:160]!r}"
    return f"unclassified; {len(html)}B response, head={html[:160]!r}"


def _login_reject_reason(result: object) -> str:
    """The portal's rejection reason, WITHOUT the CAPTCHA token.

    browser_login returns the solver's `captcha_token` alongside the response, and this used to be
    logged by dumping the whole dict -- several hundred characters of base64 per attempt, repeated
    once per retry. Confirmed 2026-08-14: the dashboard's log panel was unreadable, the actual
    reason ("datadome_challenge_on_post") buried mid-blob, and every one of those lines also went
    into the shared Redis event stream, aging real history out of the capped window that much
    faster."""
    if not isinstance(result, dict):
        return str(result)[:120]
    body = (result.get("body") or "").strip()
    reason = body[:160]
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        pass
    else:
        if isinstance(parsed, dict):
            reason = parsed.get("description") or parsed.get("type") or reason
    return f"status={result.get('status')} {reason}"


def _extract_csrf(html: str) -> str | None:
    m = _CSRF_RE.search(html or "")
    if m:
        return m.group(1)
    m = _CSRF_RE_ALT.search(html or "")
    return m.group(1) if m else None


# Portuguese validation vocabulary the portal uses when it re-renders a rejected form.
_ERR_WORDS = ("obrigat", "inválid", "invalid", "erro", "preencha", "não é válid",
              "deve ser", "incorrect", "incorret")
_ERR_TAG_RE = re.compile(
    r"<(?:span|div|li|p|td)[^>]*(?:class|id)\s*=\s*[\"'][^\"']*(?:erro|error|alert|msg)[^\"']*[\"'][^>]*>(.*?)</",
    re.I | re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")

# The posto dropdown on the Formulario page. `f0sf1` is the field ScheduleController receives the
# posto in, so the options rendered here are the definitive list of postos the portal will accept
# for the residence the questionnaire just declared.
_POSTO_SELECT_RE = re.compile(
    r"<select[^>]*\bname\s*=\s*[\"']f0sf1[\"'][^>]*>(.*?)</select>", re.I | re.S)
_OPTION_RE = re.compile(r"<option[^>]*\bvalue\s*=\s*[\"']([^\"']*)[\"'][^>]*>(.*?)</option>", re.I | re.S)


def _posto_options(form_html: str) -> list[tuple[str, str]]:
    """(value, label) pairs from the Formulario page's posto dropdown, or [] if it isn't there.

    Returning [] for "couldn't find the select" is deliberate: the caller must not treat an unparsed
    page as evidence the posto is invalid. Only a dropdown we actually found can prove that.
    """
    m = _POSTO_SELECT_RE.search(form_html or "")
    if not m:
        return []
    out = []
    for value, label in _OPTION_RE.findall(m.group(1)):
        value = value.strip()
        if value:
            out.append((value, " ".join(_TAG_RE.sub(" ", label).split())[:60]))
    return out


def _form_error_text(html: str, limit: int = 5) -> list[str]:
    """Pull the validation messages out of a form page the portal re-rendered after rejecting it.

    ScheduleController answers 200 either way; the ONLY thing distinguishing acceptance from
    rejection was the response SIZE (~54KB accepted vs ~1.27MB rejected -- perfectly bimodal across
    17 live warm-ups on 2026-08-20). The body was fetched and discarded, so a deterministic,
    per-identity rejection looked like an unexplained "ScheduleController was rejected".

    Deliberately text-only and capped: this goes into the event stream, which is a capped Redis
    stream shared by every worker -- dumping a 1.27MB body would age out real history in seconds.
    """
    if not html:
        return []
    body = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    out: list[str] = []
    seen: set[str] = set()
    for chunk in _ERR_TAG_RE.findall(body):
        txt = " ".join(_TAG_RE.sub(" ", chunk).split())
        if 3 < len(txt) <= 200 and txt.lower() not in seen:
            seen.add(txt.lower())
            out.append(txt)
            if len(out) >= limit:
                return out
    if out:
        return out
    # No error-classed element matched -- fall back to any line carrying the vocabulary.
    for line in _TAG_RE.sub("\n", body).splitlines():
        txt = " ".join(line.split())
        low = txt.lower()
        if 3 < len(txt) <= 200 and any(w in low for w in _ERR_WORDS) and low not in seen:
            seen.add(low)
            out.append(txt)
            if len(out) >= limit:
                break
    return out


def _kv(fields: dict) -> str:
    """Render an ordered dict as a `key=value` log line (Phase-K structured logging, 2026-10-01).
    Values with a space, quote or `=` are wrapped in double quotes (inner double-quotes downgraded to
    single) so the line stays unambiguously parseable; None renders empty. Never pass credentials."""
    parts = []
    for k, v in fields.items():
        s = "" if v is None else str(v)
        if any(c in s for c in ' "='):
            s = '"' + s.replace('"', "'") + '"'
        parts.append(f"{k}={s}")
    return " ".join(parts)


class LiveEngineOps(EngineOps):
    def __init__(
        self,
        cfg: EngineConfig,
        *,
        account: dict,
        posto_id: str,
        nationality: str,
        residence: str,
        quest_steps: dict,
        form_defaults: dict,
        capsolver_keys: list[str],
        warm_proxy: str,
        proxy_geos: list[str] | None = None,
        headless: bool = True,
        sub_account_id: str,
        accounts: list[dict] | None = None,
        signals=None,
        emit=None,
        redis_client=None,
        booking_id: str | None = None,
        identity_id: str | None = None,
        hold_proxy: str | None = None,
        hold_proxy_static: bool = False,
    ):
        self.cfg = cfg
        self.account = account
        self.posto_id = posto_id
        self.nationality = nationality
        self.residence = residence
        self.quest_steps = quest_steps
        self.form_defaults = form_defaults
        self.capsolver_keys = capsolver_keys
        self.warm_proxy = warm_proxy
        # Exit countries this (rotating) worker varies across on each roll, so it is never bound to
        # one possibly-throttled geo for the whole run (2026-10-01). {pin} if the operator pinned a
        # single geo, else the booking's whole PROXY_GEOS set; None/empty = no geo constraint.
        self._proxy_geos = [g.strip().upper() for g in (proxy_geos or []) if g and g.strip()]
        # phase-2 (hold/apply/discovery) proxy -- only used when HOLD_SWAP_AFTER_LOGIN is on
        # (do_swap() below). None is the correct/expected value when swap is off; the Hub only
        # leases and passes this when the setting is actually enabled (apps/hub/main.py).
        self.hold_proxy = hold_proxy
        # Phase 5: whether self.hold_proxy is a resource_type='static' resource -- a plain runtime
        # fact from the launch payload (apps/hub/main.py sets it from whether a hold proxy was leased
        # at all, since one is only ever leased static as of this phase), NOT a DB lookup (D1: this
        # worker never touches Postgres). Updated again on a successful mid-hold rotation by
        # _request_proxy_rotation. Used ONLY to decide whether do_swap()'s retry ladder may locally
        # roll the credential (roll_sticky_session) or must request a fresh lease through the Hub
        # instead -- never consulted by anything session-state-related (classify_session_state stays
        # completely proxy-blind, by design).
        self.hold_proxy_static = hold_proxy_static
        self.headless = headless
        self.sub_account_id = sub_account_id
        # Sub-account rotation candidates, ranked best-first by the Hub (_rank_live_sub_accounts) --
        # do_login() rotates through these once the sticky-retry budget on the current one is
        # exhausted (operator spec: sticky=2, rotating=len(accounts)). Falls back to the single
        # `account` dict when the Hub didn't provide a list (sim/older launch payload), so rotation
        # is simply a no-op (one candidate) rather than a missing-attribute error.
        self.accounts = accounts or [dict(account, sub_account_id=sub_account_id)]
        self._signals = signals
        self._emit = emit
        self._log = lambda t: None   # overridable, matching the confirmed probe's `ops._log = ...`

        # identity_id is what checkpoints are keyed by (bug fix #5) -- fall back to sub_account_id
        # only if the caller genuinely has no identity_id yet (should not happen in production; the
        # Hub always knows the identity a worker is bound to).
        self.identity_id = identity_id or sub_account_id
        self.booking_id = booking_id

        self.session = None
        self.posto_pdf: str | None = None
        # HOW and WHEN the booking anchor was established. `posto_pdf` alone cannot answer either,
        # and it means two different things depending on which of its two writers set it:
        #   _run_warmup_steps  -- parsed out of a Schedule.jsp we just rebuilt: anchor is FRESH.
        #   a checkpoint       -- copied from a file: the anchor may be minutes or hours old, and
        #                         nothing on this session has verified it.
        # The ScheduleController form state has a ~90-120s TTL (measured; see do_apply), so "is it
        # set" was never the right question -- "how old, and did we build it" is.
        self._anchor_src: str | None = None      # "chain" | "checkpoint" | None
        self._anchor_at: float | None = None     # monotonic stamp; None = age unknown
        self._restore_stage: str | None = None   # "alive" | "rewarmed" | "none", set by do_login
        # Rolling view of how the portal is behaving FOR THIS WORKER: per-phase durations plus
        # consecutive slow/unreachable streaks. Lets a slow window be waited out instead of
        # escalated into re-logins, which cost CAPTCHA solves and sub-account rate limit.
        self._health = PortalHealth()
        # Hub's shared gettime cache (special_days), stashed by await_discover_task() from the
        # discover command payload, consumed once by the next do_discover() call.
        self._pending_special_days: dict | None = None
        # DISCOVERY_SOURCE pushed on the discover command (Hub-live value), consumed by do_discover
        # so a booking-setting change reaches a RUNNING scout without a restart. None = use frozen cfg.
        self._pending_discover_source: str | None = None

        # P3 -- proxy rotation: which pool a rotation request should draw from. do_swap() flips this
        # to "hold" unconditionally the moment warmup finishes, regardless of whether
        # HOLD_SWAP_AFTER_LOGIN is actually on (see do_swap()'s docstring and
        # _request_proxy_rotation()) -- the operator's explicit requirement that pool selection "will
        # not be different when swap is off".
        self._current_phase = "warmup"

        self._login_sem: RedisSemaphore | None = None
        self._apply_sem: RedisSemaphore | None = None
        self._pool_sem: RedisSemaphore | None = None
        self._liveness: PortalLiveness | None = None
        # Set by _sticky_login_attempts when the portal serves its maintenance page, read by
        # _fresh_login to abandon sub-account rotation (down for everyone -- see the login path).
        self._saw_maintenance: bool = False
        # Last _run_warmup_steps exception message; lets do_apply distinguish a dead-exit re-warm
        # failure (roll the exit) from a portal rejection (keep it). See _run_warmup_steps.
        self._warmup_last_error: str = ""
        # Set when the portal says this applicant ALREADY HAS a request ("Este requerente já tem um
        # pedido registado" -- a JS alert). The identity is permanently unusable for this run (a
        # competitor booked it, or it applied within the last 5 days), so the worker must STOP and
        # the identity be retired, not retried forever. Read by the phase machine via
        # identity_unavailable().
        self._identity_unavailable: bool = False
        # /slots pacing + recovery state (2026-09-02). _slots_backoff_until gates the next attempt
        # so we stop buying tokens the endpoint will refuse; _slots_needs_relogin tells phases.py
        # that the ladder's cheap rung (re-warm) cannot help this failure.
        self._slots_backoff_until: float = 0.0
        # SLOTS_PRESOLVE cache: a (token, solved_at) pair solved during the rate-limit wait so the
        # next poll fires without paying the solve latency. Consumed once, and only while fresh
        # (age < SLOTS_TOKEN_TTL, kept under reCAPTCHA's ~120s expiry). None = nothing cached.
        self._presolved_slots: tuple[str, float] | None = None
        self._slots_last_grant_at: float = 0.0
        self._slots_needs_relogin: bool = False
        # Consecutive same-exit failures that mean the EXIT is flagged, not the request/session:
        # a WAF interstitial (page never rendered the widget) and a ReCaptchaError (token refused).
        # Login already rolls its exit on the identical reCAPTCHA verdict (token_ip_mismatch_burns_
        # accounts); discovery historically stayed put and re-ran the same flagged exit round after
        # round (confirmed live 2026-09-25, booking 0924-3018: 6/9 interstitials + repeated
        # ReCaptchaError 9 min apart, well past the rate-limit window, 0 real slots). Roll on the
        # SECOND consecutive so the first keeps today's cheaper behaviour (backoff / skip). A real
        # grant, a fresh login, or any exit roll resets the streaks.
        self._slots_interstitial_streak: int = 0
        self._slots_recaptcha_streak: int = 0
        # getTables handshake state (2026-09-23, ref-confirmed). getTables is a page-fired POST on
        # Schedule.jsp that the WAF wants confirmed ("success":"true") before it serves /slots -- but
        # it is POSTO-OPTIONAL (posto 5084 has none yet serves slots). _posto_gettables: None=unknown,
        # True=this posto fires it, False=proven not to (so we stop waiting). _gt_hook_session tracks
        # WHICH session object carries the capture hook, so a rebuilt session gets it re-installed.
        self._posto_gettables: bool | None = None
        self._gt_hook_session: object | None = None
        self._redis = redis_client   # for the shared login-token pool (consumer side)
        if redis_client is not None and booking_id:
            from libs.bus.semaphore import make_gate, make_provider_gate
            self._login_sem = make_gate(redis_client, booking_id, "login", cfg.raw.get("LOGIN_CONCURRENCY", 10))
            self._apply_sem = make_gate(redis_client, booking_id, "apply", cfg.raw.get("APPLY_CONCURRENCY", 100))
            # Host-wide pool gate, deliberately NOT scoped to this booking -- see
            # provider_semaphore_key. 0 disables it, which is the only way to opt out.
            _pool_limit = int(cfg.raw.get("PROVIDER_LOGIN_CONCURRENCY", 20) or 0)
            if _pool_limit > 0:
                self._pool_sem = make_provider_gate(
                    redis_client,
                    str(cfg.raw.get("PROXY_PROVIDER") or "soax"),
                    cfg.raw.get("PROXY_KIND") or None,
                    _pool_limit,
                )
            self._liveness = PortalLiveness(redis_client, booking_id)

    # ── shared warm-up ──────────────────────────────────────────────────────────
    def discovery_needs_relogin(self) -> bool:
        """True when the last /slots round returned the burned-session signature.

        The rebuild ladder tries a re-warm first and stops if it succeeds -- correct for a lapsed
        form, and useless here: on the 2026-09-02 17:15 run scout 28b8fe7f re-warmed successfully
        four times and still returned {"data":{}} six times out of six, because the form state was
        never the problem. Its counterpart that had re-logged in was getting real data from the same
        endpoint seconds apart.
        """
        return self._slots_needs_relogin

    def breaker_tripped(self) -> bool:
        """Always False. The booking breaker was REMOVED 2026-09-23 -- it gated the whole fleet and
        did more harm than good (froze all workers on a normal throttle burst, oscillated, starved
        discovery). This stub stays only so PhaseMachine._warmup's `getattr(ops, 'breaker_tripped')`
        keeps returning a callable that says "no gate", i.e. a failed do_login is a real login
        failure, never a wait. Portal-down is now shown to the operator via PortalLiveness, not acted
        on by freezing the fleet."""
        return False

    def do_login(self) -> bool:
        if self._login_sem is not None:
            if not self._login_sem.acquire(self.identity_id, timeout=300):
                self._log("do_login: login concurrency gate timed out")
                return False
        # The pool gate is acquired SECOND and released FIRST, so the two are strictly nested and
        # every worker takes them in the same order -- no deadlock is possible between them. A
        # failure here must still release the login slot, hence the try/finally around it rather
        # than a bare early return.
        try:
            if self._pool_sem is not None:
                if not self._pool_sem.acquire(self._pool_holder_id(), timeout=300):
                    self._log(f"do_login: pool concurrency gate ({self._pool_sem.key}) timed out")
                    return False
            try:
                if self._try_restore_from_checkpoint():
                    return True
                return self._fresh_login()
            finally:
                if self._pool_sem is not None:
                    self._pool_sem.release(self._pool_holder_id())
        finally:
            if self._login_sem is not None:
                self._login_sem.release(self.identity_id)

    def _pool_holder_id(self) -> str:
        """Holder id for the HOST-WIDE pool gate, so it must be unique across bookings.

        `identity_id` alone is not: the same identity can be assigned to more than one booking, and
        two of its workers logging in concurrently would look like a single holder to the sorted set
        -- the second would slip past a full gate, and the first to finish would release the other's
        slot.
        """
        return f"{self.booking_id or '-'}:{self.identity_id}"

    def _try_restore_from_checkpoint(self) -> bool:
        """3-tier restore chain (Phase C3): restore -> health-check -> re-warm -> give up (return
        False so do_login falls through to _fresh_login). Gated by the SAME login semaphore as fresh
        logins (Phase F12) so a mass relaunch can't fire N concurrent restores.

        BOUNDED, and self-cleaning. Confirmed live 2026-08-14: every tier is a real browser
        navigation, and with NAV_TIMEOUT_SCALE=4 a doomed restore burned 92s, 118s, 180s -- one took
        384s -- before the worker even STARTED its actual login. Worse, the chain never deleted a
        checkpoint that had just proved itself dead, so each respawn faithfully reloaded the same
        cookies and paid the same minutes again: CARLOS spent three consecutive restores, ERNESTO
        four, none of which produced a bookable session.

        Two guards:
          * a wall-clock budget (CHECKPOINT_RESTORE_BUDGET) -- once spent, abandon the chain and go
            log in, rather than starting another multi-minute tier;
          * on a failure that is about the SESSION rather than the network (it rebuilt fine but
            isn't bookable and won't re-warm), the checkpoint is deleted. A transient/proxy failure
            leaves it alone -- that says nothing about whether the cookies are still good.
        """
        data = ckpt.load_checkpoint(self.identity_id)
        if data is None:
            self._restore_stage = "none"
            return False

        # Anti-herd: stagger restore attempts (Phase F12 / the confirmed incident's plausible
        # trigger -- many workers restoring simultaneously after a checkpoint-restore event).
        jitter = float(self.cfg.raw.get("RESTORE_STAMPEDE_JITTER", 30.0))
        if jitter > 0:
            time.sleep(random.uniform(0, jitter))

        budget = float(self.cfg.raw.get("CHECKPOINT_RESTORE_BUDGET", 90.0))
        started = time.time()   # after the jitter: the anti-herd wait is not the chain's fault
        spent = lambda: time.time() - started   # noqa: E731

        self._log(f"restoring checkpoint for identity {self.identity_id} (sub_account={data.get('sub_account_id')})")
        try:
            # Same reassignment hazard as the login retry loop: anything already open here would be
            # orphaned rather than closed. Cheap to guard and correct whether or not a session exists.
            self._close_session_quietly()
            self.session = get_session(
                proxy=self.warm_proxy,
                inject_cookies=data.get("cookies"),
                inject_cookies_full=data.get("cookies_full"),
                headless=self.headless,
                fp_seed=self.account.get("username"),
            )
        except Exception as e:
            # The browser/proxy never came up -- this says nothing about the stored cookies, so the
            # checkpoint is kept for the next attempt.
            self._log(f"checkpoint restore: session rebuild failed after {spent():.0f}s ({e}) "
                      f"-- keeping checkpoint, falling back to fresh login")
            self._restore_stage = "none"
            return False

        if self._probe_alive():
            # POSTO GUARD (confirmed risk 2026-09-21 on "0921-3058", posto changed after a stop). A
            # checkpoint is keyed by IDENTITY and survives a posto change, and a bookable-stage one
            # PINS the posto it was warmed at via posto_pdf -- which then drives discovery (Schedule.jsp
            # / gettime) and the submit URL. Adopting it here SKIPS warmup, so a session warmed at the
            # OLD posto would silently discover and book against that old posto instead of this run's
            # target. Prefer the exact posto_id (stored on new checkpoints); fall back to posto_pdf for
            # legacy ones (it is the pinned posto). A login-stage checkpoint has neither set and is
            # posto-agnostic (it re-warms against the current posto), so it passes untouched.
            ckpt_posto = data.get("posto_id") or data.get("posto_pdf")
            if ckpt_posto is not None and str(ckpt_posto) != str(self.posto_id):
                self._log(f"checkpoint restore: warmed at posto {ckpt_posto} but this run targets "
                          f"{self.posto_id} -- stale after a posto change, discarding and re-warming")
                self._restore_stage = "none"
                return False
            # A checkpoint restore re-establishes an AUTHENTICATED session ONLY -- it is NOT warmed
            # and NOT bookable (operator, 2026-09-23: "regard every checkpoint restore as an authed
            # session only; don't think it has valid form_state and bookable"). The saved posto_pdf
            # is a file value never verified on THIS session, and the portal's form cursor does not
            # survive a restore -- KEEPING is an authed HOME session, not the Schedule page. So do
            # NOT adopt posto_pdf and do NOT set any anchor: leave the worker a plain authed KEEPING
            # session that must run the real questionnaire->ScheduleController->Schedule.jsp chain
            # before applying. Confirmed live on 'Prev 0923-3018 (copy)': adopting the file anchor and
            # skipping warmup made workers submit with a lapsed cursor -> form_stale -> re-login loop
            # (self-saved checkpoints, restored on re-login, restored the same dead form-state).
            self._clear_anchor()
            self._restore_stage = "none"
            self._log(f"checkpoint restore: session AUTHENTICATED after {spent():.0f}s "
                      f"-- authed-only (NOT warmed); will run the full warmup before applying")
            return True

        # Health-check failed. A re-warm is worth trying -- the session may still be authenticated
        # and merely missing form state, and re-warming is far cheaper than burning the login wall
        # and a CAPTCHA solve. But only if there is budget left for it.
        if budget > 0 and spent() >= budget:
            self._log(f"checkpoint restore: not bookable and {spent():.0f}s budget spent -- "
                      f"skipping re-warm, going straight to login")
        elif self._run_warmup_steps():
            self._restore_stage = "rewarmed"
            self._log(f"checkpoint restore: re-warm succeeded after {spent():.0f}s")
            return True

        # The session rebuilt but is not usable and would not re-warm: these cookies are spent.
        # Delete them so the next respawn doesn't pay this again for the same dead state.
        self._log(f"checkpoint restore: unusable after {spent():.0f}s -- discarding this checkpoint "
                  f"and doing a full re-login")
        try:
            ckpt.clear_checkpoint(self.identity_id)
        except Exception as e:      # best-effort; never let cleanup block the login
            self._log(f"checkpoint restore: could not clear checkpoint ({e})")
        self._restore_stage = "none"
        try:
            self.session.close_browser()
        except Exception:
            pass
        self.session = None
        return False

    def _set_anchor(self, src: str) -> None:
        """Record that the booking anchor was (re)established, and by which route."""
        self._anchor_src = src
        self._anchor_at = time.time() if src == "chain" else None

    def _clear_anchor(self) -> None:
        self.posto_pdf = None
        self._anchor_src = None
        self._anchor_at = None

    def _anchor_age(self) -> float | None:
        """Seconds since the chain last rebuilt the anchor, or None when we did not build it."""
        return None if self._anchor_at is None else time.time() - self._anchor_at

    def _anchor_desc(self) -> str:
        age = self._anchor_age()
        if self._anchor_src is None:
            return "none"
        return f"{self._anchor_src}/{age:.0f}s" if age is not None else f"{self._anchor_src}/age-unknown"

    def _roll_exit(self, why: str) -> bool:
        """Move THIS worker onto a fresh sticky exit without asking the Hub for another lease.

        The sticky session id is a number we choose, so the supply of exits is effectively unlimited
        and costs nothing -- no Hub round-trip, no lease, no pool contention. That matters because
        the alternative, _request_proxy_rotation, is bounded by the pool and by what is free.

        MEASURED 2026-08-25: our credentials are already sticky (5 requests, 1 exit IP), so a dead
        tunnel stays dead for every retry against it. The login's transient budget was spending all
        six attempts on the SAME dead exit -- "transient 1/6, 2/6, 3/6" back to back on one tunnel
        in every recent run. Retrying a dead tunnel cannot succeed; rolling off it can.
        """
        try:
            from libs.proxies.lease import roll_sticky_session
        except Exception:
            return False
        if not self.warm_proxy:
            return False
        rolled = roll_sticky_session(self.warm_proxy)
        if rolled == self.warm_proxy:
            return False
        # PER-ROLL GEO (2026-10-01): a roll starts a FRESH sticky session, so it is the safe moment
        # to also change the exit COUNTRY (no mid-session geo change that would trip a 'confirma o
        # reCAPTCHA' exit-moved verdict). Pick a fresh random country from the booking's set so the
        # worker samples all allowed geos over its retries instead of dying on one throttled country.
        if self._proxy_geos:
            try:
                from libs.proxies.rotating import reseat_geo
                rolled = reseat_geo(rolled, random.choice(self._proxy_geos))
            except Exception:
                pass
        self.warm_proxy = rolled
        self._exit_ip = None          # a new exit: the next observation is a baseline, not a change
        self._log(f"proxy: rolled to a fresh sticky exit [{_proxy_label(rolled)}] ({why})")
        return True

    def _slog(self, event: str, **fields) -> None:
        """Emit a structured `key=value` lifecycle log line (Phase-K, 2026-10-01). Standard
        correlation context (event/role/booking/identity/keepalive-mode) is attached automatically so
        a single line is back-traceable without cross-referencing the launch payload; event-specific
        fields follow. Routes through self._log, which prefixes the ISO-8601 timestamp. Used for the
        back-traceable lifecycle (signal receipt, warm-up, discovery, apply, keepalive) ALONGSIDE the
        existing free-text diagnostics -- it does not replace them."""
        # Defensive on every field: EngineOps has many partial test doubles that don't set cfg/
        # booking_id, and a log helper must never be the thing that raises.
        cfg = getattr(self, "cfg", None)
        role = getattr(getattr(cfg, "role", None), "value", getattr(cfg, "role", "")) if cfg else ""
        ka = getattr(getattr(cfg, "keepalive", None), "mode", "auth_only") if cfg else "auth_only"
        ctx = {
            "event": event,
            "role": role,
            "booking": getattr(self, "booking_id", None),
            "identity": getattr(self, "identity_id", None),
            "ka_mode": ka,
        }
        # sig: the correlation id of the signal-handling round in flight (minted by _new_sig_id when
        # a signal is received), so every lifecycle line of ONE round -- received/accepted -> warmup
        # -> discover/apply -> result -- shares an id and a single round is greppable end to end.
        sig = getattr(self, "_sig_id", "")
        if sig:
            ctx["sig"] = sig
        ctx.update(fields)
        log = getattr(self, "_log", None)
        if callable(log):
            log(_kv(ctx))

    def _new_sig_id(self) -> str:
        """Mint a fresh correlation id for a signal-handling round and make it the one _slog stamps
        onto every subsequent line until the next signal is received. 6 hex chars is plenty to
        disambiguate the rounds interleaved in a single worker's log."""
        self._sig_id = f"{random.randrange(16 ** 6):06x}"
        return self._sig_id

    def _warmup_kind(self, ok: bool) -> str:
        """The warmup failure classification for a structured log, or "" on success. Defensive --
        warmup_failure_kind reads health state that partial test doubles don't model, and a log
        helper must never raise."""
        if ok:
            return ""
        try:
            return self.warmup_failure_kind()
        except Exception:
            return "unknown"

    def _on_portal_origin(self) -> bool:
        """Is the browser sitting on a live portal page RIGHT NOW?

        Every in-page call we make -- the home-lang POST, all seven quest steps, ScheduleController,
        and (since 2026-08-24) the submit itself -- is a `fetch()` executed in the CURRENT page's
        context. `browser_fetch` never navigates first; it inherits whatever origin the page happens
        to be on. So a page left on `chrome-error://chromewebdata/` by ANY earlier failed navigation
        is an OPAQUE origin with no network access, and every subsequent fetch from it throws
        exactly `TypeError: Failed to fetch` -- forever, because nothing ever put the page back.

        CONFIRMED live 2026-08-25 on Test 2032, and it explains the operator's observation that
        login, warm-up and discovery all work on the very same proxy and session while ONLY applying
        fails. Those three paths each BEGIN with a real navigation, so they self-heal. do_apply was
        the one path that issued an in-page fetch without first proving it had a live origin:

            /slots primer navigates -> lands on chrome-error -> page stays there
              -> submit XHR            "TypeError: Failed to fetch"          (8 of 8)
              -> next round's probe    "Execution context was destroyed"     (8)
              -> next round's warm-up  "Failed to fetch" x3 -> warm-up fails (48)
              -> outcome "session", 32 leases, and finally a full re-login

        `location.origin` is the exact signal, not the href: a chrome-error page reports `"null"`
        (the opaque origin), which is precisely the condition that makes fetch throw.
        """
        try:
            raw = self.session.browser_eval("JSON.stringify({o:location.origin,h:location.href})")
        except Exception:
            # "Execution context was destroyed" lands here too -- also a page we cannot fetch from.
            return False
        try:
            st = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            return False
        return str(st.get("o") or "") == _PORTAL_ORIGIN and str(st.get("h") or "").startswith(BASE)

    def _ensure_portal_origin(self, why: str) -> bool:
        """Put the page back on a real portal origin before issuing an in-page fetch.

        Cheap when it is already there (one eval, no request). When it is not, ONE nav to
        Schedule.jsp re-anchors the session -- the same self-heal login/warm-up/discovery get for
        free by starting with a navigation.
        """
        if self._on_portal_origin():
            return True
        self._log(f"page is not on a live portal origin before {why} -- re-anchoring to Schedule.jsp")
        try:
            self.session.browser_nav(
                SCHED_JSP + "?posto_id=" + str(self.posto_pdf or self.posto_id), timeout=30)
        except Exception as e:
            self._log(f"re-anchor nav failed: {str(e)[:120]}")
            return False
        ok = self._on_portal_origin()
        if not ok:
            self._log("re-anchor did not restore a portal origin -- the tunnel is genuinely down")
        return ok

    def _schedule_state_ok(self) -> bool:
        """Is the booking anchor still live? One GET, no rebuild.

        The cheap gate in front of do_apply's round-start re-warm, mirroring storm-auto's poll
        cycle. Returns True only when Schedule.jsp actually carries the scheduling markers, so a
        False here means "genuinely stale, rebuild it" rather than "we did not look".

        Bails out (False) rather than retrying on a bot challenge or an auth redirect: neither is
        something a re-warm on this session can fix, and the caller's ladder handles both. The
        partial-render retry in between is storm-auto's: under load the portal answers 200 with the
        form not yet AJAX-populated, and a refresh -- "a manual reload" -- fixes it far more cheaply
        than a full questionnaire chain.
        """
        # The typed field, not cfg.raw: EngineConfig already parses this (and did so for months
        # without anything consuming it).
        self._portal_busy_seen = False
        for attempt in range(1 + max(0, int(self.cfg.login.partial_render_retries))):
            try:
                final_url = self.session.browser_nav(
                    SCHED_JSP + "?posto_id=" + str(self.posto_pdf or self.posto_id), timeout=30)
                html = self.session.browser_eval("document.documentElement.outerHTML") or ""
            except Exception as e:
                self._log(f"schedule-state probe failed: {str(e)[:120]}")
                return False
            if _is_challenge_body(html):
                self._log("schedule-state: bot challenge on Schedule.jsp -- session cannot POST")
                return False
            if _is_portal_refusal(html):
                # NOT a stale form. The portal is declining to serve this session right now, and it
                # declines everything: CONFIRMED live 2026-08-23 on Test 2032 (AMILCAR LOPES) --
                # the submit was refused with the 28531B "try again later" page, then Schedule.jsp
                # returned the same page (28508B) four times, which read as "200 but no scheduling
                # markers" and escalated into a full questionnaire re-warm. Every quest step was
                # refused too ("quest_state_stale"), so the ladder went on to burn a full re-login.
                #
                # Rebuilding into a portal that is refusing everything cannot succeed, and the
                # re-login costs a CAPTCHA solve and sub-account budget. Flag it so do_apply backs
                # off and keeps the session instead.
                self._portal_busy_seen = True
                self._log("schedule-state: the portal is declining requests right now "
                          f"({len(html)}B 'try again later') -- backing off, NOT rebuilding")
                return False
            if "Authentication.jsp" in (final_url or "") or "Authentication.jsp" in html:
                self._log("schedule-state: redirected to auth -- session is dead")
                return False
            if any(m in html for m in _SCHED_PAGE_MARKERS):
                return True
            self._log(f"schedule-state: 200 but no scheduling markers (len={len(html)}) "
                      f"-- refresh-retry (partial render under load)")
            time.sleep(1.5)
        return False

    def _probe_alive(self) -> bool:
        """Three-way alive check, mirrored from storm-auto's confirmed session_store.probe_session
        logic but via a real browser nav (no primp path exists in this architecture)."""
        try:
            final_url = self.session.browser_nav(SCHED_JSP + "?posto_id=" + self.posto_id, timeout=30)
        except Exception:
            return False
        if "Authentication.jsp" in final_url:
            return False
        try:
            html = self.session.browser_eval("document.documentElement.outerHTML") or ""
        except Exception:
            return False
        return "cmbPeriodo" in html or "getPeriodosOcupados" in html

    def session_probe_ok(self) -> bool:
        """POSITIVE liveness gate the scout runs BEFORE discovery (finding #2, 2026-10-01).

        A scout must not discover from a session that has not just proven itself live. The keepalive
        between rounds classifies an off-origin / dead-tunnel / UNKNOWN page as 'inconclusive, will
        re-probe' and RETURNS -- it does not stop the next discover command from running. So a
        degraded scout went straight into do_discover, gettime raised 'Execution context was
        destroyed' (the page was on chrome-error / mid-nav), and a whole ~68s round was wasted before
        the REACTIVE all-failed ladder noticed. This makes the check PROACTIVE: prove the session is
        authenticated and on a live portal origin first, and let the phase machine rebuild it if not.

        Auth-level, not form-level: KEEPING is auth-only, so this asks the same question the keepalive
        asks (classify_session_state warmed=False) on the same home page, inventing no new opinion.
        do_discover re-establishes the Schedule.jsp context it needs on its own.
        """
        if self.session is None:
            return False
        probe = self.cfg.keepalive.probe or "home"
        url = HOME_URL if probe == "home" else probe
        try:
            final_url = self.session.browser_nav(url, timeout=30)
        except Exception as e:
            self._log(f"session_probe_ok: nav failed ({str(e)[:100]}) -- not healthy")
            return False
        if probe != "home":
            # Custom KEEPALIVE_PROBE URL: a successful nav is the whole check, exactly as _do_keepalive
            # treats it (no content evidence is known for an arbitrary operator-chosen page).
            return True
        try:
            page = self._keepalive_read_page()
        except Exception as e:
            self._log(f"session_probe_ok: read failed ({str(e)[:100]}) -- not healthy")
            return False
        if self._is_dead_exit_response(page, final_url):
            self._log("session_probe_ok: dead exit / chrome-error -- not healthy")
            return False
        state = classify_session_state(page, final_url, warmed=False)
        if state is not SessionState.AUTHENTICATED:
            self._log(f"session_probe_ok: not authenticated ({state}, {len(page)}B) -- not healthy")
            return False
        return True

    def _probe_swapped_session(self) -> bool:
        """Is the session still AUTHENTICATED after moving it to the hold proxy?

        AUTH-LEVEL, NOT FORM-LEVEL, and that distinction is the whole bug this replaced.
        do_swap runs between LOGGED_IN and KEEPING -- i.e. strictly BEFORE warmup, which has been
        deferred to the slot-open/slot-found signal since 2026-09-09. It used to call
        `_probe_alive()`, which navigates Schedule.jsp and requires cmbPeriodo/getPeriodosOcupados.
        Those markers exist only once warmup has built the form-state anchor, so at swap time the
        check could not pass even in principle: it asked "is this session WARMED?" at the one point
        in the lifecycle where the machine guarantees it is not.

        MEASURED on booking Test 0916 (2026-09-16): 20/20 swap attempts failed -- 5 workers x 4
        attempts -- every one logging "new session failed health check". Deterministic, not flaky.
        Each failure cost a 600s backoff, so every worker spent ~40 minutes parked at LOGGED_IN and
        then fell through to KEEPING still on its phase-1 rotating proxy. The three appointments
        that run booked were made over soax rotating; the leased ISP exits never carried a session.
        `do_swap` moved before warmup when HOLD_SWAP_AFTER_WARMUP was renamed HOLD_SWAP_AFTER_LOGIN,
        and this probe was left pointing at the old lifecycle.

        `_probe_alive` is UNCHANGED and still correct for its other caller, the checkpoint-restore
        path, which restores `posto_pdf` and therefore does expect a warmed session.

        Reuses classify_session_state with `warmed=False` -- the same evidence the auth-only
        keepalive uses on the same page -- rather than inventing a second opinion about what a
        healthy home page looks like. PORTAL_ERROR/UNKNOWN are treated as "not proven alive": a
        swap that cannot be confirmed must revert, since the alternative is silently holding a
        session on an exit we have no evidence works.
        """
        try:
            final_url = self.session.browser_nav(HOME_URL, timeout=30)
        except Exception as e:
            self._log(f"do_swap: probe nav failed on the hold proxy ({e})")
            return False
        try:
            page = self.session.browser_eval("document.documentElement.outerHTML") or ""
        except Exception:
            return False
        state = classify_session_state(page, final_url, warmed=False)
        if state is not SessionState.AUTHENTICATED:
            self._log(f"do_swap: hold-proxy session probe -> {state} (not authenticated)")
            return False
        return True

    def _fresh_login(self) -> bool:
        """Sticky-then-rotate (operator spec, ported from storm-auto's confirmed
        app/worker.py:323-326 pattern): up to STICKY_RETRY_MAX consecutive attempts on the CURRENT
        sub-account before rotating to the next candidate in self.accounts (ranked best-first by the
        Hub, see _rank_live_sub_accounts); up to len(self.accounts) rotations total. A 3-subaccount
        identity at the sticky=2 default gets up to 2x3=6 login attempts before giving up entirely.
        Starts rotation from whichever candidate matches the CURRENT sub_account_id (not always index
        0 -- a keepalive-restore re-login must resume from where this worker actually is, not restart
        the whole fleet from the top of the ranking)."""
        started_with = self.sub_account_id
        start_idx = next((i for i, a in enumerate(self.accounts)
                          if a.get("sub_account_id") == self.sub_account_id), 0)
        ordered = self.accounts[start_idx:] + self.accounts[:start_idx]
        self._saw_maintenance = False
        for i, candidate in enumerate(ordered):
            # A maintenance page is down-for-everyone: rotating to the next sub-account would hit
            # the same page and burn it for nothing. _sticky_login_attempts sets this flag and
            # returns False; abandon the rotation and let the fleet breaker gate re-entry.
            if self._saw_maintenance:
                self._log("login: portal in maintenance -- abandoning sub-account rotation "
                          "(down for everyone), waiting on the fleet breaker")
                return False
            # STOP IS CHECKED BETWEEN EVERY SUB-ACCOUNT (2026-09-11). This whole chain -- and
            # _sticky_login_attempts below it -- used to run to completion with no bus contact at
            # all, so a worker that entered a login storm went DEAF to stop/assign/self-scout for
            # as long as the storm lasted. CONFIRMED LIVE 2026-09-07: one worker ground from 18:04
            # to past 18:31 (27+ minutes, 6 transient rolls x multiple sub-accounts) with zero
            # response to the bus. Worst case is ~39 rounds (13 per sub-account x 3), each a
            # browser launch + login attempt plus backoff, so the deaf window could be far longer
            # than that. The Hub was never the bottleneck (it delivers in ~2s, measured); the
            # worker simply never looked.
            if self.is_stopped():
                self._log("login: stop observed -- abandoning sub-account rotation")
                return False
            if i > 0:
                self._log(f"login: sticky retries exhausted on this sub-account -- rotating to "
                          f"candidate {i + 1}/{len(ordered)} ({candidate.get('sub_account_id')})")
            self.account = candidate
            self.sub_account_id = candidate.get("sub_account_id", self.sub_account_id)
            if self._sticky_login_attempts(self.cfg.login.sticky_retry_max):
                if self.sub_account_id != started_with and self._emit is not None:
                    self._emit("account_rotated", sub_account_id=self.sub_account_id)
                return True
        return False

    def _sticky_login_attempts(self, sticky_max: int) -> bool:
        """Spend up to `sticky_max` attempts ON THIS SUB-ACCOUNT, plus a separate, bounded budget for
        transient infrastructure failures that say nothing about it.

        Confirmed live 2026-08-14: this was a `for attempt in range(...)` loop in which BOTH the
        transient and the hard-failure branches simply `continue`d -- so the loop variable advanced
        either way and the transient classification had no effect whatsoever, despite logging
        "sticky count not burned". Combined with connection errors not being classified as transient
        at all, a run of bad proxies burned the whole sticky budget, rotated through every
        sub-account, failed the worker, and consumed MAX_RESPAWN_ATTEMPTS."""
        attempt = 0          # sticky attempts genuinely spent on this sub-account
        transient = 0        # infrastructure retries, bounded but not charged to the account
        max_transient = max(0, int(self.cfg.login.transient_retry_max))
        captcha_fails = 0
        captcha_budget = max(1, int(self.cfg.raw.get("CAPTCHA_FAILURE_BUDGET", 1)))
        # "Por favor, confirma o reCAPTCHA" verdicts, each answered by rolling to a FRESH exit before
        # the account is ever charged. This is an EXIT/token problem (the exit moved across the solve
        # window OR is simply flagged/bad for reCAPTCHA), never the account's fault -- CONFIRMED
        # 2026-09-24 (PEDRO RODRIGUES): one exit returned it for BOTH sub-accounts while another
        # logged the SAME accounts in seconds. Bounded like `transient`/`busy_rolls` and, like them,
        # not charged to the sub-account. Replaces the old ip_moves budget, which only rolled when
        # the exit demonstrably MOVED and so burned accounts on a stable-but-flagged exit.
        recaptcha_rolls = 0
        max_recaptcha_rolls = max(0, int(self.cfg.raw.get("LOGIN_RECAPTCHA_ROLLS", 3)))
        # "Try again later" refusals, each answered by rolling to a DIFFERENT exit before the fleet
        # is told anything. Bounded like `transient` and `ip_moves`, and like them not charged to
        # the sub-account. Its own counter rather than sharing `transient`: a dead tunnel and a
        # portal refusal are different signals and must not spend each other's budget.
        busy_rolls = 0
        max_busy_rolls = max(0, int(self.cfg.raw.get("LOGIN_PORTAL_BUSY_ROLLS", 3)))
        rounds = 0
        while attempt < sticky_max:
            # Every round here costs a browser launch + a login attempt + a backoff sleep, and the
            # transient/ip_moves/busy_rolls budgets mean `attempt` may not advance for many rounds
            # -- so this loop, not just the sub-account loop above it, is where a worker actually
            # spends most of a storm. See _fresh_login's comment for the confirmed incident.
            if self.is_stopped():
                self._log("login: stop observed -- abandoning retries on this sub-account")
                return False
            rounds += 1
            if rounds > 1:
                # Regression (L2/L3, closed by P3): a repeated login failure used to keep retrying
                # on the SAME proxy forever -- if the exit itself is the problem (burned IP,
                # rate-limited), no number of retries recovers. Move to a fresh exit before every
                # retry past the first, matching storm-auto's confirmed behavior.
                #
                # LOCAL ROLL, not a Hub round-trip (2026-09-15). This was the one unconditional
                # _request_proxy_rotation on the warm path -- every login retry round asked the Hub
                # for another pooled row. MEASURED on booking 0911: that round-trip cost 46s/97s/
                # 124s per rotation under load, because the Hub answered it from a 4-thread pool
                # whose handler ran a ~25s browser probe per candidate. Rolling the session id is a
                # string operation on a credential the worker already holds, and lands on an exit
                # that is as good as any the Hub could have leased (lease.py:153).
                self._roll_exit(f"login retry round {rounds}")
                # DRAIN THE BUS EXPLICITLY. is_stopped() only reads a flag that BusSignals._drain
                # sets, and _drain only runs inside a wait_* call -- during a login storm the only
                # one reached was wait_proxy, via the Hub rotation this round-trip just replaced.
                # Without this, removing that call would have made a storming worker deaf to `stop`
                # again, undoing the 2026-09-11 fix. Non-blocking: one XREADGROUP, no added latency.
                self._poll_signals()
                # The stop check stays: _roll_exit is instant, but the browser launch plus login
                # attempt that follow are not, and a stop landing here used to go unseen for
                # minutes. (The rotation sites further down `continue` straight back to the
                # loop top, so the check there covers them.)
                if self.is_stopped():
                    self._log("login: stop observed before retrying -- abandoning retries")
                    return False
            try:
                # Close the PREVIOUS attempt's browser before opening another. This line used to
                # simply reassign self.session, orphaning a whole Chromium tree on every retry --
                # and this loop retries up to sticky_max(3) x max_transient(6) times per worker.
                #
                # Measured live 2026-08-21 on "Test 3018": 9 workers, all cycling on transient
                # chrome-error failures, had produced 588 chromium and 656 node processes -- ~65 per
                # worker against ~29 the day before. 9 workers x ~6 retries x ~11 processes is ~590,
                # which is the number on the box.
                #
                # It is also self-amplifying, which is why it presented as "suddenly every login
                # fails": orphaned browsers raise resource pressure, pressure produces more
                # chrome-error navigation failures, each of those is classified transient, and every
                # transient retry leaks another tree. The host had memory and CPU to spare the whole
                # time -- this was never starvation, it was accumulation.
                # SHARED PRE-SOLVED TOKEN POOL (2026-10-01): grab a READY login reCAPTCHA token so
                # browser_login injects it instead of solving inline (~15-25s). The token is sitekey/
                # action-based and ProxyLess, so it is not bound to this session -- any worker can use
                # one. Empty holder -> browser_login solves inline, exactly as before (fallback).
                _pre_token = [""]
                try:
                    if self._redis is not None:
                        from libs.bus.token_pool import TokenPool
                        _tok = TokenPool(self._redis).acquire()
                        if _tok:
                            _pre_token[0] = _tok
                            self._log(f"login: using pre-solved token from pool (len {len(_tok)})")
                except Exception:
                    pass   # pool is an optimisation; a failure must never block a login
                self._close_session_quietly()
                self.session = get_session(proxy=self.warm_proxy, headless=self.headless,
                                           fp_seed=self.account.get("username"))
                # Baseline the exit before the CAPTCHA is solved, so a rejection can be attributed
                # (or not) to the address moving underneath the solve.
                self._note_exit_ip("before login attempt")
                # No min_score: the parameter is gone. It gated the pre-solved token and forced
                # race_all down its "wait for a good-enough score" path, which is what produced the
                # 180s solver stalls -- to chase a score our tokens essentially never reach and that
                # does not predict portal acceptance anyway. First usable token wins.
                if self._liveness is not None:
                    self._liveness.record_login_attempt()   # count the shot (dashboard shot-rate)
                result = self.session.browser_login(
                    self.account["username"], self.account["password"],
                    capsolver_keys=self.capsolver_keys,
                    pre_token=_pre_token,
                )
            except Exception as e:
                if transient < max_transient and self._is_transient_login_failure(str(e)):
                    transient += 1
                    self._log(f"login: transient {transient}/{max_transient} "
                              f"[{_proxy_label(self.warm_proxy)}] ({e}) -- infrastructure, not "
                              f"charged to sub-account (sticky still {attempt}/{sticky_max})")
                    # ROLL OFF THE DEAD EXIT. Our credential is sticky, so retrying without this
                    # aims all six attempts at the identical dead tunnel.
                    self._roll_exit(f"transient {transient}/{max_transient}")
                    self._backoff(transient)
                    continue          # deliberately does NOT advance `attempt`
                attempt += 1
                self._log(f"login attempt {attempt}/{sticky_max} [{_proxy_label(self.warm_proxy)}"
                          f" acct={self.sub_account_id[:8]}]: {e}")
                self._backoff(attempt)
                continue
            if self._is_login_success(result):
                self._restore_stage = "none"
                # Regression (N4): a brand-new session has zero server-side form state, but
                # posto_pdf survived from whatever the PREVIOUS session had warmed. Left stale, the
                # keepalive-restore chain's `if ok and self.posto_pdf is None: self._run_warmup_steps()`
                # incorrectly skipped re-warming after a fresh login, reporting "restore succeeded"
                # while holding an unwarmed session -- and poisoning the next checkpoint save with a
                # stage="schedule_jsp" that isn't true.
                self._clear_anchor()
                if self._liveness is not None:
                    self._liveness.record_alive()      # the portal served a successful login
                return True
            # A rejection can ALSO be infrastructure: the vendored login returns 200 with an error
            # body for "datadome_challenge_on_post" (the exit IP is flagged) and for a navigation
            # that never landed (chrome-error://chromewebdata). Neither is a verdict on the
            # credentials, so neither may burn this sub-account's budget.
            reason = _login_reject_reason(result)
            if transient < max_transient and self._is_transient_login_failure(reason):
                transient += 1
                self._log(f"login: transient {transient}/{max_transient} rejected ({reason}) -- "
                          f"infrastructure, not charged to sub-account "
                          f"(sticky still {attempt}/{sticky_max})")
                self._roll_exit(f"transient {transient}/{max_transient}")
                self._backoff(transient)
                continue
            # OUR FAULT, NOT THE ACCOUNT'S -- and therefore checked HERE, beside `transient` and
            # before `attempt` is charged, not after.
            #
            # "Por favor, confirma o reCAPTCHA" is an EXIT/token problem, NOT an account problem.
            # The token was minted against one exit and rejected -- either the exit MOVED across the
            # ~6s solve window (token/IP mismatch) or the exit is simply flagged/bad for reCAPTCHA.
            # Either way the answer is a FRESH EXIT, never the account's 2-strike budget (two strikes
            # and the portal blocks the account permanently). CONFIRMED 2026-09-24 (PEDRO RODRIGUES):
            # exit 184.146.35.41 returned this verdict for BOTH sub-accounts, while 166.113.97.67
            # logged the SAME accounts in seconds -- but because the exit did not detectably MOVE,
            # the old ip_moves gate (which required a detected move) fell through and CHARGED it to
            # the accounts, burning both and failing the worker. So roll to a fresh exit and retry,
            # bounded and uncharged, whether or not a move is observed. (_note_exit_ip is still
            # called for its logging of a detected move; it no longer GATES the roll.) Only past this
            # budget do we charge it below -- at that point several different exits have said the same
            # thing and we genuinely cannot tell a bad-exit streak from a failing account.
            if _is_recaptcha_verdict(reason) and recaptcha_rolls < max_recaptcha_rolls:
                recaptcha_rolls += 1
                moved = self._note_exit_ip("after reCAPTCHA rejection")
                self._log(f"login: reCAPTCHA verdict ({recaptcha_rolls}/{max_recaptcha_rolls}) -- "
                          f"{'exit moved' if moved else 'bad/flagged exit'}, NOT charged to "
                          f"{self.sub_account_id[:8]}; rolling to a fresh exit and retrying")
                self._roll_exit("recaptcha -- fresh exit")
                self._backoff_seconds(float(self.cfg.raw.get("LOGIN_THROTTLE_BACKOFF", 0.5)))
                continue          # deliberately does NOT advance `attempt` or `captcha_fails`
            if _is_maintenance(reason):
                # The portal is EXPLICITLY down (maintenance / 'Serviço Indisponível'). Unlike the
                # 'try again later' throttle below, this is not about our exit or our account, and
                # every exit and every sub-account gets the same page -- so rolling exits or
                # rotating accounts against it only burns the pool (booking "Test 2032": 154
                # refusals across 13 accounts). Record it to PortalLiveness (shown to the operator
                # as portal=down -- NOT a gate), STOP grinding (don't roll, don't spend a sticky
                # attempt, signal _fresh_login to abandon rotation), and back off on our OWN for a
                # cooldown -- the per-worker replacement for the removed fleet breaker: it stops this
                # worker burning accounts against a down portal without freezing the whole fleet.
                if self._liveness is not None:
                    self._liveness.record_down(self.sub_account_id)
                self._saw_maintenance = True
                self._log("login: portal MAINTENANCE page ('Serviço Indisponível') -- portal marked "
                          "DOWN for the dashboard; NOT rotating exit/account (down for everyone), "
                          f"backing this worker off {int(self.cfg.raw.get('MAINTENANCE_BACKOFF', 60))}s")
                self._backoff_seconds(float(self.cfg.raw.get("MAINTENANCE_BACKOFF", 60)))
                return False
            if _is_portal_busy(reason) and busy_rolls < max_busy_rolls:
                busy_rolls += 1
                # NEAR-ZERO BACKOFF ON A THROTTLE (2026-09-23). The portal's 'try again later' is a
                # GLOBAL rate-limit lottery, not a verdict on our rate: a success and a throttle are
                # byte-identical requests (same score-10 capsolver token, same POST) -- the portal
                # just rolls the dice, ~1% served. So bookings scale with SHOTS TAKEN, and the old
                # exponential _backoff(busy_rolls) (2-8s) only made our fleet SLEEP instead of shoot
                # -- measured at ~1.75 attempts/min/worker, mostly idle. Backing off buys nothing (it
                # is not our load being limited) and costs lottery tickets. Roll to a fresh exit and
                # fire again immediately; LOGIN_THROTTLE_BACKOFF (default 0.5s, jittered) is the only
                # pause, just enough to avoid a tight loop around the ~15s browser+solve cost.
                self._log(f"login: 'try again later' ({busy_rolls}/{max_busy_rolls}) -- rolling to a "
                          f"fresh exit and firing again immediately (throttle is a global lottery)")
                self._roll_exit(f"portal busy {busy_rolls}/{max_busy_rolls}")
                self._backoff_seconds(float(self.cfg.raw.get("LOGIN_THROTTLE_BACKOFF", 0.5)))
                continue
            attempt += 1
            self._log(f"login attempt {attempt}/{sticky_max} [{_proxy_label(self.warm_proxy)}"
                      f" acct={self.sub_account_id[:8]}]: rejected ({reason})")
            # A 'try again later' throttle is now handled PURELY per-worker (busy_rolls above rolls
            # to a different exit; past the budget it just counts as this attempt). It no longer
            # feeds any fleet-wide gate -- the booking breaker was REMOVED 2026-09-23 because that
            # gate froze the whole fleet on a normal throttle burst and oscillated (trip -> all wait
            # -> all retry together -> re-trip), starving discovery and apply exactly when throughput
            # was needed. The throttle says nothing reliable about the portal's liveness either (a
            # blocked account is throttled from every exit while other accounts log in fine), so it
            # is NOT recorded as portal-down; only the explicit maintenance page is (see above).
            # CAPTCHA BUDGET. The portal allows this account TWO reCAPTCHA failures before blocking
            # platform access outright, and the sticky budget was happily spending both -- leaving
            # the account at the edge, where any later retry in the same window blocks it for good.
            # Stop early and let the caller rotate to another sub-account: a rotation is cheap, a
            # blocked account is permanent.
            if _is_recaptcha_verdict(reason):
                # "Por favor, confirma o reCAPTCHA" is the TOKEN/IP MISMATCH signature: the token was
                # minted against one exit and submitted from another, so the portal cannot verify it.
                # Check the exit NOW, while the failed session is still open -- if it moved across
                # the ~6s solve window, that is the cause and no amount of retrying fixes it. This is
                # the one place the observation actually settles a live question, and until now it
                # only ran at warm-up and keepalive, which almost no worker reaches.
                captcha_fails += 1
                if captcha_fails >= captcha_budget:
                    self._log(f"login: {captcha_fails} reCAPTCHA rejection(s) on "
                              f"{self.sub_account_id[:8]} -- the portal blocks an account after 2, "
                              f"so stopping here and rotating rather than spending the rest")
                    return False
            self._backoff(attempt)
        return False

    @staticmethod
    def _is_login_success(result: object) -> bool:
        """browser_login always returns HTTP status 200 for every non-catastrophic outcome (the
        vendored session hardcodes this) -- the real verdict is JSON-encoded in `body`:
        {"type":"success",...} vs {"type":"error","description":"login_failed_auth_redirect"} (wrong
        credentials) or "datadome_challenge_on_post" (proxy blocked). Checking status alone (the
        original bug here) makes every login attempt register as successful. Ported from storm-auto's
        proven classification (app/worker.py's login handler, not the looser exploratory-probe
        version): an empty or HTML-prefixed body, or one that isn't valid JSON, is never a success
        regardless of status."""
        if not isinstance(result, dict) or result.get("status", 0) != 200:
            return False
        body = (result.get("body") or "").strip()
        if not body or body.startswith("<"):
            return False
        import json
        try:
            parsed = json.loads(body)
        except (TypeError, ValueError):
            return False
        return parsed.get("type", "") in ("", "200", "success")

    @staticmethod
    def _is_transient_login_failure(msg: str) -> bool:
        """Is this failure about the INFRASTRUCTURE rather than the account?

        A transient failure gets its own retry budget and must not burn the sticky counter, because
        it carries no information about whether the credentials are good. Confirmed live 2026-08-14:
        the connection-level markers below were NOT in this list, so a dead proxy burned the
        account's sticky budget, exhausted its sub-account rotation, failed the worker, and
        eventually tripped MAX_RESPAWN_ATTEMPTS -- six workers sat permanently FAILED because of
        proxy weather. The portal never rejected them.

        Deliberately NOT transient: `login_failed_auth_redirect` and a redirect back to /login --
        those are the portal's verdict on the credentials, which is exactly what the sticky/rotate
        budget exists to spend."""
        low = (msg or "").lower()
        return any(m in low for m in (
            # WAF / bot-challenge -- a property of the exit IP, not the account
            "datadome", "waf", "waiting room", "bot challenge",
            # timeouts
            "timed out", "timeout",
            # connection/proxy level: the request never reached the portal at all
            "err_connection", "err_tunnel", "err_proxy", "err_timed_out", "err_network",
            "err_empty_response", "err_name_not_resolved", "err_socks", "err_address",
            "chromewebdata",            # chrome-error://chromewebdata/ -- navigation never landed
            "failed to fetch",
        ))

    def identity_unavailable(self) -> bool:
        """True when the portal reported this applicant ALREADY HAS a request -- the identity is
        permanently unusable for this run, so the phase machine must terminate the worker (not
        respawn) rather than grind a doomed sub-account. See _already_booked_signal."""
        return self._identity_unavailable

    def _already_booked_signal(self, *bodies: str) -> str | None:
        """Return the portal's 'already has a request' text if it appears in the last JS dialog or in
        any of the given response bodies, else None. The portal delivers this ("Este requerente já
        tem um pedido registado. Poderá inserir novo 5 dias após o pedido anterior.") as a JS alert,
        which the session's dialog handler captures -- our fetch/nav flow otherwise never sees it and
        re-ran the blocked identity forever (0 occurrences in logs before the handler)."""
        try:
            dlg = self.session.browser_last_dialog() if self.session is not None else None
        except Exception:
            dlg = None
        for text in ((dlg or ""), *(b or "" for b in bodies)):
            low = text.lower()
            if any(m in low for m in _ALREADY_BOOKED_MARKERS):
                return text[:300]
        return None

    def _is_dead_exit_response(self, page: str, final_url: str) -> bool:
        """True when a nav 'succeeded' (raised nothing) but LANDED on a dead-tunnel page -- Chrome's
        chrome-error://chromewebdata shell carrying 'This site can't be reached' /
        ERR_TUNNEL_CONNECTION_FAILED / ERR_CONNECTION_CLOSED / ERR_TIMED_OUT. This is the EXIT being
        dead, not the portal and not the session, and its opaque origin also makes every in-page
        fetch throw. Confirmed dominant on booking 'Prev 0923-3018' (2026-09-23): 2456 chrome-error
        pages, 1076 ERR_CONNECTION_CLOSED, 687 ERR_TUNNEL, 481 ERR_TIMED_OUT -- returned as ~185KB
        strings, so no exception fired and classify_session_state read them as UNKNOWN, which just
        re-probed the same dead exit forever. Reuses the login path's transient-marker set."""
        if "chrome-error" in (final_url or "").lower():
            return True
        return (self._is_transient_login_failure(final_url or "")
                or self._is_transient_login_failure((page or "")[:4000]))

    def _request_proxy_rotation(self, phase: str | None = None) -> str | None:
        """Ask the Hub to ASSIGN a STATIC/ISP proxy. One caller remains: do_swap's hold ladder.

        SCOPE NARROWED 2026-09-15. This used to serve the warm path too, and was the single most
        expensive thing on it: MEASURED on booking 0911 at 46s/97s/124s per rotation, because the
        Hub answered from a 4-thread pool whose handler ran a ~25s browser probe per candidate, and
        workers emitted ~12.7 requests/min against a capacity of ~4.8/min. The queue grew, every
        rotation got slower, and each slow rotation made a worker roll MORE exits -- a feedback
        loop, on top of an already-degraded portal, right through slot-open.

        A ROTATING pool has nothing to ask the Hub for: the session id is a string the worker can
        mint itself (libs/proxies/rotating.py, _roll_exit). A STATIC one genuinely does -- it is a
        finite, individually-owned IP that two workers must not share, so it has to be assigned and
        leased by the one side with DB access. That distinction is the whole reason this method
        still exists.

        The worker process is deliberately DB-less (see apps/worker/__main__.py's own docstring),
        so it can't lease a proxy row itself -- this round-trips through the same event/command bus
        slot assignment already uses: emit `proxy_request`, block for the Hub's
        `proxy_assigned`/`proxy_unavailable` reply
        (apps/hub/events.py's `_handle_proxy_request`). `phase` defaults to `self._current_phase`,
        which do_swap() sets to "hold" unconditionally the instant warmup finishes -- so a rotation
        during holding always draws from the hold pool even when HOLD_SWAP_AFTER_LOGIN is off (the
        operator's explicit requirement: "it will not be different when swap is off"). Returns the
        new proxy URL on success, or None (caller keeps its current proxy) if there's no bus wired,
        the pool is exhausted, or the reply times out -- the same safe-degrade pattern do_swap()
        itself already uses on failure. On a successful "hold" reply, also updates
        self.hold_proxy_static from the Hub's resource_type -- the Hub is the one side that actually
        enforces the static requirement (Phase 5), so this is authoritative, not a guess."""
        if self._emit is None or self._signals is None or not hasattr(self._signals, "wait_proxy"):
            return None
        resolved_phase = phase or self._current_phase
        self._emit("proxy_request", phase=resolved_phase)
        result = self._signals.wait_proxy(timeout=30)
        if not isinstance(result, dict) or not result.get("ok"):
            # Phase 5 (Phase-4-identified gap): this reason used to be generated by the Hub and then
            # silently discarded here -- the single biggest "why did this fail" hole in the whole
            # proxy subsystem. Never includes credentials: `reason` is built from ProxyLeaseError's
            # own message (pool/health/resource_type facts only) or this phase's own static-required
            # wording, both plain text with no proxy/session objects involved.
            reason = result.get("reason") if isinstance(result, dict) else None
            if reason:
                self._log(f"proxy rotation ({resolved_phase}) failed: {reason}")
            return None
        if resolved_phase == "hold":
            self.hold_proxy_static = result.get("resource_type") == "static"
        return result.get("proxy")

    def _backoff(self, attempt: int) -> None:
        base = float(self.cfg.raw.get("RESPAWN_BACKOFF_BASE", 2.0))
        cap = float(self.cfg.raw.get("RESPAWN_BACKOFF_CAP", 120.0))
        delay = min(cap, base * (2 ** (attempt - 1))) * random.uniform(0.8, 1.2)
        # Slept in ONE uninterruptible block until 2026-09-11. At the 120s cap (x1.2 jitter) that is
        # ~144s of total deafness per failed round, on top of the browser launch and login attempt
        # that precede it -- and this is called on EVERY failure branch in the login loop, so a
        # storm's sleep time alone ran to many minutes. Chunked so is_stopped() is observed within
        # ~1s, reusing the same shape as the discovery pacing sleep above (see its comment).
        deadline = time.time() + delay
        while time.time() < deadline:
            if self.is_stopped():
                return
            time.sleep(min(1.0, max(0.0, deadline - time.time())))

    def _backoff_seconds(self, seconds: float) -> None:
        """A fixed, stop-interruptible sleep (chunked so is_stopped() is seen within ~1s). Used for
        the per-worker maintenance backoff that replaced the fleet breaker -- a worker that hit the
        portal's maintenance page waits this out on its OWN rather than the whole fleet freezing."""
        deadline = time.time() + max(0.0, seconds) * random.uniform(0.8, 1.2)
        while time.time() < deadline:
            if self.is_stopped():
                return
            time.sleep(min(1.0, max(0.0, deadline - time.time())))

    def do_warmup(self) -> bool:
        # ALWAYS run the real questionnaire->ScheduleController->Schedule.jsp chain. The old
        # short-circuit (skip when _restore_stage in {"alive","rewarmed"}) is gone (2026-09-23): a
        # checkpoint restore is an AUTHENTICATED session only, never a warmed/bookable one, so there
        # is no restored form-state to trust -- warmup must actually establish the Schedule-page form
        # cursor. The restore path no longer claims "alive/bookable" (see _try_restore_from_checkpoint),
        # so nothing sets those stages to skip on any more; this is belt-and-suspenders.
        self._slog("warmup_start", phase="signal")
        _t = time.time()
        ok = self._run_warmup_steps()
        self._slog("warmup_end", result=("ok" if ok else "fail"),
                   duration_ms=int((time.time() - _t) * 1000),
                   kind=self._warmup_kind(ok))
        return ok

    def is_warmed(self) -> bool:
        """EngineOps surface for whether the booking form-state anchor is established AND VERIFIED
        -- lets phases.py's deferred-warmup call sites (_warm_if_needed) skip a redundant re-warm
        when PREWARM_BEFORE_SLOT_OPEN or a prior round already ran the real chain.

        PROVENANCE IS LOAD-BEARING, not a detail. posto_pdf has two writers that mean opposite
        things: _run_warmup_steps sets it from a freshly-parsed Schedule.jsp (_set_anchor("chain")
        -- verified, the questionnaire chain actually ran), while a checkpoint restore sets it from
        a saved file (_set_anchor("checkpoint") -- as old as the file, never verified against the
        portal). _attempt_keepalive_restore has always known this (`if ok and self._anchor_src !=
        "chain"` -- it re-warms a checkpoint anchor); this method shipped 2026-09-09 checking only
        `posto_pdf is not None` and so trusted both equally.

        CONFIRMED LIVE 2026-09-11 (booking 0911): after slot-open, 26 workers entered DISCOVERING
        but only 3 had ever reached WARMED. The rest had restored from checkpoint, reported warmed
        here, skipped the warmup, and hit /slots with a stale anchor -- the Schedule.jsp captcha
        widget was never rendered, so every round died on "window.onCaptchaSuccess never appeared
        within 20s". Zero slots were discovered and the booking could not book. Same failure class
        as the documented keepalive-suppression bug, reintroduced at a new call site.

        AGE IS NOT OPTIONAL EITHER. FAILURE_MODES.md F28's remedy already spells out the full
        invariant -- `_anchor_src == "chain"` AND `_anchor_age()` under the form-state TTL --
        and says exactly why the second half cannot be dropped: "a failed re-warm never reaches
        `_set_anchor`, so `_anchor_src` retains the previous *successful* warm's value." Provenance
        alone therefore still reports warmed after a re-warm that failed, which is the same stale
        anchor by a different route. TTL comes from FORM_STATE_TTL (catalog, "keepalive" group);
        see its note for why the ~90-120s figure often quoted is NOT used -- it is storm-auto's and
        was never measured here (OPEN_QUESTIONS Q3/Q24)."""
        if self.posto_pdf is None or self._anchor_src != "chain":
            return False
        age = self._anchor_age()
        if age is None:
            return False      # chain anchors always stamp _anchor_at; no stamp = not trustworthy
        return age < max(1.0, float(self.cfg.raw.get("FORM_STATE_TTL", 240.0)))

    # ── portal health: slow is not broken (Phase 3) ───────────────────────────
    # What a healthy portal costs, measured on live runs 2026-08-20. Used only as the baseline that
    # "materially slower than usual" is judged against -- not as a timeout.
    _PHASE_BASELINE = {"warmup": 38.0, "apply": 47.0, "discover": 24.0}

    def _note_portal(self, phase: str, seconds: float, *, ok: bool, error: str = "") -> None:
        """Record one portal interaction, and act on what it says.

        A failure here used to be logged as a flat "do_warmup failed: <exc>", which the recovery
        ladder then treated identically whatever the cause. But the right responses are opposite:

          slow         the portal answered, eventually. WAIT. Rebuilding the session over slowness
                       spends a CAPTCHA solve and sub-account rate limit to arrive at the same slow
                       portal, and the rate limit is already the binding constraint.
          unreachable  the request never landed. The proxy/session really is the problem.

        Sustained slowness used to be reported to the booking-wide breaker; that breaker was removed
        2026-09-23 (it froze the fleet and oscillated), so a degraded window is now just logged --
        each worker handles its own slowness (wait, don't re-login into the same wall).
        """
        self._health.observe(phase, seconds, ok=ok)
        if ok:
            base = self._PHASE_BASELINE.get(phase, 0.0)
            if self._health.is_slow_against(phase, base):
                self._log(f"portal: {phase} running slow -- median "
                          f"{self._health.median(phase):.0f}s vs ~{base:.0f}s baseline")
            return

        kind = classify_failure(error)
        self._health.observe_failure(kind)
        self._log(f"{phase} failed [{kind}] after {seconds:.0f}s: {error[:140]}")

        if kind == "unreachable":
            # The request never landed, so nothing about THIS exit is going to work: re-warming and
            # re-logging in over a dead tunnel just spends time, and eventually a CAPTCHA solve and
            # a slice of the sub-account budget, to fail identically. Rotate the exit first and let
            # the ladder's next attempt run on a live one. Best-effort -- if the Hub has nothing
            # free, the ladder proceeds exactly as before.
            # Roll the sticky session id: it always works, needs no Hub round-trip and no free
            # pool row, and the exit it lands on is as good as any the Hub could have leased.
            # The Hub fallback is gone -- for a rotating pool there is no longer anything to ask
            # it FOR (2026-09-15), and a credential with no rollable session id is a static one,
            # which never reaches this path.
            self._roll_exit(f"{phase} unreachable")

        if kind == "slow" and self._health.is_degraded():
            # Not this worker's bad luck any more -- the window is degraded. Logged for visibility;
            # no longer feeds a fleet gate (the breaker was removed 2026-09-23).
            self._log(f"portal: {self._health.slow_streak} consecutive slow failures -- window "
                      f"degraded (this worker waits; no fleet-wide backoff any more)")
        if self._emit is not None:
            self._emit("portal_health", phase=phase, kind=kind, seconds=round(seconds, 1),
                       **self._health.snapshot())

    def warmup_failure_kind(self) -> str:
        """How the last portal interaction failed -- "slow" | "unreachable" | "portal" | "unknown".

        The phase machine uses this to keep a SLOW window from escalating into a re-login.
        """
        if self._health.slow_streak:
            return "slow"
        if self._health.unreachable_streak:
            return "unreachable"
        return "unknown"

    def _run_warmup_steps(self) -> bool:
        """Questionnaire -> Formulario -> ScheduleController -> Schedule.jsp (confirmed URLs/flow,
        ported from storm-auto batch_apply._run_steps_2_to_6)."""
        _t0 = time.time()
        # Records the last EXCEPTION message so a caller can tell a dead-exit warmup failure (roll
        # the exit) from a portal rejection (do not). Cleared each run; only the except path below
        # sets it, so a plain `return False` (quest_state_stale, csrf, sc-rejected) leaves it empty.
        self._warmup_last_error = ""
        # Clear any stale JS-dialog text from a prior round so an already-booked signal we detect
        # below is from THIS warmup, not a previous one.
        try:
            self.session.clear_last_dialog()
        except Exception:
            pass
        try:
            # Every step below is an in-page fetch. If a prior failed navigation left the page on
            # chrome-error, all three retries of the home-lang POST are guaranteed to raise
            # "TypeError: Failed to fetch" -- that is an opaque origin, not the transient network
            # blip the retry loop was written for. Re-anchor first so the retries mean something.
            #
            # ACT ON THE VERDICT. This called _ensure_portal_origin and DISCARDED its return, so a
            # re-anchor that had just reported "the tunnel is genuinely down" was followed by the
            # very fetch loop this comment calls guaranteed-to-fail. Measured live 2026-08-27 on
            # booking "Test2": 24 off-origin detections, 24 failed re-anchors, 24 warmup failures,
            # 96 "Failed to fetch" (4 attempts each) -- a perfect 1:1 chain, burning 30-43s per
            # cycle and reporting the symptom ("Failed to fetch") instead of the known cause.
            #
            # Raised INSIDE the try on purpose: the existing except below already records the
            # failure through _note_portal and returns False, so this reuses the established
            # taxonomy (warmup_failure_kind -> "unreachable") rather than adding a second one.
            if not self._ensure_portal_origin("the warm-up chain"):
                raise RuntimeError(
                    "portal origin could not be restored -- the in-page fetch chain cannot run")
            # Retry budget + backoff, now operator-tunable (QUEST_STEP_RETRIES / QUEST_STEP_BACKOFF,
            # wired 2026-10-01). _QUEST_STEP_RETRIES stays as the fallback default so a config that
            # omits the key is byte-identical to the old behaviour. The backoff GROWS per attempt
            # (base, 2x, 4x ... capped 8s, +-30% jitter): a 503/WAF storm is portal overload, and
            # the flat 1.5s used before burned all three attempts inside the same second (step 5
            # attempts 1-2 both at 18:41:53 in one live log) -- worse than not retrying.
            _retries = max(1, int(self.cfg.raw.get("QUEST_STEP_RETRIES", _QUEST_STEP_RETRIES)))
            _bk_base = max(0.0, float(self.cfg.raw.get("QUEST_STEP_BACKOFF", 1.5)))

            def _quest_backoff(att: int) -> float:
                if _bk_base <= 0:
                    return 0.0
                return min(_bk_base * (2 ** (att - 1)), 8.0) * random.uniform(0.7, 1.3)

            # Retried like every other in-page fetch in this chain. When the labelling went in I
            # made this one raise on the FIRST failure while giving the quest steps below three
            # attempts -- and it is by far the most common failure point: 377 aborted warmups in one
            # live run, 370 of them from just two workers stuck in a hot retry loop, none of which
            # ever reached ScheduleController. "TypeError: Failed to fetch" is a transient network
            # failure inside the page (dead proxy, dropped tunnel), exactly what a retry is for.
            for attempt in range(1, _retries + 1):
                try:
                    self.session.browser_eval(
                        "(async()=>{const r=await fetch('%s',{method:'POST',credentials:'include',"
                        "headers:{'Content-Type':'application/x-www-form-urlencoded'},"
                        "body:'lang=PT'});return r.status;})()" % HOME_URL
                    )
                    break
                except Exception as e:
                    self._log(f"home-lang POST attempt {attempt}/{_retries} failed: {e}")
                    if attempt == _retries:
                        raise RuntimeError(f"home-lang POST failed: {e}") from e
                    time.sleep(_quest_backoff(attempt))
            # Same pacing before the Questionario nav: storm-auto paces the whole early burst
            # (HOME -> lang POST -> Questionario), not just the steps.
            _gap0 = max(0.0, float(self.cfg.raw.get("QUEST_STEP_GAP", 1.0)))
            if _gap0 > 0:
                time.sleep(_gap0 * random.uniform(0.7, 1.3))
            self.session.browser_nav(QUEST_URL, timeout=35, skip_cookie_sync=True)

            quest_key = self.residence if self.residence != self.nationality else self.nationality
            steps = self.quest_steps.get(quest_key, [])
            last_fields: dict = {}
            # storm-auto merges these into EVERY quest step (`base_qs`); we sent only the step's own
            # keys. Matching the confirmed path rather than guessing which of its parameters the
            # portal actually needs -- it is the reference implementation precisely because it works.
            base_qs = {"lang": "PT", "nacionalidade": self.nationality}
            gap = max(0.0, float(self.cfg.raw.get("QUEST_STEP_GAP", 1.0)))
            for i, step in enumerate(steps):
                is_last = i == len(steps) - 1
                # PACE into each step (±30% jitter). storm-auto added this knob for exactly the
                # failure we are seeing: "the quest-step-1 403s were observed clustering on exactly
                # this post-login burst". Seven fetches fired back-to-back in well under a second is
                # not a pattern a human produces, and it is the one place the burst happens.
                if gap > 0:
                    time.sleep(gap * random.uniform(0.7, 1.3))
                # urlencode, not manual "&".join: an unencoded value containing a space, & or =
                # silently corrupts the query string. storm-auto uses urlencode here.
                params = urlencode({**base_qs, **step})
                url = f"{QUEST_NEXT}?{params}"
                js = (
                    "(async()=>{const r=await fetch(%r,{method:'GET',credentials:'include',"
                    "headers:{'Accept':'text/plain, */*; q=0.01','X-Requested-With':'XMLHttpRequest',"
                    "'Sec-Fetch-Mode':'cors','Sec-Fetch-Dest':'empty','Sec-Fetch-Site':'same-origin',"
                    "'Referer':%r}});const body=await r.text();"
                    + (
                        "const _t=document.createElement('div');_t.innerHTML=body;const f={};"
                        "_t.querySelectorAll('input').forEach(i=>{if(i.name)f[i.name]=i.value;});"
                        "return {status:r.status,fields:f,len:body.length,"
                        "snippet:body.substring(0,300)};"
                        if is_last else "return {status:r.status};"
                    )
                    + "})()"
                ) % (url, QUEST_URL)
                # storm-auto retries a questionnaire step that doesn't come back 200 -- "like a
                # manual page refresh" -- up to QUEST_STEP_RETRIES (default 3). We logged the
                # non-200 and carried on regardless, so one flaky step silently poisoned the whole
                # chain and surfaced later as the much vaguer quest_state_stale.
                res = None
                for attempt in range(1, _retries + 1):
                    try:
                        res = self.session.browser_eval(js)
                    except Exception as e:
                        # storm-auto retries the EXCEPTION path too. Ours only retried a non-200
                        # RESULT, so a transient in-page fetch failure ("Failed to fetch") escaped
                        # to the outer handler and killed the whole warmup on the first blip --
                        # 306 aborted warmups in one live run.
                        self._log(f"quest step {i+1}/{len(steps)} attempt {attempt}/"
                                  f"{_retries} raised: {e}")
                        if attempt == _retries:
                            raise RuntimeError(f"quest step {i+1} failed: {e}") from e
                        time.sleep(_quest_backoff(attempt))
                        continue
                    if isinstance(res, dict) and res.get("status") == 200:
                        break
                    self._log(f"quest step {i+1}/{len(steps)} attempt {attempt}/"
                              f"{_retries} non-200: {res}")
                    # ABORT THE CHAIN on retry-exhaustion (2026-10-01). The questionnaire is a
                    # STATEFUL server-side sequence: every step builds on the one before, so a step
                    # that never returns 200 means the remaining steps and the Formulario/
                    # ScheduleController POST are already doomed. Before, this logged the non-200 and
                    # FELL THROUGH to the next step; the chain then plowed on and failed later with
                    # the vaguer `quest_state_stale` / `csrf_missing after Formulario POST` (the
                    # exact 0925 log: step 3/7 exhausted 3x503 -> step 4 -> ... -> csrf_missing).
                    # Raise here instead so the failure is named at its real cause and the recovery
                    # ladder re-runs the WHOLE chain from step 1 (you cannot resume mid-questionnaire).
                    # Raised like the exception path so _warmup_last_error records it; a 503 message
                    # does NOT match _is_transient_login_failure, so do_apply will NOT roll the exit
                    # for portal overload -- it backs off and re-warms, which is the right cure.
                    if attempt == _retries:
                        raise RuntimeError(
                            f"quest step {i+1} never returned 200 after {_retries} attempts "
                            f"(last={res}) -- aborting the warm-up chain")
                    # storm-auto sleeps here ("retrying -- like a manual page refresh"). We slept
                    # only on the EXCEPTION path before, so a non-200 burned all attempts in the same
                    # second (step 5 attempts 1-2 both at 18:41:53 in one live log). Backoff grows
                    # per attempt so a 503/WAF storm gets real recovery time, not three hits at once.
                    time.sleep(_quest_backoff(attempt))
                if is_last and isinstance(res, dict):
                    last_fields = res.get("fields") or {}
                    if not last_fields:
                        # 196 of these in one live run with nothing to explain them. storm-auto logs
                        # the body snippet for exactly this reason -- "no inputs" is a symptom, and
                        # the page itself is the only thing that says which.
                        self._log(f"quest step {i+1}/{len(steps)} (last) returned "
                                  f"status={res.get('status')} but NO input fields "
                                  f"| len={res.get('len')} snippet={str(res.get('snippet',''))[:200]!r}")

            if not last_fields:
                self._log("do_warmup: quest_state_stale (last step yielded no fields)")
                return False

            formulario_url = FORMULARIO + "?copy=true"
            _, form_html = self.session.browser_form_post(formulario_url, last_fields, timeout=100)
            csrf = _extract_csrf(form_html)
            if not csrf:
                _, form_html = self.session.browser_form_post(FORMULARIO, last_fields, timeout=100)
                csrf = _extract_csrf(form_html)
            if not csrf:
                self._log("do_warmup: csrf_missing after Formulario POST")
                return False

            # Is this posto even on offer for the residence the questionnaire just declared?
            #
            # The portal filters the posto list by declared residence, and ScheduleController answers
            # 200 whether it accepted the posto or rejected it -- so a booking configured with a
            # posto/residence pair the portal does not recognise fails identically to a lapsed
            # session, with nothing anywhere saying which. Measured 2026-08-20: booking "Test2"
            # (residence IRL, posto 2032) warmed 9/10, while "Test 3018" (residence FRA, posto 3018)
            # warmed 1/9 -- same code, same pool, same nationality, same 7-step questionnaire shape.
            #
            # Checked but NOT enforced: an empty list means the dropdown could not be parsed, which
            # is not evidence of anything, and a portal that renders the field some other way must
            # not become a hard launch failure. Say it loudly and continue.
            postos = _posto_options(form_html)
            if postos and self.posto_id not in {v for v, _ in postos}:
                offered = ", ".join(f"{v}({lbl})" for v, lbl in postos[:12])
                self._log(f"do_warmup: posto_id={self.posto_id} is NOT offered for residence "
                          f"{self.residence!r} -- the Formulario page lists {len(postos)}: {offered}"
                          f"{' ...' if len(postos) > 12 else ''}. ScheduleController will reject "
                          f"this; fix the booking's posto/residence pairing.")

            sc_payload = schedule_controller_payload(
                self.account, self.form_defaults, posto_id=self.posto_id, csrf=csrf,
                nationality=self.nationality, residence=self.residence,
            )
            # Log what ScheduleController actually said. This was fired and DISCARDED, which is
            # why "Schedule.jsp redirected to E-VISA" (the guard below) could be observed without
            # any way to see WHY the server rejected the form -- storm-auto logs both the key
            # payload fields and the response, and that is the difference between a diagnosis and
            # another guess.
            self._log(f"ScheduleController: posto_id={self.posto_id} f0sf1={sc_payload.get('f0sf1')} "
                      f"csrf_len={len(csrf)} fields={len(sc_payload)}")
            sc = self.session.browser_fetch(
                SCHED_CTRL, sc_payload, params={"posto_id": self.posto_id},
                headers={"Referer": FORMULARIO}, encode="multipart",
                file_fields=["foto", "file1", "file2", "file3", "file4"], timeout=45,
            )
            sc_body = str(sc.get("body") or "") if isinstance(sc, dict) else ""
            sc_len = (sc.get("len") if isinstance(sc, dict) else None) or len(sc_body)
            if isinstance(sc, dict):
                self._log(f"ScheduleController -> status={sc.get('status')} "
                          f"url={str(sc.get('url'))[:90]} len={sc_len}")
            # Inspect ScheduleController's OWN response before trusting anything downstream.
            #
            # The marker check below validates the warm-up by NAVIGATING Schedule.jsp -- a GET --
            # while the thing that actually has to succeed is this POST. Those come apart under the
            # WAF pattern we see constantly: GETs render fine, POSTs get challenged. When that
            # happens Schedule.jsp still returns a page carrying the scheduling markers, the guard
            # passes, and the warm-up reports success on a session that cannot POST at all.
            #
            # Confirmed live 2026-08-22, worker SOFIA FILOMENA COSTA (Test 2032). It self-discovered
            # a real slot (2026-08-28 period 3), then:
            #   21:37:10  ScheduleController -> status=200 len=44699   <- the DataDome challenge page
            #   (no "NOT warmed" -- Schedule.jsp still had markers, so the guard passed)
            #   21:37:31  do_apply: submit response is a Chrome network-error page
            # The 44699-byte body was captured here in sc_body, its length was LOGGED, and it was
            # then discarded because the GET that followed looked healthy. 44699 is unmistakable --
            # the same body appears 43 times as "quest step 7/7 returned NO input fields".
            if _is_challenge_body(sc_body):
                self._log(f"do_warmup: ScheduleController was answered with a bot challenge "
                          f"({sc_len}B) -- the session cannot POST; NOT warmed")
                return False
            # THE guard this file was missing, and the root cause of "applying doesn't work".
            # storm-auto: "ScheduleController POST can return 200 yet the server silently fails to
            # set up form state, causing Schedule.jsp to redirect to authenticated_index. Absence
            # [of scheduling markers] means ScheduleController was rejected (bad CSRF or bad session
            # state) -- must re-warm rather than returning a false 'warmed'."
            #
            # We fired ScheduleController and navigated Schedule.jsp and DISCARDED BOTH RESULTS,
            # then returned True unconditionally. So a rejected warmup reported success, do_apply
            # POSTed SubmeterVistoCriaPDF with no server-side anchor, and the portal answered with
            # the questionnaire page. Confirmed live 2026-08-14: every such bounce carried
            # title='questionnaire to apply for a visa' at 72113B -- the questionnaire, served back
            # in response to a booking submit.
            self.session.browser_nav(SCHED_JSP + "?posto_id=" + self.posto_id, timeout=30)
            sched_html = self.session.browser_eval("document.documentElement.outerHTML") or ""
            # ALREADY-HAS-A-REQUEST is TERMINAL for this identity, checked BEFORE the markers guard
            # because the portal delivers it as a JS alert ("Este requerente já tem um pedido
            # registado") that can fire while Schedule.jsp still renders with markers -- so a
            # markers-only guard would pass it as warmed and the worker would grind a permanently
            # blocked sub-account forever (a competitor booked it, or it applied within 5 days).
            # Retire the identity and stop the run (operator, 2026-09-24, with the exact alert text).
            booked_sig = self._already_booked_signal(sc_body, sched_html)
            if booked_sig:
                self._identity_unavailable = True
                self._log(f"do_warmup: portal says this applicant ALREADY HAS a request "
                          f"({booked_sig[:120]!r}) -- retiring the identity and STOPPING this worker")
                if self._emit is not None:
                    self._emit("already_booked", detail=booked_sig)
                return False
            if not any(m in sched_html for m in _SCHED_PAGE_MARKERS):
                title = re.search(r"<title[^>]*>(.*?)</title>", sched_html, re.S | re.I)
                # SAY WHY. ScheduleController answers 200 whether it accepted the form or bounced
                # it, and the body was fetched then discarded -- so the only signal a rejection left
                # behind was the response SIZE. Measured across 17 live warm-ups on 2026-08-20 that
                # split perfectly bimodally: every acceptance ~54KB, every rejection ~1.27MB (the
                # form page re-rendered with its validation errors), and the SAME identities failed
                # every single time. A deterministic, per-identity data problem was showing up as an
                # unexplained "ScheduleController was rejected".
                errs = _form_error_text(sc_body)
                self._log(f"do_warmup: Schedule.jsp has no scheduling markers "
                          f"(len={len(sched_html)} title="
                          f"{(title.group(1).strip()[:60] if title else '?')!r}) -- ScheduleController "
                          f"was rejected (response {sc_len}B); NOT warmed")
                if errs:
                    self._log("do_warmup: ScheduleController validation errors: "
                              + " | ".join(e[:120] for e in errs))
                    # The portal saying this applicant ALREADY HOLDS a future appointment is not a
                    # transient rejection -- it is permanent for as long as that appointment stands,
                    # and it means our own records are wrong. Confirmed 2026-08-23 on Test 2032:
                    # CLAUDIA BORGES and SOFIA RODRIGUES ANDRADE both hit it while still marked
                    # status=active with no BookedAppointment row, so they were re-attached run
                    # after run, each time consuming a worker slot, a proxy lease and a login --
                    # the scarcest resource we have -- to fail identically.
                    #
                    # Report it so the Hub can retire the identity. The booking itself is NOT ours
                    # to record (we do not know its date, and inventing one is worse than missing
                    # one), so this marks the identity and nothing else.
                    joined = " | ".join(errs).lower()
                    if any(m in joined for m in _ALREADY_BOOKED_MARKERS):
                        self._log("do_warmup: the portal says this applicant ALREADY HAS a future "
                                  "appointment -- reporting it so the identity stops being reused")
                        if self._emit is not None:
                            self._emit("already_booked", detail=" | ".join(e[:200] for e in errs))
                else:
                    # No recognisable error markup: record enough of the body to identify the page,
                    # so "rejected with no errors found" is still actionable rather than a dead end.
                    body_title = re.search(r"<title[^>]*>(.*?)</title>", sc_body, re.S | re.I)
                    self._log("do_warmup: ScheduleController rejected but no validation text found "
                              f"(body title={(body_title.group(1).strip()[:60] if body_title else '?')!r}, "
                              f"{sc_len}B)")
                return False
            # posto_pdf can legitimately differ from posto_id, and the submit URL must use the one
            # the schedule page itself advertises -- we hardcoded posto_id and never read this.
            m_pdf = re.search(r"SubmeterVistoCriaPDF\?posto_id=(\d+)", sched_html)
            self.posto_pdf = m_pdf.group(1) if m_pdf else self.posto_id
            self._set_anchor("chain")
            if self.posto_pdf != self.posto_id:
                self._log(f"do_warmup: posto_pdf={self.posto_pdf} differs from posto_id={self.posto_id}")
            self._note_portal("warmup", time.time() - _t0, ok=True)
            self._note_exit_ip("warmed")
            return True
        except Exception as e:
            self._warmup_last_error = str(e)
            self._note_portal("warmup", time.time() - _t0, ok=False, error=str(e))
            return False

    def do_swap(self) -> str | None:
        """Phase-1 (login/warmup) proxy -> phase-2 (hold/apply/discovery) proxy migration.

        Off by default (HOLD_SWAP_AFTER_LOGIN, catalog default False) -- most bookings just hold on
        the same proxy they logged in with, and that's the safe, unsurprising default. When the
        operator explicitly turns it on, the Hub leases a second proxy (HOLD_PROXY_PROVIDER/
        HOLD_PROXY_KIND) and passes it as `hold_proxy`; this carries the authoritative browser cookie
        jar over to a fresh session on that proxy (mirrors storm-auto's confirmed
        `_swap_to_hold_pool`: browser jar wins over any secondary client's, since it's the one DataDome
        actually trusts) and verifies the new session is still bookable before committing to it.

        Swap retry protocol (operator spec 2026-08-14): if the first attempt fails (new session
        launch errors, or the new session doesn't pass its post-swap health check), do_swap() holds a
        bit longer on the CURRENT (phase-1) proxy (HOLD_SWAP_RETRY_HOLD_SECS, e.g. 10 min), requests
        a FRESH hold-pool proxy from the Hub (the same round-trip _fresh_login's retry loop uses --
        P3's proxy rotation), and tries again. Bounded by HOLD_SWAP_RETRIES total extra attempts.
        Exhausting the budget gives up on swapping PERMANENTLY -- the worker keeps holding on its
        original phase-1 proxy and is NEVER stopped; only a genuinely fresh call to do_swap() (there
        isn't one today -- PhaseMachine calls it exactly once) would ever try again.

        Returns a swap identifier on success (currently the hold proxy's host:port -- resolving the
        TRUE external exit IP would need a live network call through the new session, e.g. an
        ip-echo fetch, which isn't implemented here since PhaseMachine only checks this value's
        truthiness, not its content; add one if a caller ever needs the real IP), or None if swap is
        off, unavailable, or every attempt failed its health check (in which case the ORIGINAL
        phase-1 session is left untouched and the worker keeps holding on it)."""
        # OFF means OFF: skip the swap step entirely and stay on the phase-1 proxy, so later
        # rotation requests keep drawing from the phase-1 pool.
        #
        # This used to flip _current_phase unconditionally, on the reading that pool selection
        # "will not be different when swap is off". That was wrong, and it silently overrode the
        # booking's own choice of provider: HOLD_PROXY_PROVIDER defaults to webshare, so a booking
        # configured PROXY_PROVIDER=soax spent most of its life on webshare instead. Confirmed live
        # 2026-08-19 -- every worker had frozen PROXY_PROVIDER='soax' and the run still took 1427
        # webshare leases against 552 soax, with every login going through p.webshare.io.
        if not self.cfg.hold_swap.swap_after_login:
            return None
        self._current_phase = "hold"
        if not self.hold_proxy:
            self._log("do_swap: HOLD_SWAP_AFTER_LOGIN is on but no hold_proxy was provided -- skipping")
            return None
        if self.session is None:
            return None

        try:
            cookies_full = self.session.get_all_cookies()
        except Exception as e:
            self._log(f"do_swap: get_all_cookies failed ({e}) -- staying on phase-1 proxy")
            return None
        cookies = {c["name"]: c["value"] for c in cookies_full if "name" in c and "value" in c}

        total_attempts = 1 + max(0, int(self.cfg.hold_swap.swap_retries))
        for attempt in range(1, total_attempts + 1):
            if attempt > 1:
                hold_secs = max(0.0, self.cfg.hold_swap.retry_hold_secs)
                self._log(f"do_swap: attempt {attempt - 1}/{total_attempts} failed -- holding on "
                          f"phase-1 proxy for {hold_secs}s before retrying (final chance "
                          f"{attempt}/{total_attempts})")
                if hold_secs:
                    time.sleep(hold_secs)
                # Roll the hold proxy's SESSION ID first -- same rule as every other retry path:
                # the session id is the exit's identity, so retrying a failed swap against the
                # unchanged id aims at the same exit that just refused us. Free, unbounded, and no
                # lease needed; _request_proxy_rotation stays as the fallback.
                #
                # Phase 5 (D33 follow-up): NOT when self.hold_proxy_static is True. roll_sticky_session
                # is a grammar-based string transform with no idea what resource_type means -- it was
                # only ever safe for a static credential by the accident of an undefined static
                # grammar (PROXY_CONTRACTS.md: "we own none"). The instant a real static credential
                # happens to carry a pattern the roller recognises (plausible: PROXY_CONTRACTS.md
                # frames "static ISP proxy" as possibly the same providers' own static tier), rolling
                # it would silently reassign the "static" resource's actual exit IP -- exactly the
                # instability a static hold resource exists to avoid, and invisibly, since D2's
                # design is a zero-DB-write local transform. A static hold identity is never locally
                # transformed; the Hub (which now enforces resource_type='static' on every hold lease,
                # not just the first one) is asked for a fresh one instead.
                rolled = None
                if not self.hold_proxy_static:
                    try:
                        from libs.proxies.lease import roll_sticky_session
                        rolled = roll_sticky_session(self.hold_proxy) if self.hold_proxy else None
                    except Exception:
                        rolled = None
                if rolled and rolled != self.hold_proxy:
                    self.hold_proxy = rolled
                    self._log(f"do_swap: rolled the hold proxy to a fresh exit "
                              f"[{_proxy_label(rolled)}]")
                else:
                    if self.hold_proxy_static:
                        self._log("do_swap: hold resource is static -- requesting a fresh lease "
                                  "through the Hub instead of rolling")
                    new_proxy = self._request_proxy_rotation("hold")
                    if new_proxy:
                        self.hold_proxy = new_proxy

            swap_id = self._attempt_swap(cookies, cookies_full)
            if swap_id is not None:
                return swap_id

        self._log(f"do_swap: gave up after {total_attempts} attempt(s) -- staying on phase-1 proxy "
                  f"permanently, worker keeps holding")
        return None

    def _attempt_swap(self, cookies: dict, cookies_full: list) -> str | None:
        """One swap attempt against self.hold_proxy. Never touches self.session on failure -- the
        caller's retry loop (do_swap) relies on the ORIGINAL phase-1 session staying untouched and
        usable between attempts."""
        try:
            new_session = get_session(
                proxy=self.hold_proxy, inject_cookies=cookies, inject_cookies_full=cookies_full,
                headless=self.headless, fp_seed=self.account.get("username"),
            )
        except Exception as e:
            self._log(f"do_swap: new session launch failed ({e}) -- staying on phase-1 proxy")
            return None

        old_session = self.session
        self.session = new_session
        if not self._probe_swapped_session():
            self._log("do_swap: new session failed health check -- reverting to phase-1 proxy")
            self.session = old_session
            try:
                new_session.close_browser()
            except Exception:
                pass
            return None

        try:
            old_session.close_browser()
        except Exception:
            pass
        swap_id = _proxy_label(self.hold_proxy)[len("proxy="):]   # endpoint#session-id, no creds
        self._log(f"do_swap: swapped to hold proxy {swap_id}")
        return swap_id

    def await_slot_open(self) -> str:
        if self._signals is None:
            return "slot_open"
        self._initial_schedule_warm("await_slot_open")
        while True:
            # Was slot_open ALREADY known before this wait? If so the delivery that returns below is
            # a redelivery (the Hub's _latch_signals re-sends to a worker not yet confirmed notified),
            # i.e. duplicated rather than fresh -- see the signal taxonomy.
            _already = getattr(self._signals, "slot_open_seen", False)
            result = self._signals.wait_slot_open(timeout=self._keepalive_window())
            if result == "slot_open":
                self._new_sig_id()
                if _already:
                    self._slog("signal_duplicated", signal="slot_open")
                else:
                    self._slog("signal_received", signal="slot_open")
                    self._slog("signal_accepted", signal="slot_open")
                return result
            if result == "stop":
                self._slog("signal_ignored", signal="slot_open", reason="stop")
                return result
            if getattr(self._signals, "prewarm_requested", False):
                # The Hub says slot-open is imminent. Re-warm NOW rather than waiting for this
                # worker's own keepalive cycle to come round: the portal's form state dies on a
                # ~16-minute clock (measured 2026-08-20), so without this a worker enters the
                # decisive window at whatever point in that cycle it happens to be. Confirmed cost
                # of getting it wrong -- worker EDER's re-warm failed 38s AFTER slot-open fired and
                # it spent eight minutes recovering, while a worker whose state happened to be fresh
                # booked in 47 seconds.
                self._signals.prewarm_requested = False
                self._log("prewarm: slot-open imminent -- re-warming now")
                self._run_warmup_steps()
                continue
            self._do_keepalive("await_slot_open")

    # ── scout ─────────────────────────────────────────────────────────────────
    def await_discover_task(self) -> list[str] | None:
        if self._signals is None or not hasattr(self._signals, "wait_discover_task"):
            return None   # no bus wired -> nothing to discover, stop immediately
        while True:
            result = self._signals.wait_discover_task(timeout=self._keepalive_window())
            if result is not None or self._signals.stopped:
                # Hub's shared gettime cache (apps/hub/scheduler.py's _drive_scouts) rides along on
                # the SAME `discover` command when fresh -- stash it for the next do_discover() call
                # so a cache hit skips the real gettime fetch entirely.
                self._pending_special_days = getattr(self._signals, "last_discover_special_days", None)
                # DISCOVERY_SOURCE the Hub pushed on this discover command (2026-09-26). Lets a
                # config change take effect on the NEXT round of a RUNNING scout with no restart --
                # do_discover honours it over its own frozen cfg when present.
                self._pending_discover_source = getattr(self._signals, "last_discover_source", None)
                if self._signals.stopped:
                    self._slog("signal_ignored", signal="discover", reason="stop")
                elif result is INACTIVATE:
                    # False slot-open: the Hub told this scout to drop back to holding -- a real
                    # signal the worker deliberately declines to act on as discovery.
                    self._new_sig_id()
                    self._slog("signal_ignored", signal="discover", reason="false_slot_open")
                else:
                    self._new_sig_id()
                    self._slog("signal_received", signal="discover",
                               dates=(len(result) if isinstance(result, list) else result))
                    self._slog("signal_accepted", signal="discover")
                return result
            self._do_keepalive("await_discover_task")

    # ── keepalive: the actual reason a "hold" exists ─────────────────────────
    # A holding worker keeps the portal SESSION alive (server-side auth expiry) and, transitively,
    # keeps last_seen fresh (Hub's stall-reaper reaps any worker silent longer than
    # WORKER_STALL_TIMEOUT -- a genuinely healthy multi-hour hold with zero traffic looked
    # indistinguishable from a dead one, and got killed/respawned in a loop). "Loose" vs "hot"
    # holding is just how KEEPALIVE_INTERVAL is tuned per booking/template -- a long interval (the
    # 900s default) pings rarely and cheaply; a short one keeps hammering the portal for maximum
    # apply-readiness at the cost of more traffic/detection risk. No separate mode flag needed.
    def _keepalive_window(self) -> float:
        # Defensive floor beyond the resolver's own min_value=1.0 on KEEPALIVE_INTERVAL -- that floor
        # exists to catch a resolved 0 (F14's incident shape), not to bless a 1s portal-ping cadence.
        interval = max(float(self.cfg.keepalive.interval), 30.0)
        jitter = max(0.0, float(self.cfg.keepalive.jitter))
        mode = (getattr(self.cfg.keepalive, "mode", "auth_only") or "auth_only").strip().lower()
        if mode == "schedule":
            # ACTIVE cursor-keeping: a warmed Schedule.jsp cursor lapses after FORM_STATE_TTL, so the
            # ping cadence MUST stay under it or the "kept" cursor is already stale by the next cycle.
            # Cap the window (and its jitter) to SCHEDULE_KEEPALIVE_TTL_FRACTION x TTL so a re-warm
            # lands before the anchor ages out. LOWER fraction = fresher cursor (more slot-opens hit
            # an instant direct-apply) at the cost of MORE re-warm traffic -- the operator tunes this
            # per booking against portal conditions (default 0.75; 0.5 near-eliminates the lapse).
            ttl = max(1.0, float(self.cfg.raw.get("FORM_STATE_TTL", 240.0)))
            try:
                frac = float(self.cfg.raw.get("SCHEDULE_KEEPALIVE_TTL_FRACTION", 0.75) or 0.75)
            except (TypeError, ValueError):
                frac = 0.75
            frac = min(0.95, max(0.2, frac))   # stay strictly under the TTL, never a hot 1s ping
            cap = max(30.0, ttl * frac)
            interval = min(interval, cap)
            jitter = min(jitter, max(0.0, cap - interval))
        return interval + random.uniform(0, jitter)

    def _slot_open_seen(self) -> bool:
        return self._signals is not None and getattr(self._signals, "slot_open_seen", False)

    def _slot_found_seen(self) -> bool:
        return self._signals is not None and getattr(self._signals, "slot_found_seen", False)

    def slot_found_seen(self) -> bool:
        """EngineOps surface for the same flag the keepalive loop reads privately. PhaseMachine uses
        it so an apply worker that respawns mid-run skips a slot_open handshake that already
        happened -- see _run_apply."""
        return self._slot_found_seen()

    def _note_exit_ip(self, when: str) -> bool:
        """Record the exit address this session is presenting, and say so if it MOVED.

        The portal binds a session to the IP that established it, and our exits rotate underneath us
        (sessionlength 3600s, idlettl 900s, plus residential IPs simply going offline). MEASURED
        2026-08-23: half of all warmed workers later died on the network. Until now nothing observed
        its own exit, so a rotation surfaced as "form-state lapsed", "the portal is declining", or
        "Por favor, confirma o reCAPTCHA" -- three different portal diagnoses for one event, each
        answered by re-warming or re-logging in FROM THE NEW IP, which is why they so rarely worked.

        Returns True only when the exit is KNOWN to have moved since the last observation. An
        unknown reading is never "changed" -- see exit_changed's docstring for why that bias matters.

        No longer observation-only. `login` acts on it: a "Por favor, confirma o reCAPTCHA" that
        coincides with the exit moving is OUR address rotating across the solve window, not the
        account failing a challenge, and charging it to the account's 2-strike budget spends a
        sub-account on our own infrastructure. MEASURED 2026-08-25 on Test 2032: PEDRO TEIXEIRA
        RAMOS burned 3 of 3 candidate sub-accounts and AMILCAR WANDERSON 2 of 2, every one on this
        verdict, with "exit IP CHANGED under this session" logged immediately beforehand -- and both
        workers ended FAILED with no accounts left while slots sat open.
        """
        # OBSERVE THROUGH THE BROWSER when a session exists, never with a raw client.
        #
        # A raw probe is not free: measured 2026-08-24, a primp request to the PORTAL flags the exit
        # with DataDome and the browser nav that follows on the same sticky session then fails
        # (browser-only 5/6 ok, primp-then-browser 0/6). This asks api.ipify.org rather than the
        # portal, so it should not touch DataDome at all -- but "should not" is exactly the
        # reasoning that produced that bug, so when a browser is already open we use it and take no
        # chances. primp remains the fallback only when there is no session to borrow.
        try:
            from libs.proxies.exit_ip import IP_ECHO_URL, exit_changed, observe_exit_ip
        except Exception:
            return False
        ip = None
        if self.session is not None:
            # FETCH, NEVER NAVIGATE.
            #
            # This used to browser_nav() to the echo, which took the live browser OFF the portal --
            # and _run_warmup_steps calls it at "warmed", i.e. immediately after the chain finishes
            # establishing the anchor. Everything downstream is an in-page fetch, so they were all
            # then issued from api.ipify.org: cross-origin, blocked, "TypeError: Failed to fetch".
            # CONFIRMED live 2026-08-25, SERGIO PEREIRA, and it cost ~20s of critical path too:
            #
            #   12:17:45  ScheduleController -> 200      <- anchor built, page on Schedule.jsp
            #   12:18:06  /slots primer failed: TypeError: Failed to fetch
            #   12:18:06  page is not on a live portal origin before the submit -- re-anchoring
            #   12:18:21  submit -> form_stale
            #
            # Exactly the mistake the /slots primer made (see do_apply), in the same file, from the
            # same assumption that a navigation is a free way to read one value. A cross-origin
            # fetch reads the same value, keeps the page where it is, and takes about a second --
            # the echo sends Access-Control-Allow-Origin: *, so it is readable from any origin.
            try:
                body = self.session.browser_eval(
                    "(async()=>{try{const r=await fetch(%r,{cache:'no-store'});"
                    "return (await r.text()).trim();}catch(e){return '';}})()" % IP_ECHO_URL,
                    timeout=15)
                body = (body or "").strip() if isinstance(body, str) else ""
                if body and len(body) <= 45 and " " not in body and "<" not in body:
                    ip = body
            except Exception:
                ip = None
        if ip is None and self.warm_proxy:
            try:
                ip = observe_exit_ip(self.warm_proxy)
            except Exception:
                return False
        if ip is None:
            return False    # unknown, NEVER treated as changed -- see exit_changed's docstring
        prev = getattr(self, "_exit_ip", None)
        moved = exit_changed(prev, ip)
        if moved:
            self._log(f"exit IP CHANGED under this session ({prev} -> {ip}) at {when} -- the portal "
                      f"binds sessions to the establishing IP, so anything that fails from here is "
                      f"our address moving, not the portal rejecting us")
            if self._emit is not None:
                self._emit("exit_ip_changed", before=prev, after=ip, at=when)
        elif prev is None:
            self._log(f"exit IP {ip} ({when})")
        self._exit_ip = ip
        return moved

    def _mark_keep_bookable(self, bookable: bool, why: str = "") -> None:
        """Reflect whether this holding worker has a LIVE, bookable session -- and make KEEPING mean
        HEALTHY (operator 2026-09-26).

        Before, a holding worker whose keepalive found the session dead, or that was mid-rebuild,
        STAYED substate=KEEPING and only carried a NOT_BOOKABLE_PREFIX detail. That was the "holding
        is a lie": a dead worker showed KEEPING, looked like ready capacity, and (until _assign_slots
        learned to read the detail) got handed a real slot it then failed on. Now an unhealthy /
        rebuilding worker LEAVES KEEPING and shows WARMING_UP (it is re-establishing the bookable
        session -- re-login + re-warm); only a live, bookable session shows KEEPING. So KEEPING is
        healthy by construction, and _assign_slots (which selects substate==KEEPING) excludes an
        unhealthy worker by the SUBSTATE alone, not only by the detail (kept as belt-and-suspenders +
        a human-readable reason). Emits the same `status_change` event on_transition uses, so it
        flows through events.py -> Worker.substate/substate_detail with no new plumbing.
        """
        # Tracked, not just emitted: the recovery path has to know whether ANYTHING marked this
        # worker unbookable, so a recovered worker is returned to KEEPING (not left stuck).
        self._keep_unbookable = not bookable
        if self._emit is None:
            return
        if bookable:
            self._emit("status_change", substate="keeping", detail="")
        else:
            # WARMING_UP, not KEEPING: the session is not a live hold -- it is being rebuilt.
            self._emit("status_change", substate="warming_up", detail=f"{NOT_BOOKABLE_PREFIX} {why}")

    def _keepalive_read_page(self) -> str:
        """Read the probe page ONCE it has settled, so a still-navigating home page is not misread
        as a dead session. Two guards against the same nav race (root cause of the 2026-09-21
        keeping failure -- 49 'Execution context was destroyed' + 38 UNKNOWN on live, healthy
        sessions):

          1. SETTLE FIRST -- poll briefly for document.readyState=complete with body text present,
             so we read a rendered page whose markers are actually in the DOM (kills the UNKNOWN
             mid-render reads). Best-effort: a page that never settles (dead/blocked) just falls
             through to the read, which still classifies it correctly.
          2. RE-READ ONCE on 'Execution context was destroyed' -- that specific error IS the nav
             race, so settle a beat and read again. A genuinely dead page throws it BOTH times and
             the exception propagates to the outer handler (correctly counted toward dead-trip); a
             race recovers on the retry and never touches the dead-streak.
        """
        settle_js = (
            "(async () => { const dl = Date.now() + %d;"
            "  const ready = () => document.readyState === 'complete'"
            "    && !!document.body && (document.body.innerText || '').trim().length > 0;"
            "  while (Date.now() < dl) { if (ready()) return true;"
            "    await new Promise(r => setTimeout(r, 150)); } return false; })()"
            % self._KEEPALIVE_SETTLE_MS
        )
        try:
            self.session.browser_eval(settle_js)
        except Exception:
            pass   # settle is best-effort; the read below is the real check
        try:
            return self.session.browser_eval("document.documentElement.outerHTML") or ""
        except Exception as e:
            if "execution context was destroyed" not in str(e).lower():
                raise
            time.sleep(1.0)
            return self.session.browser_eval("document.documentElement.outerHTML") or ""

    def _initial_schedule_warm(self, context: str) -> None:
        """KEEPALIVE_MODE='schedule' only: establish the bookable cursor IMMEDIATELY on entering a
        KEEPING wait, instead of waiting out the first keepalive window. No-op in auth_only mode,
        when keepalive is off, or when already warmed. A slot-open/assignment fired during the warm
        is NOT lost -- the Hub's signal latch re-sends to a not-yet-notified worker -- so warming
        before the first listen is safe."""
        mode = (getattr(self.cfg.keepalive, "mode", "auth_only") or "auth_only").strip().lower()
        if mode != "schedule" or self.cfg.keepalive.off:
            return
        try:
            if self.is_warmed():
                return
        except Exception:
            pass
        self._do_keepalive(context + "/initial")

    def _do_keepalive(self, context: str) -> None:
        if self.cfg.keepalive.off:
            return
        # Cheap (raw HTTP, no browser) and it runs before the probe, so a session whose exit moved
        # says so BEFORE we start attributing the resulting failures to the portal.
        self._note_exit_ip(f"keepalive/{context}")
        if self.session is None:
            # A prior restore attempt failed outright. Confirmed gap: the original version simply
            # stopped probing forever once this happened (`session is None` short-circuited every
            # future call), so a worker in this state silently held its proxy lease and DB RUNNING
            # row until the 25-minute stall reaper eventually killed it -- storm-auto instead keeps
            # retrying (bounded) and would report failed immediately if it gives up. Bounded by
            # _MAX_KEEPALIVE_RESTORE_ATTEMPTS so this can't burn proxies/CAPTCHA solves forever.
            if getattr(self, "_keepalive_restore_attempts", 0) >= _MAX_KEEPALIVE_RESTORE_ATTEMPTS:
                return
            self._attempt_keepalive_restore(context)
            return
        # AUTH-ONLY (2026-09-09, consolidated from the HOLD_AUTHED_ONLY A/B -- Test 0907 vs
        # Test 0907-auth matched on outcome, 2/2 booked each, ~same apply latency, while the
        # authed-only side ran zero full Schedule.jsp re-warm cycles over a 21h hold vs one every
        # ~20min on the schedule-keepalive side). The routine hold-time probe is always the cheap
        # authenticated home-page check now -- it never re-verifies or re-warms the booking
        # form-state. do_apply already unconditionally re-warms at the start of every round
        # regardless of what keepalive believed, and (since C) warmup itself is deferred to the
        # slot-open/assignment signal rather than done proactively before hold -- so there is no
        # form-state to verify here even in principle. SESSION_EXPIRED and dead-trip detection
        # below are unaffected: they never depended on this.
        mode = (getattr(self.cfg.keepalive, "mode", "auth_only") or "auth_only").strip().lower()
        probe = self.cfg.keepalive.probe or "home"
        if mode == "schedule" and probe == "home":
            # KEEPALIVE_MODE='schedule' -- ACTIVE cursor-keeping (operator 2026-10-02; replaces the
            # 2026-10-01 measurement-only probe). The worker holds a WARM, bookable Schedule.jsp
            # session through KEEPING instead of an auth-only one: each cycle re-establishes the
            # booking form cursor BEFORE it lapses (FORM_STATE_TTL -- _keepalive_window paces this
            # branch under the TTL). auth_only mode is unchanged and remains the default.
            #
            # CAVEAT, recorded verbatim so it is not re-litigated: the cursor is NAVIGATION-bound and
            # do_apply STILL re-warms unconditionally before the submit, so this does NOT by itself
            # make slot-found -> apply "instant" -- gating do_apply's re-warm on is_warmed() was
            # measured to submit a stale questionnaire ([[rewarm_every_round_is_mandatory]]). What
            # active keeping delivers is the thing the operator asked for: KEEPING is a genuinely
            # warm/bookable session, not auth-only. Opting do_apply into trusting the kept cursor is
            # a SEPARATE, measured-risky change, deliberately not bundled here.
            probe = SCHED_JSP + "?posto_id=" + str(self.posto_pdf or self.posto_id)
        url = HOME_URL if probe == "home" else probe
        try:
            final_url = self.session.browser_nav(url, timeout=30)
            if mode == "schedule" and probe != "home":
                # Schedule.jsp carries DIFFERENT markers than home, so classify it on its own terms:
                # auth loss = Authentication.jsp redirect; dead exit = chrome-error. Both reuse the
                # auth-only path's handling verbatim.
                if "Authentication.jsp" in (final_url or ""):
                    self._log(f"keepalive[schedule]: session expired (auth redirect, {context}) -- "
                              f"re-authenticating")
                    self._attempt_keepalive_restore(context)
                    return
                page = self._keepalive_read_page()
                if self._is_dead_exit_response(page, final_url):
                    self._log(f"keepalive[schedule]: DEAD EXIT / chrome-error ({len(page)}B, "
                              f"{context}) -- rolling to a fresh exit and re-authenticating")
                    self._roll_exit("keepalive: dead exit (schedule probe)")
                    self._attempt_keepalive_restore(context)
                    return
                self._keepalive_dead_streak = 0
                self._portal_down_streak = 0
                cursor_present = any(m in page for m in _SCHED_PAGE_MARKERS)
                if cursor_present and self.is_warmed():
                    # Live, VERIFIED-fresh cursor (chain anchor within FORM_STATE_TTL). Nothing to do
                    # but keep the worker marked bookable -- no re-warm, no wasted chain.
                    if getattr(self, "_keep_unbookable", False):
                        self._mark_keep_bookable(True)
                    if self._emit is not None:
                        self._emit("log_line", text=(
                            f"keepalive[schedule]: cursor live & fresh ({len(page)}B, {context})"))
                    self._slog("keepalive", result="ok", mode="schedule", cursor="fresh",
                               bytes=len(page), ctx=context)
                    return
                # Cursor ABSENT (never warmed, or a bare nav verified auth but not the form) or the
                # anchor has aged past FORM_STATE_TTL -- RE-WARM to re-establish a bookable cursor,
                # which is the whole point of active keeping. A failed re-warm must NOT discard the
                # authenticated session: mark the worker not-bookable (it then shows WARMING_UP, per
                # _mark_keep_bookable / "holding is a lie") and let the next cycle try again.
                self._log(f"keepalive[schedule]: cursor "
                          f"{'ABSENT' if not cursor_present else 'stale'} -- re-warming to keep it live "
                          f"({context})")
                if self._run_warmup_steps():
                    self._mark_keep_bookable(True)
                    self._slog("keepalive", result="ok", mode="schedule", cursor="rewarmed",
                               ctx=context)
                else:
                    self._mark_keep_bookable(False, why="schedule keepalive re-warm failed")
                    self._slog("keepalive", result="rewarm_failed", mode="schedule", ctx=context)
                return
            if probe != "home":
                # A custom KEEPALIVE_PROBE URL: no positive/negative content evidence is known for
                # an arbitrary operator-chosen page, so this stays exactly what it was -- a
                # successful nav is all that's checked. Only the two real pages (home, Schedule.jsp)
                # get classified below, so this is the one path where a bare successful nav IS the
                # whole check and resetting the streak here is correct.
                self._keepalive_dead_streak = 0
                self._portal_down_streak = 0
                if self._emit is not None:
                    self._emit("log_line", text=f"keepalive: probe OK ({context})")
                return
            page = self._keepalive_read_page()
            # CONFIRMED LIVE 2026-09-03 ("Test 0901"): this reset used to sit right after
            # browser_nav, above -- before browser_eval, which is the call that actually throws
            # "Execution context was destroyed, most likely because of a navigation" (a nav race,
            # not evidence of a dead session, per the 2026-09-02 comment on the except branch
            # below). Resetting on the nav alone meant every failing cycle netted exactly
            # dead_streak=1 (0, just reset, +1 from this cycle's own eval exception) forever --
            # `dead_trip` (default 3) requires CONSECUTIVE cycle failures to accumulate, and this
            # structurally could never reach it: 3 apply workers sat at "streak=1/3" for 2+ hours
            # straight, so _attempt_keepalive_restore never fired even once. The reset now only
            # fires once the FULL cycle -- nav AND eval -- has completed without raising, so a
            # nav-succeeds-then-eval-throws cycle correctly counts as one failure toward dead_trip.
            self._keepalive_dead_streak = 0
            self._portal_down_streak = 0
            state = classify_session_state(page, final_url, warmed=False)
            if state == SessionState.AUTHENTICATED:
                # Only fires on a RECOVERY transition (was marked not-bookable, e.g. by the
                # dead-trip branch below), matching the original behaviour of never emitting a
                # bookable-reset event on an already-healthy cycle -- an event on every single
                # healthy tick is exactly the kind of write-amplification F6 exists to guard
                # against. Not gated on "warmed" any more: there is no form-state left to verify,
                # so a clean authenticated read is recovery evidence on its own.
                if getattr(self, "_keep_unbookable", False):
                    self._mark_keep_bookable(True)
                if self._emit is not None:
                    self._emit("log_line", text=f"keepalive: probe OK ({context})")
                self._slog("keepalive", result="ok", mode="auth_only", ctx=context)
                return
            if state == SessionState.SESSION_EXPIRED:
                # F22/F24's fix: a dead session must never be re-warmed (there is no session left
                # to POST a questionnaire step against) -- it must be re-authenticated. This is the
                # SAME chain the transport-failure branch below already uses, reused, not new
                # machinery.
                self._log(f"keepalive: session expired ({len(page)}B, exit={getattr(self, '_exit_ip', None)}, "
                          f"{context}) -- re-authenticating rather than re-warming a dead session")
                self._slog("keepalive", result="expired", bytes=len(page), ctx=context)
                self._attempt_keepalive_restore(context)
                return
            if state == SessionState.PORTAL_ERROR:
                # F25's fix: a portal declining everyone is neither a lapsed form nor a dead
                # session. Back off and let the next keepalive cycle re-probe -- no re-warm
                # (cannot succeed against a refusing portal), no re-login (would burn a CAPTCHA
                # solve for a condition that has nothing to do with our credentials).
                self._log(f"keepalive: portal is refusing requests ({len(page)}B, "
                          f"exit={getattr(self, '_exit_ip', None)}, {context}) -- "
                          f"backing off, not re-warming and not re-logging in")
                return
            # DEAD EXIT returned as a string (2026-09-23). classify_session_state reads a
            # chrome-error / dead-tunnel page as UNKNOWN (it carries none of the portal/session
            # markers), and the UNKNOWN branch below just re-probes -- on the SAME dead exit,
            # forever. Confirmed the dominant stall on 'Prev 0923-3018': warmed workers sat in
            # await_assignment emitting hundreds of "unrecognised pre-warm response (185266B)" while
            # their tunnel was dead, never assignable and never applying. A dead exit is not
            # inconclusive: roll to a fresh one and re-authenticate on it, the same action the login
            # path already takes when the very same markers arrive as a raised exception.
            if self._is_dead_exit_response(page, final_url):
                self._log(f"keepalive: DEAD EXIT / chrome-error ({len(page)}B, "
                          f"exit={getattr(self, '_exit_ip', None)}, {context}) -- rolling to a fresh "
                          f"exit and re-authenticating rather than re-probing a dead tunnel")
                self._slog("keepalive", result="dead_exit", bytes=len(page), ctx=context)
                self._roll_exit("keepalive: dead exit (chrome-error/tunnel)")
                self._attempt_keepalive_restore(context)
                return
            # UNKNOWN: no positive AND no negative evidence -- there is no form-state to re-warm
            # (auth-only holding never establishes one), so this is always "inconclusive, wait for
            # the next cycle" now, never a re-warm trigger.
            self._log(f"keepalive: unrecognised pre-warm response ({len(page)}B, {context}) -- "
                      f"inconclusive, will re-probe")
            self._slog("keepalive", result="inconclusive", bytes=len(page), ctx=context)
        except Exception as e:
            # STRESS MODE (KEEPALIVE_STRESS_HOLD): a HELD warm session is gold when re-login is the
            # hardest thing in stress, so do NOT roll + re-authenticate it merely because the PORTAL
            # is in a down-window. A probe exception that is a portal-reachability error (timeout /
            # connection closed/reset/refused / empty response) and NOT a proxy/tunnel/chrome-error
            # means the exit is reaching the net but the portal did not answer -- a down-window, not
            # a dead session. Record the portal DOWN and HOLD, re-probing next cycle. Bounded by
            # ~4x dead_trip so a "portal down" that is really a slowly-dying exit still escalates.
            _low = str(e).lower()
            _proxy_dead = any(m in _low for m in ("err_tunnel", "err_proxy", "err_socks",
                              "chromewebdata", "chrome-error", "err_name_not_resolved", "err_address"))
            _portal_down = any(m in _low for m in ("err_timed_out", "timed out", "timeout",
                              "err_connection_closed", "err_connection_reset",
                              "err_connection_refused", "err_empty_response"))
            if (bool(self.cfg.raw.get("KEEPALIVE_STRESS_HOLD", False))
                    and _portal_down and not _proxy_dead):
                self._portal_down_streak = getattr(self, "_portal_down_streak", 0) + 1
                _patience = max(1, int(self.cfg.keepalive.dead_trip)) * 4
                if self._portal_down_streak < _patience:
                    if self._liveness is not None:
                        self._liveness.record_down(self.sub_account_id)
                    if self._emit is not None:
                        self._emit("log_line", text=f"keepalive: portal down-window, exit reachable "
                                   f"-- HOLDING the warm session (stress {self._portal_down_streak}/"
                                   f"{_patience}) ({context})")
                    self._slog("keepalive", result="portal_down_hold", error_type=type(e).__name__,
                               streak=f"{self._portal_down_streak}/{_patience}", ctx=context)
                    return          # ride the window: no roll, no re-login
                # persisted far too long -- the exit may genuinely be dead; fall through to the
                # dead-trip ladder WITHOUT resetting the streak (so it stays past patience and keeps
                # escalating each cycle, instead of oscillating hold<->fall-through forever). It
                # resets only on a clean probe or after a restore.
            self._keepalive_dead_streak = getattr(self, "_keepalive_dead_streak", 0) + 1
            dead_trip = max(1, int(self.cfg.keepalive.dead_trip))
            if self._emit is not None:
                self._emit("log_line", text=f"keepalive: probe FAILED ({e}) "
                                             f"streak={self._keepalive_dead_streak}/{dead_trip} ({context})")
            self._slog("keepalive", result="probe_failed", error_type=type(e).__name__,
                       streak=f"{self._keepalive_dead_streak}/{dead_trip}", ctx=context)
            if self._keepalive_dead_streak >= dead_trip:
                self._keepalive_dead_streak = 0
                self._portal_down_streak = 0
                # NOT BOOKABLE, for the same reason the re-warm branch above already says so: at
                # this point the code has concluded the session is dead, but the substate stays a
                # bare KEEPING and _assign_slots counts it as ready capacity. The fix was applied to
                # the re-warm branch in 2026-08-21 and this branch was missed.
                # CONFIRMED LIVE 2026-09-02 (20:45 run of "Test 0901"): worker 1d1a9033 was handed
                # the pool's ONLY available slot at 22:22:04, went straight to `applying`, and
                # discovered there that its own page was not on a portal origin at all -- "the
                # tunnel is genuinely down". It released the slot 8 seconds later as `session`.
                # Marked here rather than on every probe failure: a single failure is frequently the
                # spurious "Execution context was destroyed" nav race (6-8 per worker per hold in
                # the 17:15 run), and marking on those would delete real capacity over nothing.
                # The dead-trip is the point at which this code already believes the session is gone.
                self._mark_keep_bookable(False, f"keepalive dead-trip ({context})")
                # A SESSION THAT IS NOT VALID FOR ITS PURPOSE IS NOT WORTH KEEPING (operator
                # principle, 2026-09-11). This used to also refuse to restore when slot_found had
                # not fired yet -- the LOOSE HOLD rule, whose premise was that a pre-slot-found
                # blip must never burn a re-login because the login wall is the scarce resource.
                #
                # That premise misreads what dead-trip is. Dead-trip is KEEPALIVE_DEAD_TRIP
                # CONSECUTIVE failures, not a blip -- the comment above already says "the point at
                # which this code already believes the session is gone". Keeping a session we
                # believe is gone saves nothing: the re-login is owed either way, and the only
                # question is whether it is paid now, during a quiet hold, or discovered at
                # slot-open when the worker is needed and has nothing.
                #
                # CONFIRMED LIVE 2026-09-11 (booking 0911): workers sat at
                # "not bookable: keepalive dead-trip" for HOURS under this rule, were still dead
                # when slot-open fired, and the booking could not book. Paying the login early is
                # strictly better. KEEPALIVE_NO_RESTORE remains honoured as the explicit
                # diagnostic opt-out.
                if self.cfg.keepalive.no_restore:
                    if self._emit is not None:
                        self._emit("log_line", text="keepalive: dead-trip reached but "
                                                      "KEEPALIVE_NO_RESTORE is set -- leaving the "
                                                      "session dead rather than rebuilding it")
                else:
                    self._attempt_keepalive_restore(context)

    def _attempt_keepalive_restore(self, context: str = "") -> bool:
        """Returns whether the session came back. The session looks dead -- try the same checkpoint-restore-then-fresh-login chain a fresh
        launch uses, rather than silently sitting on a dead session until the next real signal
        arrives and do_apply/do_discover fails outright."""
        # getattr, not attribute access: this counter is created lazily on first use.
        if getattr(self, "_keepalive_restore_attempts", 0) >= _MAX_KEEPALIVE_RESTORE_ATTEMPTS:
            return False        # bounded: never let escalation become its own unbounded loop
        self._keepalive_restore_attempts = getattr(self, "_keepalive_restore_attempts", 0) + 1
        if self._emit is not None:
            self._emit("log_line", text=f"keepalive: attempting restore "
                                         f"(attempt {self._keepalive_restore_attempts}/"
                                         f"{_MAX_KEEPALIVE_RESTORE_ATTEMPTS}, {context})")
        # THE WORKER IS NOT BOOKABLE FOR THE DURATION OF THIS REBUILD -- and now LEAVES KEEPING for
        # WARMING_UP while it rebuilds (keeping = healthy, 2026-09-26), so _assign_slots excludes it
        # by substate. The chain below closes the browser and runs a full checkpoint-restore-or-login,
        # which can take minutes. _assign_slots selects on `substate == KEEPING`, so before this a
        # worker mid-rebuild still showed KEEPING and the Hub handed it a real slot. CONFIRMED LIVE
        # 2026-09-02 (20:45 run of "Test 0901"): the pool's ONLY available
        # slot, 2026-09-30 p5, was leased at 22:22:04 to worker 41f656e6, which was inside this
        # very chain and did not reach `applying` until 22:24:41 -- 2m37s later -- whereupon its
        # warm-up failed with csrf_missing and it released the slot as `session`. The other lease
        # went to a worker whose tunnel was dead. One slot, two workers, both unable to act, and it
        # sat locked for three minutes.
        #
        # This is the marker that already exists for exactly this purpose; it simply was not set
        # here. Cleared on success below, and left set on failure -- a worker whose restore failed
        # is not bookable either, and the next successful restore or re-warm clears it.
        self._mark_keep_bookable(False, "rebuilding the session")
        try:
            if self.session is not None:
                self.session.close_browser()
        except Exception:
            pass
        self.session = None
        ok = self._try_restore_from_checkpoint() or self._fresh_login()
        # RE-WARM THE FORM ONLY IN SCHEDULE MODE. A checkpoint restore re-AUTHENTICATES only (it SETS
        # posto_pdf from the file without rebuilding anything, so `_anchor_src != "chain"` means the
        # form-state anchor is not verified on THIS session). In schedule mode KEEPING must hold a
        # warm, bookable cursor, so the restore re-warms it. In auth_only, warmup is DEFERRED to the
        # signal (do_apply/do_discover warm on demand) -- re-warming the form here put the worker in
        # WARMING_UP before slot_found/slot_open, violating the deferred model. CONFIRMED live
        # 2026-10-02 ("Booking w Auth"): a dead-exit keepalive restore ran the full warmup chain
        # ~10 min before slot_found. So auth_only restore stops at re-auth; the session is bookable
        # (authenticated) and the next do_apply/do_discover establishes the form-state when needed.
        _mode = (getattr(self.cfg.keepalive, "mode", "auth_only") or "auth_only").strip().lower()
        if ok and _mode == "schedule" and self._anchor_src != "chain":
            ok = self._run_warmup_steps()
        if ok:
            self._keepalive_restore_attempts = 0
            self._mark_keep_bookable(True)
        if self._emit is not None:
            self._emit("log_line", text=f"keepalive: restore {'succeeded' if ok else 'FAILED'}")
        return bool(ok)

    # ── /slots discovery, page-world (2026-08-31) ────────────────────────────────────────────
    # WHY THIS EXISTS. The belief that /slots returns zero/phantom data is contradicted by our own
    # capture: data/new_booking.har (a real manual booking, 2026-08-24) shows
    #     POST /slots?posto_id=3059 -> 200, 21,191 B, captcha = 2,617 chars
    # i.e. a full slot payload, 15s before the submit that booked. Meanwhile our own probe sent a
    # 2,617-char SCHEDULE_EVISA token -- same length, same action -- and got {"error":
    # ReCaptchaError"}. Same token shape, opposite result, so the difference is DELIVERY.
    #
    # The portal's page binds the token itself: the reCAPTCHA container carries
    # data-action="SCHEDULE_EVISA" data-callback="onCaptchaSuccess", and onCaptchaSuccess(token)
    # calls the page's own getSlots(posto_id, token), whose handleServerResponse() assigns the
    # result to window.data and unhides #calendarDiv. A DETACHED POST never runs any of that, so
    # window.data stays null and the portal never sees the token arrive through its own callback.
    #
    # So this delivers the token the way the page does, in the page world, and reads what the
    # page's own script produced. It is not a bypass of anything -- it is the portal's documented
    # client flow, driven correctly.
    # How long to wait for the portal's widget script to register window.onCaptchaSuccess. Checked
    # BEFORE the solve, so a page that never registers it costs seconds rather than a wasted token.
    _CALLBACK_WAIT_MS = 20000
    # How many times to RELOAD Schedule.jsp when the callback never registers but the schedule form
    # IS present (a stale widget render, recoverable). 1 = one extra fetch. An interstitial (no
    # schedule markers) is never reloaded -- re-nav would serve the same block. Kept at 1 because a
    # stale render usually recovers on the first re-fetch and /slots is rate-limited (~1 grant/100s),
    # so a blind retry loop would burn the budget without adding real coverage.
    _SLOTS_STALE_RELOADS = 1
    # Roll the exit after this many CONSECUTIVE exit-flagged failures (interstitial / ReCaptchaError)
    # on the same exit. 2 = the first keeps the cheap path (backoff/skip), the second rolls -- a
    # flagged exit never un-flags itself, so grinding it is wasted rounds (login rolls on the same
    # reCAPTCHA verdict). Reset by a real grant, a fresh login, or any roll.
    _SLOTS_ROLL_AFTER = 2
    # How long the keepalive probe waits for the home page to SETTLE before reading it. The home
    # page runs reCAPTCHA + async JS and keeps navigating for a beat after load, so an immediate
    # read races it: "Execution context was destroyed" (the outer handler counted this as a dead
    # session) and mid-render reads missing the auth markers (UNKNOWN limbo) were BOTH that race,
    # not a dead session -- confirmed live 2026-09-23, the authed home loads cleanly (57KB) given a
    # moment. Short, because a genuinely dead/blocked page settles fast and still classifies right.
    _KEEPALIVE_SETTLE_MS = 4000
    # How long to wait for the getTables WAF handshake after landing on Schedule.jsp, before /slots.
    # getTables fires early in the page load, so this is generous but bounded; a posto that proves it
    # doesn't fire getTables is remembered and never waited on again.
    _GETTABLES_WAIT_MS = 8000
    # How long to wait for the page's own getSlots()/handleServerResponse() to populate window.data
    # after the token is delivered. Raised 12s -> 25s: 5 of 55 rounds on "Test 0901" reached the
    # callback and then timed out here, which throws away a token that had already been accepted.
    _SLOTS_WAIT_MS = 25000
    # How long to wait for the intercepted /slots response to finish being read, in the case where
    # the page's own handler populated window.data before our clone().text() promise resolved.
    _SLOTS_PROBE_GRACE_MS = 1500
    # The endpoint grants roughly one real response per ~100s per session (measured: successes
    # 99s and 117s apart, every attempt in between refused). Asking sooner does not get data -- it
    # buys a CAPTCHA token and trades it for {"error": ReCaptchaError"}. Gated on the last GRANT,
    # because that is what the limit keys on.
    _SLOTS_MIN_INTERVAL = 100.0
    # Longest a single paced-out round will sleep before giving up and reporting nothing. Bounded
    # because an APPLY worker that is self-scouting runs this same path and must stay able to act
    # on an assignment.
    _SLOTS_MAX_PACING_SLEEP = 20.0
    # Pre-solve LEAD: how close to the window opening to solve the next token (SLOTS_PRESOLVE). Set
    # comfortably above the worst measured solve (~23s) so the token is ready in time, yet small
    # enough that it is only ~10-30s old when used -- far under SLOTS_TOKEN_TTL. Firing earlier
    # (e.g. as soon as the wait drops below the TTL) would cache a token that is nearly expired by
    # the time the window opens.
    _SLOTS_PRESOLVE_LEAD = 35.0

    def _no_report(self, why: str) -> list[dict]:
        """OUR tooling failed before the portal was ever asked -- report nothing about any date.

        This replaces _calendar_fallback (removed 2026-09-02, operator decision). That helper
        answered a solver outage by querying gettime+getPeriodosOcupados instead, which avoided a
        pointless re-warm but had a cost that outweighed it: /slots is the ground truth for this
        pool, and the calendar derives availability by SUBTRACTION (gettime minus
        getPeriodosOcupados) -- the exact inference that manufactures phantom slots. MEASURED on the
        2026-09-01 23:18 run of "Test 0901": the one calendar round re-confirmed 48 slots,
        refreshing their last_seen and shielding them from the miss-count retirement that had
        correctly closed 3 others. Weaker evidence was being laundered into the pool and then
        protecting itself from scrutiny.

        Returning [] rather than all-`failed` keeps the distinction the fallback got right: we never
        reached the portal, so this round is not news about the calendar AND not evidence about the
        session. An empty outcomes list writes nothing (orchestration.on_dates_reported iterates it)
        and pushes nothing, while all-`failed` would feed phases.py's rebuild counter and spend the
        login wall on a fault re-logging in cannot fix.
        """
        self._log(f"do_discover[slots]: {why} -- reporting nothing for this round "
                  f"(the portal was never asked, so there is nothing to say about any date)")
        return []

    def _solve_schedule_token(self, race_all) -> str:
        """One SCHEDULE_EVISA solve, honouring RECAPTCHA_SOLVE_PROXY exactly as the login path does
        (proxyless was 5/5 vs 2/5 proxied on Test 0901). Returns "" on no token."""
        _rc_proxy = self.warm_proxy if self.cfg.solver.solve_proxy else None
        return race_all(self.capsolver_keys, [], [], [], "SCHEDULE_EVISA", proxy=_rc_proxy) or ""

    def _slots_token(self, race_all) -> str:
        """The token for THIS /slots call. Prefers a fresh pre-solved token (SLOTS_PRESOLVE) so the
        7-23s solve is off the critical path; otherwise solves inline exactly as before."""
        ttl = float(self.cfg.raw.get("SLOTS_TOKEN_TTL", 90.0) or 90.0)
        cached = self._presolved_slots
        self._presolved_slots = None                 # consume-once, even if stale
        if cached is not None:
            token, solved_at = cached
            age = time.time() - solved_at
            if token and age < ttl:
                self._log(f"do_discover[slots]: using pre-solved token (age {age:.0f}s, len {len(token)}) "
                          f"-- solve was off the critical path")
                return token
            self._log(f"do_discover[slots]: pre-solved token discarded (age {age:.0f}s >= {ttl:.0f}s TTL)")
        _rc_proxy = self.warm_proxy if self.cfg.solver.solve_proxy else None
        self._log(f"do_discover[slots]: solving SCHEDULE_EVISA ({'via our proxy' if _rc_proxy else 'proxyless'})")
        return self._solve_schedule_token(race_all)

    def _maybe_presolve_slots(self, remaining_wait: float, race_all) -> None:
        """During the rate-limit wait, spend the otherwise-idle time solving the NEXT token so the
        poll fires the instant the window opens. Only when SLOTS_PRESOLVE is on, nothing fresh is
        already cached, and the wait is short enough that the token will still be valid when used
        (remaining_wait < SLOTS_TOKEN_TTL) -- solving earlier than that would cache a token that
        expires before the window opens."""
        if not bool(self.cfg.raw.get("SLOTS_PRESOLVE", False)):
            return
        ttl = float(self.cfg.raw.get("SLOTS_TOKEN_TTL", 90.0) or 90.0)
        if self._presolved_slots is not None:
            _t, solved_at = self._presolved_slots
            if time.time() - solved_at < ttl:
                return                                # a fresh one is already waiting
        lead = min(self._SLOTS_PRESOLVE_LEAD, ttl)
        if remaining_wait <= 0 or remaining_wait > lead:
            return                                    # too early -- a token solved now would age out
        try:
            token = self._solve_schedule_token(race_all)
        except Exception as e:
            self._log(f"do_discover[slots]: pre-solve failed ({str(e)[:80]}) -- will solve inline")
            return
        if token:
            self._presolved_slots = (token, time.time())
            self._log(f"do_discover[slots]: pre-solved next token (len {len(token)}, {remaining_wait:.0f}s "
                      f"of wait left) -- ready for the window")

    # getTables capture hook: wrap fetch + XHR (in the page world, before the page's own scripts run
    # via an init script) to flag window.__gt_ok the moment a getTables response returns
    # '"success":"true"'. Non-destructive -- clones the response, never consumes the original.
    _GETTABLES_HOOK_JS = """
    (() => {
      try {
        window.__gt_ok = false;
        const mark = (url, body) => {
          try {
            if (String(url).indexOf('getTables') !== -1 &&
                ((body || '').indexOf('"success":"true"') !== -1 ||
                 (body || '').indexOf('"success": "true"') !== -1)) {
              window.__gt_ok = true;
            }
          } catch (e) {}
        };
        const of = window.fetch;
        if (of) {
          window.fetch = function (...a) {
            return of.apply(this, a).then((r) => {
              try {
                const u = (r && r.url) || (a && a[0]) || '';
                if (String(u).indexOf('getTables') !== -1) {
                  r.clone().text().then((t) => mark(u, t)).catch(() => {});
                }
              } catch (e) {}
              return r;
            });
          };
        }
        const oo = XMLHttpRequest.prototype.open, os = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.open = function (m, u, ...rest) { this.__gt_u = u; return oo.call(this, m, u, ...rest); };
        XMLHttpRequest.prototype.send = function (...a) {
          try {
            this.addEventListener('load', () => { try { mark(this.__gt_u || '', this.responseText || ''); } catch (e) {} });
          } catch (e) {}
          return os.apply(this, a);
        };
      } catch (e) {}
    })();
    """

    def _ensure_gettables_hook(self) -> None:
        """Install the getTables capture hook into THIS session, once. Re-installs on a rebuilt
        session (the init script lives on the browser context, so a new context needs it again)."""
        if self.session is None or self._gt_hook_session is self.session:
            return
        add = getattr(self.session, "browser_add_init_script", None)
        if not callable(add):
            return   # a session double with no init-script support (tests) -- getTables is best-effort
        try:
            add(self._GETTABLES_HOOK_JS)
            self._gt_hook_session = self.session
        except Exception as e:
            self._log(f"do_discover[slots]: could not install getTables hook ({str(e)[:60]})")

    def _await_gettables(self) -> None:
        """Wait for the getTables WAF handshake to confirm before /slots -- the reference's mandatory
        pre-/slots handshake (posto 5084 excepted). Bounded and posto-remembered: a posto proven not
        to fire getTables is never waited on again. Advisory: we proceed to /slots either way, exactly
        as the reference does; the value is not firing /slots BEFORE the handshake has completed."""
        if self._posto_gettables is False:
            return   # this posto proved it doesn't use getTables
        poll = ("(async () => { const dl = Date.now() + %d;"
                "  while (Date.now() < dl) { if (window.__gt_ok === true) return true;"
                "    await new Promise(r => setTimeout(r, 200)); } return false; })()"
                % self._GETTABLES_WAIT_MS)
        try:
            ok = self.session.browser_eval(poll, timeout=self._GETTABLES_WAIT_MS // 1000 + 10)
        except Exception as e:
            self._log(f"do_discover[slots]: getTables probe error ({str(e)[:60]}) -- proceeding")
            return
        if ok is True or str(ok).lower() == "true":
            self._posto_gettables = True
            self._log("do_discover[slots]: getTables handshake confirmed -- ready for /slots")
        else:
            if self._posto_gettables is None:
                self._posto_gettables = False   # first miss -- assume this posto doesn't use it
            self._log("do_discover[slots]: getTables not seen (posto may not use it) -- proceeding to /slots")

    def _discover_via_slots(self, dates: list[str]) -> list[dict]:
        """Solve SCHEDULE_EVISA, hand the token to window.onCaptchaSuccess, read window.data."""
        from libs.engine.vendor import race_all
        want = {str(d) for d in dates}
        # DON'T BUY A TOKEN THE ENDPOINT WILL REFUSE. Returning early rather than sleeping keeps the
        # worker responsive to stop and lets the normal poll_gap do the pacing; the round simply
        # reports nothing, which is true -- we did not ask.
        _wait = max(self._slots_backoff_until - time.time(),
                    self._SLOTS_MIN_INTERVAL - (time.time() - self._slots_last_grant_at)
                    if self._slots_last_grant_at else 0.0)
        if _wait > 0:
            # WAIT OUT the remainder rather than returning instantly. Returning cost nothing, so the
            # discovery loop simply span at poll_gap speed -- ~7s per cycle doing no work, which on
            # the 2026-09-02 20:45 run produced 1,566 status changes in four hours, almost all of it
            # DISCOVERING <-> POLLING flapping. Sleeping in short chunks keeps is_stopped responsive,
            # and the wait is CAPPED (not the full interval) because this same path runs on an apply
            # worker that is self-scouting: holding one of those for 100s could delay it acting on a
            # real assignment, which is a far worse trade than an extra cycle.
            _deadline = time.time() + min(_wait, self._SLOTS_MAX_PACING_SLEEP)
            while time.time() < _deadline:
                if self.is_stopped():
                    break
                time.sleep(min(1.0, max(0.0, _deadline - time.time())))
            _wait = max(self._slots_backoff_until - time.time(),
                        self._SLOTS_MIN_INTERVAL - (time.time() - self._slots_last_grant_at)
                        if self._slots_last_grant_at else 0.0)
            # Spend the remaining wait pre-solving the next token (no-op unless SLOTS_PRESOLVE is on).
            # Done AFTER re-computing _wait so the freshness/TTL check sees the true remaining time.
            self._maybe_presolve_slots(_wait, race_all)
        if _wait > 0:
            return self._no_report(
                f"/slots grants about one response per {self._SLOTS_MIN_INTERVAL:.0f}s and the last "
                f"one was too recent ({_wait:.0f}s to go) -- not spending a solve")
        # WAIT FOR THE CALLBACK TO EXIST BEFORE SPENDING A SOLVE.
        #
        # browser_nav returns when the document loads, but window.onCaptchaSuccess is registered by
        # the portal's reCAPTCHA widget script, which is still loading. MEASURED 2026-09-01 on
        # booking "Test 0901": of 55 attempts, 47 died on `onCaptchaSuccess not defined` -- and the
        # CAPTCHA had ALREADY been solved by then (tokens of 2,553-2,702 chars, 7-23s each), so we
        # paid the solver 47 times and threw every token away because the page was not ready to
        # receive it. One round in 55 succeeded; that one returned SEVENTY slots.
        #
        # Polling for the callback costs nothing and moves the check BEFORE the solve, so a page
        # that never registers it costs a few seconds instead of a wasted token.
        ready_js = ("(async () => { const dl = Date.now() + %d;"
                    "  while (Date.now() < dl) {"
                    "    if (typeof window.onCaptchaSuccess === 'function') return true;"
                    "    await new Promise(r => setTimeout(r, 250)); }"
                    "  return false; })()" % self._CALLBACK_WAIT_MS)
        # SMART RELOAD (2026-09-25, operator: rely on /slots harder -- it is the ground truth; the
        # calendar path manufactures phantoms). When the callback never registers, the page is either
        # a STALE RENDER (the schedule form IS there -- _SCHED_PAGE_MARKERS present -- but the widget
        # script hasn't run) or a WAF INTERSTITIAL (a wrong page, no markers). A stale render is
        # recoverable by re-fetching the page; an interstitial is not (re-nav serves the same block).
        # So reload ONCE for a stale render, skip immediately for an interstitial -- never a blind
        # retry loop. Each reload re-installs the getTables hook and re-runs the handshake.
        ready = False
        for _try in range(self._SLOTS_STALE_RELOADS + 1):
            # Install the getTables capture hook BEFORE navigating -- getTables fires during the page
            # load, so the fetch/XHR wrapper must already be registered as an init script.
            self._ensure_gettables_hook()
            try:
                self.session.browser_nav(
                    SCHED_JSP + "?posto_id=" + str(self.posto_pdf or self.posto_id), timeout=30)
            except Exception as e:
                self._log(f"do_discover[slots]: could not reach Schedule.jsp ({str(e)[:90]})")
                return [{"date": d, "outcome": "failed"} for d in dates]
            # WAF handshake: wait for getTables to confirm before spending a solve / firing /slots.
            # Posto-optional, so it proceeds either way (see _await_gettables).
            self._await_gettables()
            try:
                ready = self.session.browser_eval(ready_js, timeout=45)
            except Exception as e:
                self._log(f"do_discover[slots]: could not probe for onCaptchaSuccess ({str(e)[:80]})")
                ready = False
            if ready is True or str(ready).lower() == "true":
                ready = True
                break
            # Callback missing -- classify the page to decide whether a reload can help.
            try:
                _html = self.session.browser_eval("document.documentElement.outerHTML") or ""
            except Exception:
                _html = ""
            _stale = any(m in _html for m in _SCHED_PAGE_MARKERS)
            if _stale and _try < self._SLOTS_STALE_RELOADS:
                self._log("do_discover[slots]: onCaptchaSuccess missing but Schedule.jsp markers "
                          "present (stale widget render) -- reloading once")
                continue
            # Deliberately `failed`, not _no_report: we DID reach the portal; it served a page that
            # never registered the widget. That is evidence about the session/page, so it should keep
            # feeding phases.py's rebuild counter.
            self._log(f"do_discover[slots]: window.onCaptchaSuccess never appeared within "
                      f"{self._CALLBACK_WAIT_MS // 1000}s -- "
                      f"{'stale render, reload did not recover' if _stale else 'no schedule markers (interstitial), not reloading'} "
                      f"(no solve spent)")
            # A page that never brings up the widget is a flagged/blocked EXIT, not a fixable
            # session state -- and it does not un-flag itself, so re-running the same exit next round
            # just repeats it (6/9 rounds, booking 0924-3018). Roll off it on the second consecutive.
            self._slots_recaptcha_streak = 0
            self._slots_interstitial_streak += 1
            if self._slots_interstitial_streak >= self._SLOTS_ROLL_AFTER:
                self._log(f"do_discover[slots]: {self._slots_interstitial_streak} consecutive "
                          "interstitials -- the exit is flagged; rolling off it")
                self._roll_exit("slots interstitial")
                self._slots_interstitial_streak = 0
            return [{"date": d, "outcome": "failed"} for d in dates]
        try:
            # HONOUR RECAPTCHA_SOLVE_PROXY, exactly as the login path does
            # (session.py:1712 `_rc_proxy = self.proxy if _recaptcha_solve_proxy() else None`).
            #
            # This was hardcoded to self.warm_proxy and it produced a 100% reCAPTCHA failure rate on
            # booking "Test 0901": every solve came back `createTask failed: ERROR_PROXY...` because
            # CapSolver cannot connect out through our webshare exits. The vendored helper's own
            # docstring names that pool -- "measured only against soax/mobile and webshare and was
            # WRONG for soax/rotating" -- and the catalog records 5 solves/mode: proxied 2/5 (3
            # failed outright), proxyless 5/5. The booking had RECAPTCHA_SOLVE_PROXY=False set; this
            # path simply never read it.
            #
            # No min_score either. It was 100, which makes race_all sit out its full grace waiting
            # for a token score_token() only awards below 2,320 chars -- a length heuristic, and one
            # whose ordering rationale §16 already invalidated. The login path passes no min_score
            # (first usable token wins) and that is the path with the bookings behind it.
            token = self._slots_token(race_all)
        except Exception as e:
            self._log(f"do_discover[slots]: CAPTCHA solve failed ({str(e)[:90]})")
            return self._no_report("the CAPTCHA solve failed")
        if not token:
            return self._no_report("no token was produced")
        self._log(f"do_discover[slots]: SCHEDULE_EVISA token len={len(token)} -> onCaptchaSuccess")

        tok = json.dumps(token)
        # Deliver exactly as the page does, then wait for ITS script to populate window.data.
        # The `window.data = []` coercion is the crash guard: when the portal answers an error or a
        # zero-slot response the body is an OBJECT, and the calendar's own
        # `data.find(d => d.date === key)` throws "data.find is not a function" and freezes the UI.
        #
        # INSTRUMENTATION (2026-09-02): capture the ACTUAL /slots response, not just its residue.
        #
        # Everything this code knew about /slots came from window.data -- a value left behind by the
        # portal's own handler. MEASURED on the 2026-09-01 23:18 run: 206 of 214 delivered tokens
        # produced `{"data":{}}` there, and nothing in the system could say whether that was a 200
        # carrying no slots, a 429, a 403, a DataDome interstitial, or a session redirect. Those
        # demand opposite responses (wait / rotate the exit / re-login), so guessing between them is
        # how a fleet spends 200 CAPTCHA solves re-warming against a wall. The interceptor is
        # installed in THIS eval, strictly before onCaptchaSuccess fires, so the request cannot be
        # issued before the hook exists.
        #
        # Both transports are patched because we do not control which one the portal's script uses,
        # and clone() is mandatory on the fetch path: reading the original body consumes the stream
        # and the page's own handler would then receive nothing. Read-only -- it records and returns
        # the response untouched.
        js = """
        (async () => {
          const t = %s;
          if (typeof window.onCaptchaSuccess !== 'function')
            return {err: 'onCaptchaSuccess not defined'};

          const MARK = %s;
          window.__slotsProbe = null;
          const rec = (o) => { if (!window.__slotsProbe) window.__slotsProbe = o; };
          if (!window.__slotsHooked) {
            window.__slotsHooked = true;
            const of_ = window.fetch;
            window.fetch = function () {
              const a = arguments;
              const u = String((a[0] && a[0].url) || a[0] || '');
              const pr = of_.apply(this, a);
              if (u.indexOf(MARK) !== -1) {
                pr.then(function (r) {
                  r.clone().text().then(function (b) {
                    rec({via: 'fetch', url: u, status: r.status,
                         ct: (r.headers && r.headers.get('content-type')) || '',
                         len: b.length, body: b.slice(0, 600)});
                  }, function () { rec({via: 'fetch', url: u, status: r.status, err: 'body unreadable'}); });
                }, function (e) { rec({via: 'fetch', url: u, err: String(e)}); });
              }
              return pr;
            };
            const oo = XMLHttpRequest.prototype.open;
            const os_ = XMLHttpRequest.prototype.send;
            XMLHttpRequest.prototype.open = function (m, u) {
              this.__slotsUrl = String(u || ''); return oo.apply(this, arguments);
            };
            XMLHttpRequest.prototype.send = function () {
              const self_ = this;
              if (this.__slotsUrl && this.__slotsUrl.indexOf(MARK) !== -1) {
                this.addEventListener('loadend', function () {
                  let b = '';
                  try { b = self_.responseText || ''; } catch (e) { b = '<unreadable>'; }
                  let ct = '';
                  try { ct = self_.getResponseHeader('content-type') || ''; } catch (e) { ct = ''; }
                  rec({via: 'xhr', url: self_.__slotsUrl, status: self_.status,
                       ct: ct, len: b.length, body: b.slice(0, 600)});
                });
              }
              return os_.apply(this, arguments);
            };
          }

          try { window.onCaptchaSuccess(t); } catch (e) { return {err: 'callback threw: ' + e}; }
          const deadline = Date.now() + %d;
          while (Date.now() < deadline) {
            if (window.data !== undefined && window.data !== null) break;
            await new Promise(r => setTimeout(r, 250));
          }
          // window.data is set by the page's handler, which can win the race against our own
          // clone().text(). Give the probe a bounded grace so a successful round still reports what
          // the endpoint actually said.
          const pdl = Date.now() + %d;
          while (window.__slotsProbe === null && Date.now() < pdl) {
            await new Promise(r => setTimeout(r, 100));
          }
          const probe = window.__slotsProbe;
          let d = window.data;
          if (d && !Array.isArray(d)) { window.data = []; return {coerced: true, raw: JSON.stringify(d).slice(0,300), probe: probe}; }
          if (!Array.isArray(d)) return {err: 'window.data never populated', probe: probe};
          return {ok: true, data: d, probe: probe};
        })()
        """ % (tok, json.dumps(_SLOTS_PATH_MARK), self._SLOTS_WAIT_MS, self._SLOTS_PROBE_GRACE_MS)
        try:
            res = self.session.browser_eval(js, timeout=45)
        except Exception as e:
            self._log(f"do_discover[slots]: page-world eval failed ({str(e)[:90]})")
            return [{"date": d, "outcome": "failed"} for d in dates]
        if isinstance(res, str):
            try: res = json.loads(res)
            except Exception: res = {"err": f"unparseable: {str(res)[:120]}"}
        res = res or {}
        # RE-ANCHOR IF THE PAGE SLIPPED DURING THE SOLVE (registrar-confirmed failure mode,
        # [[registrar_opaque_origin]]). `onCaptchaSuccess not defined` AFTER the callback WAS present
        # at ready-check time means the browser's proxy tunnel dropped the page off-origin during the
        # ~15-25s solve while the exit itself stayed alive (MEASURED on the registrar: a raw client
        # survives nav+28s+POST 5/5; only the in-page world slips). The reCAPTCHA token is still
        # valid (~110s), so re-nav the slots page ONCE to re-register the widget and re-deliver the
        # SAME token rather than discarding a paid solve and waiting out the roll-after-2 streak --
        # the slip is near-100% on bad proxy weather, which is precisely what makes discovery go
        # blind. If the re-anchor cannot bring the callback back, the page (not just the origin) is
        # gone; fall through to the existing failed/roll path untouched.
        if res.get("err") == "onCaptchaSuccess not defined":
            self._log("do_discover[slots]: callback vanished after the solve (page slipped "
                      "off-origin during the solve gap) -- re-anchoring the slots page once and "
                      "re-delivering the same token")
            try:
                self._ensure_gettables_hook()
                self.session.browser_nav(
                    SCHED_JSP + "?posto_id=" + str(self.posto_pdf or self.posto_id), timeout=30)
                self._await_gettables()
                _re_ready = self.session.browser_eval(ready_js, timeout=45)
            except Exception as e:
                self._log(f"do_discover[slots]: re-anchor nav failed ({str(e)[:80]})")
                _re_ready = False
            if _re_ready is True or str(_re_ready).lower() == "true":
                try:
                    res = self.session.browser_eval(js, timeout=45)
                    if isinstance(res, str):
                        try: res = json.loads(res)
                        except Exception: res = {"err": f"unparseable: {str(res)[:120]}"}
                    res = res or {}
                    self._log("do_discover[slots]: re-anchor restored the callback; "
                              "token re-delivered")
                except Exception as e:
                    self._log(f"do_discover[slots]: re-delivery eval failed ({str(e)[:80]})")
            else:
                self._log("do_discover[slots]: re-anchor did not restore the callback -- the exit "
                          "page is gone, not just off-origin")
        self._log(f"do_discover[slots]: endpoint said {_fmt_slots_probe(res.get('probe'))}")
        kind = _classify_slots_response(res)
        if kind == SLOTS_RATE_LIMITED:
            # A ReCaptchaError on the FIRST occurrence is treated as a rate-limit ("asked too soon"):
            # back off a window and retry the SAME exit -- the token was refused, not the session.
            # But if it REPEATS on the same exit past that window it is not rate-limiting, it is a
            # FLAGGED EXIT -- the exact signal login rolls on (token_ip_mismatch_burns_accounts).
            # Confirmed live 2026-09-25 (089ebd: ReCaptchaError at 06:12 AND 06:21, 9 min apart, 0
            # slots). So roll on the second consecutive rather than backing off onto a dead exit.
            self._slots_interstitial_streak = 0
            self._slots_recaptcha_streak += 1
            if self._slots_recaptcha_streak >= self._SLOTS_ROLL_AFTER:
                self._log(f"do_discover[slots]: {self._slots_recaptcha_streak} consecutive "
                          "ReCaptchaError on this exit -- flagged, not rate-limited; rolling off it")
                self._roll_exit("slots recaptcha -- fresh exit")
                self._slots_recaptcha_streak = 0
                return self._no_report("rolled off a flagged exit after repeated ReCaptchaError")
            self._slots_backoff_until = time.time() + self._SLOTS_MIN_INTERVAL
            return self._no_report(
                f"the endpoint refused the token (rate limit); next attempt in "
                f"{self._SLOTS_MIN_INTERVAL:.0f}s")
        if kind == SLOTS_SESSION_BURNED:
            # Only a fresh LOGIN clears this. Flagged for phases.py so the rebuild ladder skips
            # straight past the re-warm rung, which is measured to never help here.
            self._slots_needs_relogin = True
            self._slots_interstitial_streak = self._slots_recaptcha_streak = 0   # relogin resets
            self._log("do_discover[slots]: the endpoint returned an empty object -- this SESSION is "
                      "burned for /slots; a re-warm cannot fix it, only a fresh login")
            return [{"date": d, "outcome": "failed"} for d in dates]
        if kind == SLOTS_BLOCKED:
            # The exit is done, not the session. Roll it and let the next round try from elsewhere.
            self._log("do_discover[slots]: the endpoint blocked this exit -- rolling off it")
            self._roll_exit("slots blocked")
            self._slots_interstitial_streak = self._slots_recaptcha_streak = 0   # fresh exit
            return self._no_report("the endpoint blocked this exit")
        if res.get("err"):
            self._log(f"do_discover[slots]: {res['err']}")
            return [{"date": d, "outcome": "failed"} for d in dates]
        if res.get("coerced"):
            # A NON-ARRAY body we do NOT recognise. Still `failed` rather than `empty`: reporting
            # it as empty would prune real dates from the pool (bug fix #6's whole point).
            self._log(f"do_discover[slots]: non-array payload, coerced to [] to protect the page "
                      f"| raw={str(res.get('raw'))[:160]}")
            return [{"date": d, "outcome": "failed"} for d in dates]

        # A real array is the grant the rate limit meters, and proof the session is fine.
        self._slots_last_grant_at = time.time()
        self._slots_needs_relogin = False
        self._slots_interstitial_streak = self._slots_recaptcha_streak = 0   # this exit works
        found, capacity = {}, {}
        for entry in (res.get("data") or []):
            if not isinstance(entry, dict):
                continue
            d = str(entry.get("date") or "")
            if not d:
                continue
            # CONFIRMED response shape (operator capture, 2026-08-31):
            #   [{"date":"2026-08-31","periods":[{"id":3,"description":"8-9"},
            #                                    {"id":3,"description":"9-10"},
            #                                    {"id":4,"description":"10-11"}]}]
            # Two things this codebase would otherwise get wrong:
            #   * a period is an OBJECT, not a scalar. Passing it through unchanged puts a dict into
            #     `cmbPeriodo` (booking_payload does str(slot["period"])) and submits garbage.
            #   * the SAME id repeats with different descriptions -- id 3 appears twice above, as
            #     08-09 and 09-10. That is two bookable times behind ONE selector value, so the
            #     repeat is CAPACITY, not a duplicate to be silently collapsed and forgotten.
            # Accepts bare scalars too, so a payload of plain ids still parses.
            ids, seen = [], set()
            for p in (entry.get("periods") or []):
                pid = p.get("id") if isinstance(p, dict) else p
                if pid is None:
                    continue
                try:
                    pid = int(pid)
                except (TypeError, ValueError):
                    continue
                capacity[(d, pid)] = capacity.get((d, pid), 0) + 1
                if pid not in seen:
                    seen.add(pid)
                    ids.append(pid)
            found[d] = ids
        if capacity:
            multi = {f"{d}/{p}": n for (d, p), n in capacity.items() if n > 1}
            if multi:
                self._log(f"do_discover[slots]: repeated period ids (= capacity) {multi}")
        self._log(f"do_discover[slots]: window.data carried {len(found)} date(s)")
        out = []
        for d in dates:
            if d in found and found[d]:
                out.append({"date": d, "outcome": "found", "periods": found[d]})
            elif d in found:
                out.append({"date": d, "outcome": "empty"})
            else:
                # Absent from a payload the portal DID return means no availability for that date.
                out.append({"date": d, "outcome": "empty"})
        return out

    def _discover_done(self, out: list[dict], source: str) -> list[dict]:
        """Emit the structured discovery-round summary (Phase-K) and pass the result through, so every
        do_discover return site is back-traceable without threading timing through each branch."""
        found = sum(1 for r in out if r.get("outcome") == "found")
        empty = sum(1 for r in out if r.get("outcome") == "empty")
        failed = sum(1 for r in out if r.get("outcome") == "failed")
        self._slog("discover_end", source=source, dates=len(out),
                   found=found, empty=empty, failed=failed)
        return out

    def do_discover(self, dates: list[str]) -> list[dict]:
        """Bug fix #6: 3-way per-date outcome (found/empty/failed). Uses gettime + real-browser-nav
        getPeriodosOcupados per confirmed_pipeline_path.md -- never browser_fetch/primp for these
        endpoints, DataDome 403s raw fetch regardless of session health.

        Shared gettime cache: if the Hub handed this worker a still-fresh special_days payload
        alongside its discover assignment (apps/hub/scheduler.py's _drive_scouts,
        GETTIME_CACHE_SECS), skip the gettime fetch entirely and use it directly -- storm-auto's
        proven pattern (app/core/slot_manager.py's _gettime_cache) is a fleet-wide shared TTL cache,
        not a per-worker one. Otherwise fetch fresh and, on success, report it back via a
        `gettime_result` event so the Hub can cache it for the rest of the fleet.

        DISCOVERY_SOURCE (2026-08-31) finally has a read site. It has been in the settings catalog
        from the start and parsed into EngineConfig.discovery.source, and NOTHING consumed it -- the
        same unwired-setting trap as QUEST_STEP_GAP and PARTIAL_RENDER_RETRIES before it.

            "calendar"  gettime + getPeriodosOcupados (DEFAULT, unchanged -- the confirmed path)
            "slots"     the portal's own page-world flow via onCaptchaSuccess -> getSlots
            "both"      slots first, then calendar for any date slots could not resolve

        `both` exists because the two disagree in a useful direction: /slots is what the live page
        itself uses, and data/new_booking.har shows it returning a 21KB payload 15s before a real
        booking -- while the calendar path is the one this project has actually booked with.
        Preferring slots and back-filling from calendar takes the better answer without giving up
        the proven one."""
        # PREFER the Hub-pushed live value (self._pending_discover_source, set from the discover
        # command) over the launch-frozen cfg -- this is what lets a DISCOVERY_SOURCE change reach a
        # RUNNING scout without a restart. Fall back to the frozen cfg when the Hub sent none.
        _pushed = (self._pending_discover_source or "").strip().lower()
        source = _pushed or str(getattr(self.cfg.discovery, "source", "calendar")
                                or "calendar").strip().lower()
        if source not in _DISCOVERY_SOURCES:
            # SAY SO. The dashboard renders this as free text (the settings catalog has no enum
            # type), so `slot`, `Slots-only` or a typo is a realistic operator input. Silently
            # defaulting to calendar is the worst outcome available: the operator sets the value,
            # sees no error, believes the new path is under test, and measures the old one. That is
            # the same "looks configured, isn't" class as every unwired setting this project has
            # found. Fall back to the proven path -- but never quietly.
            self._log(f"do_discover: DISCOVERY_SOURCE={source!r} is not one of "
                      f"{sorted(_DISCOVERY_SOURCES)} -- REFUSING it and using 'calendar'. "
                      f"Fix the booking setting; this run is NOT testing what you configured.")
            source = "calendar"
        self._slog("discover_start", dates=len(dates), source=source)
        if source in ("slots", "both"):
            res = self._discover_via_slots(dates)
            if source == "slots":
                return self._discover_done(res, source)
            # UNRESOLVED = every date /slots did not DEFINITIVELY answer. A definitive answer is
            # `found` or `empty` (the portal told us about that date); everything else -- a per-date
            # `failed`, OR a date missing from `res` entirely -- is unresolved and gets the calendar.
            #
            # The missing-date case is the one that mattered live (2026-09-21): a rate-limited /slots
            # round returns `_no_report()` -> `[]`, so the old `[r for r in res if outcome=='failed']`
            # came back EMPTY and calendar never ran -- "both" silently degraded to "report nothing"
            # exactly when /slots was down and calendar was the whole point of choosing "both". Now
            # an empty (or partial) /slots round backfills the un-answered dates from calendar.
            answered = {r["date"] for r in res if r.get("outcome") in ("found", "empty")}
            unresolved = [d for d in dates if d not in answered]
            if not unresolved:
                return self._discover_done(res, source)
            self._log(f"do_discover[both]: slots resolved {len(answered)}/{len(dates)} date(s); "
                      f"falling back to calendar for the other {len(unresolved)}")
            by_date = {r["date"]: r for r in res}
            for r in self._discover_via_calendar(unresolved):
                by_date[r["date"]] = r
            return self._discover_done([by_date[d] for d in dates if d in by_date], source)
        return self._discover_done(self._discover_via_calendar(dates), source)

    def _discover_via_calendar(self, dates: list[str]) -> list[dict]:
        """gettime + getPeriodosOcupados -- the confirmed path, logic untouched. Split out of
        do_discover only so DISCOVERY_SOURCE can dispatch without editing a line of it."""
        out = []
        special_days = getattr(self, "_pending_special_days", None)
        self._pending_special_days = None
        fetched_fresh = special_days is None
        if fetched_fresh:
            # Ensure a LIVE portal origin before gettime (finding #1/#C, 2026-10-01). A gettime that
            # fires from a chrome-error / off-origin page raises "Execution context was destroyed"
            # (the nav raced, or the exit is dead): the old code failed the whole round AND left the
            # page off-origin, so the NEXT warm-up logged "not on a live portal origin -- re-anchoring
            # to Schedule.jsp" (exactly ss#1 from the 0925 logs). Anchor first; and treat a context-
            # destroyed / dead-exit failure as transient -- re-anchor + retry ONCE before giving up.
            self._ensure_portal_origin("discovery gettime")
            gettime_body = ""
            _gt_ok = False
            for _gt_attempt in (1, 2):
                try:
                    self.session.browser_nav(f"{GETTIME_URL}?id_posto={self.posto_id}", timeout=30)
                    gettime_body = self.session.browser_eval("document.documentElement.outerHTML") or ""
                    _gt_ok = True
                    break
                except Exception as e:
                    _msg = str(e)
                    _raced = "execution context was destroyed" in _msg.lower()
                    _dead = self._is_transient_login_failure(_msg)
                    if _gt_attempt == 1 and (_raced or _dead):
                        self._log(f"do_discover: gettime raced/dead-exit ({_msg[:100]}) -- "
                                  f"re-anchoring and retrying once")
                        self._ensure_portal_origin("discovery gettime retry")
                        time.sleep(1.0)
                        continue
                    self._log(f"do_discover: gettime nav failed ({e}) -- all requested dates FAILED, not empty")
                    # A CONFIRMED dead exit (chrome-error/tunnel) will fail identically every round;
                    # roll it now so the scout loop's rebuild re-logs in on a fresh exit, instead of
                    # re-probing the dead tunnel forever ([[dead_exit_page_not_rolled]]).
                    if _dead:
                        self._roll_exit("discovery: dead tunnel at gettime")
                    break
            if not _gt_ok:
                return [{"date": d, "outcome": "failed"} for d in dates]

            gt_status = classify_gettime_body(gettime_body)
            if gt_status != GettimeStatus.OK:
                # SAY WHY. This was a bare `return` -- no log, no emit, no reason -- and it is the
                # single most misleading silence in the engine. CONFIRMED live 2026-08-23 on
                # "Test 2032 (copy)": a worker flipped discovering -> holding fifteen times in seven
                # minutes, 5-12s per cycle, emitting NOTHING. The dashboard showed a worker busily
                # discovering; it had in fact scanned zero dates every single time, because gettime
                # kept answering with a body this classifier rejects (WAF challenge or the portal's
                # "try again later" page, the same two pages refusing everything else).
                #
                # The operator's read was "discovery works but the session can't do it" -- exactly
                # right, and there was no way to see that from any log.
                why = _classify_gettime_page(gettime_body)
                self._log(f"do_discover: gettime returned {gt_status} ({why}, {len(gettime_body)}B) "
                          f"-- scanned NOTHING; all {len(dates)} date(s) reported failed, not empty")
                if self._emit is not None:
                    self._emit("portal_health", phase="discover", kind=why,
                               seconds=0.0, **self._health.snapshot())
                # BACK OFF. The caller loops on poll_gap(), so a blocked gettime was retried every
                # ~30s forever. Measured live 2026-08-27 on "Test 2032": one worker went
                # `-> discovering` TWENTY times in ~10 minutes, every one answered
                # portal_try_again_later (28,499B) -- the portal literally saying "try again later"
                # while we asked again 30 seconds later, five workers wide, for hours.
                #
                # storm-auto carries this fix and the scar behind it: "~2.5h of empty polling drew
                # 77 DataDome pages -> fixed by bounded backoff after this tag." Hammering a portal
                # that is throttling us is the one thing here that plausibly makes the throttling
                # worse, and it is entirely under our control.
                #
                # Escalates per CONSECUTIVE block and resets the moment gettime answers properly, so
                # a transient block costs one short pause and a sustained one stops the hammering.
                self._gettime_block_streak = getattr(self, "_gettime_block_streak", 0) + 1
                base = float(self.cfg.raw.get("DISCOVERY_BLOCKED_BACKOFF", 30.0))
                cap = float(self.cfg.raw.get("DISCOVERY_BLOCKED_BACKOFF_CAP", 300.0))
                if base > 0:
                    nap = min(cap, base * (2 ** (self._gettime_block_streak - 1)))
                    nap *= random.uniform(0.85, 1.15)   # de-sync the fleet; they all block together
                    self._log(f"do_discover: backing off {nap:.0f}s "
                              f"(consecutive blocks: {self._gettime_block_streak})")
                    time.sleep(nap)
                return [{"date": d, "outcome": "failed"} for d in dates]
            # gettime answered properly -- the block is over, so the ladder starts from zero again.
            self._gettime_block_streak = 0
            special_days = parse_special_days(gettime_body)
            if self._emit is not None:
                self._emit("gettime_result", special_days=special_days_to_json(special_days))
        else:
            # A cache hit arrives already json-decoded (str keys, list-typed days) -- the bus's real
            # transport (libs/bus/bus.py's json.dumps/loads) can't preserve int keys or sets, so this
            # undoes special_days_to_json's encoding before the int/set-typed membership check below.
            special_days = special_days_from_json(special_days)

        for d in dates:
            try:
                iso_date = date.fromisoformat(d)
            except ValueError:
                out.append({"date": d, "outcome": "failed"})
                continue
            if iso_date.day in special_days.get(iso_date.year, {}).get(iso_date.month, set()):
                out.append({"date": d, "outcome": "empty"})   # confirmed closed/special day
                continue
            url = f"{GETPERIODOS_URL}?id_posto={self.posto_id}&data_agendamento={iso_date.strftime('%Y/%m/%d')}"
            # Retry a CONNECTION-level nav failure in the SAME warm session before giving up on this
            # date -- no logout/restore/re-login. Ported from storm-auto's confirmed ace423a pattern
            # (nav_retries/nav_backoff were parsed into EngineConfig but never read here -- dead
            # setting until now). A 200-with-no-marker or untokenizable body is a CONTENT issue, not a
            # connection failure, so those stay single-shot below, same as storm-auto.
            html = None
            last_err: Exception | None = None
            for attempt in range(self.cfg.discovery.nav_retries + 1):
                try:
                    self.session.browser_nav(url, timeout=30)
                    html = self.session.browser_eval("document.documentElement.outerHTML") or ""
                    break
                except Exception as e:
                    last_err = e
                    if attempt < self.cfg.discovery.nav_retries:
                        backoff = self.cfg.discovery.nav_backoff * (attempt + 1) * random.uniform(0.7, 1.3)
                        self._log(f"do_discover: {d} nav error ({e}) -- "
                                  f"retry {attempt + 1}/{self.cfg.discovery.nav_retries} in {backoff:.1f}s")
                        time.sleep(backoff)
            if html is None:
                self._log(f"do_discover: {d} nav error ({last_err}) -- "
                          f"exhausted {self.cfg.discovery.nav_retries} retries -- FAILED, not empty")
                out.append({"date": d, "outcome": "failed"})
                continue
            m = re.search(r"periodos_ocupados='([0-9]*)", html)
            if not m:
                out.append({"date": d, "outcome": "failed"})   # no marker -> WAF/context issue, UNKNOWN
                continue
            raw = m.group(1)
            free, _occupied = parse_periodos_ocupados(raw, max_period=self.cfg.discovery.max_period)
            if free is None:
                out.append({"date": d, "outcome": "failed"})   # untokenizable -> UNKNOWN, never prune
            elif free:
                # `raw` is kept on the outcome for the phantom check below and logged there. It is
                # the only evidence that distinguishes a real per-date answer from the portal's
                # fallback template, and until 2026-09-18 it was thrown away the moment it was
                # decoded -- so a phantom round left nothing to diagnose it by.
                out.append({"date": d, "outcome": "found", "periods": free, "raw": raw})
            else:
                out.append({"date": d, "outcome": "empty"})
        return self._reject_phantom_round(out)

    def _reject_phantom_round(self, out: list[dict]) -> list[dict]:
        """Downgrade a discovery round whose finds match the PHANTOM signature to UNKNOWN.

        THE MECHANISM. The calendar path derives availability by SUBTRACTION: free = active range
        minus the closed prefix decoded from getPeriodosOcupados. That inference has no way to tell
        "the portal answered this date" from "the portal's backend query failed and it served the
        template with a default occupied string". Under load the second happens, the marker is still
        present, and EVERY date decodes to the same thin set -- a handful of periods, identical
        across dates. The codebase already named this as "the exact inference that manufactures
        phantom slots" when it removed the calendar fallback on 2026-09-02.

        CONFIRMED LIVE 2026-09-18, Booking 0918, slot-open 10:39. One scout got a real Schedule.jsp
        during the ~60s the portal stayed up, then found {3,4} on 2026-10-14 AND {3,4} on 2026-10-15.
        The portal's own calendar showed 12/13/14 October available and the 15th greyed out. Both
        finds fired slot_found; three apply workers leased them, re-warmed into a 503, and the
        recovery ladder escalated them to re-login -- discarding authenticated sessions and spending
        reCAPTCHA strikes on accounts, for slots that did not exist. Operator rule, confirmed on
        earlier runs: "if there are only 3, 4 periods per every day and few slots than expected --
        regard it as phantom slots found."

        THE RULE (operator-confirmed, configurable). A round is phantom when at least
        PHANTOM_MIN_DATES dates were found, every one of them carries the SAME period set, and that
        set has at most PHANTOM_MAX_PERIODS entries. Uniformity is the load-bearing half: a real
        open answers per date, with different occupied sets; only a template answers every date
        identically. The size bound keeps a genuinely uniform but generous open (say 3 dates all
        showing 6 periods) from being refused.

        THE VERDICT IS "failed", NOT "empty" -- deliberately. "failed" is UNKNOWN: no slot rows are
        written, slot_found does not fire, and no date is retired (bug fix #6 keeps failed distinct
        from empty precisely so an unreadable round never prunes the window). The next round asks
        again. "empty" would have retired real dates on the strength of a template.

        PHANTOM_MAX_PERIODS=0 disables the check entirely.
        """
        max_p = int(self.cfg.raw.get("PHANTOM_MAX_PERIODS", 2) or 0)
        min_d = int(self.cfg.raw.get("PHANTOM_MIN_DATES", 2) or 2)
        found = [o for o in out if o.get("outcome") == "found"]
        raws = {o.get("raw") for o in found}
        # `raw` served its purpose (this decision + the log line below). Strip it before the
        # outcomes leave: the slots_found/dates_reported bus payloads and the Hub's slot rows carry
        # exactly {date, outcome, periods}, and that contract is asserted by exact equality.
        for o in found:
            o.pop("raw", None)
        if max_p <= 0 or len(found) < min_d:
            return out
        sets = {tuple(sorted(o.get("periods") or [])) for o in found}
        if len(sets) != 1:
            return out                       # per-date variation = a real answer
        (sig,) = sets
        if len(sig) > max_p:
            return out                       # uniform but generous -- allowed
        self._log(f"do_discover: PHANTOM round -- {len(found)} date(s) all decoded to periods {list(sig)} "
                  f"from {len(raws)} distinct raw string(s) {sorted(r or '' for r in raws)[:3]} -- "
                  f"a real open answers per date; a template answers every date the same. "
                  f"Reporting {len(found)} date(s) as FAILED (unknown), not found: no slot rows, no "
                  f"slot_found, no retirement. (PHANTOM_MAX_PERIODS={max_p}, PHANTOM_MIN_DATES={min_d})")
        if self._emit is not None:
            self._emit("portal_health", phase="discover", kind="phantom_slots",
                       seconds=0.0, **self._health.snapshot())
        return [({"date": o["date"], "outcome": "failed"} if o.get("outcome") == "found" else o)
                for o in out]

    def release_slot(self, slot: dict, reason: str = "bounce") -> None:
        """Hand a slot back to the pool. The worker has no DB access, so this rides the same event
        bus everything else does; apps/hub/events.py releases the lease and reopens the slot.

        `reason` carries storm-auto's two-tier penalty (slot_manager.release(lease, ...)):
          "bounce"     -- the slot may genuinely be gone; counts against it.
          "form_stale" -- OUR form-state expired, the slot is still real. Released WITHOUT penalty,
                          so a stale-form loop can't retire good slots (and can't fool the Hub's
                          false-slot revert into condemning a real pool)."""
        if self._emit is not None:
            self._emit("slot_released", slot=slot, reason=reason)

    def push_slots(self, slots: list[dict]) -> None:
        if self._emit is not None:
            self._emit("slots_found", slots=slots)

    def report_dates(self, outcomes: list[dict]) -> None:
        if self._emit is not None:
            self._emit("dates_reported", outcomes=outcomes)

    def poll_gap(self) -> None:
        # HOLD_POLL_GAP is guaranteed >= its settings-catalog floor by the resolver (Phase F14) --
        # this is the confirmed fix for the incident where this value resolved to 0.0 and the hold
        # loop spun unthrottled for 66 minutes. Still clamp defensively at the call site (belt and
        # suspenders, per Phase F14's "defense in depth" note) rather than trusting config alone.
        gap = max(float(self.cfg.hold_swap.poll_gap), 1.0)
        jitter = float(self.cfg.keepalive.jitter)
        time.sleep(gap + random.uniform(0, min(jitter, gap)))

    # ── apply ─────────────────────────────────────────────────────────────────
    def await_assignment(self) -> dict | None:
        if self._signals is None:
            return None
        self._initial_schedule_warm("await_assignment")
        while True:
            result = self._signals.wait_assignment(timeout=self._keepalive_window())
            if result is not TIMEOUT:
                if getattr(self._signals, "stopped", False):
                    self._slog("signal_ignored", signal="assignment", reason="stop")
                elif isinstance(result, dict):
                    self._new_sig_id()
                    self._slog("signal_received", signal="assignment", slot=result)
                    self._slog("signal_accepted", signal="assignment")
                else:
                    # result is None but not stopped: the scouts_invalid self-scout fallback trigger.
                    self._new_sig_id()
                    self._slog("signal_received", signal="assignment", slot=None)
                    self._slog("signal_accepted", signal="assignment", reason="self_scout_fallback")
                return result   # a real assign (slot dict or the scouts_invalid slot=None) or stop
            self._do_keepalive("await_assignment")

    def _poll_signals(self) -> None:
        """Non-blocking bus drain, so `stopped` can become true somewhere other than a wait_*.

        See BusSignals.poll for why this has to be explicit now. Tolerant of a signals object that
        does not implement it -- EngineOps has many partial test doubles, and a double with no bus
        has nothing to drain."""
        poll = getattr(self._signals, "poll", None)
        if callable(poll):
            poll()

    def is_stopped(self) -> bool:
        return self._signals is not None and self._signals.stopped

    def scouts_invalid(self) -> bool:
        if self._signals is not None and hasattr(self._signals, "scouts_invalid"):
            return self._signals.scouts_invalid()
        return False

    # ─────────────────────────────────────────────────────────────────────────────────────────
    # REVERTED VERBATIM TO ad1c570 (2026-08-21), the last commit under which this system actually
    # booked -- 17 real appointments. Restored on branch `apply-revert-proven`, 2026-08-25, at the
    # operator's instruction after a week of incremental fixes failed to make applying work again.
    #
    # Everything I changed here between 2026-08-22 and 2026-08-25 is GONE, deliberately, including
    # changes I still believe were individually correct:
    #   - the XHR submit and XHR /slots primer (f02041d)   -> back to navigation + navigation
    #   - the _schedule_state_ok() round-start gate        -> back to an unconditional re-warm
    #   - _ensure_portal_origin guards inside do_apply     -> unnecessary: a navigation does not
    #                                                         care what page the browser is on
    #   - the primp third route, the anchor= logging, the
    #     portal-busy back-off, form_stale reclassification
    #
    # The bug-fix comments below are ad1c570's own and refer to ITS history, not to anything from
    # this week. Do not "improve" this method again without a booking to prove the change.
    # ─────────────────────────────────────────────────────────────────────────────────────────
    def _submit_via_primp(self, submit_url: str, body_dict: dict, timeout: float):
        """HYBRID submit (HYBRID_SUBMIT): SubmeterVistoCriaPDF via a DETACHED primp client on the
        SAME exit with the browser's cookies -- CONFIRMED 2026-10-07 that this BOOKS (status 200,
        ~1.5KB, the same signature as the browser XHR). Fast, light, and origin-independent (no live
        in-page origin to slip off, so opaque-origin/down-window slips can't strand it). Returns
        (status, html). The %PDF artifact 403s a detached client, so it is fetched via the BROWSER
        afterward (see the PDF block) -- by then the booking is already made."""
        import primp
        from urllib.parse import urlencode
        cookies = {c["name"]: c["value"] for c in (self.session.get_all_cookies() or [])
                   if isinstance(c, dict) and c.get("name") in ("Vistos_sid", "cookiesession1")}
        try:
            ua = (self.session.browser_eval("navigator.userAgent") or "").strip()
        except Exception:
            ua = ""
        pc = primp.Client(proxy=self.warm_proxy, verify=False,
                          timeout=int(max(5.0, timeout)), follow_redirects=True)
        try:
            pc.set_cookies(_PORTAL_ORIGIN + "/", cookies)
        except Exception:
            pass
        headers = {
            "User-Agent": ua or "Mozilla/5.0",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": SCHED_JSP + "?posto_id=" + str(self.posto_pdf),
            "Origin": _PORTAL_ORIGIN,
            "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        }
        r = pc.post(submit_url, data=urlencode(body_dict), headers=headers)
        return (getattr(r, "status_code", None), (getattr(r, "text", "") or ""))

    def do_apply(self, slot: dict) -> str:
        """Bug fix #3: re-warm at the START of every round (ScheduleController form-state TTL is
        ~90-120s and lapses during a long hold -- submitting on stale context bounces -> false
        'taken'). Bug fix #1: /slots POST before SubmeterVistoCriaPDF (non-fatal, best-effort --
        HANDOFF confirms /slots alone under-reports so it's never trusted for availability, only
        used as the session-state primer the confirmed HAR capture showed precedes a real submit).
        The submit body itself is booking_payload()'s minimal 5 fields, confirmed against
        storm-auto's actual proven submit code (see libs/engine/payload.py's module docstring) --
        NOT the full form_defaults merge, which is only correct for the earlier ScheduleController
        warmup POST. Bug fix #4: PDF via submit_form_binary (page.route() interception)."""
        # THE ONE DEVIATION FROM ad1c570 IN THIS METHOD, isolated in its own commit so it can be
        # dropped with a single `git revert` if you want the byte-exact original.
        #
        # These two gates are not "how we apply" -- they are what we report when we DID NOT apply at
        # all. ad1c570 returned "bounced" for both, which charges OUR gate to the SLOT: the lease is
        # released with a penalty, the penalty feeds the Hub's false-slot revert, the slot is marked
        # phantom and closed, and the worker parks on "5 consecutive bounces". Measured 2026-08-15:
        # 5082 bounced leases against a pool that was never phantom.
        #
        # Left as "bounced", a run of this branch would destroy its own slot pool and could not tell
        # us whether the reverted apply path works -- which is the only question this branch exists
        # to answer.
        if self._apply_sem is not None:
            if not self._apply_sem.acquire(self.identity_id, timeout=60):
                return "portal_unreachable"
        _t_apply = time.time()   # round-start -> submit latency (the hot-period metric; see report)
        try:
            # (The booking breaker's is_tripped gate here was removed 2026-09-23 -- do_apply no
            # longer refuses fleet-wide; a down portal is shown on the dashboard, not gated.)

            # ROUND-START RE-WARM. KEEPALIVE_MODE forks here (2026-10-02):
            #   auth_only (default): RE-WARM UNCONDITIONALLY right before the submit. KEEPING is an
            #     authed HOME session and the cursor is NAVIGATION-bound, so it is dead by submit time
            #     -- see the else branch and the measured history below.
            #   schedule: KEEPING already holds a fresh Schedule.jsp cursor (kept warm there), so
            #     apply DIRECTLY when is_warmed() -- see the if branch.
            # The unconditional-re-warm rationale (operator model, 2026-09-23), which still governs
            # auth_only: the portal's Schedule-page form cursor is NAVIGATION-bound, not time-bound:
            # KEEPING is an authed HOME session (the keepalive nav'd there), and any nav away from
            # destroys the cursor -- so a "chain anchor within FORM_STATE_TTL" is NOT a live cursor.
            # Gating the re-warm on is_warmed()/FORM_STATE_TTL let a worker submit with a lapsed
            # cursor and get the questionnaire back -- form_stale -> re-login loop, measured on 'Prev
            # 0923-3018 (copy)' as 20/46 applies entering APPLYING with no warm-up. The chain is the
            # only thing that establishes the cursor, so run it every round; ~40s is the price of a
            # valid submit, and a skipped warm is a guaranteed form_stale (zero bookings).
            #
            # SHOW the re-warm (2026-10-01). phases.py sets substate=APPLYING BEFORE calling do_apply,
            # and this mandatory ~40s re-warm emits no substate of its own, so the dashboard shows
            # KEEPING->APPLYING with a silent warm-up and it LOOKS like warm-up was skipped on a
            # slot-found (recurring operator report). It is not skipped -- it always runs here. Emit
            # the warming_up substate so the apply round's warm-up is visible, then applying for the
            # submit. Observability only; no control-flow change. Uses the same status_change emit
            # _mark_keep_bookable already uses.
            _mode = (getattr(self.cfg.keepalive, "mode", "auth_only")
                     or "auth_only").strip().lower()
            if _mode == "schedule" and self.is_warmed():
                # SCHEDULE (eager-warmup model): KEEPING already holds a VERIFIED-fresh Schedule.jsp
                # cursor -- the keepalive keeps the browser on Schedule.jsp and re-warms before
                # FORM_STATE_TTL -- so apply DIRECTLY, no round-start re-warm. This deliberately forks
                # around rewarm_every_round_is_mandatory, which is an auth_only truth: there the
                # keepalive navs to HOME and the nav-bound cursor is dead by submit time. Here it is
                # not. A form_stale/bounce clears the anchor (below) so the retry re-warms.
                self._slog("apply_round_warm_skipped", reason="schedule_cursor_fresh", slot=slot)
                if self._emit is not None:
                    self._emit("status_change", substate="applying",
                               detail="apply: kept-warm cursor (schedule) -- submitting")
                _warm_ok = True
                _t_warm = time.time()
            else:
                # auth_only (always), OR schedule with a stale/absent cursor: re-warm first.
                if self._emit is not None:
                    self._emit("status_change", substate="warming_up",
                               detail="apply: round-start re-warm (mandatory)")
                self._slog("warmup_start", phase="apply_round", slot=slot)
                _t_warm = time.time()
                _warm_ok = self._run_warmup_steps()
                self._slog("warmup_end", phase="apply_round",
                           result=("ok" if _warm_ok else "fail"),
                           duration_ms=int((time.time() - _t_warm) * 1000),
                           kind=self._warmup_kind(_warm_ok))
            if not _warm_ok:
                # NOT "bounced". This is OUR session failing before we ever spoke to the portal
                # about this slot, and calling it a bounce blames the slot for our dead proxy:
                # the lease is released WITH a penalty, that penalty feeds the Hub's false-slot
                # revert, and the worker parks on "5 consecutive bounces". All three were observed
                # live 2026-08-15 -- 5082 bounced leases against a pool that was never phantom.
                #
                # ROLL THE EXIT before giving the slot back (2026-09-23). The round-start re-warm
                # fails overwhelmingly because the exit is dead (chrome-error/tunnel) -- confirmed on
                # 'Prev 0923-3018' as 178 `session` releases. Releasing without rolling means the
                # worker re-logs in on the SAME dead exit and the NEXT slot it leases dies
                # identically; the 37 available slots churned this way with 4 bookings. Rolling now
                # means the re-login the caller triggers runs on a fresh exit. Roll ONLY when the
                # re-warm failed on a dead-exit exception (chrome-error/tunnel), not a portal
                # rejection (quest_state_stale/csrf/sc-rejected leave _warmup_last_error empty), so a
                # live exit with a portal hiccup is not needlessly discarded.
                # ALREADY-BOOKED is TERMINAL, not a session problem: the warmup detected the portal
                # saying this applicant already has a request. Don't roll the exit, don't retry --
                # hand the slot back untouched and tell the caller to STOP and retire the identity.
                if self._identity_unavailable:
                    self._log("do_apply: identity is unavailable (already has a request) -- "
                              "releasing the slot and stopping the worker")
                    return "already_booked"
                if self._is_transient_login_failure(getattr(self, "_warmup_last_error", "")):
                    self._roll_exit("apply: dead exit at round-start re-warm")
                self._log("do_apply: re-warm at round start failed -- session problem, not the slot")
                return "session"

            # Warm-up succeeded -- back to APPLYING for the submit (pairs with the warming_up emit
            # above so the dashboard shows warm-up THEN submit, not a silent gap inside "applying").
            if self._emit is not None:
                self._emit("status_change", substate="applying", detail="apply: warmed, submitting")

            # THE /slots PRIMER IS GONE. It destroyed the server-side booking cursor every round.
            #
            # It POSTed `{"posto_id": ..., "captcha": ""}` -- a CAPTCHA-less request the portal
            # rejects -- as a real navigation, and discarded the response. MEASURED 2026-08-27,
            # controlled, fresh session per arm, only the primer differing:
            #
            #     control (wait 6s, no primer)  Schedule.jsp 55,194B -> 55,196B, all 5 markers
            #     primer                        Schedule.jsp 55,196B -> 39B, ZERO markers
            #
            # `_SCHED_PAGE_MARKERS` on Schedule.jsp IS this codebase's definition of a live booking
            # cursor -- it is the exact check _run_warmup_steps uses to decide it is warmed. So the
            # chain was: warm-up establishes the cursor and its guard passes; the primer destroys it;
            # SubmeterVistoCriaPDF then has no cursor and the portal answers with the questionnaire
            # start page (captured at var/submits/, 72,211B, title 'Questionnaire to apply for a
            # visa', carrying the authenticated Logout/Change-Password nav -- so the SESSION was
            # never the problem). That was classified form_stale, which sent the ladder off to
            # re-warm and re-login -- rebuilding the very cursor the next round's primer would
            # destroy again.
            #
            # Not replaced with a CAPTCHA-solving variant: measured the same day, even a genuinely
            # solved SCHEDULE_EVISA token (2,617 chars) came back `{"error": ReCaptchaError"}`, so
            # sending it properly would still be a rejected POST. storm-auto's /slots is real
            # secondary discovery with a solved token whose response it INSPECTS -- not a fire-and-
            # forget primer -- and this project already treats /slots as under-reporting and never
            # trusts it for availability. The HAR precedent that motivated it does not outrank a
            # controlled measurement.

            # DIAGNOSTIC ONLY (2026-08-27b) -- observational, changes no control flow.
            #
            # Removing the /slots primer did NOT stop form_stale, which falsifies the assumption the
            # removal rested on: that Schedule.jsp carrying _SCHED_PAGE_MARKERS is the same thing
            # the submit needs. The markers were provably present after the warm-up and the submit
            # was still answered with the questionnaire page.
            #
            # Everything measured so far looked at the cursor in ISOLATION -- in a probe, seconds
            # after a warm-up. Nothing has ever measured it at the instant of the REAL submit,
            # inside the real round, with real timing (28s elapse between ScheduleController's 200
            # and the submit). Read the document the form is about to be built in: one in-page eval,
            # no navigation, no extra request.
            #
            #   markers ABSENT  -> something in that gap still destroys the cursor; hunt it.
            #   markers PRESENT -> the cursor is fine and form_stale means the slot really is taken,
            #                      which makes this a DISCOVERY problem, not an apply one.

            # EXPERIMENT C (the pre-submit /slots "cursor activation") WAS REMOVED 2026-09-26. Live
            # measurement (booking "Prev 0924-3018 copy") retired its premise: the apply-time /slots
            # returned {"error":"ReCaptchaError"} on EVERY round (it never activated anything), added
            # ~20-40s per attempt (getTables + a wasted CAPTCHA solve + the reject), and a real
            # booking (d1c93ae4) landed WITHOUT it working. So it was pure latency on the apply round,
            # and apply latency is what loses the form_stale slot races. The submit stands on the
            # round-start warm-up alone (the PRE-SUBMIT read below confirms an intact cursor); form_
            # stale with that intact cursor is a DISCOVERY/slot-taken problem, not an apply one
            # (see [[pt_apply_path_confirmed_live]]).
            try:
                _pre = self.session.browser_eval("document.documentElement.outerHTML") or ""
                _marks = [m for m in _SCHED_PAGE_MARKERS if m in _pre]
                _t = re.search(r"<title[^>]*>(.*?)</title>", _pre, re.S | re.I)
                self._log(f"do_apply: PRE-SUBMIT doc {len(_pre)}B markers={_marks or 'NONE'} "
                          f"title={(_t.group(1).strip()[:50] if _t else '?')!r} "
                          f"origin_ok={self._on_portal_origin()}")
            except Exception as e:
                self._log(f"do_apply: PRE-SUBMIT doc unreadable ({type(e).__name__}: {str(e)[:90]})")

            body = booking_payload(self.quest_steps, slot, residence=self.residence)
            submit_url = SUBMIT_URL + "?posto_id=" + str(self.posto_pdf)
            # PT-VERSION EXPERIMENT (2026-09-24): submit as an in-page XHR from Schedule.jsp, exactly
            # as the confirmed manual booking does (data/new_booking.har): X-Requested-With:
            # XMLHttpRequest, urlencoded body, Referer Schedule.jsp -- NOT a full form NAVIGATION
            # (browser_form_post) as the ENG version did. The HAR's successful submit is an AJAX call;
            # our navigation-POST got the questionnaire back (form_stale). browser_fetch runs fetch()
            # in the live Schedule.jsp context (Referer set automatically), so the origin must be the
            # live schedule page -- which the round-start warmup establishes.
            try:
                # STRESS MODE: a submit into a down-window hangs the full timeout, spanning up-
                # windows. APPLY_SUBMIT_TIMEOUT (default 100, the proven value) lets the operator
                # fast-fail (~15-20s) so the worker re-fires several times per up-window.
                _submit_timeout = max(5.0, float(self.cfg.raw.get("APPLY_SUBMIT_TIMEOUT", 100.0)
                                                 or 100.0))
                if bool(self.cfg.raw.get("HYBRID_SUBMIT", False)):
                    # HYBRID: submit via a detached primp client (confirmed to book). The %PDF is
                    # fetched via the browser below -- POSTPONED, because the artifact endpoint 403s
                    # a detached client and the booking is already made by submit time.
                    _status, html = self._submit_via_primp(submit_url, body, _submit_timeout)
                else:
                    _res = self.session.browser_fetch(
                        submit_url, body, method="POST",
                        headers={"X-Requested-With": "XMLHttpRequest",
                                 "Referer": SCHED_JSP + "?posto_id=" + str(self.posto_pdf)},
                        encode="form", timeout=_submit_timeout)
                    html = str(_res.get("body") or "") if isinstance(_res, dict) else str(_res or "")
                    _status = _res.get("status") if isinstance(_res, dict) else None
                _final_url = submit_url        # XHR: no navigation, still on Schedule.jsp
                _round_ms = int((time.time() - _t_apply) * 1000)
                self._log(f"do_apply: SubmeterVistoCriaPDF XHR -> status={_status} len={len(html)} "
                          f"round_ms={_round_ms} body[:200]={html[:200]!r}")
                # Structured, back-traceable submit event: len is the booked(~1.5KB) vs
                # form_stale(~66KB questionnaire) discriminator, round_ms is the hot-period latency.
                self._slog("apply_submit", status=_status, len=len(html), round_ms=_round_ms,
                           slot=slot)
            except Exception as e:
                self._log(f"do_apply: SubmeterVistoCriaPDF XHR failed: {e}")
                return "bounced"

            # storm-auto guards the submit response before classifying it, because a blank
            # chrome-error page or a DataDome interstitial is not a portal answer at all -- it
            # falls back to a primp POST there. We have no primp path, so the honest outcome is
            # "the portal never answered": report it as a bounce and let the breaker see it,
            # rather than mis-reading an interstitial as a rejection of the slot.
            _snip = (html or "")[:300]
            _blank = (html or "").strip() in ("", "<html><head></head><body></body></html>")
            _blocked = "body{margin:0;background:#fff}" in _snip or "datadome" in _snip.lower()
            # Chrome's own network-error page ("This site can't be reached"). Confirmed live
            # 2026-08-16: every one of the run's 7 submits came back as the SAME 187,732-byte
            # chrome-error page, and the login path logged 96 rejections at
            # chrome-error://chromewebdata/ in the same window. It matched neither guard above --
            # it is far too big to look blank and says nothing about DataDome -- so it fell through
            # as an unexplained "[error]" and the slot was released as BOUNCED. The request never
            # reached the portal at all; blaming the slot for our dead tunnel is the same
            # misattribution that had a dead proxy manufacturing phantom-slot votes.
            _chrome_error = ("chrome-error://" in (_final_url or "")
                             or ('color-scheme' in _snip and 'theme-color' in _snip
                                 and 'dir="ltr"' in _snip))
            if _blank or _blocked or _chrome_error:
                why = ("Chrome network-error page" if _chrome_error else
                       "blank page" if _blank else "DataDome interstitial")
                self._log(f"do_apply: submit response is a {why} ({len(html or '')}B) -- the request "
                          f"never reached the portal | slot={slot} | final_url={_final_url}")
                # OUR network, not the slot -- but ALSO not our session: ScheduleController had
                # just returned 200 seconds earlier. Distinct from "session" so the caller does not
                # tear down a demonstrably healthy login: confirmed live 2026-08-18, one dropped
                # submit POST cost a full re-login and ~10 MINUTES back to bookable, three times
                # over, while the run's real constraint is how rarely a login succeeds at all.
                return "portal_unreachable"

            outcome = self._classify_submit_response(html, _final_url)
            if outcome == "verify_pdf":
                # We overshot the intermediate auto-submit page and are looking at Chrome's
                # pdf-viewer shell, which says nothing either way. Settle it on the bytes, using
                # storm-auto's strict rule: %PDF magic or nothing. A booking that really happened
                # yields a real document; a rejected one yields an empty/absent render (confirmed
                # 2026-08-14 -- that exact shell came back for an appointment that does not exist).
                pdf_body = b""
                try:
                    pdf_body = self._fetch_pdf_in_page(_final_url)
                except Exception as e:
                    self._log(f"do_apply: PDF verification fetch failed: {e}")
                if pdf_body[:4] == b"%PDF":
                    self._log(f"do_apply: BOOKED -- verified PDF ({len(pdf_body)}B) at {_final_url}")
                    # Capture the confirmation RESPONSE too (2026-10-02): `html` is the
                    # SubmeterVistoCriaPDF submit body that carries the portal's appointment
                    # reference in TEXT (BookedAppointment.appointment_ref, never populated to date).
                    # The PDF has it as well but only as a rendered document; this HTML is the
                    # parseable source. Additive, no control-flow change.
                    try:
                        self._save_submit_body(html, "booked")
                    except Exception:
                        pass
                    if self._emit is not None:
                        self._emit("booked", slot=slot)
                        self._emit("booked_pdf", pdf_path=self._save_pdf(pdf_body), slot=slot)
                    if self._liveness is not None:
                        self._liveness.record_alive()      # a booking proves the portal is up
                    return "booked"
                # No PDF => no booking. Fall back to whatever the body markers say, so a bounce is
                # still classified and still feeds the per-slot / false-slot counters.
                self._log(f"do_apply: MostrarPdf reached but NO valid PDF "
                          f"({len(pdf_body)}B) -- not a booking | slot={slot}")
                outcome = self._classify_submit_response(html, "")
                if outcome == "verify_pdf":
                    outcome = "bounced"
            if outcome != "booked":
                self._last_apply_outcome = outcome
                # SCHEDULE mode: the submit consumed/lost the Schedule.jsp cursor (a non-booked
                # outcome means the cursor is gone -- form_stale is exactly "our form-state expired").
                # Clear the anchor so is_warmed() is false and the NEXT do_apply re-warms, matching
                # the "re-warm on bounce" model. Harmless in auth_only (it re-warms every round
                # regardless, overwriting the anchor). phases._run_apply drives the retry.
                self._clear_anchor()
                # Confirmed live 2026-08-14: this returned silently. A holding applier went
                # `-> applying` then `-> holding` 43s later with terminal "apply bounced" and NOT ONE
                # log line in between, so there was no way to tell a rejected slot from a bounced
                # session from a WAF page. The submit response is the single most important thing
                # this worker does; its rejection must always say why.
                _saved = self._save_submit_body(html, outcome)
                self._log(f"do_apply: submit NOT confirmed [{outcome}] -- "
                          f"{_submit_bounce_reason(html)} | slot={slot} | final_url={_final_url}"
                          + (f" | body={_saved}" if _saved else ""))
                return outcome

            self._log(f"do_apply: BOOKED slot={slot} | final_url={_final_url}")
            # CAPTURE THE CONFIRMATION RESPONSE (2026-10-02). Every NON-booked submit body is saved
            # above, but the one that matters most -- the booked confirmation HTML -- was discarded.
            # It carries the portal's appointment reference, which BookedAppointment.appointment_ref
            # is meant to hold and nothing ever populated (all bookings show ref=None). Saving it is
            # additive (same mechanism, a string already in hand, no control-flow change) and is the
            # evidence needed to wire appointment_ref from a confirmed booking.
            try:
                self._save_submit_body(html, "booked")
            except Exception:
                pass
            # Announce the booking BEFORE going after the PDF. The appointment is real the moment
            # the portal confirms it; the PDF is an artifact that may or may not come back, and
            # making the Hub's record depend on it lost real bookings.
            if self._emit is not None:
                self._emit("booked", slot=slot)
            pdf_body = b""
            try:
                # PT flow: the MostrarPdf form is in the XHR RESPONSE BODY, not the live DOM -- parse
                # it out and fetch directly (fixes route_never_captured, 2026-09-26).
                pdf_body = self._fetch_pdf_from_submit_body(html)
                if pdf_body[:4] != b"%PDF":
                    # ENG/nav fallback: the live page MIGHT carry the auto-submit form.
                    pdf = self.session.submit_form_binary(action_contains="MostrarPdf", timeout=45)
                    self._log(f"MostrarPdf(form): status={pdf.get('status')} "
                              f"ct={pdf.get('content_type')} len={len(pdf.get('body', b''))}")
                    _b = pdf.get("body") or b""
                    if _b[:4] == b"%PDF" or "application/pdf" in (pdf.get("content_type") or ""):
                        pdf_body = _b
                    else:
                        # storm-auto's josand5837 scar: never save a non-PDF as a "PDF".
                        pdf_body = b""
                if pdf_body:
                    self._log(f"do_apply: MostrarPdf PDF captured ({len(pdf_body)}B)")
                    if self._emit is not None:
                        self._emit("booked_pdf", pdf_path=self._save_pdf(pdf_body), slot=slot)
                else:
                    self._log("do_apply: no %PDF from the submit body or the live form -- "
                              "booking IS confirmed, PDF missing")
            except Exception as e:
                # NEVER let PDF retrieval downgrade the outcome -- the appointment is already made.
                self._log(f"do_apply: PDF retrieval failed (booking IS confirmed, PDF missing): {e}")
            if self._liveness is not None:
                self._liveness.record_alive()      # a booking proves the portal is up
            return "booked"
        finally:
            if self._apply_sem is not None:
                self._apply_sem.release(self.identity_id)

    def _fetch_pdf_from_submit_body(self, html: str) -> bytes:
        """Fetch the visa PDF referenced by the SubmeterVistoCriaPDF response body.

        The PT submit is an in-page XHR, so its response (which carries the MostrarPdf auto-submit
        form) is NEVER navigated to -- the live page is still Schedule.jsp. session.submit_form_binary
        queries the LIVE DOM, finds no MostrarPdf form, falls back to the wrong form, and reports
        `route_never_captured` (confirmed live 2026-09-26 on d1c93ae4: a real booking that lost its
        PDF). So parse the MostrarPdf form OUT of the response body and fetch it directly. MostrarPdf
        answers a GET (confirmed HAR), so the form's hidden inputs ride the query string."""
        m = re.search(r'<form[^>]*action\s*=\s*["\']([^"\']*MostrarPdf[^"\']*)["\'][^>]*>(.*?)</form>',
                      html, re.I | re.S)
        action, inner = (m.group(1), m.group(2)) if m else ("", "")
        if not action:
            m2 = re.search(r'["\']([^"\']*MostrarPdf[^"\']*)["\']', html, re.I)  # bare url fallback
            if not m2:
                return b""
            action, inner = m2.group(1), ""
        params = dict(re.findall(
            r'<input[^>]*name\s*=\s*["\']([^"\']+)["\'][^>]*value\s*=\s*["\']([^"\']*)["\']',
            inner, re.I))
        if action.startswith("http"):
            url = action
        elif action.startswith("/"):
            url = "https://pedidodevistos.mne.gov.pt" + action
        else:
            url = BASE + "/" + action.lstrip("/")
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        return self._fetch_pdf_in_page(url)

    def _fetch_pdf_in_page(self, url: str) -> bytes:
        """Read a PDF the browser is already displaying. page.content() only yields Chromium's
        pdf-viewer shell, so go through a credentialed in-page fetch and base64 the bytes back."""
        import base64
        js = ("(async()=>{const r=await fetch(%r,{credentials:'include'});"
              "if(!r.ok)return '';const b=new Uint8Array(await r.arrayBuffer());"
              "let s='';const CH=8192;"
              "for(let i=0;i<b.length;i+=CH){s+=String.fromCharCode.apply(null,b.subarray(i,i+CH));}"
              "return btoa(s);})()" % url)
        b64 = self.session.browser_eval(js, timeout=45) or ""
        return base64.b64decode(b64) if b64 else b""

    @staticmethod
    def _classify_submit_response(html: str, final_url: str = "") -> str:
        """Ported verbatim (logic, not code) from storm-auto's confirmed apply_book classification.
        HTTP 200 is NOT sufficient -- a rejected booking (taken slot, or lapsed form-state) also
        returns 200, but bounces to the questionario/schedule page or the home dashboard instead of
        the confirmation page. The MostrarPdf auto-submit form is the deterministic success signal;
        'comprovativo' is only a secondary positive, gated by no bounce/error markers."""
        low = (html or "").lower()
        url_low = (final_url or "").lower()

        # Reaching MostrarPdf is NOT by itself a booking -- checked against the portal 2026-08-14:
        # worker e5dfc1b1 ended there with Chrome's pdf-viewer shell and the account holds NO
        # appointment. It only means browser_form_post OVERSHOT the intermediate auto-submit page
        # (it waits for navigation to settle; storm-auto reads that page before the JS fires), which
        # leaves the response unclassifiable from the body. The caller must prove it on the bytes.
        overshot_to_pdf = "mostrarpdf" in url_low or "pdf_embedder" in low

        has_pdf_form = "mostrarpdf" in low
        has_confirm = "comprovativo" in low
        has_error_kw = any(kw in low for kw in _ERROR_KEYWORDS)
        bounced_back = any(kw in low for kw in _BOUNCE_KEYWORDS)
        # The MostrarPdf auto-submit form IS a confirmation, DECISIVELY -- a stray bounce/error
        # keyword elsewhere on that page must not override it (that would drop a real booking).
        confirmed = has_pdf_form or (has_confirm and not bounced_back and not has_error_kw)
        if confirmed:
            return "booked"
        if overshot_to_pdf:
            return "verify_pdf"      # "I can't tell from here" -- go read the actual PDF bytes
        if has_error_kw or bounced_back:
            on_schedule_page = any(m in low for m in _SCHEDULE_MARKERS)
            looks_like_home = all(m in low for m in _HOME_MARKERS) or _is_home_url(final_url)
            home_dashboard = looks_like_home and not on_schedule_page
            if home_dashboard:
                return "form_stale"
            # SPLITS storm-auto's flavour (a) in two, on our own evidence.
            #
            # That rule treats any bounce to the schedule/questionario form as "the slot was taken,
            # session is FINE". That holds when we land back on the SCHEDULE page -- the anchor is
            # intact and someone simply beat us to it. It does not hold when the portal answers a
            # SubmeterVistoCriaPDF POST with the QUESTIONNAIRE and no schedule markers at all: that
            # is the response to a submit with no server-side workflow anchor, documented elsewhere
            # in this file from the 2026-08-14 trace ("title='questionnaire to apply for a visa' at
            # 72113B, the questionnaire served back in response to a booking submit").
            #
            # Calling that "no_slot" is the expensive mistake: the slot is released WITH a bounce
            # penalty, bounce_streak climbs until the worker parks, and the volume convinces the
            # Hub's false-slot revert that a real pool is phantom. Measured 2026-08-24 on this
            # booking: 60 bounced leases, 0 booked, and all 16 genuinely-available slots closed --
            # every one of those bounces was this page.
            #
            # form_stale keeps the slot and re-warms, which is what a lapsed anchor actually needs.
            # The downside if this is ever wrong is bounded (REWARM_MAX_ATTEMPTS, then the ladder
            # escalates); the downside of the old reading was destroying a real slot pool.
            if "questionario" in low and not on_schedule_page:
                return "form_stale"
            return "no_slot"
        # No confirmation marker and no bounce marker at all: genuinely unexpected, not a bounce.
        return "error"

    def _save_submit_body(self, html: str, outcome: str) -> str:
        """Persist the portal's answer to SubmeterVistoCriaPDF. DIAGNOSTIC, not part of any verdict.

        storm-auto captures this on every submit (`_evidence(..., "pdf_submission", always=True)`).
        storm-hub captured nothing, so the one document that explains a rejected booking was
        discarded every time -- five separate hypotheses were argued from a keyword match and a byte
        count because the body itself no longer existed. Measured 2026-08-27: a 72,133B response
        titled 'questionnaire to apply for a visa' classified `form_stale`, and we could not tell
        whether that meant our form state had lapsed or the slot was simply taken.

        Best-effort and never raises: a failed write must not change an apply outcome.
        """
        from pathlib import Path as _Path
        import time as _time
        try:
            d = _Path(__file__).resolve().parents[2] / "var" / "submits"
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{self.identity_id}_{outcome}_{int(_time.time())}.html"
            p.write_text(html or "", encoding="utf-8", errors="replace")
            return str(p)
        except Exception as e:      # pragma: no cover - diagnostics must never break the round
            self._log(f"do_apply: could not save the submit body ({e})")
            return ""

    def _save_pdf(self, body: bytes) -> str:
        """Persist a confirmed booking's PDF to local disk (same var/ convention as checkpoints --
        never in Redis). The Hub's event consumer picks up the path from the `booked_pdf` event and
        creates the Artifact/BookedAppointment rows; the worker never touches the DB directly."""
        from pathlib import Path as _Path
        import time as _time

        d = _Path(__file__).resolve().parents[2] / "var" / "artifacts"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{self.identity_id}_{int(_time.time())}.pdf"
        path.write_bytes(body)
        return str(path)

    # ── lifecycle ────────────────────────────────────────────────────────────────
    def save_checkpoint_now(self) -> None:
        """Call after reaching a healthy holding state. Always overwrites with the latest state
        (Phase C3's write policy) -- the caller (WorkerRuntime/phases.py hold loop) decides when a
        state is healthy enough to snapshot; this module never decides that on its own."""
        if self.session is None:
            return
        try:
            cookies_full = self.session.get_all_cookies()
        except Exception:
            cookies_full = []
        cookies = {c["name"]: c["value"] for c in cookies_full if "name" in c and "value" in c}
        ckpt.save_checkpoint(
            self.identity_id, cookies=cookies, sub_account_id=self.sub_account_id,
            cookies_full=cookies_full, stage="schedule_jsp" if self.posto_pdf else "login",
            posto_pdf=self.posto_pdf,
            # Store the run's target posto too, so the restore posto-guard can compare EXACTLY rather
            # than lean on posto_pdf (which can legitimately differ from posto_id on some responses).
            posto_id=self.posto_id,
        )

    def _close_session_quietly(self) -> None:
        """Tear down the current browser, if any, without letting cleanup mask the real error.

        Used on every path that is about to REPLACE self.session. Failures are logged rather than
        swallowed (see close()) because a close that silently fails leaks a Chromium tree, which is
        the documented 2600-process incident and the 588-process one measured on 2026-08-21.
        """
        if self.session is None:
            return
        try:
            self.session.close_browser()
        except Exception as e:
            self._log(f"close_browser failed while replacing the session ({e}) -- force-killing")
            try:
                from libs.engine.vendor.session import _kill_proc_tree
                _kill_proc_tree(getattr(self.session, "_driver_pid", None))
            except Exception:
                pass
        self.session = None

    def close(self) -> None:
        if self.session is not None:
            try:
                self.session.close_browser()
            except Exception as e:
                # NOT silent. A close that fails leaves a Chromium tree alive, and this project has
                # a documented 2600-leaked-process incident from exactly that. Swallowing is still
                # right -- a cleanup failure must not mask the real error -- but it has to be
                # visible, and the vendored session's own kill path is the fallback.
                # close_browser already force-kills the driver subtree when the graceful path
                # HANGS. This branch is the other case -- it raised for some other reason -- and it
                # used to be `pass`, so a failed close left a Chromium tree alive with no trace. The
                # project has a documented 2600-leaked-process incident from exactly this.
                self._log(f"close_browser failed ({e}) -- force-killing the driver tree")
                try:
                    from libs.engine.vendor.session import _kill_proc_tree
                    _kill_proc_tree(getattr(self.session, "_driver_pid", None))
                except Exception as ke:
                    self._log(f"force-kill also failed ({ke}) -- possible leaked browser")
            self.session = None

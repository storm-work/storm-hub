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

from libs.bus.breaker import RedisBreaker
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

from .bus_signals import TIMEOUT

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
# storm-auto matches only the PORTUGUESE nav text. We send lang=ENG in both the warmup and the
# booking payload, so those strings can simply be absent from an English-rendered dashboard, and a
# genuine form-state expiry then gets mis-read as "the slot was taken" -- no re-warm, and the pool
# wrongly blamed. Confirmed live 2026-08-14: 20 of 24 bounces landed on the bare /VistosOnline/
# root, but only 6 matched the Portuguese markers.
#
# So the final URL is accepted as equivalent evidence: a page the portal served AT its own site root
# IS the dashboard, whatever language it rendered in. The schedule-marker veto below stays absolute
# in both cases -- a page carrying cmbPeriodo/getSlots is the schedule form, not the dashboard, and
# must remain no_slot however it was reached.
# The scheduling page's own markers, verbatim from storm-auto's _sched_markers. Their ABSENCE from
# Schedule.jsp means ScheduleController was silently rejected and there is no form state to book
# against -- see _run_warmup_steps.
_SCHED_PAGE_MARKERS = ("getSlots", "captchaDiv", "SubmeterVistoCriaPDF", "f_date_c", "cmbPeriodo")
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
    """True when the portal answered with its generic 'try again later' refusal."""
    low = (html or "").lower()
    return any(m in low for m in _PORTAL_BUSY_MARKERS)
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
    if _is_portal_busy(page):
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
        # phase-2 (hold/apply/discovery) proxy -- only used when HOLD_SWAP_AFTER_WARMUP is on
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

        # P3 -- proxy rotation: which pool a rotation request should draw from. do_swap() flips this
        # to "hold" unconditionally the moment warmup finishes, regardless of whether
        # HOLD_SWAP_AFTER_WARMUP is actually on (see do_swap()'s docstring and
        # _request_proxy_rotation()) -- the operator's explicit requirement that pool selection "will
        # not be different when swap is off".
        self._current_phase = "warmup"

        self._login_sem: RedisSemaphore | None = None
        self._apply_sem: RedisSemaphore | None = None
        self._breaker: RedisBreaker | None = None
        if redis_client is not None and booking_id:
            from libs.bus.semaphore import make_gate
            self._login_sem = make_gate(redis_client, booking_id, "login", cfg.raw.get("LOGIN_CONCURRENCY", 10))
            self._apply_sem = make_gate(redis_client, booking_id, "apply", cfg.raw.get("APPLY_CONCURRENCY", 100))
            self._breaker = RedisBreaker(
                redis_client, booking_id,
                trip_threshold=int(cfg.raw.get("BREAKER_TRIP_THRESHOLD", 5)),
                cooldown=float(cfg.raw.get("BREAKER_COOLDOWN", 120.0)),
            )

    # ── shared warm-up ──────────────────────────────────────────────────────────
    def do_login(self) -> bool:
        if self._breaker is not None and self._breaker.is_tripped():
            self._log("do_login: booking breaker is tripped -- portal appears down, not retrying yet")
            return False

        if self._login_sem is not None:
            if not self._login_sem.acquire(self.identity_id, timeout=300):
                self._log("do_login: login concurrency gate timed out")
                return False
        try:
            if self._try_restore_from_checkpoint():
                return True
            return self._fresh_login()
        finally:
            if self._login_sem is not None:
                self._login_sem.release(self.identity_id)

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
            self.posto_pdf = data.get("posto_pdf")
            # UNVERIFIED on this session: copied from a file, not rebuilt. _anchor_at stays None so
            # nothing downstream can mistake it for a fresh chain.
            self._set_anchor("checkpoint")
            self._restore_stage = "alive"
            self._log(f"checkpoint restore: session alive and bookable after {spent():.0f}s "
                      f"-- skipping login+warmup")
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
        self.warm_proxy = rolled
        self._exit_ip = None          # a new exit: the next observation is a baseline, not a change
        self._log(f"proxy: rolled to a fresh sticky exit [{_proxy_label(rolled)}] ({why})")
        return True

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
            if _is_portal_busy(html):
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
        for i, candidate in enumerate(ordered):
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
        # Rejections attributable to OUR exit moving across the solve window. Bounded like
        # `transient` and, like it, not charged to the sub-account -- see the use site below.
        ip_moves = 0
        max_ip_moves = max(0, int(self.cfg.raw.get("LOGIN_IP_MISMATCH_RETRIES", 2)))
        rounds = 0
        while attempt < sticky_max:
            rounds += 1
            if rounds > 1:
                # Regression (L2/L3, closed by P3): a repeated login failure used to keep retrying
                # on the SAME leased proxy forever -- if the proxy itself is the problem (burned IP,
                # rate-limited), no number of retries recovers. Rotate before every retry past the
                # first, matching storm-auto's confirmed behavior of a fresh proxy per sticky
                # attempt. Best-effort: if the Hub has nothing free, keep using the current proxy.
                new_proxy = self._request_proxy_rotation(self._current_phase)
                if new_proxy:
                    self.warm_proxy = new_proxy
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
                result = self.session.browser_login(
                    self.account["username"], self.account["password"],
                    capsolver_keys=self.capsolver_keys,
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
                if self._breaker is not None:
                    self._breaker.record_success()
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
            # "Por favor, confirma o reCAPTCHA" is the token/IP mismatch signature: the token was
            # minted against one exit and submitted from another, so the portal cannot verify it.
            # When the exit demonstrably moved across the ~6s solve window, that is the cause, and
            # spending the account's two-strike budget on it spends a sub-account on our own
            # rotation -- at two strikes the portal blocks the account permanently. MEASURED
            # 2026-08-25: PEDRO TEIXEIRA RAMOS burned 3 of 3 candidates and AMILCAR WANDERSON 2 of
            # 2, every one on this verdict with "exit IP CHANGED" logged immediately before, and
            # both ended FAILED with no accounts left while slots sat open.
            #
            # Bounded by max_ip_moves: past the cap we stop making excuses and charge it, because at
            # that point we genuinely cannot tell an unstable exit from a failing account.
            if (_is_recaptcha_verdict(reason) and ip_moves < max_ip_moves
                    and self._note_exit_ip("after reCAPTCHA rejection")):
                ip_moves += 1
                self._log(f"login: reCAPTCHA rejection coincided with the exit moving "
                          f"({ip_moves}/{max_ip_moves}) -- token/IP mismatch, NOT charged to "
                          f"{self.sub_account_id[:8]}; rotating to a fresh exit and retrying")
                if not self._roll_exit("recaptcha token/ip mismatch"):
                    new_proxy = self._request_proxy_rotation(self._current_phase)
                    if new_proxy:
                        self.warm_proxy = new_proxy
                self._backoff(ip_moves)
                continue          # deliberately does NOT advance `attempt` or `captcha_fails`
            attempt += 1
            self._log(f"login attempt {attempt}/{sticky_max} [{_proxy_label(self.warm_proxy)}"
                      f" acct={self.sub_account_id[:8]}]: rejected ({reason})")
            # The portal DECLINING to serve us is fleet-wide information, not this worker's bad
            # luck. MEASURED 2026-08-23: 265 of 266 "Não foi possível de momento concretizar o seu
            # pedido" refusals landed at login, and each worker independently kept trying -- 341
            # login attempts to produce 30 sessions. The CAPTCHA solves fine and the token is
            # accepted, so nothing about retrying harder helps; the whole fleet should back off
            # together and come back later.
            #
            # Fed to the SAME breaker the submit path already uses (its trip is consecutive-only, so
            # an isolated refusal among successes cannot trip it). Deliberately NOT counted for a
            # credential rejection or a reCAPTCHA verdict -- those say something about this account,
            # not about the portal's willingness to serve anyone.
            if self._breaker is not None and _is_portal_busy(reason):
                self._breaker.record_portal_down()
                self._log("login: that was the portal's 'try again later' page -- reported to the "
                          "booking breaker so the whole fleet backs off, not just this worker")
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

    def _request_proxy_rotation(self, phase: str | None = None) -> str | None:
        """Ask the Hub for a fresh proxy from the pool matching `phase` ("warmup" or "hold") -- P3,
        spec'd directly by the operator: "each worker process request[s] a rotation of proxy to
        proxy provider ... based on the phase: in warmup phase select one from warmup proxy pool, in
        hold phase select one from hold proxy pool." The worker process is deliberately DB-less (see
        apps/worker/__main__.py's own docstring), so it can't lease a proxy row itself -- this
        round-trips through the same event/command bus slot assignment already uses: emit
        `proxy_request`, block for the Hub's `proxy_assigned`/`proxy_unavailable` reply
        (apps/hub/events.py's `_handle_proxy_request`). `phase` defaults to `self._current_phase`,
        which do_swap() sets to "hold" unconditionally the instant warmup finishes -- so a rotation
        during holding always draws from the hold pool even when HOLD_SWAP_AFTER_WARMUP is off (the
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
        time.sleep(min(cap, base * (2 ** (attempt - 1))) * random.uniform(0.8, 1.2))

    def do_warmup(self) -> bool:
        if self._restore_stage in ("alive", "rewarmed"):
            # CONSUMED, not just read. This short-circuit is true exactly ONCE -- for the warm-up
            # that immediately follows a checkpoint restore, which already left the session bookable
            # so redoing the questionnaire chain would be waste. It was never cleared, so it stayed
            # true for the worker's entire life, and every LATER do_warmup() -- all of which come
            # from the recovery ladder asking for a genuine re-warm -- returned True instantly
            # without touching the portal.
            #
            # That turned the ladder into a no-op and produced a perfect infinite loop. Traced live
            # 2026-08-20 on booking "Test 3018", worker CARLOS FURTADO, ~26s per cycle, 120 slot
            # leases deep and still going:
            #
            #   do_warmup: csrf_missing after Formulario POST
            #   do_apply: re-warm at round start failed -- session problem, not the slot
            #   [slot_released]
            #   -> holding (re-warming: apply re-warm failed)
            #   -> warming_up
            #   -> warmed          <- SAME SECOND; a real warm-up takes ~38s
            #   -> holding
            #   -> applying        <- leases another slot, fails identically, forever
            #
            # The worker could never recover, and each cycle churned a slot lease: leased, released
            # (reopening the slot), re-leased. Consuming the flag makes the second call do real work.
            self._restore_stage = "none"
            # Honest about what this is: the checkpoint probe found the session bookable, but the
            # questionnaire chain did NOT run, so the anchor is as old as the file. do_apply
            # re-warms unconditionally at round start, which is what makes this safe -- without
            # that, this is a worker displaying `warmed` on form state nobody verified.
            self._log(f"do_warmup: skipped -- checkpoint restore left the session bookable "
                      f"(anchor={self._anchor_desc()}); the first apply round re-warms")
            return True   # checkpoint restore already got us to a bookable state
        return self._run_warmup_steps()

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

        Sustained slowness is also reported to the booking-wide RedisBreaker, so a degraded window
        makes the whole fleet back off together rather than each worker independently deciding to
        re-login into the same wall.
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
            # Prefer rolling the sticky session id: it always works, needs no Hub round-trip and
            # no free pool row, and the exit it lands on is as good as any the Hub could lease.
            # _request_proxy_rotation stays as the fallback for a credential with no session id.
            if self._roll_exit(f"{phase} unreachable"):
                pass
            else:
                new_proxy = self._request_proxy_rotation(self._current_phase)
                if new_proxy:
                    self.warm_proxy = new_proxy
                    self._log(f"portal: {phase} unreachable -- rotated to a fresh exit before retrying")

        if kind == "slow" and self._health.is_degraded():
            # Not this worker's bad luck any more -- the window is degraded.
            if self._breaker is not None:
                self._breaker.record_portal_down()
            self._log(f"portal: {self._health.slow_streak} consecutive slow failures -- "
                      f"reporting the window as degraded so the booking backs off together")
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
        # Every step below is an in-page fetch. If a prior failed navigation left the page on
        # chrome-error, all three retries of the home-lang POST are guaranteed to raise
        # "TypeError: Failed to fetch" -- that is an opaque origin, not the transient network blip
        # the retry loop was written for. Re-anchor first so the retries mean something.
        self._ensure_portal_origin("the warm-up chain")
        try:
            # Retried like every other in-page fetch in this chain. When the labelling went in I
            # made this one raise on the FIRST failure while giving the quest steps below three
            # attempts -- and it is by far the most common failure point: 377 aborted warmups in one
            # live run, 370 of them from just two workers stuck in a hot retry loop, none of which
            # ever reached ScheduleController. "TypeError: Failed to fetch" is a transient network
            # failure inside the page (dead proxy, dropped tunnel), exactly what a retry is for.
            for attempt in range(1, _QUEST_STEP_RETRIES + 1):
                try:
                    self.session.browser_eval(
                        "(async()=>{const r=await fetch('%s',{method:'POST',credentials:'include',"
                        "headers:{'Content-Type':'application/x-www-form-urlencoded'},"
                        "body:'lang=ENG&lang=PT'});return r.status;})()" % HOME_URL
                    )
                    break
                except Exception as e:
                    self._log(f"home-lang POST attempt {attempt}/{_QUEST_STEP_RETRIES} failed: {e}")
                    if attempt == _QUEST_STEP_RETRIES:
                        raise RuntimeError(f"home-lang POST failed: {e}") from e
                    time.sleep(1.5)
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
                for attempt in range(1, _QUEST_STEP_RETRIES + 1):
                    try:
                        res = self.session.browser_eval(js)
                    except Exception as e:
                        # storm-auto retries the EXCEPTION path too. Ours only retried a non-200
                        # RESULT, so a transient in-page fetch failure ("Failed to fetch") escaped
                        # to the outer handler and killed the whole warmup on the first blip --
                        # 306 aborted warmups in one live run.
                        self._log(f"quest step {i+1}/{len(steps)} attempt {attempt}/"
                                  f"{_QUEST_STEP_RETRIES} raised: {e}")
                        if attempt == _QUEST_STEP_RETRIES:
                            raise RuntimeError(f"quest step {i+1} failed: {e}") from e
                        time.sleep(1.5)
                        continue
                    if isinstance(res, dict) and res.get("status") == 200:
                        break
                    self._log(f"quest step {i+1}/{len(steps)} attempt {attempt}/"
                              f"{_QUEST_STEP_RETRIES} non-200: {res}")
                    # storm-auto sleeps 1.5s here ("retrying -- like a manual page refresh"). We
                    # slept only on the EXCEPTION path, so a non-200 burned all three attempts in
                    # the same second -- visible in the live log: step 5 attempts 1 and 2 both at
                    # 18:41:53, step 6 attempts 1-3 across 18:41:54-56. Against a rate limiter or a
                    # WAF that is worse than not retrying at all: three hits where one would do.
                    if attempt < _QUEST_STEP_RETRIES:
                        time.sleep(1.5)
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
            self._note_portal("warmup", time.time() - _t0, ok=False, error=str(e))
            return False

    def do_swap(self) -> str | None:
        """Phase-1 (login/warmup) proxy -> phase-2 (hold/apply/discovery) proxy migration.

        Off by default (HOLD_SWAP_AFTER_WARMUP, catalog default False) -- most bookings just hold on
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
        if not self.cfg.hold_swap.swap_after_warmup:
            return None
        self._current_phase = "hold"
        if not self.hold_proxy:
            self._log("do_swap: HOLD_SWAP_AFTER_WARMUP is on but no hold_proxy was provided -- skipping")
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
        if not self._probe_alive():
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
        while True:
            result = self._signals.wait_slot_open(timeout=self._keepalive_window())
            if result in ("slot_open", "stop"):
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

    def _mark_hold_bookable(self, bookable: bool, why: str = "") -> None:
        """Publish whether this HOLDING worker can actually take a slot.

        Emits the same `status_change` event the PhaseMachine's on_transition emits, so it flows
        through the existing events.py -> Worker.substate/substate_detail path with no new plumbing.
        The substate stays HOLDING (a holding -> holding transition is legal, and the worker really
        is still holding -- authenticated, keepalive-ing, recoverable in place); only the DETAIL
        changes, carrying apps/hub/scheduler.py's NOT_BOOKABLE_PREFIX marker that _assign_slots
        filters on. Same machine-readable-prefix convention as LAUNCH_FAILED_PREFIX.
        """
        if self._emit is None:
            return
        detail = f"{NOT_BOOKABLE_PREFIX} {why}" if not bookable else ""
        self._emit("status_change", substate="holding", detail=detail)

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
        # STATE-AWARE. This used to navigate HOME_URL unconditionally, which keeps the AUTH session
        # alive and nothing else -- so a worker could hold for an hour, stay perfectly logged in, and
        # still have a long-lapsed booking form-state. Its first gettime or first submit then failed,
        # and the failure was counted toward a re-login rather than the re-warm that would fix it.
        #
        # Once a worker has warmed (posto_pdf set), the thing worth keeping alive is READINESS, not
        # just the session: probe the schedule page and check it still carries the scheduling
        # markers. Absent means the ScheduleController anchor is gone -- re-warm in place, which is
        # ~40s and needs no login. Before warmup there is no form-state to hold, so home is right.
        probe = self.cfg.keepalive.probe or "home"
        warmed = self.posto_pdf is not None
        if probe == "home" and warmed:
            url = SCHED_JSP + "?posto_id=" + str(self.posto_pdf)
        else:
            url = HOME_URL if probe == "home" else probe
        try:
            final_url = self.session.browser_nav(url, timeout=30)
            self._keepalive_dead_streak = 0
            if probe != "home":
                # A custom KEEPALIVE_PROBE URL: no positive/negative content evidence is known for
                # an arbitrary operator-chosen page, so this stays exactly what it was -- a
                # successful nav is all that's checked. Only the two real pages (home, Schedule.jsp)
                # get classified below.
                if self._emit is not None:
                    self._emit("log_line", text=f"keepalive: probe OK ({context})")
                return
            page = self.session.browser_eval("document.documentElement.outerHTML") or ""
            state = classify_session_state(page, final_url, warmed=warmed)
            if state == SessionState.AUTHENTICATED:
                # Only fires on a RECOVERY transition (streak was > 0), matching the original
                # behaviour of never emitting a bookable/streak-reset event on an already-healthy
                # cycle -- an event on every single healthy tick is exactly the kind of
                # write-amplification F6 exists to guard against.
                if warmed and getattr(self, "_rewarm_fail_streak", 0) > 0:
                    self._rewarm_fail_streak = 0
                    self._mark_hold_bookable(True)
                if self._emit is not None:
                    self._emit("log_line", text=f"keepalive: probe OK ({context})")
                return
            if state == SessionState.SESSION_EXPIRED:
                # F22/F24's fix: a dead session must never be re-warmed (there is no session left
                # to POST a questionnaire step against) -- it must be re-authenticated. This is the
                # SAME chain the transport-failure branch below already uses, reused, not new
                # machinery.
                self._log(f"keepalive: session expired ({len(page)}B, exit={getattr(self, '_exit_ip', None)}, "
                          f"{context}) -- re-authenticating rather than re-warming a dead session")
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
            # UNKNOWN: no positive AND no negative evidence. Pre-warm, there is nothing to re-warm
            # (no ScheduleController anchor exists yet) -- wait for the next cycle rather than
            # guess. Warmed, this is exactly the original "form-state lapsed" path: markers are
            # absent, but nothing here says the SESSION is dead (that's the SESSION_EXPIRED branch
            # above), so re-warming in place remains the correct bounded attempt, escalating to a
            # full session rebuild via _maybe_escalate_stuck_rewarm if it keeps failing (F26: an
            # unrecognised response gets a real "we don't know" branch instead of being silently
            # forced into a specific verdict).
            if not warmed:
                self._log(f"keepalive: unrecognised pre-warm response ({len(page)}B, {context}) -- "
                          f"inconclusive, will re-probe")
                return
            self._log(f"keepalive: form-state lapsed ({len(page)}B, no scheduling markers) "
                      f"-- re-warming in place ({context})")
            if self._run_warmup_steps():
                self._rewarm_fail_streak = 0
                self._log(f"keepalive: re-warm restored the schedule form ({context})")
                # Clear the not-bookable marker: this worker can take a slot again.
                self._mark_hold_bookable(True)
            else:
                # Still no re-login here: before slot-found the login wall is the scarce
                # resource, and a lapsed FORM anchor is not a dead auth session (the LOOSE
                # HOLD rule below). That part was always right. What was wrong is that the
                # failure was recorded ONLY as a log line -- no substate, no detail, no
                # counter -- so the worker kept reporting a bare `holding`, the Hub counted
                # it as ready capacity, and it bounced the first slot it was handed.
                # Confirmed live 2026-08-21: 110 such failures against 42 recoveries.
                self._rewarm_fail_streak = getattr(self, "_rewarm_fail_streak", 0) + 1
                self._log(f"keepalive: re-warm failed -- holding but NOT BOOKABLE "
                          f"(streak={self._rewarm_fail_streak}, {context})")
                self._mark_hold_bookable(
                    False, f"form-state lapsed, re-warm failed x{self._rewarm_fail_streak}")
                self._maybe_escalate_stuck_rewarm(context)
        except Exception as e:
            self._keepalive_dead_streak = getattr(self, "_keepalive_dead_streak", 0) + 1
            dead_trip = max(1, int(self.cfg.keepalive.dead_trip))
            if self._emit is not None:
                self._emit("log_line", text=f"keepalive: probe FAILED ({e}) "
                                             f"streak={self._keepalive_dead_streak}/{dead_trip} ({context})")
            if self._keepalive_dead_streak >= dead_trip:
                self._keepalive_dead_streak = 0
                # LOOSE HOLD rule, ported from storm-auto's confirmed anti-attrition logic: before a
                # slot has actually been found, there is nothing to book yet, so a proxy/session
                # blip must NEVER burn a re-login -- the login wall is the scarce resource, not
                # portal traffic. Only actively restore once slot_found has actually fired (or the
                # operator has explicitly disabled this caution via KEEPALIVE_NO_RESTORE=False AND
                # slot_found is set). This was previously unconditional -- a transient WAF/DataDome
                # flicker during a multi-hour pre-slot-found hold burned a full re-login by default.
                if self.cfg.keepalive.no_restore or not self._slot_found_seen():
                    if self._emit is not None:
                        self._emit("log_line", text="keepalive: dead-trip reached but slot not found "
                                                      "yet (or KEEPALIVE_NO_RESTORE set) -- preserving "
                                                      "session, not burning a re-login")
                else:
                    self._attempt_keepalive_restore(context)

    def _maybe_escalate_stuck_rewarm(self, context: str) -> None:
        """A re-warm that keeps failing is not a lapsed form -- rebuild the session instead.

        CONFIRMED LIVE 2026-08-22 (SOFIA FILOMENA COSTA on Test 2032). The worker held a perfectly
        healthy AUTH session -- its keepalive nav returned a full 28,505-byte page every cycle -- but
        every questionnaire POST came back 403 (a pt-pt block page, and once a 200 with an opaque
        encoded body: the WAF challenging POSTs while letting GETs through). The exit IP was flagged,
        not the session.

        That state could not recover, because the two ladders each declined to act:
          - the dead-session ladder never fired: browser_nav SUCCEEDS, so _keepalive_dead_streak
            stayed 0 and the dead-trip never tripped. The session does not look dead because it
            is not dead.
          - the re-warm ladder marked NOT BOOKABLE and returned, by design.
        So it looped lapse -> re-warm -> 403 -> mark -> wait, reaching streak 7 and climbing, on the
        same proxy that was blocked the whole time. Re-warming cannot clear an IP-level block.

        The remedy is the one the dead-session path already owns: drop the browser and rebuild
        through a fresh login, which rotates the proxy on the way. Gated on slot_found because
        before that the LOOSE HOLD rule applies -- a re-login costs a CAPTCHA solve and a slice of
        the sub-account budget, and pre-slot-found there is nothing to book that would justify it.
        Bounded by _MAX_KEEPALIVE_RESTORE_ATTEMPTS, so this cannot become a new unbounded loop.
        """
        after = max(1, int(self.cfg.raw.get("REWARM_ESCALATE_AFTER", 3)))
        if self._rewarm_fail_streak < after:
            return
        # DEADLOCK, confirmed live 2026-08-23. Gating on slot_found is right for the SPLIT model,
        # where an idle applier waits and scouts do the discovering. Under MONO_BOOKING the apply
        # worker IS the discoverer, so the gate becomes circular: its form-state is lapsed -> it
        # cannot discover -> no slots are found -> slot_found never fires -> escalation is refused
        # forever. Four workers sat at "re-warm failed x7" for over an hour, displayed as `holding`,
        # while the booking had nothing else that could ever find a slot.
        #
        # Once slot_OPEN has fired in mono mode there is real work to do and a rebuild is justified;
        # the LOOSE HOLD rule still applies before that, and still applies throughout in split mode.
        mono = bool(self.cfg.raw.get("MONO_BOOKING", False))
        gate_open = self._slot_found_seen() or (mono and self._slot_open_seen())
        if not gate_open:
            self._log(f"keepalive: {self._rewarm_fail_streak} failed re-warms, but this booking has "
                      f"not opened yet -- holding rather than burning a re-login ({context})")
            return
        self._log(f"keepalive: {self._rewarm_fail_streak} consecutive re-warm failures -- the form "
                  f"is not the problem; rebuilding the session on a fresh proxy ({context})")
        self._rewarm_fail_streak = 0          # the ladder owns it now; don't re-escalate every tick
        # posto_pdf still holds the value from the warm-up that has since lapsed. Clearing it is what
        # makes _attempt_keepalive_restore re-warm rather than stop at re-authenticating -- its
        # `if ok and self.posto_pdf is None` guard exists for a launch-time restore, where a set
        # posto_pdf really does mean the anchor is good. Here it means the opposite.
        self._clear_anchor()
        if self._attempt_keepalive_restore(context) and self.posto_pdf is not None:
            self._mark_hold_bookable(True)

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
        try:
            if self.session is not None:
                self.session.close_browser()
        except Exception:
            pass
        self.session = None
        ok = self._try_restore_from_checkpoint() or self._fresh_login()
        # `posto_pdf is None` was the wrong test. A checkpoint restore SETS posto_pdf from the file
        # without rebuilding anything, so an unverified anchor silently suppressed this re-warm and
        # the restore reported success on form state nothing had checked. Re-warm unless the chain
        # itself established the anchor on THIS session.
        if ok and self._anchor_src != "chain":
            ok = self._run_warmup_steps()   # restore only re-authenticates; form-state needs re-warming
        if ok:
            self._keepalive_restore_attempts = 0
        if self._emit is not None:
            self._emit("log_line", text=f"keepalive: restore {'succeeded' if ok else 'FAILED'}")
        return bool(ok)

    def do_discover(self, dates: list[str]) -> list[dict]:
        """Bug fix #6: 3-way per-date outcome (found/empty/failed). Uses gettime + real-browser-nav
        getPeriodosOcupados per confirmed_pipeline_path.md -- never browser_fetch/primp for these
        endpoints, DataDome 403s raw fetch regardless of session health.

        Shared gettime cache: if the Hub handed this worker a still-fresh special_days payload
        alongside its discover assignment (apps/hub/scheduler.py's _drive_scouts,
        GETTIME_CACHE_SECS), skip the gettime fetch entirely and use it directly -- storm-auto's
        proven pattern (app/core/slot_manager.py's _gettime_cache) is a fleet-wide shared TTL cache,
        not a per-worker one. Otherwise fetch fresh and, on success, report it back via a
        `gettime_result` event so the Hub can cache it for the rest of the fleet."""
        out = []
        special_days = getattr(self, "_pending_special_days", None)
        self._pending_special_days = None
        fetched_fresh = special_days is None
        if fetched_fresh:
            try:
                self.session.browser_nav(f"{GETTIME_URL}?id_posto={self.posto_id}", timeout=30)
                gettime_body = self.session.browser_eval("document.documentElement.outerHTML") or ""
            except Exception as e:
                self._log(f"do_discover: gettime nav failed ({e}) -- all requested dates FAILED, not empty")
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
                return [{"date": d, "outcome": "failed"} for d in dates]
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
            free, _occupied = parse_periodos_ocupados(m.group(1), max_period=self.cfg.discovery.max_period)
            if free is None:
                out.append({"date": d, "outcome": "failed"})   # untokenizable -> UNKNOWN, never prune
            elif free:
                out.append({"date": d, "outcome": "found", "periods": free})
            else:
                out.append({"date": d, "outcome": "empty"})
        return out

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
        while True:
            result = self._signals.wait_assignment(timeout=self._keepalive_window())
            if result is not TIMEOUT:
                return result   # a real assign (slot dict or the scouts_invalid slot=None) or stop
            self._do_keepalive("await_assignment")

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
        try:
            if self._breaker is not None and self._breaker.is_tripped():
                return "portal_unreachable"      # see the note above: our gate, not the slot's fault

            if not self._run_warmup_steps():
                # NOT "bounced". This is OUR session failing before we ever spoke to the portal
                # about this slot, and calling it a bounce blames the slot for our dead proxy:
                # the lease is released WITH a penalty, that penalty feeds the Hub's false-slot
                # revert, and the worker parks on "5 consecutive bounces". All three were observed
                # live 2026-08-15 -- 5082 bounced leases against a pool that was never phantom.
                self._log("do_apply: re-warm at round start failed -- session problem, not the slot")
                return "session"

            try:
                self.session.browser_form_post(
                    SLOTS_URL + "?posto_id=" + str(self.posto_pdf),
                    {"posto_id": self.posto_pdf, "captcha": ""},
                    timeout=45,
                )
            except Exception as e:
                self._log(f"do_apply: /slots primer failed (non-fatal): {e}")

            body = booking_payload(self.quest_steps, slot, residence=self.residence)
            submit_url = SUBMIT_URL + "?posto_id=" + str(self.posto_pdf)
            try:
                _final_url, html = self.session.browser_form_post(submit_url, body, timeout=100)
            except Exception as e:
                self._log(f"do_apply: SubmeterVistoCriaPDF POST failed: {e}")
                if self._breaker is not None:
                    self._breaker.record_portal_down()
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
                if self._breaker is not None:
                    self._breaker.record_portal_down()
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
                    if self._emit is not None:
                        self._emit("booked", slot=slot)
                        self._emit("booked_pdf", pdf_path=self._save_pdf(pdf_body), slot=slot)
                    if self._breaker is not None:
                        self._breaker.record_success()
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
                # Confirmed live 2026-08-14: this returned silently. A holding applier went
                # `-> applying` then `-> holding` 43s later with terminal "apply bounced" and NOT ONE
                # log line in between, so there was no way to tell a rejected slot from a bounced
                # session from a WAF page. The submit response is the single most important thing
                # this worker does; its rejection must always say why.
                self._log(f"do_apply: submit NOT confirmed [{outcome}] -- "
                          f"{_submit_bounce_reason(html)} | slot={slot} | final_url={_final_url}")
                return outcome

            self._log(f"do_apply: BOOKED slot={slot} | final_url={_final_url}")
            # Announce the booking BEFORE going after the PDF. The appointment is real the moment
            # the portal confirms it; the PDF is an artifact that may or may not come back, and
            # making the Hub's record depend on it lost real bookings.
            if self._emit is not None:
                self._emit("booked", slot=slot)
            try:
                pdf = self.session.submit_form_binary(action_contains="MostrarPdf", timeout=45)
                self._log(f"MostrarPdf: status={pdf.get('status')} ct={pdf.get('content_type')} "
                          f"len={len(pdf.get('body', b''))}")
                pdf_body = pdf.get("body") or b""
                if pdf_body[:4] != b"%PDF" and "application/pdf" not in (pdf.get("content_type") or ""):
                    # storm-auto's josand5837 scar: a 58KB HTML fallback page was once saved as a
                    # "PDF". Don't persist a non-PDF -- but the booking itself is already confirmed
                    # by the auto-submit form, so this never changes the outcome.
                    self._log(f"do_apply: MostrarPdf returned a non-PDF ({len(pdf_body)}B) -- "
                              f"booking still confirmed by the auto-submit form; not saving")
                    pdf_body = b""
                if pdf_body and self._emit is not None:
                    pdf_path = self._save_pdf(pdf_body)
                    self._emit("booked_pdf", pdf_path=pdf_path, slot=slot)
            except Exception as e:
                # NEVER let PDF retrieval downgrade the outcome -- the appointment is already made.
                self._log(f"do_apply: PDF retrieval failed (booking IS confirmed, PDF missing): {e}")
            if self._breaker is not None:
                self._breaker.record_success()
            return "booked"
        finally:
            if self._apply_sem is not None:
                self._apply_sem.release(self.identity_id)

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

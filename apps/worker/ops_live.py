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

import json
import random
import re
import time
from datetime import date

from libs.bus.breaker import RedisBreaker
from libs.bus.semaphore import RedisSemaphore
from libs.engine import checkpoint as ckpt
from libs.engine.config import EngineConfig
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
_BOUNCE_KEYWORDS = ("questionario", "getslots", "captchadiv", "authentication.jsp")
# storm-auto's confirmed apply_book contract distinguishes two very different bounces, and treating
# them alike is what made a bounce look terminal here:
#   form_stale -- landed back on the questionnaire / home dashboard / auth page. The server-side
#                 workflow anchor lapsed. THE SLOT IS STILL REAL. Re-POSTing is futile; the fix is a
#                 RE-WARM (and, if that keeps failing, a fresh login) and then retry the SAME slot.
#   no_slot    -- landed back on the schedule form / slots page. Somebody else took it, or every
#                 period is leased. The session is fine; ask for a different slot.
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
# Flavour (b) is identified positively, by the home dashboard's own nav text AND the absence of any
# schedule marker -- never by a single keyword.
_HOME_MARKERS = ("solicitar pedido de visto", "consultar pedidos")
_SCHEDULE_MARKERS = ("getperiodosocupados", "cmbperiodo", "getslots")

# storm-auto bounds its equivalent restore<->re-warm chain with MAX_WARMUP_RESTORES=3 ("a session
# whose re-warm keeps bouncing to home can't churn fresh proxies forever"). Mirrored here as the
# keepalive-restore retry budget once a worker enters the "session is dead" state.
_MAX_KEEPALIVE_RESTORE_ATTEMPTS = 3


def _proxy_label(conn_url: str | None) -> str:
    """host:port of a proxy, credentials stripped -- safe to log.

    Every login attempt records which proxy it used. Without it a failure cannot be attributed to a
    proxy vs an account vs the portal, which is exactly the ambiguity behind "why is one role's
    login rate worse" -- the roles run identical code with identical accounts, so proxy draw is the
    remaining variable and it was unobservable."""
    if not conn_url:
        return "proxy=none"
    return "proxy=" + conn_url.rsplit("@", 1)[-1]


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
        return f"bounced back to {hits} (session/form state lost, or the slot was taken)"
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
        # phase-2 (hold/apply/discovery) proxy -- only used when HOLD_SWAP_AFTER_FOUND is on
        # (do_swap() below). None is the correct/expected value when swap is off; the Hub only
        # leases and passes this when the setting is actually enabled (apps/hub/main.py).
        self.hold_proxy = hold_proxy
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
        self._restore_stage: str | None = None   # "alive" | "rewarmed" | "none", set by do_login
        # Hub's shared gettime cache (special_days), stashed by await_discover_task() from the
        # discover command payload, consumed once by the next do_discover() call.
        self._pending_special_days: dict | None = None

        # P3 -- proxy rotation: which pool a rotation request should draw from. do_swap() flips this
        # to "hold" unconditionally the moment warmup finishes, regardless of whether
        # HOLD_SWAP_AFTER_FOUND is actually on (see do_swap()'s docstring and
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
                self.session = get_session(proxy=self.warm_proxy, headless=self.headless,
                                           fp_seed=self.account.get("username"))
                result = self.session.browser_login(
                    self.account["username"], self.account["password"],
                    capsolver_keys=self.capsolver_keys,
                    min_score=50,
                )
            except Exception as e:
                if transient < max_transient and self._is_transient_login_failure(str(e)):
                    transient += 1
                    self._log(f"login: transient {transient}/{max_transient} "
                              f"[{_proxy_label(self.warm_proxy)}] ({e}) -- infrastructure, not "
                              f"charged to sub-account (sticky still {attempt}/{sticky_max})")
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
                self.posto_pdf = None
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
                self._backoff(transient)
                continue
            attempt += 1
            self._log(f"login attempt {attempt}/{sticky_max} [{_proxy_label(self.warm_proxy)}"
                      f" acct={self.sub_account_id[:8]}]: rejected ({reason})")
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
        during holding always draws from the hold pool even when HOLD_SWAP_AFTER_FOUND is off (the
        operator's explicit requirement: "it will not be different when swap is off"). Returns the
        new proxy URL on success, or None (caller keeps its current proxy) if there's no bus wired,
        the pool is exhausted, or the reply times out -- the same safe-degrade pattern do_swap()
        itself already uses on failure."""
        if self._emit is None or self._signals is None or not hasattr(self._signals, "wait_proxy"):
            return None
        self._emit("proxy_request", phase=phase or self._current_phase)
        result = self._signals.wait_proxy(timeout=30)
        if not isinstance(result, dict) or not result.get("ok"):
            return None
        return result.get("proxy")

    def _backoff(self, attempt: int) -> None:
        base = float(self.cfg.raw.get("RESPAWN_BACKOFF_BASE", 2.0))
        cap = float(self.cfg.raw.get("RESPAWN_BACKOFF_CAP", 120.0))
        time.sleep(min(cap, base * (2 ** (attempt - 1))) * random.uniform(0.8, 1.2))

    def do_warmup(self) -> bool:
        if self._restore_stage in ("alive", "rewarmed"):
            return True   # checkpoint restore already got us to a bookable state
        return self._run_warmup_steps()

    def _run_warmup_steps(self) -> bool:
        """Questionnaire -> Formulario -> ScheduleController -> Schedule.jsp (confirmed URLs/flow,
        ported from storm-auto batch_apply._run_steps_2_to_6)."""
        try:
            self.session.browser_eval(
                "(async()=>{const r=await fetch('%s',{method:'POST',credentials:'include',"
                "headers:{'Content-Type':'application/x-www-form-urlencoded'},"
                "body:'lang=ENG&lang=PT'});return r.status;})()" % HOME_URL
            )
            self.session.browser_nav(QUEST_URL, timeout=35, skip_cookie_sync=True)

            quest_key = self.residence if self.residence != self.nationality else self.nationality
            steps = self.quest_steps.get(quest_key, [])
            last_fields: dict = {}
            for i, step in enumerate(steps):
                is_last = i == len(steps) - 1
                params = "&".join(f"{k}={v}" for k, v in step.items())
                url = f"{QUEST_NEXT}?{params}"
                js = (
                    "(async()=>{const r=await fetch(%r,{method:'GET',credentials:'include',"
                    "headers:{'Accept':'text/plain, */*; q=0.01','X-Requested-With':'XMLHttpRequest',"
                    "'Sec-Fetch-Mode':'cors','Sec-Fetch-Dest':'empty','Sec-Fetch-Site':'same-origin',"
                    "'Referer':%r}});const body=await r.text();"
                    + (
                        "const _t=document.createElement('div');_t.innerHTML=body;const f={};"
                        "_t.querySelectorAll('input').forEach(i=>{if(i.name)f[i.name]=i.value;});"
                        "return {status:r.status,fields:f};"
                        if is_last else "return {status:r.status};"
                    )
                    + "})()"
                ) % (url, QUEST_URL)
                res = self.session.browser_eval(js)
                if not isinstance(res, dict) or res.get("status") != 200:
                    self._log(f"quest step {i+1}/{len(steps)} non-200: {res}")
                if is_last and isinstance(res, dict):
                    last_fields = res.get("fields") or {}

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

            sc_payload = schedule_controller_payload(
                self.account, self.form_defaults, posto_id=self.posto_id, csrf=csrf,
                nationality=self.nationality, residence=self.residence,
            )
            self.session.browser_fetch(
                SCHED_CTRL, sc_payload, params={"posto_id": self.posto_id},
                headers={"Referer": FORMULARIO}, encode="multipart",
                file_fields=["foto", "file1", "file2", "file3", "file4"], timeout=45,
            )
            self.session.browser_nav(SCHED_JSP + "?posto_id=" + self.posto_id, timeout=30)
            self.posto_pdf = self.posto_id
            return True
        except Exception as e:
            self._log(f"do_warmup failed: {e}")
            return False

    def do_swap(self) -> str | None:
        """Phase-1 (login/warmup) proxy -> phase-2 (hold/apply/discovery) proxy migration.

        Off by default (HOLD_SWAP_AFTER_FOUND, catalog default False) -- most bookings just hold on
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
        # Flipped unconditionally, before either early-return below -- a rotation request during
        # holding must draw from the hold pool even when swap itself is off/unavailable (P3, the
        # operator's explicit requirement).
        self._current_phase = "hold"
        if not self.cfg.hold_swap.swap_after_found:
            return None
        if not self.hold_proxy:
            self._log("do_swap: HOLD_SWAP_AFTER_FOUND is on but no hold_proxy was provided -- skipping")
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
        swap_id = self.hold_proxy.rsplit("@", 1)[-1]   # host:port, credentials stripped for logging
        self._log(f"do_swap: swapped to hold proxy {swap_id}")
        return swap_id

    def await_slot_open(self) -> str:
        if self._signals is None:
            return "slot_open"
        while True:
            result = self._signals.wait_slot_open(timeout=self._keepalive_window())
            if result in ("slot_open", "stop"):
                return result
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

    def _slot_found_seen(self) -> bool:
        return self._signals is not None and getattr(self._signals, "slot_found_seen", False)

    def slot_found_seen(self) -> bool:
        """EngineOps surface for the same flag the keepalive loop reads privately. PhaseMachine uses
        it so an apply worker that respawns mid-run skips a slot_open handshake that already
        happened -- see _run_apply."""
        return self._slot_found_seen()

    def _do_keepalive(self, context: str) -> None:
        if self.cfg.keepalive.off:
            return
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
        probe = self.cfg.keepalive.probe or "home"
        url = HOME_URL if probe == "home" else probe
        try:
            self.session.browser_nav(url, timeout=30)
            self._keepalive_dead_streak = 0
            if self._emit is not None:
                self._emit("log_line", text=f"keepalive: probe OK ({context})")
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

    def _attempt_keepalive_restore(self, context: str = "") -> None:
        """The session looks dead -- try the same checkpoint-restore-then-fresh-login chain a fresh
        launch uses, rather than silently sitting on a dead session until the next real signal
        arrives and do_apply/do_discover fails outright."""
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
        if ok and self.posto_pdf is None:
            ok = self._run_warmup_steps()   # restore only re-authenticates; form-state needs re-warming
        if ok:
            self._keepalive_restore_attempts = 0
        if self._emit is not None:
            self._emit("log_line", text=f"keepalive: restore {'succeeded' if ok else 'FAILED'}")

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

            if classify_gettime_body(gettime_body) != GettimeStatus.OK:
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
            try:
                self.session.browser_nav(url, timeout=30)
                html = self.session.browser_eval("document.documentElement.outerHTML") or ""
            except Exception as e:
                self._log(f"do_discover: {d} nav error ({e}) -- FAILED, not empty")
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
        if self._apply_sem is not None:
            if not self._apply_sem.acquire(self.identity_id, timeout=60):
                return "bounced"
        try:
            if self._breaker is not None and self._breaker.is_tripped():
                return "bounced"

            if not self._run_warmup_steps():
                self._log("do_apply: re-warm at round start failed")
                return "bounced"

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
            if _blank or _blocked:
                why = "blank/chrome-error page" if _blank else "DataDome interstitial"
                self._log(f"do_apply: submit response is a {why} -- not a portal answer | slot={slot}")
                if self._breaker is not None:
                    self._breaker.record_portal_down()
                return "bounced"

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
            home_dashboard = (all(m in low for m in _HOME_MARKERS)
                              and not any(m in low for m in _SCHEDULE_MARKERS))
            return "form_stale" if home_dashboard else "no_slot"
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

    def close(self) -> None:
        if self.session is not None:
            try:
                self.session.close_browser()
            except Exception:
                pass
            self.session = None

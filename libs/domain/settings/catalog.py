"""The settings catalog — every setting mapped to exactly ONE home layer (spec §6).

This is the single source of truth the operator's comment demanded: "list down all possible
settings and their belongings ... we will not accept overlap of settings between layer - but
in required situation overrides can be done in the proposed priority order."

Each `Setting` declares:
  - `home`   : the layer that owns it (where it is normally set).
  - `override_layers` : the *higher-precedence* layers additionally allowed to override it
                        (empty for most; used only for the few (override-ok) knobs in §6).
  - `type` / `default` : for validation + the effective-config fallback.

Precedence (resolver): WORKER > TEMPLATE > BOOKING > GLOBAL.
Anything set at a layer that is neither its home nor an allowed override layer is REJECTED
by the resolver (no silent overlap).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..enums import SettingsLayer as L

# Shorthands for the documented override policies (§6):
_TPL = frozenset({L.TEMPLATE, L.WORKER})   # global default, override-ok at template (and worker)
_WRK = frozenset({L.WORKER})               # template knob, rare per-worker instance override
_TPL_BK = frozenset({L.BOOKING, L.WORKER}) # template knob exposed as a per-booking operator shortcut
_BK_TPL = frozenset({L.BOOKING, L.TEMPLATE, L.WORKER})  # settable at every operator-facing layer


@dataclass(frozen=True)
class Setting:
    key: str
    home: L
    type: type = str
    default: Any = None
    override_layers: frozenset = field(default_factory=frozenset)
    group: str = ""
    note: str = ""
    min_value: float | None = None
    # Enumerated values, when a setting has a closed set. Empty tuple = free-form (the default, and
    # what every setting was before 2026-09-01). Added because DISCOVERY_SOURCE rendered as a free
    # text box in the dashboard, so `slot` instead of `slots` was a realistic operator input that
    # nothing rejected -- the framework had no way to express "one of these three".
    choices: tuple = ()

    def settable_layers(self) -> frozenset:
        return frozenset({self.home}) | self.override_layers


def _s(key, home, type=str, default=None, override=frozenset(), group="", note="",
       min_value=None, choices=()):
    return Setting(key, home, type, default, override, group, note, min_value, tuple(choices))


# ── RETIRED SETTINGS ─────────────────────────────────────────────────────────────────────────
# Keys that USED to be in the catalog and deliberately are not any more. `validate_layer` ignores a
# stored value for one of these instead of raising UnknownSetting.
#
# THIS EXISTS BECAUSE DELETING A CATALOG ENTRY IS A PRODUCTION OUTAGE, and it happened on
# 2026-09-16. The six rotating-credential keys below were removed from the catalog when credentials
# moved to the environment (D40) -- but a backfill had already written them into the live `settings`
# table. `resolve()` then raised UnknownSetting on EVERY call, which is on the scheduler's per-tick
# path: the scheduler crash-looped for ~2h45m, booking "Test 0916" never left SCHEDULED, and no
# worker could launch. One stale row took down every booking on the host.
#
# The catalog already warned about exactly this, in TELEGRAM_BOT_TOKEN's note: "Kept because
# deleting the catalog entry would make resolve() reject the stored value and break every worker
# launch." Retiring a key is now an explicit, safe operation rather than a deletion that happens to
# be a landmine for whichever host has the value stored.
#
# A retired key is NOT settable: writing one still fails, because it has no catalog entry and
# nothing reads it. Only reading a leftover row is tolerated, which is the only case that matters.
RETIRED_SETTINGS = frozenset({
    # Rotating-proxy credentials -> moved to the environment, 2026-09-16 (DECISIONS.md D40).
    "SOAX_ROTATING_PACKAGE", "SOAX_ROTATING_PASSWORD",
    "SOAX_MOBILE_PACKAGE", "SOAX_MOBILE_PASSWORD",
    "WEBSHARE_PREFIX", "WEBSHARE_PASSWORD",
    # Scout discovery redesigned to the round-robin full-range ROTATION model, 2026-09-21 -- the
    # single-probe/split-partition scheme these configured no longer exists. SINGLE_MODE_INTERVAL's
    # pacing role is now DISCOVERY_MIN_INTERVAL; the rest have no analogue (a full-range round has no
    # per-scout slice to cap, partition, or retire dates from). Kept here (not deleted silently) so a
    # booking still carrying a stored value doesn't crash resolve() on the per-tick path.
    "DISCOVERY_MODE", "DISCOVERY_CONCURRENCY", "DISCOVERY_MAX_PARALLEL",
    "SINGLE_MODE_INTERVAL", "DATE_RETIRE_ENABLED", "DATE_RETIRE_AFTER_MISSES",
    # The booking-wide circuit breaker was REMOVED 2026-09-23. It gated the whole fleet (do_login/
    # do_apply refused while tripped) and did more harm than good: a normal post-restart throttle
    # burst tripped it, froze every worker ("waiting out the booking breaker"), and oscillated
    # (trip -> all wait -> all retry together -> re-trip), starving discovery and apply. Portal-down
    # is now SHOWN to the operator via PortalLiveness (portal_liveness_key) instead of freezing the
    # fleet, and a worker that hits the maintenance page backs off on its own (MAINTENANCE_BACKOFF).
    "BREAKER_TRIP_THRESHOLD", "BREAKER_COOLDOWN",
    "BREAKER_BUSY_DISTINCT_ACCOUNTS", "BREAKER_BUSY_WINDOW",
    "BREAKER_MAINT_DISTINCT_ACCOUNTS", "BREAKER_MAINT_WINDOW",
    # Replaced by LOGIN_RECAPTCHA_ROLLS 2026-09-24. The old knob only excused a reCAPTCHA verdict
    # when the exit DEMONSTRABLY moved, so a stable-but-flagged exit (same IP, same rejection for
    # every sub-account) fell through and burned the accounts. The replacement rolls to a fresh exit
    # on the verdict whether or not a move is detected, uncharged, since it is always an exit problem.
    "LOGIN_IP_MISMATCH_RETRIES",
})


# ─────────────────────────────────────────────────────────────────────────────
# 6.1 GLOBAL — system, infra, credentials, provider config
# ─────────────────────────────────────────────────────────────────────────────
_GLOBAL = [
    # Infra
    _s("DATABASE_URL", L.GLOBAL, str, group="infra",
       note=
       "Postgres connection string. Infrastructure -- set in the environment, not per run."),
    _s("REDIS_URL", L.GLOBAL, str, group="infra",
       note=
       "Redis connection string. Infrastructure -- set in the environment, not per run."),
    _s("HOST_SESSION_CAP", L.GLOBAL, int, 175, group="infra",
       note="max concurrent worker sessions on this host"),
    # Credentials
    _s("CAPSOLVER_KEYS", L.GLOBAL, str, group="credentials",
       note=
       "Comma-separated CapSolver API keys. Credential -- never commit it to source."),
    # ── rotating-pool credentials: DELIBERATELY ABSENT ───────────────────────────────────────
    # ROTATING CREDENTIALS ARE NOT SETTINGS (operator direction 2026-09-16). SOAX_ROTATING_PACKAGE
    # /_PASSWORD, SOAX_MOBILE_*, WEBSHARE_PREFIX/_PASSWORD were briefly catalogued here and have
    # been removed: they are deployment facts and secrets, so they live in the environment (.env)
    # and are declared per pool in libs/proxies/providers/. Two concrete reasons, not taste:
    #   * a setting is editable from the web dashboard and readable by anything with a DB
    #     connection -- neither is appropriate for a proxy password;
    #   * while they were settings, credential resolution had to consult ambient os.environ, and
    #     because apps/hub/config.py loads .env at import, the TEST SUITE resolved live production
    #     credentials -- one appeared verbatim in a pytest failure message.
    # The TTLs below stay: they are non-secret operational knobs an operator legitimately tunes.
    _s("SOAX_SESSIONLENGTH", L.GLOBAL, int, 3600, group="credentials", min_value=1.0,
       note="Seconds a SOAX sticky session lives after binding. Also the hard ceiling on how long "
            "any session id can be 'established'."),
    _s("SOAX_BINDTTL", L.GLOBAL, int, 600, group="credentials", min_value=1.0,
       note="Seconds SOAX holds the bound exit IP for a session."),
    _s("SOAX_IDLETTL", L.GLOBAL, int, 900, group="credentials", min_value=1.0,
       note="Seconds of inactivity before SOAX drops the session's exit. KEEPALIVE_INTERVAL must "
            "stay under this or a holding worker's exit dies between probes."),
    _s("TELEGRAM_BOT_TOKEN", L.GLOBAL, str, group="credentials",
       note=
       "Telegram bot token. NOT IMPLEMENTED -- stored and validated, but nothing reads it yet. Kept "
       "because deleting the catalog entry would make resolve() reject the stored value and break "
       "every worker launch."),
    _s("TELEGRAM_CHAT_ID", L.GLOBAL, str, group="credentials",
       note=
       "Telegram chat id for notifications. NOT IMPLEMENTED -- see TELEGRAM_BOT_TOKEN."),
    # Registration/mail-verification credentials (the registration UI itself is deferred, but these
    # are legitimate stored GLOBAL config recovered from the dump -- without a catalog entry,
    # resolve() rejects them and breaks EVERY worker launch, not just registration, since it
    # validates the whole GLOBAL layer regardless of what a given launch actually needs).
    # "mailtm" | "cloudflare". Default matches apps/hub/main.py's own fallback
    # (`g.get("EMAIL_PROVIDER", "") or "mailtm"`) -- a catalog default that disagreed with the code's
    # fallback would mean the panel showed one provider while an unset install used the other.
    # Both are fully implemented (libs/engine/vendor/email_pool.py: MailTM and CloudflareMail); the
    # registrar picks between them in _mk_mail, so this really is just a switch.
    _s("EMAIL_PROVIDER", L.GLOBAL, str, "mailtm", group="credentials",
       note="mailbox source for registration: 'mailtm' (throwaway, no setup) or 'cloudflare' (CF_MAIL_DOMAIN)"),
    # The domain every generated sub-account address is created under. Worth calling out because it
    # is shared by EVERY registration: on 2026-08-21 the portal began refusing with "Dispoe de mais
    # 0 tentativas" (0 attempts remaining) after 528 registrations, and the mail domain is the only
    # attribute all of them have in common -- proxies rotate per registration, so a per-IP quota is
    # ruled out. Changing this is therefore the cheapest test of whether that limit is per-domain.
    _s("CF_MAIL_DOMAIN", L.GLOBAL, str, group="credentials",
       note="mail domain for generated sub-account addresses -- change to test a per-domain quota"),
    _s("CF_WORKER_URL", L.GLOBAL, str, group="credentials",
       note="Cloudflare Worker endpoint that receives and exposes verification mail"),
    _s("CF_WORKER_SECRET", L.GLOBAL, str, group="credentials",
       note="shared secret for CF_WORKER_URL"),
    # Browser / stealth defaults (override-ok at template)
    _s("HEADLESS_MODE", L.GLOBAL, str, "new", _TPL, "browser",
       choices=("new", "old", "headful"),
       note=
       "Chromium browser mode, settable per TEMPLATE. 'new' (default) = Chrome --headless=new, the "
       "modern render path -- far less detectable than 'old' (a bot tell), needs NO display. "
       "'headful' = a real visible window; a reference bot built from a successful-booking HAR "
       "insists the WAF only passes a headful browser -- but it needs a display (xvfb): where none "
       "is present the worker auto-starts a virtual one, and if that is unavailable it falls back to "
       "'new' with a warning rather than failing to launch. Headful is heavy, so use it for a SMALL "
       "critical fleet, not 100+ workers."),
    _s("DRIVER", L.GLOBAL, str, "playwright", _TPL, "browser",
       note=
       "Browser automation driver. 'playwright' is the only path exercised in production here."),
    _s("DETERMINISTIC_FP", L.GLOBAL, bool, True, _TPL, "browser",
       note=
       "Derive a STABLE browser fingerprint per sub-account, so the same account presents the same "
       "client across fresh logins, checkpoint restores and hold swaps. Off makes every session look "
       "like a new device to the portal."),
    _s("WEBRTC_BLOCK", L.GLOBAL, bool, True, _TPL, "browser",
       note=
       "Block WebRTC so the real IP cannot leak past the proxy. Leave on -- a leak defeats the proxy "
       "entirely."),
    _s("STEALTH_HARDEN", L.GLOBAL, bool, True, _TPL, "browser",
       note=
       "Apply the anti-automation hardening script. Off is diagnostic only."),
    _s("HUMAN_BEHAVIOR", L.GLOBAL, bool, True, _TPL, "browser",
       note=
       "Pace interactions like a human rather than firing them back-to-back. Slower per worker; the "
       "questionnaire burst is exactly the pattern that draws challenges."),
    # Solver defaults (override-ok at template for A/B)
    _s("LOGIN_CAPSOLVER_ONLY", L.GLOBAL, bool, False, _TPL, "solver",
       note=
       "Restrict the solver race to CapSolver. Useful for isolating a provider; note race_all races "
       "PROVIDERS in parallel, so restricting it removes redundancy rather than changing method."),
    _s("LOGIN_ANTICAPTCHA_ONLY", L.GLOBAL, bool, False, _TPL, "solver",
       note=
       "Restrict the solver race to AntiCaptcha. Same trade-off as LOGIN_CAPSOLVER_ONLY."),
    # Settable at BOOKING so the proxied/proxyless choice is a per-run operator switch, not something
    # you have to re-arm a whole fleet's templates to change. NOTE the precedence order
    # (WORKER > TEMPLATE > BOOKING > GLOBAL): a template that PINS this key still outranks the
    # booking switch, which is exactly how the "Apply/Scout Proxy-aware" templates silently made the
    # booking-level value inert. scripts/unpin_template_setting.py strips it from those templates so
    # the booking switch is the single control point; set it at TEMPLATE only for a deliberate A/B.
    #
    # Measured 2026-08-20, 5 solves/mode against LOGIN_EVISA: proxied via soax/rotating scored 10 on
    # 2/5 attempts (3 failed outright); proxyless scored 10 on 5/5. Same score, far fewer tokens --
    # so False is the better default and this switch exists to re-test that, not to escape it.
    _s("RECAPTCHA_SOLVE_PROXY", L.GLOBAL, bool, False, _BK_TPL, "solver",
       note="mint the login reCAPTCHA token through the worker's own proxy instead of the solver's IPs"),
    _s("NATIVE_SOLVE", L.GLOBAL, bool, False, _TPL, "solver",
       note=
       "Attempt the CAPTCHA in-browser instead of via a solver API. Not the proven path here."),
    # Defaults corrected 2026-08-14 to match the PROVEN values. Both are read by the vendored solver
    # from os.environ (libs/engine/vendor/solver.py), which until now nothing ever set -- so these
    # catalog numbers were inert and the engine silently used its own 25/180. The moment the worker
    # started actually exporting them (apps/worker/__main__.py's _export_vendor_env), the old 4.0/
    # 120.0 would have taken effect and CUT the solver's grace period from 25s to 4s. storm-auto,
    # which has booked successfully for months, ships no .env at all and therefore runs on exactly
    # these 25/180 code defaults -- treat those as ground truth, not the reconstructed guesses.
    _s("SOLVER_RACE_GRACE", L.GLOBAL, float, 25.0, _TPL, "solver",
       note="seconds to keep racing for a better CAPTCHA score after the first acceptable solve"),
    _s("SOLVER_RACE_HARD_TIMEOUT", L.GLOBAL, float, 180.0, _TPL, "solver",
       note="absolute ceiling on one solve race"),
]

# ─────────────────────────────────────────────────────────────────────────────
# 6.2 BOOKING — the run/event
# (Core targets type/posto/nationality/residence live as BOOKING COLUMNS, not settings.)
# ─────────────────────────────────────────────────────────────────────────────
_BOOKING = [
    # Test-only / run mode
    _s("MONO_BOOKING", L.BOOKING, bool, False, group="mode",
       note="apply workers discover their OWN slots and book them, instead of holding idle until a "
            "scout assigns one -- storm-auto's confirmed architecture, where no worker is ever idle. "
            "Also the practical fix for anchor lapse during hold: a worker running its own "
            "getPeriodosOcupados rounds keeps the server-side booking anchor alive, which a bare "
            "keepalive nav does not (147 lapses vs 42 restores, measured 2026-08-21). Scouts are "
            "unnecessary in this mode but not forbidden."),
    _s("no_book", L.BOOKING, bool, False, group="mode", note="test dry-run: apply workers never submit"),
    # NOT IMPLEMENTED -- audited 2026-08-21, zero read sites anywhere in the codebase (the only
    # references are this line and a resolver test asserting the default). Kept rather than deleted
    # because both name real intent, but the note must say so: an operator ticking a box that does
    # nothing is worse than no box at all, and this is the recurring trap in this catalog.
    #
    # auto_ops was meant to gate auto-driving the command flow. The Scheduler now does that
    # unconditionally for every live booking -- scheduled start, slot-open latching, slot
    # assignment, hold-respawn -- so the behaviour it guarded became the default and the flag is
    # vestigial. Wiring it now would mean adding an off switch for the main loop, which is probably
    # not what anyone wants.
    _s("auto_ops", L.BOOKING, bool, False, group="mode",
       note="NOT IMPLEMENTED (nothing reads it) -- the scheduler already auto-drives every booking"),
    # persist_events was meant to write raw worker logs/events to Postgres. They currently live ONLY
    # in the capped Redis evt stream (maxlen 5000, shared by every booking) and the WS broadcast, so
    # history ages out in hours -- which repeatedly cost us evidence mid-investigation on 2026-08-21.
    # M.IdentityEvent persists identity MILESTONES (booked, etc.); there is no raw-event table.
    _s("persist_events", L.BOOKING, bool, False, group="mode",
       note="NOT IMPLEMENTED (nothing reads it) -- raw events live only in the capped Redis stream"),
    # Default flipped to "live" (operator, 2026-08-21). The original "sim" default came from the
    # reconstruction, when live mode was unproven and an accidental real booking was the risk worth
    # guarding against. Live is now the only mode anyone runs, and the guard had inverted into a
    # foot-gun: a booking created without touching this ran a full fleet that could never book.
    _s("ENGINE_MODE", L.BOOKING, str, "live", group="mode",
       note="'live' books real appointments; 'sim' drives the same flow against SimEngineOps"),
    # Phase 3 (D32): all six proxy settings below take _WRK (worker override) -- a worker can
    # override its booking's proxy configuration. Previously override=frozenset() on every one of
    # them, meaning NO worker-level override existed for any proxy setting despite the resolver
    # already implementing WORKER > TEMPLATE > BOOKING > GLOBAL precedence correctly; this was a
    # missing opt-in, not a missing mechanism. See workers.settings_json (Worker model) for where
    # a worker's override value now actually comes from.
    #
    # Widened to _TPL (operator instruction, 2026-09-09): a WorkerTemplate can now ALSO carry
    # these six, so several templates (e.g. "soax warmup" / "webshare warmup") can be attached to
    # different identities within the SAME booking instead of needing one booking per proxy
    # config. Reduces blast radius: a single provider's outage (soax, 2026-09-07/08) no longer
    # has to stall a whole booking's apply capacity if some workers are on a different template.
    # Mechanism was already proven end-to-end by HOLD_SWAP_AFTER_LOGIN, the first L.TEMPLATE-home
    # setting -- this just opens the same door to booking-home settings via override, not home.
    _s("PROXY_PROVIDER", L.BOOKING, str, "soax", _TPL, group="mode",
       note="phase-1 (login/warmup) leased-proxy provider: 'soax' | 'webshare' (self-minted rotating), "
            "a static/ISP provider name (leased, e.g. 'capsolver' | 'thordata'), or 'all-isp' to LEASE "
            "ACROSS EVERY ISP POOL AT ONCE (capacity = union of all ISP pools; the provider filter is "
            "dropped and resource_type='static' bounds it to ISP). 'all-isp' also forces the lease "
            "path, so PROXY_KIND need not be set with it. Template-overridable (2026-09-09) so "
            "different worker templates in one booking can draw from different pools."),
    # Exit countries the phase-1 pool may draw from, comma-separated (e.g. "DE,IT"); blank = all.
    # Measured over 1392 real login attempts 2026-08-18: DE 23.7% / IT 23.4% / PT 10.8% / NL 10.6%
    # / ES 8.1% / LU 4.9% / CZ 2.9%. An 8x spread, and the pool was being spent almost backwards --
    # CZ took the MOST attempts (277) at the WORST rate. Narrowing to the top geos is the single
    # highest-leverage change available against the login wall, which is where ~90% of this fleet's
    # time goes. Left blank by default: it trades pool breadth for pass rate, and how far to push
    # that is an operator call, not a default.
    _s("PROXY_GEOS", L.BOOKING, str, "", _TPL, group="mode",
       note=
       "Comma-separated exit countries, injected into the proxy credential. Login success varies ~8x "
       "by geo (DE 23.7% / IT 23.4% / PT 10.8% / NL 10.6% / ES 8.1% / CZ 2.9%), so a narrow low-rate "
       "set can stop the fleet logging in at all. Empty = no geo constraint. Template-overridable."),
    _s("HOLD_PROXY_GEOS", L.BOOKING, str, "", _TPL, group="mode",
       note="same, for the phase-2 (hold/apply) pool; blank = all. Template-overridable."),
    # Named ISP pools (2026-09-16). MULTIPLE pools per booking is the point, not a convenience: an
    # ISP purchase is a fixed number of individually-owned IPs -- "10 from Thordata" -- and that is
    # routinely fewer than the worker count, so a booking draws from the UNION of the pools it
    # names. Blank = any pool, which is what every existing booking means.
    #
    # Only meaningful when the phase actually uses a STATIC pool; rotating pools are minted per
    # worker and have no rows to belong to a pool (D39/D40). Mirrors PROXY_GEOS/HOLD_PROXY_GEOS
    # deliberately -- same comma-separated shape, same blank-means-all rule, same per-phase pair --
    # so there is one filter idiom to learn rather than two.
    _s("PROXY_POOLS", L.BOOKING, str, "", _TPL, group="mode",
       note="Comma-separated NAMED ISP pools the phase-1 (login/warmup) static pool may draw from, "
            "e.g. 'thordata-oct,iproyal-a'. Blank = any pool. A booking may name several because one "
            "ISP purchase is usually smaller than the worker count. Ignored for rotating pools, "
            "which have no rows. Template-overridable."),
    _s("HOLD_PROXY_POOLS", L.BOOKING, str, "", _TPL, group="mode",
       note="same, for the phase-2 (hold/apply/discovery) static pool; blank = any. "
            "Template-overridable."),
    _s("PROXY_KIND", L.BOOKING, str, override=_TPL, group="mode",
       note="optional phase-1 proxy kind filter (Proxy.kind): 'rotating' | 'mobile'; blank = no filter. "
            "Template-overridable."),
    _s("HOLD_PROXY_PROVIDER", L.BOOKING, str, "webshare", _TPL, group="mode",
       note="phase-2 (hold/apply/discovery) leased-proxy provider, used only when HOLD_SWAP_AFTER_LOGIN "
            "is on -- default 'webshare' (long, ~24h session lifetime) vs phase-1's 'soax' default "
            "(~1h session lifetime): a long hold needs to outlive soax's own session cap, which a "
            "same-provider swap can't fix -- see libs.domain.settings.catalog's hold_swap group. "
            "Template-overridable."),
    _s("HOLD_PROXY_KIND", L.BOOKING, str, override=_TPL, group="mode",
       note="optional phase-2 proxy kind filter ('rotating' | 'mobile'); blank = no filter. "
            "Template-overridable."),
    # The only real bound on how hard we hit one provider at once. `Proxy.max_sessions` was never
    # one: all 42,600 rows carried max_sessions=2, a nominal 85,200-lease ceiling nothing could
    # reach -- and as of 2026-09-15 rotating pools have no rows to carry it. The gap was
    # acknowledged-but-unimplemented in apps/hub/main.py's registration path: "a provider like SOAX
    # does not establish unlimited concurrent sticky sessions per package."
    #
    # Host-wide (keyed by provider+kind, NOT by booking) because the limit being modelled is the
    # provider's, and the package is shared by every booking on the host.
    #
    # The default is a GUESS, not a measurement -- soax publishes no per-package concurrency figure
    # and we have never probed for one. 20 is chosen to be clearly below the ~40-160 simultaneous
    # establishments that preceded the 2026-09-11 ERR_CONNECTION_CLOSED storms while staying above
    # LOGIN_CONCURRENCY's default of 10, so a single booking is not additionally throttled by it.
    # Tune against an observed limit before trusting it.
    _s("PROVIDER_LOGIN_CONCURRENCY", L.BOOKING, int, 20, group="mode",
       note="max concurrent LOGIN attempts host-wide against one proxy pool (provider+kind), across "
            "ALL bookings. Bounds the rate at which fresh sticky sessions are established, which is "
            "what a provider actually rate-limits. 0 disables the gate. Default 20 is an untuned "
            "guess -- see the catalog comment."),
    # Timing (warmup_start_at / slot_open_at are booking timestamp columns)
    _s("WARMUP_START_JITTER", L.BOOKING, float, 0.0, group="timing",
       note="anti-herd (operator spec 2026-08-14: 'hub spawns worker processors -- concurrency "
            "window + jitter do matter'): each worker's actual spawn moment is delayed by a random "
            "[0, jitter) seconds BEFORE it does anything (including its proxy-lease health probe), "
            "so a fleet launch doesn't hit the portal/proxy pool in one instant. Applied in "
            "_launch_worker_fleet, both for a scheduled auto-start and a manual Start Instantly. "
            "Off (0) by default -- unlike KEEPALIVE_JITTER/RESTORE_STAMPEDE_JITTER (which jitter a "
            "background loop's own timing), this one delays _launch_worker_fleet ITSELF, which the "
            "/bookings/{id}/start HTTP handler calls synchronously and waits on -- a nonzero value "
            "makes that request take up to ~jitter_secs to return for a fleet start, so this is an "
            "explicit operator opt-in per booking, never a silent default. The concurrency window "
            "(_LAUNCH_FLEET_CONCURRENCY, currently a fixed constant, not a setting) separately "
            "bounds how many launches run at once; jitter only spreads out WHEN each one starts "
            "within that window."),
    # Date window
    _s("CAL_START_DATE", L.BOOKING, str, group="date_window",
       note=
       "First date discovery scans (YYYY-MM-DD). Empty = start from today. Together with "
       "CAL_END_DATE this is the single biggest lever on how much portal traffic a run generates."),
    _s("CAL_END_DATE", L.BOOKING, str, group="date_window",
       note=
       "Last date discovery scans (YYYY-MM-DD). Empty = use CAL_NUM_DAYS. A wide window means more "
       "gettime and getPeriodosOcupados calls per round, on a portal that throttles."),
    _s("CAL_NUM_DAYS", L.BOOKING, int, 0, group="date_window",
       note=
       "Days forward to scan when CAL_END_DATE is empty. 0 = the posto's own default horizon."),
    # Discovery / pool policy
    _s("DISCOVERY_SOURCE", L.BOOKING, str, "calendar", group="discovery",
       choices=("calendar", "slots", "both"),
       note="Which endpoint discovery reads. One of: calendar | slots | both. "
            "calendar = gettime + getPeriodosOcupados (default, the path this project has "
            "actually booked with). slots = the portal's own page-world flow, delivering a "
            "SCHEDULE_EVISA token through window.onCaptchaSuccess so its getSlots() populates "
            "window.data (data/new_booking.har shows /slots returning 21KB of real availability "
            "15s before a confirmed booking). both = slots first, calendar for any date slots "
            "could not resolve. The catalog has no enum type, so this renders as free text -- an "
            "unrecognised value is REFUSED LOUDLY by the worker rather than silently falling back."),
    _s("DISCOVERY_MIN_INTERVAL", L.BOOKING, float, 30.0, group="discovery", min_value=1.0,
       note="ROTATION model (2026-09-21): minimum seconds between successive REAL discovery rounds "
            "fleet-wide -- one scout does a full-range round, then the next healthy scout after this "
            "interval, round-robin, so with n healthy scouts a given scout is revisited about every "
            "MIN*n. A FAILED round (dead session / nothing reported) does NOT wait this interval -- "
            "the rotation moves to the next scout immediately. Paces real portal traffic, not the "
            "5s scheduler tick. (Replaced SINGLE_MODE_INTERVAL and the split-mode knobs "
            "DISCOVERY_MODE/DISCOVERY_CONCURRENCY/DISCOVERY_MAX_PARALLEL/DATE_RETIRE_* -- see "
            "RETIRED_SETTINGS.)"),
    _s("SLOT_OPEN_INACTIVATE_AFTER", L.BOOKING, float, 1200.0, group="discovery",
       note="false-slot-open guard (operator spec 2026-08-14): if slot-open has been fired for this "
            "many seconds and NOT ONE slot has been found in that whole window, acknowledge the "
            "slot-open was false -- clear slot_open_fired_at, send every worker an `inactivate` "
            "signal, and revert the booking to WARMING so scouts fall back to plain holding instead "
            "of grinding discovery (which risks portal rate-limiting/detection for nothing). "
            "Slot-open can then be re-fired, manually or on schedule. 0 disables the guard entirely; "
            "never fires once slot_found has fired (there ARE slots -- the open was real)."),
    # Seconds BEFORE the scheduled slot_open_at to tell every holding worker to re-warm, so the
    # fleet enters the window with fresh form state rather than scattered across their own
    # ~16-minute keepalive cycles. 0 disables. Only meaningful when slot_open_at is set --
    # a manually fired slot-open has no advance notice to act on.
    # Phantom-slot guard (operator-confirmed rule, 2026-09-18). The calendar discovery path derives
    # availability by subtraction (active range minus getPeriodosOcupados' closed prefix), which
    # cannot tell a real per-date answer from the portal's fallback template under load -- and the
    # template decodes every date to the SAME thin period set. Confirmed on Booking 0918: {3,4} on
    # 10-14 AND {3,4} on 10-15 while the portal's own calendar showed the 15th unavailable. Both
    # fired slot_found; three apply workers leased them, re-warmed into a 503, and the recovery
    # ladder re-logged them in -- discarding good sessions and spending reCAPTCHA strikes on slots
    # that did not exist. A phantom round is reported as FAILED (unknown): no rows, no slot_found,
    # no date retirement; the next round simply asks again.
    _s("PHANTOM_MAX_PERIODS", L.BOOKING, int, 2, _TPL, group="discovery", min_value=0,
       note="a discovery round whose found dates ALL decode to the same period set of at most this "
            "many periods is treated as phantom (portal template, not real availability) and "
            "reported as unknown rather than found. 0 disables. Uniformity across dates is the "
            "signal; a real open answers each date differently."),
    _s("PHANTOM_MIN_DATES", L.BOOKING, int, 2, _TPL, group="discovery", min_value=1,
       note="the phantom verdict needs at least this many found dates in the round -- uniformity "
            "across one date is not evidence of anything."),
    # Blind apply (operator direction 2026-09-18, libs/domain/blind.py). A blind slot is never
    # discovered, so nothing would ever retire it; each BOUNCED release counts against it and past
    # this many it is CLOSED. Capacity per (date, period) is >= 3, so a real combination can absorb
    # a couple of bounces from workers racing each other -- 3 keeps a real one alive through that
    # while retiring a phantom after three independent failures. 0 = never retire.
    # Genuine blind-apply MODE (operator direction 2026-09-21). Distinct from the mid-run
    # /blind-apply fallback: this is configured up front. The operator seeds blind slots BEFORE
    # scheduling, and on start the booking skips discovery entirely -- no scouts, no /slots polling,
    # no waiting for slot-open/slot-found on a timer. slot-open is fired the moment the booking
    # starts (so it goes RUNNING) and slot-found fires immediately off the seeded pool, so apply
    # workers go straight to warmup -> apply and cycle the blind grid, retiring what bounces and
    # booking what is real. A real booking already derives apply-only workers, which is exactly what
    # this needs.
    _s("BLIND_APPLY_MODE", L.BOOKING, bool, False, _TPL, group="discovery",
       note="run the booking purely off an operator-seeded blind slot pool: no scouts, no discovery, "
            "signals auto-fire on start. Seed the grid with POST /bookings/{id}/blind-apply before "
            "starting. Off by default."),
    _s("BLIND_MAX_BOUNCES", L.BOOKING, int, 3, _TPL, group="discovery", min_value=0,
       note="bounces off an operator-seeded blind slot before it is retired (CLOSED). Discovered "
            "slots are unaffected; they retire via the scouts' miss count. 0 = never."),
    _s("PREWARM_BEFORE_SLOT_OPEN", L.BOOKING, float, 120.0, group="discovery",
       note="re-warm holding workers this many seconds before the scheduled slot-open"),
    _s("SLOT_LEASE_TTL", L.BOOKING, float, 180.0, group="discovery",
       note="seconds a worker may hold a slot lease without converting it into an apply before the "
            "scheduler reclaims it and returns the slot to the pool. A lease is a promise to attempt "
            "a booking within seconds; a worker that dies, hangs, or bounces without emitting a "
            "terminal event would otherwise sterilise those units forever. A worker actually in "
            "APPLYING/BOOKED is never reclaimed, however long it takes. 0 disables reclaim."),
    # Slots-path CAPTCHA pre-solve (2026-09-20, A/B lever). Today _discover_via_slots waits out the
    # ~100s /slots rate-limit window and THEN solves a token (7-23s), so the solve latency sits on
    # the critical path of every poll. With this on, the token is solved DURING the tail of that
    # otherwise-idle wait and cached, so when the window opens the request fires immediately. Off by
    # default: this touches the confirmed booking path, and the win is only realised in steady-state
    # polling -- the first poll at slot-open has no wait to pre-solve in. Measure on a test booking
    # before trusting it on a real run.
    _s("SLOTS_PRESOLVE", L.BOOKING, bool, False, _TPL, group="discovery",
       note="pre-solve the /slots CAPTCHA during the rate-limit wait so the poll fires the instant "
            "the window opens, instead of paying the 7-23s solve after the wait. Off by default."),
    _s("SLOTS_TOKEN_TTL", L.BOOKING, float, 90.0, _TPL, group="discovery", min_value=1.0,
       note="max age (s) a pre-solved /slots token may be used at. reCAPTCHA tokens live ~120s; the "
            "90s default keeps a safety margin so a cached token is never used past expiry."),
    _s("SLOT_CAPACITY_PER_PERIOD", L.BOOKING, str, "auto", group="discovery",
       note="how many concurrent apply-worker leases one (date, period) slot can absorb before "
            "it's exhausted -- 'auto' falls back to a per-slot Slot.capacity_estimate if the "
            "discovering scout supplied one, else the DEFAULT_SLOT_CAPACITY constant (5); set a "
            "literal integer here to force the same cap fleet-wide regardless of per-slot estimates"),
    _s("PER_PERIOD_FANOUT", L.BOOKING, int, 4, group="discovery",
       note=
       "How many workers may be assigned the same period concurrently. Higher wins races on a "
       "contested slot; too high spends the fleet on one period."),
    _s("SLOT_MAX_PERIOD", L.BOOKING, int, 14, group="discovery",
       note=
       "Highest period id considered bookable. Postos differ -- 3018 advertises 4,5,7,8 while 5084 "
       "only ever advertised 3,4 -- so this filters what discovery will even look at."),
    _s("SLOT_PERIOD_FILTER", L.BOOKING, str, group="discovery",
       note=
       "Comma-separated period ids to restrict discovery to. Empty = no filter. Note that real "
       "bookings have landed on periods 3,4,5,7 and 8, so a narrow filter can exclude bookable "
       "inventory."),
    # Scout pool
    _s("SCOUT_POOL", L.BOOKING, bool, True, group="scout_pool",
       note=
       "Use the shared slot pool. Off means each worker keeps its finds private, so the Hub never "
       "learns a slot exists and no other worker can be assigned it."),
    _s("SCOUT_POOL_MULTI_CAPACITY", L.BOOKING, bool, True, group="scout_pool",
       note=
       "Allow more than one lease per slot, up to its capacity estimate. The /slots payload repeats "
       "a period id per bookable time, so one period really can hold several appointments."),
    _s("SCOUT_POOL_KEEP", L.BOOKING, bool, False, group="scout_pool",
       note=
       "Keep the pool across runs instead of retiring it at /start. Off is safer: prior-run slots "
       "are observations of a portal that has moved on, and applying against them wastes the fleet."),
    _s("SCOUT_CLOSE_AFTER_MISSES", L.BOOKING, int, 2, group="scout_pool",
       note=
       "Consecutive discovery rounds a known slot may go unseen before it is closed. Too low retires "
       "slots on one flaky scan; too high keeps dead slots drawing lease attempts."),
    _s("SCOUT_LEASE_TIMEOUT", L.BOOKING, float, 120.0, group="scout_pool",
       note=
       "Seconds before an unresolved slot lease is reclaimed. Bounds a worker that took a slot and "
       "died without releasing it -- otherwise the slot is stuck LEASED and nobody can book it."),
    # Guards
    _s("MAX_BOOKINGS", L.BOOKING, int, 0, group="guards",
       note=
       "Stop the booking after this many confirmed appointments. 0 = unlimited."),
    _s("BOOKING_TIMEOUT_HOURS", L.BOOKING, float, 48.0, group="guards",
       note="hard stop for the whole booking; replaces the removed per-worker hold timeout"),
    # Per-worker backoff after the portal's maintenance page (replaces the removed fleet breaker):
    # a worker that hits 'Serviço Indisponível' waits this many seconds on its OWN before retrying,
    # so it stops burning sub-accounts against a down portal without freezing the whole fleet.
    _s("MAINTENANCE_BACKOFF", L.BOOKING, float, 60.0, group="guards", min_value=0.0,
       note="seconds a worker backs off after seeing the portal maintenance page (per-worker, not "
            "fleet-wide -- the booking breaker was removed 2026-09-23; portal state is shown on the "
            "dashboard via PortalLiveness instead)"),
    # Consecutive apply bounces before re-warming IN PLACE and trying again (operator, 2026-08-21).
    # Two failures in a row is far more likely a stale server-side form anchor than two genuinely
    # taken slots -- and a re-warm is ~38s against a re-login's CAPTCHA solve and sub-account spend.
    # Sits INSIDE the APPLY_MAX_ATTEMPTS budget: re-warm at 2 and 4, park at 5, so the outer bound
    # that protects the Hub's false-slot detection from one degraded session is unchanged.
    _s("APPLY_REWARM_AFTER_BOUNCES", L.BOOKING, int, 2, group="guards", min_value=1,
       note="re-warm after this many consecutive apply bounces instead of just retrying"),
    _s("APPLY_MAX_ATTEMPTS", L.BOOKING, int, 5, group="guards",
       note=
       "Consecutive apply bounces before the worker PARKS (keeps its session, backs off, re-polls). "
       "A bounce must never end the run -- the authed session is the expensive thing. Note the park "
       "is only 3 poll gaps (~15-20s), which measurement showed is too short to brake a repeating "
       "failure."),
    # Consecutive apply bounces across the fleet after which the Hub stops believing its own slot
    # pool. Operator's call 2026-08-14: "even when there are some slots found - they may be false
    # positive. if applying keeps being failed then regard the slots as false, and revert to hold."
    _s("FALSE_SLOT_BOUNCE_LIMIT", L.BOOKING, int, 12, group="guards",
       note=
       "BOUNCED leases since slot-found before the pool is declared phantom and wiped. KNOWN BLIND "
       "SPOT (F29): only `bounced` counts, and a run of 641 leases on 2026-08-28 produced 246 "
       "form_stale, 393 session and ZERO bounced -- so the guard could not fire while the fleet "
       "ground phantom slots."),
    # Consecutive fully-failed discovery rounds after which a scout rebuilds its session in place.
    # Confirmed live 2026-08-14: scout 062e6dd6 logged in fine, then every gettime died with
    # "Execution context was destroyed" -- 631 failures over 53 minutes at 0.6s per round (a real
    # gettime takes ~15s), because a dead browser page is not a transient and nothing ever checked.
    # Two of three scouts discovered NOTHING all run while showing a healthy running/polling.
    _s("SCOUT_REBUILD_AFTER_FAILED_ROUNDS", L.BOOKING, int, 3, group="guards",
       note=
       "Consecutive rounds where EVERY date failed before a scout rebuilds its session. Failed is "
       "not empty: an empty calendar is a successful answer and must never trigger a rebuild."),
    _s("WORKER_STALL_TIMEOUT", L.BOOKING, float, 1500.0, group="guards", min_value=1.0,
       note="no events from a worker longer than this -> reaped as stalled"),
    _s("MAX_RESPAWN_ATTEMPTS", L.BOOKING, int, 5, group="guards",
       note="hold-phase respawn hard cap; beyond this the worker is left FAILED, not retried forever"),
    _s("WORKER_EVENT_RATE_LIMIT", L.BOOKING, float, 5.0, group="guards", min_value=0.5,
       note="events/sec sustained beyond this auto-kills the worker (runaway-loop circuit breaker)"),
]

# ─────────────────────────────────────────────────────────────────────────────
# 6.3 WORKER TEMPLATE — role behavior bundle (override-ok at worker instance)
# ─────────────────────────────────────────────────────────────────────────────
_TEMPLATE = [
    # Login / warmup resilience
    _s("NAV_TIMEOUT_SCALE", L.TEMPLATE, float, 1.0, _WRK, "login_resilience",
       note=
       "Multiplier on every browser navigation timeout. Raise when the portal is slow -- a SLOW "
       "portal is not a broken session, and a re-login cannot make it faster. Production runs at "
       "4.0."),
    _s("LOGIN_FIELD_RETRIES", L.TEMPLATE, int, 3, _WRK, "login_resilience",
       note=
       "Retries for filling a single login form field before the attempt is abandoned. Guards a "
       "partially rendered form, not a rejection."),
    _s("LOGIN_STORM_RETRIES", L.TEMPLATE, int, 8, _WRK, "login_resilience",
       note=
       "Attempts against the portal's login wall during a storm. The wall rejects roughly 90% of "
       "attempts, so this trades sub-account budget for a chance at a session."),
    _s("STICKY_RETRY_MAX", L.TEMPLATE, int, 2, _WRK, "login_resilience",
       note="login attempts spent on ONE sub-account before rotating to the next candidate. Only "
            "charged for failures that are actually a verdict on the credentials -- see "
            "LOGIN_TRANSIENT_RETRY_MAX"),
    _s("LOGIN_TRANSIENT_RETRY_MAX", L.TEMPLATE, int, 6, _WRK, "login_resilience",
       note="separate budget for INFRASTRUCTURE login failures (proxy connection errors, DataDome "
            "challenges, timeouts, navigations that never landed). These say nothing about the "
            "account, so charging them to STICKY_RETRY_MAX burns through every sub-account and "
            "fails the worker over proxy weather -- confirmed live 2026-08-14, six workers "
            "permanently FAILED that way. 0 makes every failure count against the sub-account, "
            "i.e. the old (broken) behaviour."),
    _s("QUEST_STEP_RETRIES", L.TEMPLATE, int, 10, _WRK, "login_resilience",
       note=
       "Retries per questionnaire step. The chain must complete or the booking cursor is never "
       "built, so this is worth more than most retry budgets."),
    _s("CAPTCHA_FAILURE_BUDGET", L.TEMPLATE, int, 1, _WRK, "login_resilience", min_value=1.0,
       note="reCAPTCHA rejections to spend on ONE sub-account before rotating to another. The "
            "portal states its own rule -- 'apenas permite falhar o processo de ReCaptcha 2 vezes' "
            "-- and blocks platform access beyond it, so the default of 1 leaves a margin. The "
            "login path used to spend its whole sticky budget walking an account to that edge; "
            "measured 2026-08-23, 34 accounts were taken there."),
    _s("LOGIN_PORTAL_BUSY_ROLLS", L.TEMPLATE, int, 3, _WRK, "login_resilience", min_value=0.0,
       note="how many DIFFERENT exits must receive the portal's 'try again later' refusal before "
            "the booking's circuit breaker is told the portal is declining to serve anyone. Each "
            "refusal below this rolls to a fresh exit and retries, and is not charged to the "
            "sub-account. Default 3. Raise it if your exit pool is large and flaky; set 0 to go "
            "straight to the fleet breaker on the first refusal, which is the pre-2026-09-02 "
            "behaviour. Rationale: that behaviour treated a per-exit refusal as portal-wide -- "
            "measured on the 2026-09-01 run, 11 such pages coexisted with 4 successful logins in "
            "one 10-minute window -- and because only a SUCCESSFUL login resets the breaker, a "
            "degraded exit pool locked the fleet out entirely: 145 refusals over 91 minutes with "
            "zero logins after the first 14."),
    # THROUGHPUT (2026-09-23). The 'try again later' throttle is a GLOBAL rate-limit lottery, not a
    # verdict on our rate -- a success and a throttle are byte-identical requests (same score-10
    # token, same POST), the portal just serves ~1%. So bookings scale with SHOTS TAKEN, and backing
    # off after a throttle only makes the fleet sleep instead of shoot (measured ~1.75 attempts/min/
    # worker). This is the per-throttle pause, kept tiny so a worker rolls to a fresh exit and fires
    # again immediately -- just enough jitter to avoid a tight loop around the ~15s browser+solve.
    _s("LOGIN_THROTTLE_BACKOFF", L.TEMPLATE, float, 0.5, _WRK, "login_resilience", min_value=0.0,
       note="seconds a worker pauses after a 'try again later' throttle before firing again on a "
            "fresh exit. Default 0.5. The throttle is a global lottery, not our-rate-limiting, so "
            "this is deliberately near-zero to MAXIMISE login shots -- raise it only if a near-zero "
            "retry rate is causing a different problem (it does not reduce the portal's throttle)."),
    _s("LOGIN_RECAPTCHA_ROLLS", L.TEMPLATE, int, 3, _WRK, "login_resilience", min_value=0.0,
       note="fresh exits to roll through on a 'Por favor, confirma o reCAPTCHA' verdict before the "
            "rejection is charged to the sub-account. This verdict is an EXIT/token problem, not the "
            "account's fault -- the exit moved across the solve window OR is flagged/bad for "
            "reCAPTCHA -- so the answer is a fresh exit, never the account's 2-strike budget "
            "(two strikes = permanent block). CONFIRMED 2026-09-24 (PEDRO RODRIGUES): one exit "
            "returned it for BOTH sub-accounts while another logged the SAME accounts in seconds. "
            "Replaces LOGIN_IP_MISMATCH_RETRIES, which only rolled when the exit DEMONSTRABLY moved "
            "and so burned accounts on a stable-but-flagged exit. Set 0 to charge every rejection."),
    _s("QUEST_STEP_GAP", L.TEMPLATE, float, 1.0, _WRK, "login_resilience",
       note="seconds (+-30%% jitter) paced before each warm-up questionnaire step and before the "
            "Questionario nav. Ported from storm-auto and then NEVER READ -- the classic unwired "
            "setting: it sat here at 0.0 while the 7-step warm-up fired as a sub-second burst right "
            "after login, which is exactly where quest-step 403s cluster (storm-auto's own note: "
            "'the quest-step-1 403s were observed clustering on exactly this post-login burst'). "
            "Wired to a real read site in ops_live._run_warmup_steps on 2026-08-22 and defaulted to "
            "1.0. Costs ~7s per warm-up; a warm-up that 403s costs the whole session. 0 restores "
            "the old no-gap behaviour."),
    _s("WARMUP_MAX_ATTEMPTS", L.TEMPLATE, int, 6, _WRK, "login_resilience",
       note=
       "Attempts at the warm-up chain before giving up on it. A warm-up failure means the server- "
       "side anchor is not where we need it -- it does NOT mean the auth session is dead."),
    _s("PARTIAL_RENDER_RETRIES", L.TEMPLATE, int, 3, _WRK, "login_resilience",
       note="refresh-retries when Schedule.jsp answers 200 but its form is not yet AJAX-populated. "
            "storm-auto's 'manual reload': under load the portal renders the page without the form, "
            "and a refresh fixes it far more cheaply than escalating to a full questionnaire "
            "re-warm. Ported from storm-auto, parsed into EngineConfig.partial_render_retries "
            "and then never CONSUMED by any decision -- the second such setting found on "
            "2026-08-22, after QUEST_STEP_GAP. Wired to a real read site in "
            "ops_live._schedule_state_ok."),
    # Wired 2026-08-14 (was inert since it was written -- nothing read it). This is storm-auto's
    # MAX_FORM_STALE_REWARMS: a bounce to the questionnaire/home page means the server-side workflow
    # anchor lapsed, NOT that the slot is gone, so the fix is a re-warm rather than a re-POST.
    # Default corrected 3 -> 1 to match the proven value: abc5 (storm-auto, 2026-07-23) showed
    # re-warm is UNRELIABLE on a long-held session -- replaying steps 2-6 yields Formulario
    # "Session lost" en masse (852x in one run) and only a FRESH LOGIN rebuilds the anchor. So the
    # ladder is: re-warm once, then re-login IN-PROCESS. Neither step ever ends the process.
    _s("REWARM_MAX_ATTEMPTS", L.TEMPLATE, int, 1, _WRK, "login_resilience",
       note=
       "In-place re-warms before escalating to a fresh login. Default 1 is the PROVEN value: storm- "
       "auto abc5 showed re-warm is unreliable on a long-held session (852 'Session lost' in one "
       "run) and only a fresh login rebuilds the anchor. Raising this mostly buys failed re-warms."),
    # Keepalive
    # 600, not 900. MEASURED 2026-08-20 across 28 keepalive cycles on booking "Test 3018": the
    # portal's scheduling form state dies on a 16.1-minute median clock (min 15.8, max 33.6). A
    # 900s interval left a 60-second margin against a 15.8-minute worst case -- and the re-warm
    # itself takes longer exactly when the portal is slow, so the margin closes from both ends.
    #
    # That is not theoretical: worker EDER's keepalive re-warm failed 38 seconds AFTER slot-open
    # fired, and it spent the next eight minutes recovering while a worker whose form state happened
    # to be fresh booked in 47 seconds. 600s leaves ~6 minutes of headroom and costs one extra
    # lightweight probe per worker per hour.
    _s("KEEPALIVE_INTERVAL", L.TEMPLATE, float, 600.0, _WRK, "keepalive", min_value=1.0,
       note=
       "Seconds between keepalive probes on a holding worker. CONTESTED: the catalog rationale "
       "measured form state dying on a 16.1-min median (2026-08-20, 28 cycles) while CONFIRMED_FACTS "
       "§30 measured 4-6 min (2026-08-27). 600 is defensible under the first and too slow under the "
       "second. LEAVE AT 600 until that is reconciled -- both bookings of 2026-08-27 booked at "
       "exactly this value."),
    # Discovery back-off on a BLOCKED gettime (the portal's "try again later" / WAF page, not an
    # empty calendar). MEASURED live 2026-08-27 on "Test 2032": one worker went `-> discovering`
    # twenty times in ~10 minutes, every one answered portal_try_again_later (28,499B), five workers
    # wide, for hours -- asking again 30s after the portal said "try again later". storm-auto
    # carries this fix and the scar: "~2.5h of empty polling drew 77 DataDome pages -> fixed by
    # bounded backoff." Doubles per CONSECUTIVE block, resets the moment gettime answers properly.
    # Set 0 to disable.
    _s("DISCOVERY_BLOCKED_BACKOFF", L.TEMPLATE, float, 30.0, _WRK, "discovery", min_value=0.0,
       note=
       "Seconds to wait after gettime returns the portal's throttle page, doubling per CONSECUTIVE "
       "block and resetting on a healthy answer. 0 disables. Measured 2026-08-27: without it one "
       "worker re-asked 20 times in 10 minutes while the portal was explicitly saying 'try again "
       "later'."),
    _s("DISCOVERY_BLOCKED_BACKOFF_CAP", L.TEMPLATE, float, 300.0, _WRK, "discovery", min_value=0.0,
       note=
       "Ceiling (seconds) for that doubling. Bounds the wait so a recovered portal is noticed "
       "promptly."),
    _s("KEEPALIVE_DECAY_MIN", L.TEMPLATE, float, 30.0, _WRK, "keepalive",
       note=
       "Lower bound (seconds) of the randomised decay applied between keepalive probes, so a fleet "
       "does not poll the portal in lockstep."),
    _s("KEEPALIVE_DECAY_MAX", L.TEMPLATE, float, 60.0, _WRK, "keepalive",
       note=
       "Upper bound (seconds) of that randomised decay. Keep above KEEPALIVE_DECAY_MIN."),
    _s("KEEPALIVE_NO_RESTORE", L.TEMPLATE, bool, False, _WRK, "keepalive",
       note=
       "Never rebuild the session from a failed keepalive -- just keep probing. Honours the LOOSE "
       "HOLD rule (a re-login costs a CAPTCHA solve and sub-account budget) at the cost of a worker "
       "that can sit dead until something else notices."),
    _s("KEEPALIVE_BROWSER_CONFIRM", L.TEMPLATE, bool, True, _WRK, "keepalive",
       note=
       "Confirm a keepalive result through the browser rather than trusting a cheaper client. Slower "
       "per probe; the browser jar is the authoritative one, so a cheaper client can disagree with "
       "reality."),
    _s("KEEPALIVE_BROWSER_FROM", L.TEMPLATE, str, "", _WRK, "keepalive",
       note=
       "Optional URL the browser keepalive navigates from. Empty = the default probe target."),
    _s("KEEPALIVE_DEAD_TRIP", L.TEMPLATE, int, 3, _WRK, "keepalive",
       note=
       "Consecutive failed keepalive probes before the worker treats the session as dead and "
       "rebuilds. Low values burn logins on transient blips; high values leave a dead worker "
       "displaying 'keeping'."),
    _s("FORM_STATE_TTL", L.TEMPLATE, float, 240.0, _WRK, "keepalive", min_value=1.0,
       note=
       "Seconds a chain-verified booking form-state anchor (ScheduleController -> Schedule.jsp, "
       "produced BY warmup) stays usable before a re-warm is required. Read by is_warmed(), which "
       "gates the warm-on-signal steps and do_apply's per-round re-warm. CONTESTED, like "
       "KEEPALIVE_INTERVAL: our own measurements say 4.17min alive / 6.12min dead (2026-08-27 "
       "two-arm probe) and a 16.1-min median (2026-08-20, 28 cycles) -- see OPEN_QUESTIONS Q24, "
       "which records that they disagree ~3x and are unreconciled. 240 is the conservative end of "
       "the shorter measurement. The ~90-120s figure sometimes quoted is storm-auto's, was NEVER "
       "measured here (Q3), and is deliberately NOT used. Resolve by repeating the Q24 probe, not "
       "by editing this default."),
    _s("KEEPALIVE_PROBE", L.TEMPLATE, str, "home", _WRK, "keepalive",
       note=
       "What a holding worker fetches to stay alive: 'home', or a full URL. Auth-only since "
       "2026-09-09 (consolidated after the Test 0907/0907-auth A/B) -- keepalive only ever "
       "verifies the AUTH session is alive, never the booking form-state, which do_apply/"
       "do_discover establish fresh on demand instead of keepalive trying to keep it warm."),
    _s("KEEPALIVE_OFF", L.TEMPLATE, bool, False, _WRK, "keepalive",
       note=
       "Disable keepalive entirely. Diagnostic only: a holding worker then has NO portal traffic and "
       "its session can silently die without anyone noticing until the next real signal."),
    _s("KEEPALIVE_JITTER", L.TEMPLATE, float, 30.0, _WRK, "keepalive",
       note="anti-herd: randomize keepalive timing so many workers don't probe in the same instant"),
    _s("CHECKPOINT_RESTORE_BUDGET", L.TEMPLATE, float, 90.0, _WRK, "keepalive",
       note="wall-clock seconds the checkpoint restore chain (rebuild -> health probe -> re-warm) "
            "may spend before abandoning and going straight to a full login. Every tier is a real "
            "browser navigation, so with NAV_TIMEOUT_SCALE=4 a doomed restore burned up to 384s "
            "before the worker even started logging in (confirmed live 2026-08-14). 0 disables the "
            "budget and lets the chain run to completion."),
    _s("RESTORE_STAMPEDE_JITTER", L.TEMPLATE, float, 30.0, _WRK, "keepalive",
       note="anti-herd: stagger checkpoint-restore attempts after a mass relaunch"),
    # Hold / ISP swap
    # Renamed 2026-09-09 from HOLD_SWAP_AFTER_WARMUP: fires right after LOGIN completes now
    # (phases.py's _warmup(), between LOGGED_IN and KEEPING), not after warmup -- warmup itself is
    # no longer done unconditionally at this point in the lifecycle, it's deferred to the moment
    # it's actually needed (slot-open/assignment). Earlier history: this key was previously named
    # HOLD_SWAP_AFTER_WARMUP because the swap used to fire right after warmup completed, and before
    # that, a deprecated alias HOLD_SWAP_AFTER_FOUND was removed 2026-08-26 (Phase 6, DECISIONS.md
    # D35) after being found inconsistently read by some Hub-side call sites -- straight renames
    # only since then, no dual-spelling aliases. When OFF the swap step is skipped entirely and the
    # worker stays on its phase-1 proxy for the whole run.
    _s("HOLD_SWAP_AFTER_LOGIN", L.TEMPLATE, bool, False, _TPL_BK, "hold_swap",
       note="on/off switch for the phase-1->phase-2 proxy swap (LiveEngineOps.do_swap); off by "
            "default -- a worker just keeps its phase-1 (PROXY_PROVIDER) proxy for the whole run. "
            "NAME IS LITERAL: the swap fires right after login finishes -- i.e. before the worker "
            "starts its (potentially many-hour) hold, and before any warmup happens at all (warmup "
            "is deferred until slot-open/assignment). This is intentional: the operator's design is "
            "soax (short, ~1h session) for the brief login, then a swap to webshare (long, ~24h "
            "session) BEFORE the long hold starts, so the hold never outlives its proxy's own "
            "session cap. When on, a second proxy is leased per HOLD_PROXY_PROVIDER/HOLD_PROXY_KIND. "
            "Booking-layer override is the dashboard's booking-settings shortcut checkbox -- a "
            "per-booking operator choice, not something that should require editing a worker "
            "template"),
    _s("HOLD_SWAP_RETRIES", L.TEMPLATE, int, 3, _WRK, "hold_swap",
       note="swap retry protocol (operator spec 2026-08-14): if the FIRST swap attempt fails (new "
            "session doesn't pass its post-swap health check, or launch itself fails), do_swap() "
            "holds a bit longer on the CURRENT (phase-1) proxy -- HOLD_SWAP_RETRY_HOLD_SECS -- then "
            "requests a FRESH hold-pool proxy and tries again. Bounded by this many total extra "
            "attempts (set to 1 for the operator's literal example: one initial try, one final "
            "chance). Exhausting the budget gives up on swapping PERMANENTLY for this worker -- it "
            "keeps holding on its original phase-1 proxy and is never stopped."),
    _s("HOLD_SWAP_RETRY_HOLD_SECS", L.TEMPLATE, float, 600.0, _WRK, "hold_swap",
       note="how long do_swap() holds on the current proxy after a failed swap attempt before "
            "requesting a fresh hold-pool proxy and retrying (operator's example: 10 minutes)."),
    _s("HOLD_VERIFY", L.TEMPLATE, bool, True, _WRK, "hold_swap",
       note=
       "Verify the session is still bookable after a hold-pool swap before trusting it. Off risks "
       "holding a session that looks alive and cannot book."),
    # HOLD_POLL_GAP: the hold-loop's own sleep between iterations. A resolved value of 0 means the
    # loop spins unthrottled — this is the CONFIRMED root cause of the 2026-08-07 incident (a worker's
    # frozen effective_settings_json showed HOLD_POLL_GAP=0.0, and it wrote ~150-280 events/sec for 66
    # minutes straight). Never leave this at a 0-is-the-placeholder default; min_value is enforced by
    # the resolver (see resolver.py) as a hard backstop regardless of what any layer sets it to.
    _s("HOLD_POLL_GAP", L.TEMPLATE, float, 5.0, _WRK, "hold_swap", min_value=1.0,
       note=
       "Seconds between polls while holding. Also sets the park duration (3 gaps), so raising it "
       "lengthens the back-off after repeated failures as well as the idle cadence."),
    _s("SWAP_DIRECT_BOOTSTRAP", L.TEMPLATE, bool, False, _WRK, "hold_swap",
       note=
       "Bootstrap the hold session directly instead of migrating cookies from the warm session. "
       "Migration is the proven path; this exists for diagnosis."),
    _s("SESSION_PRESERVING_FANOUT", L.TEMPLATE, bool, True, _WRK, "hold_swap",
       note=
       "Fan out to more workers without discarding warm sessions. Off rebuilds them, which costs a "
       "login wall and a CAPTCHA solve each."),
    # Discovery / poll cadence
    _s("DISCOVERY_NAV_RETRIES", L.TEMPLATE, int, 0, _WRK, "poll_cadence",
       note=
       "Retries for a CONNECTION-level nav failure during discovery, in the SAME warm session. A 200 "
       "with no marker is a CONTENT problem, not a connection one, and is deliberately not retried "
       "here."),
    _s("DISCOVERY_NAV_BACKOFF", L.TEMPLATE, float, 0.75, _WRK, "poll_cadence",
       note=
       "Seconds between those nav retries, scaled per attempt and jittered."),
    _s("GETTIME_CACHE_SECS", L.TEMPLATE, float, 300.0, _WRK, "poll_cadence",
       note=
       "How long the Hub may reuse one worker's gettime result for the whole fleet. Higher means far "
       "less portal traffic; too high means scanning against a stale special-days map."),
    # Concurrency
    _s("LOGIN_CONCURRENCY", L.TEMPLATE, int, 10, _WRK, "concurrency",
       note=
       "Simultaneous logins allowed fleet-wide. The portal's login wall is the scarcest resource in "
       "the system; raising this raises rejections rather than sessions."),
    _s("APPLY_CONCURRENCY", L.TEMPLATE, int, 100, _WRK, "concurrency",
       note=
       "Simultaneous apply submissions allowed. Bounded separately from login because a warm session "
       "is expensive and a submit is cheap."),
    _s("RESPAWN_BACKOFF_BASE", L.TEMPLATE, float, 2.0, _WRK, "backoff",
       note=
       "Base seconds for the exponential backoff between worker respawns. Too low turns a failing "
       "worker into a respawn storm."),
    _s("RESPAWN_BACKOFF_CAP", L.TEMPLATE, float, 120.0, _WRK, "backoff",
       note=
       "Ceiling for that backoff, so a permanently broken worker still retries occasionally instead "
       "of never."),
    _s("HOLD_RESPAWN", L.TEMPLATE, bool, True, _WRK, "backoff",
       note="feature flag for the hold-phase respawn mechanism"),
    # Role behavior
    _s("ROLE", L.TEMPLATE, str, "apply", _WRK, "role",
       note=
       "Worker role: 'apply' books, 'scout' only discovers. On a TEST booking the operator assigns "
       "this per identity; on a REAL booking it is derived from the identity type."),
    _s("CAPTURE_ONLY", L.TEMPLATE, bool, False, _WRK, "role", note="never submits -- the worker discovers and reports slots but the apply/booking path is "
       "skipped entirely. Always True for a scout role; set it on an apply worker to "
       "exercise the whole pipeline without booking anything."),
    _s("WARMUP_ONLY", L.TEMPLATE, bool, False, _WRK, "role",
       note=
       "Stop after warm-up without holding, discovering or applying. Diagnostic: proves the login "
       "and questionnaire chain work without spending slots."),
]

# WORKER-instance settings are runtime bindings (proxy/identity/subaccount order) resolved by
# the Hub, not free-form knobs — so they are not in the catalog; the worker layer's catalog role
# is to override TEMPLATE/GLOBAL knobs (via override_layers above).

CATALOG: dict[str, Setting] = {s.key: s for s in (_GLOBAL + _BOOKING + _TEMPLATE)}

assert len(CATALOG) == len(_GLOBAL) + len(_BOOKING) + len(_TEMPLATE), "duplicate setting key in catalog"
assert all(
    s.default is None or s.min_value is None or s.default >= s.min_value for s in CATALOG.values()
), "a setting's own default violates its declared min_value"


def settings_for_layer(layer: L) -> list[Setting]:
    """Settings whose HOME is `layer` (what a layer's editor shows), sorted by group then key."""
    return sorted(
        (s for s in CATALOG.values() if s.home == layer),
        key=lambda s: (s.group, s.key),
    )

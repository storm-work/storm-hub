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
    _s("DONUT_API_URL", L.GLOBAL, str, "http://127.0.0.1:10108", group="credentials",
       note=
       "Donut Browser Local API base URL (BROWSER_BACKEND=donut). The app binds 127.0.0.1:10108, or a "
       "random free port if that one was busy at startup -- the Local API tab shows which."),
    _s("DONUT_API_TOKEN", L.GLOBAL, str, "", group="credentials",
       note=
       "Donut Browser Local API bearer token (Donut > Integrations > Local API). Credential -- set "
       "it in Global settings (stored in the DB, masked on read); never commit it to source."),
    _s("WEBSHARE_API_TOKEN", L.GLOBAL, str, "", group="credentials",
       note=
       "Webshare ACCOUNT API token (apidocs.webshare.io, 'Authorization: Token ...') -- powers the "
       "per-pool remaining-bandwidth readout on the Proxies page. DISTINCT from a pool's proxy "
       "credentials (PREFIX/PASSWORD); this is the account token that can read the data plan's "
       "limit/usage. Credential -- set it in Global settings (stored in the DB, masked on read); "
       "never commit it to source. Empty = the bandwidth widget shows 'not configured'."),
    _s("WEBSHARE_BILLING_DAY", L.GLOBAL, int, 1, group="credentials", min_value=1.0,
       note=
       "Day of the month the Webshare plan's bandwidth allowance renews (1-28). Used to compute the "
       "current billing period for the remaining-bandwidth readout, so 'used' reflects this cycle, "
       "not a rolling window. Default 1."),
    _s("LOGIN_TOKEN_POOL_TARGET", L.GLOBAL, int, 0, group="credentials", min_value=0.0,
       note=
       "Depth of the SHARED pre-solved login reCAPTCHA token pool kept full by a Hub background "
       "factory. 0 = OFF (default): every login solves inline (~15-25s). >0: a login pops a READY "
       "token and fires at browser-launch speed instead of solve speed -- the login THROUGHPUT lever "
       "(bookings scale with SHOTS taken, ~1% of identical requests served). The token is sitekey/"
       "ProxyLess, not session-bound, so one pool serves the whole fleet. CapSolver BILLS PER SOLVE: "
       "size to demand (~ one wave of login workers); tokens live ~110s, so an oversized pool wastes "
       "solves on expiry. See libs/bus/token_pool.py."),
    _s("LOGIN_TOKEN_POOL_BATCH", L.GLOBAL, int, 4, group="credentials", min_value=1.0,
       note="Max concurrent CapSolver solves per factory cycle when refilling the login token pool."),
    # ── PortalPulse: real-time portal liveness (libs/bus/portal_pulse.py, 2026-10-23) ──────────
    _s("PORTAL_PULSE", L.GLOBAL, bool, True, group="portal_pulse",
       note=
       "Run the Hub's active portal prober while any booking is RUNNING: an anonymous GET of the "
       "portal home page every PORTAL_PULSE_INTERVAL s from the server and PORTAL_PULSE_EXITS "
       "residential exits, recorded into the fleet-wide PortalPulse that workers also feed from their "
       "own requests. Held workers wake the moment anyone sees the portal answer (the hammer trigger). "
       "Off = workers still report and read the pulse; only the active probing stops."),
    _s("PORTAL_PULSE_INTERVAL", L.GLOBAL, float, 3.0, group="portal_pulse", min_value=1.0,
       note=
       "Seconds between probes per vantage (+-20%). Probes overlap (up to 3 in flight per vantage), "
       "so a 25 s slow accept never blinds the prober. Bandwidth ~28 KB per probe per exit."),
    _s("PORTAL_PULSE_PROVIDER", L.GLOBAL, str, "soax", group="portal_pulse",
       note=
       "Rotating provider (or pool:<name>) the prober mints its residential exits from. Needed to see "
       "the APPLICATION: the Hub server's own IP is refused by the portal edge (403 'try again later', "
       "measured 2026-10-23), so it can only show whether the edge is reachable. Empty or 'none' = "
       "server vantage only."),
    _s("PORTAL_PULSE_EXITS", L.GLOBAL, int, 2, group="portal_pulse", min_value=0.0,
       note="How many residential exits the prober probes from (each re-minted when its own IP-echo "
            "control fails or the edge refuses it)."),
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
    # Where the browser comes from (libs/engine/vendor/donut.py). Settable per TEMPLATE so one booking
    # can run a Donut fleet beside a Chromium fleet on the same exits/accounts -- the A/B that decides
    # whether Donut is worth it. Every booking confirmed so far ran on 'chromium'.
    _s("BROWSER_BACKEND", L.GLOBAL, str, "chromium", _TPL, "browser",
       choices=("chromium", "donut"),
       note=
       "Browser source. 'chromium' (default) = the engine launches Playwright's Chromium with its "
       "own fingerprint + stealth (every confirmed booking ran on this). 'donut' = each session runs "
       "in a Donut Browser (Wayfern) profile attached over CDP; Wayfern owns the fingerprint, so the "
       "engine's fingerprint pool, version alignment, stealth script and HEADLESS_MODE do not apply. "
       "Needs the Donut app running on this host, signed in to a plan with automation, Local API "
       "on, and DONUT_API_TOKEN set. Login/warm-up/apply flows are unchanged."),
    _s("DONUT_PROFILES_PER_HOUR", L.GLOBAL, int, 80, _TPL, "browser", min_value=0,
       note=
       "BROWSER_BACKEND=donut. The registrar starts at most this many accounts per rolling hour (each "
       "creates one Donut profile on its first launch), shared across registrar runs through Redis. "
       "Donut's Pro plan allows 100 new profiles per hour and blocks the account for 24h above that "
       "(2026-10-29: ~236 profiles in two hours). 80 leaves headroom for workers' first launches. "
       "0 disables the budget."),
    _s("REGISTRAR_BROWSER_BACKEND", L.GLOBAL, str, "", _TPL, "browser",
       choices=("", "chromium", "donut"),
       note=
       "Browser the ACCOUNT REGISTRAR uses, independent of BROWSER_BACKEND (workers keep following "
       "that). Blank = follow BROWSER_BACKEND. Set 'chromium' to register accounts while Donut refuses "
       "new profiles (HTTP 429 WAYFERN_GENERATION_LIMIT_REACHED): registration then creates no Donut "
       "profile. Accounts registered before 2026-10-27 were all registered on Chromium and later booked "
       "on Donut profiles, so this works; each such account gets its profile on its first Donut "
       "login. Takes effect on the next registration run."),
    _s("DONUT_HEADLESS", L.GLOBAL, bool, True, _TPL, "browser",
       note=
       "BROWSER_BACKEND=donut only: start Donut profiles headless. Off = a real window, which needs "
       "the Donut app itself to run on a display (Xvfb) -- heavier per session."),
    _s("DONUT_PROFILE_OS", L.GLOBAL, str, "windows", _TPL, "browser",
       choices=("windows", "macos", "linux"),
       note=
       "BROWSER_BACKEND=donut only: the OS the Wayfern fingerprint claims, fixed when a profile is "
       "created. A fingerprint OS other than this host's (Linux) needs a plan with cross-OS "
       "fingerprints -- Donut answers 402 otherwise."),
    _s("DONUT_PROFILE_SCOPE", L.GLOBAL, str, "account", _TPL, "browser",
       choices=("account", "session"),
       note=
       "BROWSER_BACKEND=donut only. 'account' (default) = one persistent profile per sub-account: the "
       "same device across that account's logins (what DETERMINISTIC_FP does for Chromium), cookies "
       "cleared on close, and created once -- so it stays under Donut's hourly new-fingerprint limit. "
       "'session' = a throwaway profile per session (new device every time; every launch counts "
       "against that limit)."),
    _s("DONUT_EXISTING_PROFILES_ONLY", L.GLOBAL, bool, True, _TPL, "browser",
       note=
       "BROWSER_BACKEND=donut only. Offer a live worker only the sub-accounts whose Donut profile ALREADY "
       "EXISTS. Donut blocks an account for 24 hours after it creates too many profiles in one hour "
       "(registrar, 2026-10-29: ~236 profiles, then HTTP 429 WAYFERN_GENERATION_LIMIT_REACHED for the "
       "rest of the day), and a sub-account without a profile cannot launch a browser while that block "
       "is on. Turn OFF once the block has lifted. Fails open: if Donut cannot be asked, nothing is "
       "filtered."),
    _s("DONUT_CDP_THROTTLE", L.GLOBAL, bool, True, _TPL, "browser",
       note=
       "BROWSER_BACKEND=donut only. Route the session's CDP socket through a local relay that paces "
       "the commands Donut rate-limits (Runtime.evaluate / callFunctionOn, ~100/min per PROFILE, "
       "~200 burst) and resends any it still rejects, so the engine never sees the throttle. Without "
       "it Playwright reports the throttle as 'Execution context was destroyed', which the engine "
       "reads as a dead exit and answers with proxy rolls and re-logins. Off = attach directly."),
    _s("DONUT_CDP_PER_MIN", L.GLOBAL, float, 90.0, _TPL, "browser", min_value=1.0,
       note=
       "BROWSER_BACKEND=donut only. Sustained counted commands per minute the relay lets through per "
       "profile. Donut documents 100/min (measured ~98/min); the default leaves headroom. Lower it if "
       "the log shows 'resending' lines (the relay paced too loosely)."),
    _s("DONUT_CDP_BURST", L.GLOBAL, int, 180, _TPL, "browser", min_value=1.0,
       note=
       "BROWSER_BACKEND=donut only. Counted commands the relay lets through at once before pacing "
       "starts. Donut's own burst measured ~200-214 per profile; one login uses ~73."),
    _s("DONUT_PROFILE_KEEP_STATE", L.GLOBAL, bool, False, _TPL, "browser",
       note=
       "BROWSER_BACKEND=donut, DONUT_PROFILE_SCOPE=account only. Off (default) = a profile's cookies are "
       "cleared when it closes, so every session starts clean. On = the profile keeps its cookies, "
       "storage and history between sessions (reCAPTCHA's own cookies age with the account); the "
       "session still clears the PORTAL's cookies on attach, so a stale portal session never leaks "
       "into a fresh login. Whether kept state helps the reCAPTCHA score is unmeasured -- opt in "
       "deliberately. Persistent cookies and localStorage survive a restart; session-only cookies do not."),
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
    _s("PLAYWRIGHT_STEALTH", L.GLOBAL, bool, False, _TPL, "browser",
       note=
       "Apply the playwright-stealth library's patches ON TOP of the built-in deterministic "
       "fingerprint script. Default OFF (the built-in script is the proven path). ON is for A/B "
       "testing whether the extra patches lower the reCAPTCHA-flag rate -- it can OVERRIDE the "
       "deterministic canvas/WebGL noise, so it may help or HURT; validate login rates live before "
       "leaving it on. Does NOT affect the global 'try again later' throttle (that is a portal "
       "load-shed, not a detection signal). Requires `uv pip install playwright-stealth`."),
    _s("PRIMP_IMPERSONATE", L.GLOBAL, str, "chrome_148", _TPL, "browser",
       choices=("chrome_148", "chrome_147", "chrome_146", "chrome_145", "chrome_144"),
       note=
       "The TLS/JA3 + header-order profile the detached primp client impersonates on the post-login "
       "hot path (warmup quest steps, keepalive probe, calendar discovery, submit). MEASURED "
       "2026-10-16: the portal flips to a STRICT anti-bot posture ~75min before slot-open and serves "
       "a ~44KB interstitial to primp while the real browser passes -- and primp was running UNSET "
       "(a generic/random fingerprint) while the browser aligns to the real Chromium (149). Pinning "
       "primp to the closest valid Chrome profile removes that proxy-independent automation tell. "
       "COUPLING: this must stay <= the bundled Chromium major AND be a target primp actually ships "
       "-- primp does NOT raise on an unknown value, it SILENTLY falls back to 'random', which is the "
       "bug this fixes. chrome_148 is the closest valid match to Chromium 149 (primp 1.3.1 ships "
       "chrome_144..148; chrome_149 falls back). On a primp or Chromium upgrade, re-probe the valid "
       "set: `python -c \"import primp; primp.Client(impersonate='chrome_NNN')\"` prints 'does not "
       "exist, using random' for an invalid one. An out-of-allowlist value here is coerced to the "
       "default with a log rather than left to degrade to random."),
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
    # Which reCAPTCHA solver a booking's workers use. The engine still RACES whatever key lists it is
    # handed (libs/engine/vendor/solver.py's race_all); this picks WHICH lists -- 'capsolver' (only
    # CAPSOLVER_KEYS), 'anticaptcha' (only ANTICAPTCHA_KEYS; falls back to capsolver if none set so a
    # misconfig can't brick login), or 'race' (one key from EACH, raced). LOAD IS DISTRIBUTED: each
    # worker is handed ONE key from the chosen solver (round-robin across the fleet), so N keys spread
    # the load instead of every worker hammering key #1. Overridable per booking and per worker
    # template; registration has its own REGISTRATION_SOLVER.
    _s("RECAPTCHA_SOLVER", L.GLOBAL, str, "capsolver", _BK_TPL, "solver",
       choices=("capsolver", "anticaptcha", "race"),
       note="which reCAPTCHA solver this booking uses by DEFAULT: 'capsolver' | 'anticaptcha' | 'race' "
            "(one key from each, raced). Keys per solver live in CAPSOLVER_KEYS / ANTICAPTCHA_KEYS; "
            "load is distributed one key per worker. Booking/template-overridable. Per-ACTION overrides "
            "below (LOGIN_RECAPTCHA_SOLVER / SCHEDULE_RECAPTCHA_SOLVER) can point login and the "
            "schedule page at different solvers; left on 'inherit' they both use this value."),
    _s("LOGIN_RECAPTCHA_SOLVER", L.GLOBAL, str, "inherit", _BK_TPL, "solver",
       choices=("inherit", "capsolver", "anticaptcha", "race"),
       note="solver for the LOGIN reCAPTCHA (action LOGIN_EVISA) specifically. 'inherit' (default) = "
            "use RECAPTCHA_SOLVER. Set it to split transports -- e.g. LOGIN_RECAPTCHA_SOLVER=anticaptcha "
            "while SCHEDULE_RECAPTCHA_SOLVER=capsolver, so login uses the solver with the better login "
            "pass-rate and the schedule page uses the faster one. Same CAPSOLVER_KEYS/ANTICAPTCHA_KEYS "
            "pools and one-key-per-worker distribution."),
    _s("SCHEDULE_RECAPTCHA_SOLVER", L.GLOBAL, str, "inherit", _BK_TPL, "solver",
       choices=("inherit", "capsolver", "anticaptcha", "race"),
       note="solver for the SCHEDULE-PAGE reCAPTCHA (action SCHEDULE_EVISA -- /slots discovery) "
            "specifically. 'inherit' (default) = use RECAPTCHA_SOLVER. Pair with LOGIN_RECAPTCHA_SOLVER "
            "to solve login and the schedule page with different providers (e.g. capsolver here for "
            "speed). Same key pools and distribution as RECAPTCHA_SOLVER."),
    _s("REGISTRATION_SOLVER", L.GLOBAL, str, "capsolver", group="solver",
       choices=("capsolver", "anticaptcha", "race"),
       note="which reCAPTCHA solver account-registration runs use (same choices as RECAPTCHA_SOLVER). "
            "Registration is not per-booking, so this is the global default; the /registration/run "
            "request can override it per run."),
    _s("ANTICAPTCHA_KEYS", L.GLOBAL, str, "", group="credentials",
       note="Comma-separated AntiCaptcha API keys (anti-captcha.com). Multiple keys are load-balanced "
            "one per worker, same as CAPSOLVER_KEYS. Credential -- set it in Global settings (stored in "
            "the DB, masked on read); never commit it to source. Used when RECAPTCHA_SOLVER / "
            "REGISTRATION_SOLVER is 'anticaptcha' or 'race'."),
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
    _s("SELF_SCOUT_FALLBACK", L.BOOKING, bool, True, group="mode",
       note="When True (default), an apply worker falls back to discovering its OWN slots if the "
            "booking's pool is empty AND no scout is actually scouting (scheduler._maybe_self_scout "
            "sends it a scouts_invalid assignment). Set False to FORBID that fallback: an apply "
            "worker then only ever books slots a scout found, and simply keeps holding when there "
            "are none. Independent of MONO_BOOKING, which is the deliberate always-self-scout mode "
            "and is not gated by this."),
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
       note="phase-1 (login/warmup) leased-proxy provider: 'soax' | 'webshare' | 'capsolver' "
            "(self-minted rotating; capsolver KIND is 'mobile' | 'residential'), 'pool:<name>' for a "
            "NAMED rotating pool (a stored soax package / webshare / capsolver account, managed on the "
            "Proxies page -- carries its own provider+kind+creds, so PROXY_KIND is ignored), a "
            "static/ISP provider name (leased, e.g. 'thordata'), or 'all-isp' to LEASE "
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
    _s("ACCOUNT_GEO_PIN", L.BOOKING, bool, True, _TPL, group="mode",
       note=
       "Pin each sub-account to the exit COUNTRY its registration succeeded from (recorded at "
       "registration). A device that registered from DE and then logs in from CZ -- with a timezone and "
       "locale that follow the exit -- is two stories for one account. Applies to accounts that HAVE a "
       "recorded country; older accounts keep the booking's PROXY_GEOS behaviour. The operator's "
       "per-worker geo pin still wins. Honoured on soax, capsolver and webshare rotating exits "
       "(country reseated on each roll / account switch) and, for static pools, at launch and on "
       "Hub rotation -- a static exit cannot change country when the worker switches to another "
       "sub-account (logged). Off = ignore the recorded country. Template-overridable."),
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
    _s("DISCOVERY_SOURCE", L.BOOKING, str, "slots", group="discovery",
       choices=("calendar", "slots", "both"),
       note="DEFAULT REVISED 2026-10-26 -> slots: the source both reference bookings used (0925, 1002) and the one the browser path (now default) supports; calendar remains for full-primp runs (/slots is browser-only). "
            "Which endpoint discovery reads. One of: calendar | slots | both. "
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
    _s("HYBRID_MODE", L.BOOKING, bool, False, group="guards",
       note=
       "DEFAULT REVISED 2026-10-26 -> False (BROWSER PATH): the portal's bot challenge blocks detached primp whenever it runs strict -- 44,699 B interstitial on anonymous and quest-step primp requests (2026-10-16 strict window, 2026-10-23 afternoon, 2026-10-26 morning), while the real browser passes. Both reference bookings ran on the browser path (0925 with PDF, 1002 x12). Turn ON only for a known-permissive window; a primp warm-up that meets the challenge now degrades that worker to the browser for the rest of its process instead of re-logging in. Original note: "
       "FULL HYBRID (CONFIRMED end-to-end 2026-10-08, 3 live bookings): run as many steps as possible "
       "on a DETACHED primp client on the worker's SAME sticky exit with the browser's cookies, keeping "
       "the browser ONLY for the two steps that genuinely need it -- LOGIN (reCAPTCHA token lifecycle) "
       "and the PDF artifact (MostrarPdf 403s a detached client). The portal's only post-login gate is "
       "reCAPTCHA (a server-side token carried only by login + /slots); warm-up, keepalive, CALENDAR "
       "discovery (gettime+getPeriodosOcupados -- tokenless cookie GETs), the pre-submit re-warm and the "
       "submit are all cookie-gated and confirmed to work detached. Benefit: the whole hot path leaves "
       "the heavy browser (host-overload) and the nav-bound cursor behind -- primp doesn't navigate, so "
       "the cursor stays fresh and keeping survives the unstable hot period. Master switch; it gates the "
       "four HYBRID_* legs below (effective leg = HYBRID_MODE AND HYBRID_<leg>). Default ON. Set OFF to "
       "fall back to the all-browser path, or disable a single leg below if it misbehaves."),
    _s("HYBRID_SUBMIT", L.BOOKING, bool, True, group="guards",
       note=
       "HYBRID leg: send the SubmeterVistoCriaPDF submit via the detached primp client instead of the "
       "in-page browser XHR (confirmed books, status 200 ~1.5KB). Effective only when HYBRID_MODE is on "
       "(effective = HYBRID_MODE AND HYBRID_SUBMIT). The %PDF is fetched via the browser afterward "
       "(postponed; the booking is already made). Default ON; set OFF to keep just the submit on the "
       "browser while other legs stay hybrid."),
    _s("HYBRID_WARMUP", L.BOOKING, bool, True, group="guards",
       note=
       "HYBRID leg: run the warm-up / re-warm quest chain (home-lang -> Questionario -> quest steps -> "
       "Formulario/CSRF -> ScheduleController -> Schedule.jsp) on the detached primp client (confirmed; "
       "establishes the bookable cursor without the browser). Effective = HYBRID_MODE AND HYBRID_WARMUP. "
       "Default ON."),
    _s("HYBRID_DISCOVERY", L.BOOKING, bool, True, group="guards",
       note=
       "HYBRID leg: run CALENDAR discovery (gettime + getPeriodosOcupados -- tokenless session-cookie "
       "GETs) on the detached primp client (confirmed to return the same availability as the browser "
       "nav). Effective = HYBRID_MODE AND HYBRID_DISCOVERY, and ONLY the calendar leg -- /slots stays "
       "browser-mediated (its reCAPTCHA token lives in-page), so pair with DISCOVERY_SOURCE=calendar. "
       "Default ON."),
    _s("HYBRID_CLOSE_BROWSER", L.BOOKING, bool, True, group="guards",
       note=
       "HYBRID leg (Phase C, CONFIRMED 2026-10-08): after login + checkpoint, CLOSE the Playwright "
       "browser and run the entire hot path (warm-up, keepalive, discovery, re-warm, submit) "
       "browserless on primp, freeing the browser's RAM/handles for the whole hold. The engine's "
       "close_browser() keeps the primp session alive ('primp carries the session forward'); the %PDF "
       "reopens a fresh browser on the same exit with the saved cookie jar injected (confirmed: session "
       "survives the close, real booking + 240KB %PDF). A dead-exit re-login reopens a browser (and the "
       "next primp step re-closes it). Effective = HYBRID_MODE AND HYBRID_CLOSE_BROWSER. This frees "
       "browser resources over long holds; it does NOT reduce the acute login-concurrency peak (that is "
       "WARMUP_START_JITTER / concurrency). Default ON; set OFF to keep the browser parked (Phase B)."),
    _s("HYBRID_KEEPALIVE", L.BOOKING, bool, True, group="guards",
       note=
       "HYBRID leg: run keepalive (the schedule-mode active-cursor re-warm, or the auth_only liveness "
       "probe) on the detached primp client, so KEEPING holds a bookable session through the portal's "
       "down-windows without the browser navigating (which would stale the cursor). Effective = "
       "HYBRID_MODE AND HYBRID_KEEPALIVE. Default ON."),
    _s("HYBRID_PRIMP_CONNECT_TIMEOUT", L.BOOKING, float, 30.0, group="guards", min_value=1.0,
       note=
       "Connect timeout (s) for the detached primp client used on every hybrid leg (warmup / "
       "discovery / keepalive / submit). CRITICAL: primp.Client's constructor `timeout=` is NOT "
       "enforced per-request, so a webshare exit whose gateway accepts the TCP connection but whose "
       "upstream is dead would hang a primp call FOREVER -- confirmed live 2026-10-16, a whole apply "
       "fleet froze in ep_poll for 55-86 min mid re-warm, never cleared `not bookable`, and nobody "
       "applied though the slot pool was full. connect_timeout + read_timeout ARE enforced, so they "
       "bound the call and a stall is raised as a timeout (which _is_transient_login_failure matches, "
       "so the exit is rolled). Default RAISED 10 -> 30 on 2026-10-23 (F42): a STRESSED portal took "
       "25.6 s to accept a connection, and at 10 s the whole Primp fleet of run 1023 was locked out "
       "after recovery (35/35 warm-ups, then 1,298 more, died on 'client error (Connect)') while "
       "browsers squeezed through. Any bound prevents the hang; a dead webshare tunnel fails in ~3 s "
       "regardless ('tunnel error'), and PortalPulse tells a dead exit from a slow portal."),
    _s("HYBRID_PRIMP_READ_TIMEOUT", L.BOOKING, float, 30.0, group="guards", min_value=2.0,
       note=
       "Read timeout (s) for the detached primp client on every hybrid leg: the max gap with no "
       "bytes from the portal before the request is aborted. This is the one that catches the live "
       "freeze (gateway connected, upstream never responds). Enforced by primp (unlike the "
       "constructor `timeout=`). Keep comfortably above a healthy quest-step response but well under "
       "any human-noticeable hold -- 30 is a safe default; lower it (~15) alongside "
       "APPLY_SUBMIT_TIMEOUT when riding a stress window. See [[rewarm-every-round-is-mandatory]]."),
    _s("APPLY_SUBMIT_TIMEOUT", L.BOOKING, float, 100.0, group="guards", min_value=5.0,
       note=
       "STRESS MODE lever. Timeout (s) for the SubmeterVistoCriaPDF submit XHR. Default 100 (the "
       "proven value). When the portal is in stress mode (up 30-90s then down), a submit into a "
       "down-window HANGS the full timeout -- spanning entire up-windows -- so the worker gets ~1 "
       "attempt per 100s. Lower it to ~15-20 so a down-portal submit FAILS FAST (portal_unreachable) "
       "and the worker can re-fire several times per up-window ('ride the window'). Too low risks "
       "aborting a slow-but-live submit; 15-20 is the stress sweet spot, 100 for a healthy portal."),
    _s("STRESS_RETRY_GAP", L.BOOKING, float, 3.0, group="guards", min_value=0.0,
       note=
       "DEFAULT REVISED 2026-10-26 -> 3 (the stress recipe): a submit that never reached the portal keeps its slot and fires again after 3 s instead of releasing it and waiting a Hub round-trip -- riding the portal's 30-90 s up-windows. Bounded by the park brake. Original note: "
       "STRESS MODE lever. 0 (default) = OFF: on portal_unreachable the worker releases the slot and "
       "waits a full HOLD_POLL_GAP, re-leasing via the Hub next tick. When > 0, the worker instead "
       "KEEPS its assigned slot and retries the submit after this short gap (s) -- no slot release, "
       "no Hub re-assignment round-trip -- so it fires the instant the portal is back up. The cursor "
       "survives a portal_unreachable (the submit never reached the portal), so in schedule mode the "
       "retry is a ~3s direct-apply. Bounded by APPLY_MAX_ATTEMPTS (the park brake still applies). "
       "Pair with a low APPLY_SUBMIT_TIMEOUT and schedule keepalive mode."),
    # F40/F41 (run 1023, 2026-10-23): hold the login through a stressed portal instead of re-logging
    # in and rolling exits into an outage. See docs/engineering/REPORT_2026-10-23_stress_window.md.
    _s("STRESS_HOLD", L.BOOKING, bool, True, group="guards",
       note=
       "STRESS MODE (default ON). When a warm-up fails because the PORTAL is slow or down (503, slow "
       "accept, or no answer to an exit that still reaches the internet), KEEP the login and re-warm "
       "on it until the portal answers -- no re-login, no exit roll, not counted as a session "
       "failure. The first answer decides: warmed -> apply; logged out -> re-login at once. Run 1023: "
       "75/123 workers threw away a live login on ONE failed re-warm and only 25 got back in; after "
       "recovery every logged-in worker warmed in 1-2 min. OFF = the old behaviour exactly."),
    _s("STRESS_HOLD_MAX_S", L.BOOKING, float, 7200.0, group="guards", min_value=0.0,
       note=
       "Longest a single STRESS_HOLD lasts (s) before the old ladder (rebuild / re-login) resumes. "
       "Default 2 h -- run 1023's outage was ~85 min. 0 = never hold."),
    _s("STRESS_HOLD_GAP", L.BOOKING, float, 15.0, group="guards", min_value=1.0,
       note=
       "Pause (s, +-20% jitter) between re-warm attempts while holding through portal stress. The "
       "portal comes back in 30-90 s up-windows, so this should be well under that; each attempt is "
       "itself 30-110 s under stress."),
    _s("PORTAL_DOWN_EXIT_ROLL_S", L.BOOKING, float, 900.0, group="guards", min_value=0.0,
       note=
       "How long (s) to trust 'this exit reaches the internet but the portal does not answer it' as "
       "PORTAL DOWN (keep the exit + login) before rolling the exit anyway. Bounded because a webshare "
       "tunnel failure can be exit-specific (one roll took reachability 25% -> 83%). Resets on any "
       "portal answer or roll. Run 1023 rolled exits ~9,800 times into a down portal. Needs STRESS_HOLD."),
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
    _s("NAV_TIMEOUT_SCALE", L.TEMPLATE, float, 4.0, _WRK, "login_resilience",
       note=
       "Multiplier on every browser navigation timeout. Raise when the portal is slow -- a SLOW "
       "portal is not a broken session, and a re-login cannot make it faster. DEFAULT REVISED 2026-10-26 "
       "1.0 -> 4.0 to match every seeded template (production). The browser-init deadline now scales "
       "with it (session.py) so a slow bootstrap is not killed at a fixed 120 s. Production runs at "
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
    _s("QUEST_STEP_RETRIES", L.TEMPLATE, int, 3, _WRK, "login_resilience", min_value=1.0,
       note=
       "Retries per questionnaire step. The chain must complete or the booking cursor is never "
       "built, so this is worth more than most retry budgets. UNWIRED until 2026-10-01: the code "
       "hardcoded _QUEST_STEP_RETRIES=3 and never read this (default was a never-validated 10) -- "
       "corrected to 3 to match the proven value and wired to a real read site in "
       "ops_live._run_warmup_steps. On retry-EXHAUSTION the warm-up now ABORTS the whole chain "
       "(the questionnaire is a stateful server-side sequence; advancing past a failed step only "
       "surfaces later as the vaguer csrf_missing/quest_state_stale), so raise this only with "
       "QUEST_STEP_BACKOFF in mind -- more retries x longer backoff = a slower warm-up that loses "
       "slot races."),
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
            "fresh exit. Default 0.5. NOTE the throttle is per-account, not a global lottery "
            "(see LOGIN_ACCOUNT_HOURLY_CAP): the pause itself buys little, the per-account shot cap "
            "is what protects the account."),
    _s("LOGIN_ACCOUNT_HOURLY_CAP", L.TEMPLATE, int, 3, _WRK, "login_resilience", min_value=0.0,
       note="max login shots per sub-account username inside LOGIN_ACCOUNT_WINDOW_S (default 1h), "
            "counted across ALL workers and respawns through a Redis ledger (login:acct:<username>). "
            "A worker whose account is at the cap moves to a sub-account that has rested, and when "
            "every one of its accounts is at the cap it waits for the earliest to recover instead of "
            "firing. MEASURED 2026-10-26 over 24,880 login POSTs: the portal's 'try again later' "
            "tracks recent attempts on the SAME username -- 0 prior attempts in the hour 25% success, "
            "1-2 prior 14%, 3-9 prior 3.2%, 10+ prior 0.02% -- and 71% of our shots were in the 10+ "
            "bucket (Test 1026: 2,093 shots on 12 accounts, 6 logins). 3 keeps every shot in the "
            "0-2 prior buckets. 0 disables the cap (the pre-2026-10-27 fire-again-immediately "
            "behaviour). Only logins the PORTAL answered about the username count (success, 'try again "
            "later', wrong credentials); a raised failure, solver timeout, reCAPTCHA-token verdict, "
            "WAF challenge, dead tunnel or maintenance page does not."),
    _s("LOGIN_ACCOUNT_WINDOW_S", L.TEMPLATE, float, 3600.0, _WRK, "login_resilience", min_value=60.0,
       note="the rolling window (seconds) LOGIN_ACCOUNT_HOURLY_CAP is counted over. Default 3600."),
    _s("LOGIN_RECAPTCHA_ROLLS", L.TEMPLATE, int, 3, _WRK, "login_resilience", min_value=0.0,
       note="fresh exits to roll through on a 'Por favor, confirma o reCAPTCHA' verdict before the "
            "rejection is charged to the sub-account. This verdict is an EXIT/token problem, not the "
            "account's fault -- the exit moved across the solve window OR is flagged/bad for "
            "reCAPTCHA -- so the answer is a fresh exit, never the account's 2-strike budget "
            "(two strikes = permanent block). CONFIRMED 2026-09-24 (PEDRO RODRIGUES): one exit "
            "returned it for BOTH sub-accounts while another logged the SAME accounts in seconds. "
            "Replaces LOGIN_IP_MISMATCH_RETRIES, which only rolled when the exit DEMONSTRABLY moved "
            "and so burned accounts on a stable-but-flagged exit. Set 0 to charge every rejection."),
    _s("LOGIN_SOLVER_FAILURE_RETRIES", L.TEMPLATE, int, 3, _WRK, "login_resilience", min_value=0.0,
       note="retries on a reCAPTCHA SOLVER outage (the solver returned no token: a 180s solver "
            "timeout or all selected solvers failed) before the login round gives up -- and, like "
            "the reCAPTCHA-verdict and portal-busy budgets, NOT charged to the sub-account's 2-strike "
            "sticky budget. CONFIRMED 2026-10-16 ('Test login 1016 - auth', anticaptcha): a solver "
            "timeout raised 'reCAPTCHA: no token after all attempts', which matched no transient "
            "marker and so was charged as a real login attempt -- a solver hiccup burned real "
            "accounts. Login solving is ProxyLess, so the exit is NOT rolled here (the exit is fine, "
            "the solver failed); the worker just backs off LOGIN_THROTTLE_BACKOFF and re-solves. "
            "Set 0 to charge a no-token solver failure to the account (not recommended)."),
    _s("QUEST_STEP_GAP", L.TEMPLATE, float, 1.0, _WRK, "login_resilience",
       note="seconds (+-30%% jitter) paced before each warm-up questionnaire step and before the "
            "Questionario nav. Ported from storm-auto and then NEVER READ -- the classic unwired "
            "setting: it sat here at 0.0 while the 7-step warm-up fired as a sub-second burst right "
            "after login, which is exactly where quest-step 403s cluster (storm-auto's own note: "
            "'the quest-step-1 403s were observed clustering on exactly this post-login burst'). "
            "Wired to a real read site in ops_live._run_warmup_steps on 2026-08-22 and defaulted to "
            "1.0. Costs ~7s per warm-up; a warm-up that 403s costs the whole session. 0 restores "
            "the old no-gap behaviour."),
    _s("QUEST_STEP_BACKOFF", L.TEMPLATE, float, 1.5, _WRK, "login_resilience", min_value=0.0,
       note="base seconds to wait before RETRYING a questionnaire step that failed (raised or came "
            "back non-200). Grows exponentially per attempt (base, base*2, base*4 ...) capped at "
            "~8s, +-30%% jitter. Added 2026-10-01: the retry loop used a flat 1.5s, which burned all "
            "three attempts inside the same second against a 503/WAF storm (step 5 attempts 1-2 both "
            "at 18:41:53 in one live log) -- worse than not retrying. A 503 is portal overload (high "
            "traffic / slow render), so backing off between attempts is the reliable recovery; it is "
            "NOT a dead-exit signal and does not roll the exit. Default 1.5 preserves the first "
            "attempt's wait; 0 restores the old no-backoff behaviour."),
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
    # 360 (+0..120 jitter = 6-8 min) since 2026-10-28, because the portal SESSION idles out at ~10.4 min
    # and 600+30 sat on that line. History: 600, not 900. MEASURED 2026-08-20 across 28 keepalive cycles on booking "Test 3018": the
    # portal's scheduling form state dies on a 16.1-minute median clock (min 15.8, max 33.6). A
    # 900s interval left a 60-second margin against a 15.8-minute worst case -- and the re-warm
    # itself takes longer exactly when the portal is slow, so the margin closes from both ends.
    #
    # That is not theoretical: worker EDER's keepalive re-warm failed 38 seconds AFTER slot-open
    # fired, and it spent the next eight minutes recovering while a worker whose form state happened
    # to be fresh booked in 47 seconds. 600s leaves ~6 minutes of headroom and costs one extra
    # lightweight probe per worker per hour.
    _s("KEEPALIVE_INTERVAL", L.TEMPLATE, float, 360.0, _WRK, "keepalive", min_value=1.0,
       note=
       "Seconds between keepalive probes on a holding worker; the actual wait is this plus a random "
       "0..KEEPALIVE_JITTER (default 360 + 0..120 = every 6-8 minutes). Sized to the PORTAL SESSION's "
       "idle timeout, which measured ~10.4 min (3 of 4 held sessions were found expired 10.3-10.5 min "
       "after login, 2026-10-28; the old 600s + 30s landed right on that line, so sessions died "
       "between probes). Must also stay under the proxy exit's idlettl. FORM STATE is a different, "
       "shorter clock (CONTESTED: 16.1-min median on 2026-08-20 vs 4-6 min in CONFIRMED_FACTS §30) -- "
       "KEEPALIVE_MODE=schedule caps the cadence under FORM_STATE_TTL itself."),
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
    _s("KEEPALIVE_MODE", L.BOOKING, str, "quest_hold", _WRK, "keepalive",
       choices=("auth_only", "schedule", "quest_hold"),
       note=
       "DEFAULT REVISED 2026-10-29 -> quest_hold: park the logged-in browser on Questionario and keep "
       "the session alive with ONE quest step (QuestNextQuestion) every QUEST_HOLD_MIN_S..QUEST_HOLD_MAX_S "
       "(jittered), reloading Questionario once every step is answered. No Schedule.jsp cursor is "
       "spent, so there is no 35-45s re-warm exposure to the portal's stress window and the traffic is "
       "a handful of tiny requests; on the signal the first warm-up RESUMES the questionnaire at the "
       "step the hold had reached (ops_live._qh_take) instead of starting over. MEASURED "
       "(probes/quest_hold_probe.py, 60-300s jitter, 40 min): 13/13 events clean, session alive, and "
       "the full warm-up chain still reached the bookable cursor. Once the worker holds a FRESH "
       "bookable cursor (is_warmed) the hold falls back to the auth_only home probe. "
       "Earlier note, 2026-10-26: schedule -> schedule: auth_only DEFERS the multi-request warm-up to the signal, i.e. INTO the portal's stress window -- run 1023 warmed 0 of ~260 between 10:30 and 11:10 after a healthy 08:45-10:00 hold. schedule warms during the hold and keeps the cursor fresh, so the signal-time action is (when the cursor is still within FORM_STATE_TTL) a direct submit; it booked 12 on 1002. Cost: a re-warm per worker every ~0.75 x FORM_STATE_TTL. Mixed counter-evidence: 1016 (calm window, hybrid) booked 2 in schedule vs 3 in auth_only. Original note: "
       "Keepalive strategy for a holding worker. 'auth_only' (DEFAULT, the proven model): probe the "
       "authenticated HOME page; booking form-state is NEVER kept warm (do_apply re-warms every round "
       "regardless; warmup is deferred to the signal). 'schedule' (EAGER-warmup model, operator "
       "2026-10-02): warm BEFORE keeping so the worker holds a bookable Schedule.jsp cursor; the "
       "keepalive probes Schedule.jsp and RE-WARMS it before FORM_STATE_TTL (paced UNDER the TTL -- "
       "hotter cadence = more portal traffic); on the signal the worker applies/discovers DIRECTLY "
       "(do_apply skips its round-start re-warm when is_warmed()), re-warming only when stale or "
       "bounced. This forks around the auth_only truth that the cursor is navigation-bound -- SAFE "
       "here because the keepalive keeps the browser ON Schedule.jsp (auth_only navs to HOME, which "
       "is why auth_only must re-warm every round). Earlier 'schedule' was a non-destructive "
       "MEASUREMENT only (the 147-vs-42 nav-verifies-but-does-not-renew finding). Deliberately at the "
       "BOOKING layer (not TEMPLATE) so a template cannot silently override the operator's per-booking "
       "choice. See docs/engineering/REPORT_2026-10-01_hot_period_reliability.md."),
    _s("QUEST_HOLD_MIN_S", L.BOOKING, float, 60.0, _WRK, "keepalive", min_value=15.0,
       note="KEEPALIVE_MODE=quest_hold: shortest wait (seconds) between hold events (one quest step, or "
            "the Questionario reload after the last step). The wait is uniform in [MIN, MAX]. The "
            "portal's idle expiry was measured at ~10.4 min; 60-300s survived a 40 min hold."),
    _s("QUEST_HOLD_MAX_S", L.BOOKING, float, 300.0, _WRK, "keepalive", min_value=15.0,
       note="KEEPALIVE_MODE=quest_hold: longest wait (seconds) between hold events. Keep it well under "
            "the ~10.4 min idle expiry."),
    _s("SCHEDULE_KEEPALIVE_TTL_FRACTION", L.BOOKING, float, 0.75, _WRK, "keepalive",
       min_value=0.2,
       note=
       "schedule mode ONLY: the keepalive probe/re-warm cadence as a fraction of FORM_STATE_TTL. The "
       "window is capped at fraction x TTL so a re-warm lands before the cursor ages out. LOWER = "
       "fresher cursor (a slot-open is more likely to find the cursor live and apply INSTANTLY, no "
       "round-start re-warm) at the cost of MORE re-warm traffic -- riskier on a throttled portal. "
       "0.5 near-eliminates the ~one-TTL-window lapse per cycle; 0.75 (default) keeps traffic lower "
       "and accepts that a slot-open landing in the lapse window pays a ~50s re-warm (it still books). "
       "Ignored in auth_only mode. CONFIRMED live 2026-10-02 that schedule direct-apply books (~3s "
       "submit); see docs."),
    _s("KEEPALIVE_STRESS_HOLD", L.BOOKING, bool, True, _WRK, "keepalive",
       note=
       "DEFAULT REVISED 2026-10-26 -> True: part of the measured stress recipe, and consistent with STRESS_HOLD (default on): a portal-down window must not cost a held login. Bounded (~4x KEEPALIVE_DEAD_TRIP), and a real dead exit still rolls. Original note: "
       "STRESS MODE lever. When the portal is up 30-90s then down, a held warm session is gold -- "
       "re-login is the hardest thing in stress. By default a keepalive probe failure counts toward "
       "KEEPALIVE_DEAD_TRIP and, past it, ROLLS the exit + re-authenticates -- which during a "
       "portal-DOWN window destroys a perfectly good session. With this ON, a probe that fails with "
       "a PORTAL-reachability error (timeout / connection closed/reset/refused / empty response) "
       "while the error is NOT a proxy/tunnel/chrome-error (= the exit is reaching the net, the "
       "PORTAL is down) is treated as a down-window: record the portal DOWN and HOLD the session, "
       "re-probing next cycle -- no roll, no re-login. Bounded: after ~4x KEEPALIVE_DEAD_TRIP "
       "consecutive portal-down cycles it falls through to the normal restore (the exit may really "
       "be dead). A genuine dead-exit (tunnel/proxy/chrome-error) still rolls immediately. OFF by "
       "default (unchanged behaviour). Pair with schedule mode for the hot period."),
    _s("KEEPALIVE_OFF", L.TEMPLATE, bool, False, _WRK, "keepalive",
       note=
       "Disable keepalive entirely. Diagnostic only: a holding worker then has NO portal traffic and "
       "its session can silently die without anyone noticing until the next real signal."),
    _s("KEEPALIVE_JITTER", L.TEMPLATE, float, 120.0, _WRK, "keepalive",
       note="Random 0..N seconds ADDED to KEEPALIVE_INTERVAL on every wait (default 120: with the 360s "
            "interval, probes land every 6-8 minutes). Anti-herd -- many workers must not probe in the "
            "same instant -- and it keeps the cadence from being an exact grid."),
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

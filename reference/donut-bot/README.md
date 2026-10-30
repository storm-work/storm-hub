# ptbot pure-API booking bot (v3)

A no-browser, HTTP-only Python bot that runs the same visa-appointment booking flow the
`document/main-human` browser extension does (login → editProfile if fresh → Questionario →
Formulario → Schedule/slots → book → PDF), but talks to the target site directly via `httpx` +
`curl_cffi` instead of driving a real Chromium instance.

It is a direct evolution of the prior-generation prototype
(`/home/yns/Documents/ptbot/visa_api_bot_v2.py`) — cross-validated against the freshest real HAR
capture (2026-08-20), which confirmed its request shapes are still accurate. The one structural
change is that this version is **dashboard-integrated**: accounts and proxies come from the same
machine API `orchestrator.py` and the extensions already use, and every step reports its progress
back — so this bot's activity shows up live in the same Booking / BookingWorker /
BookingAppointment views, and it can run concurrently with the browser-extension path against the
same account/proxy pool with zero coordination code (the claim endpoint is already atomic).

## What it does (every `POLL_INTERVAL` seconds)

1. `GET /api/settings` → `{enabled, postoId}`. Idles if disabled.
2. Tops up to `CONCURRENCY` in-flight accounts: `GET /api/account/claim` (atomic,
   `SKIP LOCKED` — safe to call alongside the login extension's own claims).
3. Per account: `GET /api/proxy/unassigned`, then runs the 7-step flow, reporting each
   milestone via `POST /api/account/event` (with a `stage` that drives the dashboard's
   auto-created-per-posto Booking/BookingWorker sync) and status transitions via
   `POST /api/account/status`. A successful booking reports `event: 'booking_success'` with
   `detail: {"pdfUrl", "date", "period"}` — the exact shape the dashboard parses into a
   `BookingAppointment`.
4. Proxy-routed failures (connect/timeout/banned-IP) report `POST /api/proxy/health` —
   generic site rejections do not (avoids misattributing a site-side block to the proxy).

## Formulario -> ScheduleController is a hybrid real-browser leg

Confirmed 2026-09-03 (reproduced 4x across two consular posts and two Formulario-fill-timing
profiles, ruling out timing/proxy as the cause): a pure-HTTP `ScheduleController` submission
always gets its 302 redirected straight to `BD_problm.jsp` — the site's BD layer doesn't trust
a fabricated `getTables` fingerprint (`_do_fingerprint_check`, still used as-is for the lower-
risk login/schedule-slots pages, where it's proven fine) for this specific, high-value POST.

`step_fill_formulario` therefore drives this one leg — Questionario -> native form-POST to
Formulario -> fill 84 fields -> submit -> land on Schedule.jsp — inside a real (headless)
Playwright/Chromium tab (stealth-patched via `playwright-stealth`), handed this session's own
httpx cookies, then syncs the resulting cookies + Schedule.jsp HTML back into `self.session` so
steps 6-7 (slots, booking) continue via plain httpx as before. One Chromium instance is shared
per process (`_get_shared_browser()`) across accounts/workers via fresh per-call incognito
contexts, to avoid repeatedly paying a slow-environment ~15-25s cold-start cost.

The getTables channel elsewhere (login page, Schedule.jsp before the slots POST) is still
answered with the older **best-effort fabricated** payload — plausible `Salted__`-prefixed
base64 blobs, not a real implementation of `bs_datefuncs.js`'s fingerprinting — and that has
held up fine in every test so far. If a similar 403/BD_problm.jsp pattern ever shows up on
those steps too, the same real-browser-handoff approach is the template to reuse.

## Deploy

```bash
# 1. copy this folder to the target VPS, e.g. /home/USER/visa-dashboard/api-bot
#    (needs second_form_mapping_2.json, sample_photo.jpg, sample_attachment.pdf alongside
#    api_bot.py — all already included here)
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/playwright install chromium   # one-time; the Formulario step needs a real browser

# 2. configure
cp .env.example .env
nano .env          # set MACHINE_TOKEN (= dashboard SOCKET_INTERNAL_SECRET),
                   # CAPSOLVER_API_KEY / ANTICAPTCHA_API_KEYS

# 3. single-account smoke test FIRST (see plan §E) — set CONCURRENCY=1, watch logs/api_bot.log
#    against the HAR sequence step-by-step before trusting it at any scale
set -a; . ./.env; set +a
./venv/bin/python3 api_bot.py

# 4. run forever via systemd (independent of orchestrator.service — no shared state)
sudo cp api-bot.service /etc/systemd/system/
sudo sed -i "s/USER/$(whoami)/g" /etc/systemd/system/api-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now api-bot
journalctl -u api-bot -f
```

## Config (`.env`)

| var | meaning |
|-----|---------|
| `NEXT_API_BASE` | dashboard URL |
| `MACHINE_TOKEN` | **required** — dashboard `SOCKET_INTERNAL_SECRET` |
| `CAPSOLVER_API_KEY` / `ANTICAPTCHA_API_KEYS` | reCAPTCHA Enterprise solvers (CapSolver first, comma-separated Anti-Captcha keys as fallback) |
| `CONCURRENCY` | accounts processed in parallel per process (default 5) |
| `MAX_RETRIES_PER_ACCOUNT` | per-account attempt budget (default 3) |
| `POLL_INTERVAL` | main-loop cadence, seconds (default 5) |
| `POSTO_ID` | fallback only — live value comes from `/api/settings` |
| `COUNTRY_OF_RESIDENCE` / `NATIONALITY` / `DURATION_OF_STAY_DAYS` / `LANGUAGE` | booking-target constants |

## Logs — realtime status recognition

- `logs/api_bot.log` — rotating (10MB × 10), DEBUG+, everything: full request/response
  headers and bodies at every step (login, questionnaire, Formulario, slots, booking, PDF),
  not just pass/fail summaries.
- `logs/users/<username>.log` — the same content, filtered to one account — easiest way to
  trace a single run start to finish.
- **`[STATUS] <username> | stage=... | event=... | detail=...`** — every single thing this
  bot reports to the dashboard (`POST /api/account/event`) is ALSO logged locally in this
  exact uniform shape, at the moment it happens, regardless of whether the dashboard is
  reachable. `tail -f logs/api_bot.log | grep '\[STATUS\]'` (or just watch the console) gives
  a live, per-account status feed independent of the dashboard UI.
- **Live status table** — the main loop prints a compact snapshot of every in-flight
  account's current stage once per `POLL_INTERVAL`, e.g.:
  ```
  [Cycle 42] 3 account(s) in flight:
    emmilypa83949      | APPLYING    | quest_next_chain_step        |   1.2s ago | id=16 status=200
    josecarl76291      | POLLING     | captcha_solving               |   8.7s ago | schedule
    maria70656         | LOGGING_IN  | login_submitted               |   3.1s ago |
  ```
- `PDFs/` — downloaded booking confirmations.
- `debug_html/` — raw HTML saved on unexpected responses, for post-mortem.

## Dashboard endpoints it uses (all guarded by `x-socket-secret`)

- `GET  /api/settings` → `{ enabled, postoId, ... }`
- `GET  /api/account/claim` → `{ account: {id, loginUsername, loginPassword, firstName, lastName, passportNo, email} | null }`
- `GET  /api/proxy/unassigned` → `{ proxy: {id, ip, port, username, password, method, label} | null }`
- `POST /api/account/event` `{accountId, event, detail?, stage?, proxyId?}`
- `POST /api/account/status` `{accountId, workStatus}`
- `POST /api/proxy/health` `{proxyId, healthy, detail?}`

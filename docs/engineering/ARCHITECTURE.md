# Architecture — as implemented

Originally reconstructed 2026-08-25 from the code at `dc133fb` (branch `apply-revert-proven`).
**Reconciled 2026-10-02** against branch `request-manual-review` (base `pt-version`, commit
`81d67a2`): line counts, the removed booking breaker, the PT in-page XHR apply path, and the
subsystems added since August are corrected below, with a dated roll-up in §12. Where the
implementation differs from what a docstring or plan claims, the difference is called out rather
than smoothed over.

## 1. What the system does

storm-hub books appointments on the Portuguese e-visa portal
(`pedidodevistos.mne.gov.pt/VistosOnline`). An operator defines a *booking*, attaches
*identities* (each owning several portal *sub-accounts*), and the Hub launches **one OS
process per identity**. Each worker logs into the portal in a real browser, warms a
server-side application form, and then either scans for open appointment slots (**scout**)
or waits to be handed one and submits it (**apply**).

Predecessor: `/srv/projects/storm-auto` (113 confirmed bookings). storm-hub is a rewrite
that keeps storm-auto's browser engine vendored verbatim and rebuilds the control plane.

## 2. Components

```
apps/hub/        FastAPI control plane (94 routes) — owns ALL database access
  main.py        REST + SPA host + WebSocket    4111 lines
  scheduler.py   background tick thread          1352 lines
  events.py      Redis evt consumer + WS fanout   847 lines
  supervisor.py  process spawn/reap by PID        284 lines
  auth.py        JWT, roles, single-operator seat
apps/worker/     one process per identity — NO database access at all
  __main__.py    entrypoint; engine selection sim|live from the launch payload
  ops_live.py    LiveEngineOps — the portal implementation   4136 lines
  ops_sim.py     SimEngineOps — same Protocol, no browser
  runtime.py     emits started/status_change/terminal; triggers checkpoints
  bus_signals.py worker side of the command stream
apps/registrar/  sub-account registration runs (separate process kind)
apps/dashboard/  React + Vite SPA, single 2856-line App.jsx
libs/domain/     PURE: enums, state machines, settings catalog + resolver, rules
libs/engine/     phases.py (PhaseMachine + EngineOps Protocol), payload, discovery,
                 pool_manager, checkpoint, orchestration, portal_health
libs/engine/vendor/  storm-auto's Playwright engine, vendored, ~2911 lines — NOT modified
libs/bus/        Redis streams (cmd/evt), pub/sub signals, semaphore, portal_liveness,
                 token_pool (pre-solved login tokens)  [booking breaker removed 2026-09-23, D-portal-liveness]
libs/proxies/    lease.py (pool selection + leasing), health, exit_ip, credentials (Phase 3)
libs/db/         SQLAlchemy models (24) + Alembic migrations
```

**Process boundary (CONFIRMED, architectural):** workers never touch Postgres. Everything a
worker needs travels in its `launch` command payload; everything it reports travels as a bus
event. The Hub is the only DB client. This is the single most important structural property
of the system — it is what makes a worker crash survivable and a worker cheap to relaunch.

## 3. Runtime flow

### Hub startup (`main.py` lifespan)
1. SQLAlchemy engine, explicit pool (`pool_size=10, max_overflow=20, pool_pre_ping=True`).
2. `supervisor.reconcile({worker_id: "apps.worker"})` over every DB-`RUNNING` worker —
   re-discovers processes a *previous* Hub instance spawned by scanning `/proc` for the exact
   `python -m apps.worker <id>` cmdline. Without this a Hub restart made every live worker
   unstoppable and falsely dead (CONFIRMED bug, fixed).
3. `EventHub.start()` — background thread tailing the shared `evt` stream.
4. `Scheduler.start()` — background tick thread.

### Booking start (`POST /bookings/{id}/start`)
Every start is a **fresh run** (DECISION, operator, 2026-08-14): it clears the
slot-open/slot-found latch, retires the previous run's slots, and re-warms. It deliberately
does not resume into RUNNING.

### Worker lifecycle (`libs/engine/phases.py: PhaseMachine`)
```
launch cmd ──▶ login ──▶ warmup ──▶ [swap to ISP proxy] ──▶ hold
                                                             │
   scout:  hold ─▶ await_slot_open ─▶ await_discover_task ─▶ do_discover
                    ─▶ push_slots ─▶ report_dates ─▶ poll_gap ─▶ (loop)
   apply:  hold ─▶ await_assignment ─▶ do_apply ─▶ booked | bounced ─▶ hold
           fallback: if all scouts invalid ─▶ self-discover ─▶ apply
```
Browser logic is **identical** for both roles; only phase sequencing differs. `MONO_BOOKING`
mode has apply workers self-discover and book.

Terminal outcomes map to worker status in `events.py:_TERMINAL_STATUS`. `held` is **not** a
failure — it means the machine ran to a clean end without booking (no slot assigned, or the
apply bounced). It was mapped to FAILED for the whole reconstruction, which showed healthy
workers red and burned their respawn budget (CONFIRMED bug, fixed).

### Scheduler tick (every few seconds, `scheduler.py:_tick_once`)
Booking timeout · hold-respawn with backoff and a hard cap · dead-process reap · stall reap ·
**runaway-event-rate reap** · self-scout fallback · signal latch · scout task assignment ·
slot assignment · false-slot-open revert · stale-lease reclaim · orphan stream sweep.

Each mechanism **recomputes its view from DB + Redis every tick** rather than trusting
in-process state, so a Hub restart needs no manual reconciliation. The pure decision
functions (`plan_booking_actions`, `plan_slot_assignments`, `should_timeout_booking`) are
unit-testable without a DB or clock.

## 4. Communication

| Channel | Direction | Transport | Delivery | Bounded |
|---|---|---|---|---|
| `cmd:{worker_id}` | Hub → worker | Redis stream + consumer group | at-least-once, acked | `maxlen~200` |
| `evt` | worker → Hub | Redis stream, shared | at-least-once | `maxlen~5000` |
| `signal:{booking_id}` | Hub → all workers | Redis pub/sub | **fire-and-forget, no durability** | n/a |
| `/ws/events` | Hub → dashboard | WebSocket | best-effort | n/a |

**Pub/sub signals have no delivery guarantee** — a worker not subscribed at publish time
never sees the message. The Hub compensates with a durable latch
(`notified:{booking_id}:{signal}`, a Redis set) marking each worker **only after the send
returns successfully** (confirm-before-mark). That latch must be cleared on a worker's own
restart as well as on a fresh booking start, or a relaunched worker is starved of a signal
its predecessor consumed (CONFIRMED bug, fixed via `_reset_worker_bus_state`).

Command dedup: `Bus.has_pending_commands()` prevents re-sending while one is outstanding —
the general fix for a worker that once accrued 56 unconsumed `assign` commands.

## 5. Session lifecycles and TTLs — FIVE independent lifetimes

**Corrected 2026-08-25.** These were previously treated as one thing. They are five, and none
guarantees another. See `CONFIRMED_FACTS.md` §18.

| # | Lifetime | Known value / range | Evidence | What refreshes it | What ends it |
|---|---|---|---|---|---|
| 1 | **Portal auth session** (`Vistos_sid`) | **highly variable: 33-45 min observed once (§24), ~180 min observed on 3/3 sessions (§27)** | MEASURED twice, 12 h apart, contradicting | **UNKNOWN** — HOME_URL and an auth-gated GET performed identically; neither is inadequate | **UNKNOWN** — not a fixed TTL; survives a browser change and an IP change |
| 2 | **Server-side form/draft state** | ~90-120 s | **HYPOTHESIS** — storm-auto's figure, never measured here | any authed GET on that `Vistos_sid` (storm-auto's claim) | idle time; **not** an IP change (§22) |
| 3 | **Proxy session** (session id) | soax **60.2-63.4 min** (20/20); webshare no ceiling observed | MEASURED, Q1 (§17) | nothing — a hard ceiling on soax | provider policy (`sessionlength`) |
| 4 | **Proxy exit / IP** | webshare **0.278 changes/session-h** (CI 0.159-0.451); soax bounded by #3 | MEASURED, Q1 (§17) | **nothing — traffic has no effect** | provider-side, independent of our activity |
| 5 | **Browser session** | process lifetime | CONFIRMED by construction | — | process exit, crash, `close_browser` |
| — | Checkpoint file | until swept | code | overwritten at HOLDING/POLLING | `cleanup_stale` — which has no production caller |

**These are five independent lifetimes. None guarantees another.** The two that now dominate the
architecture are #1 (shortest, and nothing we know refreshes it) and #4 (uncontrollable, but
**harmless to the session** per §22).

**Ordering is NOT stable.** On the night of 2026-08-26 lifetime #1 (33-45 min) was shorter than #3
on soax (60 min), which is what invalidated Q19's vehicle (§25). The next morning #1 exceeded 180
min on 3/3 sessions (§27), which reverses the ordering. **Treat #1 as variable, not as a constant** —
this is the single most important open item in the table.

**The invalidated assumption:** *"keeping the session alive also keeps the proxy exit stable."*
Measured false (§17). Keepalive acts on lifetimes 1 and 2; it has no measurable effect on 4 and
cannot extend 3.

### Where IP continuity is actually required

| operation | IP continuity | evidence |
|---|---|---|
| reCAPTCHA solve → login submit | **REQUIRED** | 554 `Por favor, confirma o reCAPTCHA` rejections correlating with logged exit moves; two burn a sub-account |
| Authenticated session (`Vistos_sid`) across an IP change | **NOT REQUIRED** | **VERIFIED HERE 2026-08-25, 3/3** (`CONFIRMED_FACTS.md` §22): survives a fresh browser on a different exit |
| Form-state survival across an IP change | **NOT REQUIRED** | same 3/3 runs: all five scheduling markers present after migration |
| Discovery (`gettime`, `getPeriodosOcupados`) | **UNKNOWN** | never isolated from other failure causes |
| Apply (`ScheduleController` → `SubmeterVistoCriaPDF`) | **UNKNOWN** | never isolated |
| Keepalive nav | **not required** | plain authed GET |

The second row is the load-bearing one and it rests on a *predecessor's* claim, not on our own
measurement. It is the target of the smallest next experiment.

### The discovery/apply boundary

`PhaseMachine` runs login → warmup → swap → hold → discover → apply **inside one process, one
browser, one portal session**, and — because `do_swap` is off — **one proxy and one exit**.

Coupling audit:

| coupling | required by | evidence |
|---|---|---|
| same worker process | our implementation | `LiveEngineOps` holds `self.session`, `self.posto_pdf`, `self.account` as instance state |
| same browser | our implementation | in-page fetches and `browser_eval` read live page state |
| same portal session | **the portal** | form state is server-side, keyed to `Vistos_sid` |
| same proxy session | our implementation | one `conn_url` per worker for its whole life |
| same **exit IP** | **NOT required by the portal** | Q16, 3/3 — the coupling is ours, not the portal's |

`do_swap` exists precisely to break the last two couplings, and its very existence is an
assertion that they are *not* portal requirements. It has never run here.

## 6. Browser layer

Vendored from storm-auto and **not modified** — Playwright Chromium, driven through a
**threaded command queue**: the caller pushes a command and blocks on a result queue.

The critical structural rule, learned the hard way (CONFIRMED):

- `browser_form_post` / `browser_nav` = a **navigation** → **origin-independent**
- `browser_fetch` / `browser_eval`+fetch = an **in-page fetch** → **origin-DEPENDENT**

An in-page fetch inherits the current page's origin. If a navigation failed and left the page
on `chrome-error://chromewebdata/` (an *opaque* origin), **every** subsequent in-page fetch
throws `TypeError: Failed to fetch` — forever, for the life of that page. Login, warmup and
discovery self-heal because each *begins* with a navigation.

**Updated 2026-10-02 (pt-version).** The confirmed apply path is now the PT in-page **XHR submit**
to `SubmeterVistoCriaPDF` (`lang=PT`, matching a real booking HAR), not the old ENG nav+nav round —
and it books (a 240 KB `%PDF` captured, `CONFIRMED_FACTS.md`; the ENG nav+nav round remains proven
on the `apply-revert-proven` line). Because the submit is an in-page fetch, it is origin-dependent,
so do_apply re-warms (a navigation) immediately before it — a short gap, no CAPTCHA solve — and an
off-origin submit throws → "bounced" → the next round re-warms. That is the correct recovery: apply
needs the full Schedule.jsp **cursor**, not merely the origin, so a cheap re-anchor would be wrong.

Two stages that DO pair an in-page fetch with a long (~15-25 s) CAPTCHA-solve gap — where the page
can slip off-origin *during* the solve while the exit stays alive — now **re-anchor** on that
specific failure: the **registrar** register POST re-navs `Authentication.jsp` before POSTing
(fixed 2026-10-01), and **slots discovery** re-navs the slots page once and re-delivers the same
still-valid token rather than discarding a paid solve (fixed 2026-10-02). Login is immune: it
submits via a real DOM `form.submit()` — a navigation — so its own solve→submit gap cannot strand
it. See §12 and `FAILURE_MODES.md`.

Stealth/anti-detection is env-driven (`STEALTH_HARDEN`, `DETERMINISTIC_FP`, `HUMAN_BEHAVIOR`,
`WEBRTC_BLOCK`), read into module constants **at import time** — so `vendor_env.apply()` must
run *before* the vendor module is first imported. That is why `ops_live` is imported
function-locally in `apps/worker/__main__.py`. A Hub-side prober that skipped this condemned
639 proxies with anti-detection off (CONFIRMED bug, fixed).

Stale-result protection: `_StampedResultQueue` tags each reply with its command's sequence
number. Without it, a caller that timed out while the worker thread was still running would
receive that reply on its *next, unrelated* call — silent state corruption. This is why portal
slowness caused damage out of proportion to the delay.

Process hygiene: `_kill_proc_tree` on close; `Supervisor.kill_tree` SIGTERMs the tree then
SIGKILLs survivors after a grace period. Precedent: 2600+ leaked Chromium processes in
storm-auto.

## 7. Concurrency

| Shared resource | Owner | Protection |
|---|---|---|
| Proxy row capacity | DB | `FOR UPDATE SKIP LOCKED` on the winning candidate only; probe runs *outside* any transaction |
| Slot capacity | DB | per-slot active-lease count vs `resolve_slot_capacity` |
| One lease per worker | DB | inline check in `_assign_slots` (see debt below) |
| Login concurrency | Redis | `RedisSemaphore`, atomic Lua prune+check+add |
| Apply concurrency | Redis | same |
| ~~Portal-down booking breaker~~ | — | **Removed 2026-09-23** — it froze the fleet, oscillated and starved discovery. Replaced by a **record-only** `portal_liveness` badge (shown to the operator) + per-worker maintenance backoff; it never gates the fleet. |
| Operator seat | DB | partial unique index `uq_single_active_operate` |
| Identity exclusivity | DB | partial unique index `uq_identity_active_allocation` |

Lease selection is LRU-ordered within health tiers, shuffled inside a bounded window to
decorrelate concurrent leasers. A **lost claim race is charged separately from a failed health
probe** — conflating them made contention look like pool exhaustion (CONFIRMED, 4 of 9 workers
stuck at READY naming healthy candidates).

## 8. Observability

**Present:** `/ops/status` (Redis memory + `evt` length + disk + worker counts); per-worker
append-only logs under `var/worker-logs/` with a dated launch marker; `/audit`; identity event
history; `/workers/{id}/logs`; live WS event stream; per-worker event-rate tracking feeding an
automatic runaway kill.

**Absent (gaps):**
- No structured logging and no correlation id spanning run → worker → operation → request.
- No metrics/alerting — `/ops/status` must be polled by something, and nothing polls it.
- No retention on `var/worker-logs/` (21 MB, 224 files) beyond a manual script.
- Proxy identity in logs is the **session id** (`proxy=p.webshare.io:80#51`); before
  2026-08-25 it was `host:port`, identical for every webshare credential — so every worker in
  every run logged the same string while claiming to support failure attribution.

## 9. Deployment

Single host. `storm-hub.service` (systemd) → uvicorn → FastAPI; Postgres; Redis
(`maxmemory 2 GB`, policy `noeviction` — loud failure over silent data loss);
`storm-hub-backup.timer` for `pg_dump`; Caddy reverse proxy; fail2ban on the auth endpoint.
Workers are spawned by the Hub with `start_new_session=True`, so they survive a Hub restart.

## 10. Known technical debt

- `pool_manager.enforce_single_lease` is defined and unit-tested but **called from nowhere**;
  `_assign_slots` implements the invariant inline. Dead code that reads as live.
- `checkpoint.cleanup_stale` has **no production caller** — tested and probed only.
- `apps/hub/main.py` is 2773 lines and `App.jsx` is 2856; both mix concerns.
- `libs/engine/orchestration.py` (134 lines) overlaps `scheduler.py`'s inline logic.
- ~~`HOLD_SWAP_AFTER_FOUND` is retained as a deprecated alias~~ — **removed 2026-08-26 (Phase 6,
  `DECISIONS.md` D35)**, not merely renamed. It was never a legitimate second spelling; it named a
  trigger point the code never had, and its continued existence let the Hub-side launch/rotation
  gates drift onto reading it specifically while the worker's own `EngineConfig` read both spellings
  OR'd. `HOLD_SWAP_AFTER_WARMUP` is now the sole recognized key.
- The dashboard duplicates the worker action-legality table (`WORKER_ACTIONS` in `App.jsx`)
  that `state_machines.py:WORKER_ACTION_STATUSES` owns — two copies that can drift.

## 11. Session-state model and recovery — DESIGN, PARTIALLY IMPLEMENTED (Phase 1, 2026-08-26)

Written 2026-08-26 after Q22, as the target the (then-unbuilt) implementation would be measured
against in `FAILURE_MODES.md` F23-F25. **§11.1's classifier and most of §11.2's recovery machine
are now implemented** (`apps/worker/ops_live.py`'s `SessionState`/`classify_session_state`,
Phase 1, same day) — see §11.3 for exactly what matches this design, what diverges, and what
remains undone. The design below is preserved as originally written; §11.3 is the reconciliation.

### 11.1 The five states

Classification must be **positive-evidence based**. Each state names the evidence that establishes
it; anything else is UNKNOWN.

| State | Established ONLY by | Never inferred from |
|---|---|---|
| `SESSION_AUTHENTICATED` | an auth-gated marker in the body: `consultar pedidos`, or the `Terminar Sessão` logout link | nav success, HTTP 200, body size, presence of `solicitar pedido de visto` |
| `SESSION_EXPIRED` | `sessionLost.jsp` in the final URL; **or** a redirect to `Authentication.jsp`; **or** a recognised portal page that positively lacks the auth-gated marker (the 58,266 B home / 28,717 B `Pedidos`) | absence of an expected marker on an *unrecognised* page |
| `PROXY_TRANSPORT_FAILURE` | the navigation raised: `ERR_TUNNEL_CONNECTION_FAILED`, `ERR_CONNECTION_CLOSED`, `ERR_TIMED_OUT`, `ERR_PROXY_*` | any portal-served page, whatever it contains |
| `PORTAL_ERROR` | `_PORTAL_BUSY_MARKERS` ("não foi possível de momento…"); `_ERROR_KEYWORDS`; a DataDome challenge body | a page we merely do not recognise |
| `UNKNOWN` | **the default.** Anything not positively matched above | — |

**Validated marker table** (measured 2026-08-26, authenticated vs cookie-less browser, same URL):

| marker | authenticated | logged out | usable |
|---|---|---|---|
| `consultar pedidos` | present | **absent** | **yes — gated** |
| `Terminar Sessão` | present | **absent** | **yes — gated** |
| `iniciar sess` | present | present | **NO — on both pages** |
| `solicitar pedido de visto` | present | present | **NO — public menu item** |
| body size | 59,244 / 59,689 B | 58,266 / 28,717 B | **NO — use as corroboration only** |

The last two rows are exactly the signals that produced this project's two classifier failures.

### 11.2 Minimal recovery machine

Four transitions added to the existing hold loop. No new worker statuses, no new substates — this
rides on `WorkerSubstate.HOLDING` and the existing `_rebuild_session` ladder.

```
                    ┌──────────────────────────────────────────┐
  READY → WARM → HOLD ──(SESSION_AUTHENTICATED)──────────────► stay
                    │
                    ├─(SESSION_EXPIRED)──► REAUTHENTICATE ──► WARM ──► HOLD
                    │                       (existing _fresh_login + _run_warmup_steps)
                    │
                    ├─(PROXY_TRANSPORT_FAILURE)──► roll session id, retry
                    │                              (existing _roll_exit; N strikes → rotate proxy)
                    │
                    ├─(EXIT_CHANGED)────► CONTINUE HOLDING, log only
                    │                     (Q16 3/3 + Q19 n=1 say the session survives)
                    │
                    ├─(PORTAL_ERROR)────► back off, do NOT re-login, do NOT re-warm
                    │
                    └─(UNKNOWN)─────────► back off and RE-PROBE; escalate only on a
                                          repeated, bounded streak — never act on one unknown
```

**Why it is compatible with what exists:** every action is already implemented and already reached
from the hold loop — `_fresh_login`, `_run_warmup_steps`, `_roll_exit`, `_request_proxy_rotation`,
`_park`, `_attempt_keepalive_restore`. What is missing is the **classifier that decides between
them**. Today the hold loop branches on symptom counters (`_keepalive_dead_streak`,
`_rewarm_fail_streak`), not on a state.

**The single highest-value change** this implies is not a new state machine — it is making
`_do_keepalive` classify its own response instead of treating "the navigation returned" as health.

### 11.3 Implementation status (added 2026-08-26, after Phase 1)

**Matches this design:**
- `SessionState(str, Enum)` with `AUTHENTICATED` / `SESSION_EXPIRED` / `PORTAL_ERROR` / `UNKNOWN`,
  and `classify_session_state(page, final_url, *, warmed)`, both in `apps/worker/ops_live.py`.
- `SESSION_EXPIRED` via `sessionLost.jsp`/`Authentication.jsp` in the final URL, checked before any
  page-content marker — closes F22.
- `PORTAL_ERROR` via the existing `_is_portal_busy`, now also consulted inside the keepalive loop —
  closes F25.
- `UNKNOWN` as an explicit, first-class outcome on both the pre-warm and warmed paths, never
  silently promoted to a specific verdict — closes F26 for the keepalive loop specifically (the
  `events.py` `_TERMINAL_STATUS.get()` instance of the same defect class is untouched, tracked
  separately in `FAILURE_MODES.md` F26).
- `EXIT_CHANGED` stays informational-only, exactly as D31 prescribes — `classify_session_state`
  never consults `_note_exit_ip`'s result.
- Recovery actions for `AUTHENTICATED`/`SESSION_EXPIRED`/`PORTAL_ERROR`/`UNKNOWN` match §11.2's
  transitions (stay / reauthenticate via the existing `_attempt_keepalive_restore` chain / back off
  / back off and re-probe, escalating only via the existing bounded `_maybe_escalate_stuck_rewarm`
  streak) — closes F23/F24.

**Diverges from this design, deliberately, as smaller-scope choices:**
- The validated marker table (§11.1) lists **two** usable authenticated-home signals,
  `consultar pedidos` and the `Terminar Sessão` logout link. The implementation wires only
  `consultar pedidos` (via the pre-existing `_HOME_MARKERS` constant). Both are independently
  validated in `CONFIRMED_FACTS.md` §27 and agreed on every probed row there — using one is not
  currently known to be wrong, but the design's redundancy (either marker independently proves the
  state) is not yet built. Low risk, cheap to add later.
- §11.2's `PROXY_TRANSPORT_FAILURE` transition ("roll session id, retry" before escalating) was
  **not implemented**. Phase 1 deliberately left the pre-existing exception-catching branch around
  `browser_nav` untouched (still `_keepalive_dead_streak` → `_attempt_keepalive_restore` — a full
  re-authentication after `KEEPALIVE_DEAD_TRIP` failures, not a cheap in-band session-id roll
  first), per Phase 1's own "smallest clean change" scope boundary. `PROXY_TRANSPORT_FAILURE` is
  therefore not a `SessionState` enum member in the implementation — it remains the pre-existing
  branch, functionally in the same place in the control flow but not a named classifier output.

**Remaining limitation this design already names, still true:** the model assumes detect-and-recover
per the D29/Q22 finding, not a duration budget — consistent, not a gap.

**Future evolution, not scoped to any authorized phase:** wiring the `Terminar Sessão` marker as a
second signal; building the cheaper roll-then-retry ladder for `PROXY_TRANSPORT_FAILURE`; extending
the same classifier/`UNKNOWN` discipline to `events.py`'s `_TERMINAL_STATUS.get()` (F26's other
half).

## 12. Reconciliation — what changed since the 2026-08-25 snapshot (added 2026-10-02)

Everything above §12 was written at `dc133fb` on `apply-revert-proven`. The live line is now
`pt-version` (`request-manual-review` @ `81d67a2`). The structural model (one DB-less worker process
per identity, the phase machine, nav-vs-in-page-fetch, the DB-recomputed tick) is unchanged; these
are the deltas worth knowing before reading the code.

- **PT apply path (the big one).** The confirmed submit is an in-page **XHR** to
  `SubmeterVistoCriaPDF` with `lang=PT`, reproduced from a real booking HAR and verified by a
  captured `%PDF`. The flow skips `Formulario`/`ScheduleController` relative to the ENG round.
  Success ≈ 1.5 KB, `form_stale` ≈ 66 KB (slot taken, **not** a cursor fault), PDF is a separate
  failable step. See §6 and `CONFIRMED_FACTS.md`.

- **Booking breaker removed (2026-09-23).** The consecutive-portal-down breaker was deleted
  outright — it froze the fleet and starved discovery. It is replaced by a **record-only**
  `libs/bus/portal_liveness.py` badge plus per-worker maintenance backoff. The *event-rate* circuit
  breaker (F16, `scheduler.py`/`events.py`) is a DIFFERENT mechanism and stays — it auto-kills a
  single worker emitting an anomalous event rate.

- **Discovery source + rotation.** `DISCOVERY_SOURCE ∈ {calendar, slots, both}` is pushed on each
  `discover` command (Hub-live value, read in `do_discover`). `/slots` is the operator-preferred
  ground truth (`getTables` WAF handshake installed as an init script before the nav). Discovery
  orchestration is a **round-robin, one-scout, full-range rotation** (2026-09-21) — it replaced the
  split/single-range model; `DISCOVERY_MIN_INTERVAL` paces it and six older settings were retired.

- **Cross-booking slot feed.** A booking may list `discovery_feeder_booking_ids` (JSON column,
  migration `cp000`); the scheduler's `_mirror_feeder_slots` copies a disposable *detector*
  booking's finds into a precious *main* booking's pool and fires the main's slot-open — no main
  restart. Guarded by posto-match and CLOSED-slot dedupe.

- **Login token pool (throughput lever, OFF by default).** `libs/bus/token_pool.py` + a Hub factory
  thread solve `LOGIN_EVISA` reCAPTCHA tokens continuously (ProxyLess, so not IP-bound) into a
  perishable Redis list; the worker login loop pops one and passes `pre_token` to the vendored
  `browser_login`, which already accepts it. Enabled only when `LOGIN_TOKEN_POOL_TARGET > 0`.
  Rationale: login throttle is a ~1% global lottery on byte-identical requests, so the lever is
  SHOTS/minute, and the solve (~15-25 s) is the removable cost.

- **Per-roll geo.** An unpinned worker carries the whole `PROXY_GEOS` set and varies its exit
  country on each roll via `reseat_geo` (soax `-country-cc`), instead of freezing one geo for the
  run (the login-throttle worst case). An operator pin still forces a single geo.

- **Scheduler tick made crash-proof.** `supervisor.running_ids()` iterated the `_pids` dict
  unlocked while another thread mutated it → `RuntimeError: dictionary changed size during
  iteration` crashed the WHOLE tick (~1234×/day), which is why signals fired late. Fixed with a
  `threading.RLock` + snapshot, and the tick now isolates faults per-booking (`_drive_booking`) and
  per-maintenance-phase so one booking's error cannot abort the others. `systemd` needs
  `KillMode=process` so a Hub restart does not cgroup-kill every worker.

- **Origin recovery for the two long-solve-gap in-page fetches.** Registrar register POST and slots
  discovery now re-anchor when the page slips off-origin during the solve (§6). This is the
  `opaque_origin` failure class applied to the two paths that were still exposed; login/warmup/
  calendar-discovery/keepalive were already safe (navigation-based or no long gap).

**Current counts (2026-10-02):** 94 Hub routes · 24 DB models · migration head `cp000` · 1306 tests
across 70 files. Line counts in §2 are current as of this commit.

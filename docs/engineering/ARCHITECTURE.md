# Architecture — as implemented, 2026-08-25

Reconstructed by reading the code at `dc133fb`. Where the implementation differs from what
a docstring or plan claims, the difference is called out rather than smoothed over.

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
apps/hub/        FastAPI control plane (73 routes) — owns ALL database access
  main.py        REST + SPA host + WebSocket    2773 lines
  scheduler.py   background tick thread          1055 lines
  events.py      Redis evt consumer + WS fanout   696 lines
  supervisor.py  process spawn/reap by PID        215 lines
  auth.py        JWT, roles, single-operator seat
apps/worker/     one process per identity — NO database access at all
  __main__.py    entrypoint; engine selection sim|live from the launch payload
  ops_live.py    LiveEngineOps — the portal implementation   2269 lines
  ops_sim.py     SimEngineOps — same Protocol, no browser
  runtime.py     emits started/status_change/terminal; triggers checkpoints
  bus_signals.py worker side of the command stream
apps/registrar/  sub-account registration runs (separate process kind)
apps/dashboard/  React + Vite SPA, single 2856-line App.jsx
libs/domain/     PURE: enums, state machines, settings catalog + resolver, rules
libs/engine/     phases.py (PhaseMachine + EngineOps Protocol), payload, discovery,
                 pool_manager, checkpoint, orchestration, portal_health
libs/engine/vendor/  storm-auto's Playwright engine, vendored, ~2742 lines — NOT modified
libs/bus/        Redis streams (cmd/evt), pub/sub signals, semaphore, breaker
libs/proxies/    lease.py (pool selection + leasing), health, exit_ip
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

## 5. Session lifecycle and TTLs

| Session | Created by | Kept alive by | Expiry | Detection | Status |
|---|---|---|---|---|---|
| Portal login (JSESSIONID) | `browser_login` | keepalive nav | UNKNOWN duration | page markers | **UNKNOWN TTL** |
| ScheduleController form state | warmup chain | round-start re-warm | ~90–120 s (inherited from storm-auto) | submit bounces to questionnaire | **HYPOTHESIS — never measured in storm-hub** |
| Webshare sticky exit | first connection | *nothing we control* | see below | `_note_exit_ip` | **CONFIRMED unstable** |
| Browser context | `get_session` | — | process lifetime | — | CONFIRMED |
| Checkpoint file | `save_checkpoint_now` | overwritten at HOLDING/POLLING | `cleanup_stale` | — | **sweep never called** |

The keepalive design assumes traffic preserves the proxy exit. **A measurement on 2026-08-25
contradicts that** — see `CONFIRMED_FACTS.md` §1. This is the most important open structural
risk in the system.

## 6. Browser layer

Vendored from storm-auto and **not modified** — Playwright Chromium, driven through a
**threaded command queue**: the caller pushes a command and blocks on a result queue.

The critical structural rule, learned the hard way (CONFIRMED):

- `browser_form_post` / `browser_nav` = a **navigation** → **origin-independent**
- `browser_fetch` / `browser_eval`+fetch = an **in-page fetch** → **origin-DEPENDENT**

An in-page fetch inherits the current page's origin. If a navigation failed and left the page
on `chrome-error://chromewebdata/` (an *opaque* origin), **every** subsequent in-page fetch
throws `TypeError: Failed to fetch` — forever, for the life of that page. Login, warmup and
discovery self-heal because each *begins* with a navigation. Apply did not, which is why apply
failed in isolation while everything else worked. The proven apply round is navigation +
navigation.

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
| Portal-down | Redis | `RedisBreaker`, consecutive portal-down only |
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
- `HOLD_SWAP_AFTER_FOUND` is retained as a deprecated alias — the name described a trigger
  point that was never the code's.
- The dashboard duplicates the worker action-legality table (`WORKER_ACTIONS` in `App.jsx`)
  that `state_machines.py:WORKER_ACTION_STATUSES` owns — two copies that can drift.

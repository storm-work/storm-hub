# Layer-Based Review Guide — `request-manual-review`

Full-structure review, bottom-up. Each layer only depends on the ones above it, so reviewing in
order means every layer is understood before the layer that calls it.

- Branch under review: **`request-manual-review`** (base: `pt-version`)
- Diff a file/layer: `git diff pt-version...request-manual-review -- <path>`
- Whole-branch stat: `git diff pt-version...request-manual-review --stat`
- Run a layer's tests: `.venv/bin/python -m pytest tests/<dir> -q`

Legend: **★batch** = source file changed in this branch · **★key** = highest-leverage file to read
even if unchanged.

---

## 0 · Read first — architecture & decisions (context, not code)

| File | Purpose |
|---|---|
| `docs/engineering/ARCHITECTURE.md` | how it all fits, as-built |
| `docs/engineering/DECISIONS.md` | D-numbered locked decisions |
| `docs/engineering/CONFIRMED_FACTS.md` | measured facts + retractions |
| `docs/engineering/FAILURE_MODES.md` | F-numbered live failures |
| `docs/engineering/PROXY_CONTRACTS.md` | proxy grammar/invariants (D2) |
| `docs/engineering/OPEN_QUESTIONS.md` | unresolved |
| `docs/engineering/REPORT_2026-10-01_hot_period_reliability.md` | this session's analysis |

---

## 1 · Domain — pure types/rules, no I/O · LOW risk

| File | Role |
|---|---|
| `libs/domain/enums.py` | BookingStatus / WorkerStatus / Substate / Role |
| `libs/domain/settings/catalog.py` **★batch** | EVERY tunable knob — the control surface |
| `libs/domain/settings/resolver.py` | layer resolution GLOBAL<BOOKING<TEMPLATE |
| `libs/domain/settings/__init__.py` | `resolve()` / `validate_layer()` |
| `libs/domain/state_machines.py` | legal booking/worker transitions |
| `libs/domain/rules.py` | business rules |
| `libs/domain/priority.py` | identity priority levels |
| `libs/domain/nationality.py` | nationality → quest-key mapping |
| `libs/domain/blind.py` | blind-apply domain |

**Verify (batch):** new knobs safe-by-default — `LOGIN_TOKEN_POOL_TARGET=0` (OFF),
`KEEPALIVE_MODE` default `auth_only` (L.BOOKING, not template-overridable),
`QUEST_STEP_RETRIES` default corrected 10→3. Tests: `tests/domain/`.

---

## 2 · Persistence — DB schema + migrations · LOW risk

| File | Role |
|---|---|
| `libs/db/models.py` | SQLAlchemy models = the schema |
| `libs/db/base.py` | Base / metadata |
| `apps/hub/db.py` | engine + SessionLocal |
| `migrations/versions/*.py` | Alembic history (newest: `cp000_discovery_feeder.py`) |

**Verify:** no schema change in this batch; confirm `cp000` (discovery_feeder) is the head and the
model matches. Tests: parts of `tests/hub/`.

---

## 3 · Bus — Redis messaging + coordination · MEDIUM risk

| File | Role |
|---|---|
| `libs/bus/bus.py` | event stream + per-worker command streams + signals |
| `libs/bus/names.py` **★batch** | ALL Redis key naming (adds `token_pool_key`) |
| `libs/bus/semaphore.py` | login/apply/provider concurrency gates |
| `libs/bus/portal_liveness.py` | portal-liveness badge |
| `libs/bus/token_pool.py` **★batch (NEW)** | shared pre-solved reCAPTCHA token pool |

**Verify (batch):** `acquire()` = single atomic `lpop` (no double-use); perishability
(`max_age`~100s < ~110s token life); FIFO oldest-first; `fill_pool` solves only the deficit and
tolerates solver failures; `run_token_factory` cost tracks demand + clean stop. Tests:
`tests/bus/test_token_pool.py` (9).

---

## 4 · Proxies — minting/leasing/geo/health · MEDIUM risk (credentials)

| File | Role |
|---|---|
| `libs/proxies/credentials.py` | per-provider credential builders |
| `libs/proxies/rotating.py` **★batch** | mint/roll rotating URLs (adds `reseat_geo`) |
| `libs/proxies/lease.py` | static/ISP leasing + `_inject_geo` |
| `libs/proxies/health.py` | static-proxy health screening |
| `libs/proxies/geos.py` | supported-geo tables (soax/webshare) |
| `libs/proxies/exit_ip.py` | observed exit-IP detection |
| `libs/proxies/providers/base.py` | provider interface |
| `libs/proxies/providers/soax.py` | soax grammar |
| `libs/proxies/providers/webshare.py` | webshare grammar |

**Verify (batch):** `reseat_geo` replaces `-country-xx` (not duplicate), inserts when absent, drops
on `None`, preserves session id + password; webshare/unknown URLs returned unchanged (D2). Tests:
`tests/proxies/test_rotating.py`.

---

## 5 · Engine — role-agnostic control logic · MEDIUM/HIGH risk

| File | Role |
|---|---|
| `libs/engine/phases.py` **★key** | THE worker phase machine (login→keep→warm→discover/apply) |
| `libs/engine/config.py` | EngineConfig: resolved settings → typed cfg |
| `libs/engine/payload.py` | booking / ScheduleController payload shapes |
| `libs/engine/orchestration.py` | scout round-robin rotation |
| `libs/engine/pool_manager.py` | slot pool lease/assign |
| `libs/engine/portal_health.py` | `classify_session_state` |
| `libs/engine/checkpoint.py` | session checkpoint save/restore |
| `libs/engine/stealth.py` | anti-detect init-script generator |
| `libs/engine/vendor_env.py` | export cfg → vendor os.environ knobs |
| `libs/engine/discovery/gettime.py` | special-days parsing |
| `libs/engine/discovery/periodos.py` | getPeriodosOcupados parsing |

**Verify:** `phases.py` not changed in this batch, but it is the contract `ops_live` implements —
read it to review the worker layer correctly. Tests: `tests/engine/`.

---

## 5b · Vendored engine — LOCKED cornerstone (review, do NOT refactor)

| File | Role |
|---|---|
| `libs/engine/vendor/session.py` **★key** | browser/login engine (command-queue, `pre_token`) |
| `libs/engine/vendor/solver.py` | captcha solve (`race_all`, LOGIN_EVISA) |
| `libs/engine/vendor/email_pool.py` | mail.tm pool (registration) |
| `libs/engine/vendor/proxy_bridge.py` | primp/browser proxy bridge |

**Verify:** unchanged this batch. Read `session.py:1808` (`pre_token` injection) to confirm the
token-pool consumer in `ops_live` matches the engine's existing contract. Operator rule: changes go
AROUND this, not inside.

---

## 6 · Worker — the per-identity process · HIGH risk (near login cornerstone)

| File | Role |
|---|---|
| `apps/worker/__main__.py` **★batch** | entrypoint: launch payload → build ops |
| `apps/worker/ops_live.py` **★batch ★key** | LiveEngineOps: do_login/warmup/discover/apply |
| `apps/worker/ops_sim.py` | SimEngineOps (test double for phases) |
| `apps/worker/bus_signals.py` | consume signals/commands (slot_open/assign/discover) |
| `apps/worker/runtime.py` | WorkerRuntime: drives the phase machine |
| `apps/registrar/__main__.py` | registration-run process (account creation) |

**Verify (batch):**
- Per-roll geo (`_roll_exit`): geo changes ONLY on a fresh roll, no-op when `self._proxy_geos`
  empty, import guarded.
- Token consumer (login loop): best-effort `acquire()`, `self._redis` None-safe, empty holder →
  inline-solve fallback (unchanged). Purely additive; never blocks/fails a login.
- `proxy_geos` read in `__main__._build_ops`; launch-payload seam test still green.
- Slots re-anchor (`_discover_via_slots`): on `onCaptchaSuccess not defined` AFTER a page that was
  ready pre-solve (the page slipped off-origin DURING the ~15-25s solve — the registrar's mode),
  re-nav the slots page ONCE and re-deliver the SAME still-valid token instead of discarding a paid
  solve; falls through to the existing failed/roll path if the callback does not return. Additive,
  only on that err branch. Tests: `test_slots_reanchors_and_redelivers_when_callback_slips_mid_solve`
  + the un-recoverable case.
Tests: `tests/worker/test_ops_live.py`, `tests/worker/test_main_engine_selection.py`.

---

## 7 · Control plane — Hub (orchestration + API) · HIGH risk

| File | Role |
|---|---|
| `apps/hub/main.py` **★batch ★key** | FastAPI app, all endpoints, lifespan, launch |
| `apps/hub/scheduler.py` **★batch ★key** | the tick: timed transitions/assign/respawn |
| `apps/hub/supervisor.py` **★batch ★key** | PID process tracking |
| `apps/hub/events.py` | event-stream consumer → DB + WebSocket |
| `apps/hub/schemas.py` | API request/response models |
| `apps/hub/auth.py` | Principal / operate-seat auth |
| `apps/hub/audit.py` | background audit writer |
| `apps/hub/settings_store.py` | global/booking settings persistence |
| `apps/hub/config_store.py` | form_defaults/quest_steps store |
| `apps/hub/config.py` | app config (env) |
| `apps/hub/importers.py` | identity/sub-account import |
| `apps/hub/genaccounts.py` | account generation helper |

**Verify (batch), in order:**
1. `supervisor.py` — RLock guards EVERY `_pids`/`_procs` access; `running_ids` snapshots under lock.
2. `scheduler.py` — `_drive_booking` is a faithful extraction (`continue`→`return` the only
   control-flow change); per-booking + per-maintenance try/except; latency/lateness logs.
3. `main.py` — token factory OFF by default + clean shutdown; `_resolve_and_freeze_worker_geo` now
   pin-only; mint uses random `_init_geo`; launch ships `proxy_geos`; `_cmd_log` best-effort;
   `GET /bookings/{id}/logs`.
Tests: `tests/hub/test_supervisor.py`, `test_scheduler_resilience.py`, `test_live_launch.py`,
`test_scheduler.py`, `test_two_bookings_concurrent.py`.

---

## 8 · UI — dashboard (React SPA) · committed earlier on `pt-version`

| File | Role |
|---|---|
| `apps/dashboard/src/App.jsx` | the whole SPA (Command-log tab, feeder multi-select, feed status) |
| `apps/dashboard/src/api.js` | fetch wrapper |
| `apps/dashboard/src/main.jsx` | mount |
| `apps/dashboard/src/styles.css` | styles |

**Verify:** not in this branch's diff — `git log --oneline pt-version -- apps/dashboard`.

---

## 9 · Config data

| File | Role |
|---|---|
| `config/form_defaults.json` | static form fields (lang=PT) |
| `config/quest_steps.json` | per-nationality questionnaire steps |

---

## Tests — mirror the layers

| Dir | Layer |
|---|---|
| `tests/domain/` | 0–1 |
| `tests/bus/` | 3 |
| `tests/proxies/` | 4 |
| `tests/engine/` | 5 |
| `tests/worker/` | 6 |
| `tests/hub/` | 2, 7 |

Run everything: `.venv/bin/python -m pytest tests/domain tests/bus tests/proxies tests/engine tests/worker tests/hub -q`

---

## Two things to scrutinise hardest
1. **`scheduler._drive_booking` extraction** — read side-by-side with the old inline loop; a stray
   `continue`→`return` slip is the only way behavior changed.
2. **`ops_live` login path** — near the locked login flow; confirm the token consumer and per-roll
   geo are purely additive and fall back to prior behavior when disabled/empty.

Everything risky is OFF or no-op by default (token pool target 0, geo lists unset, keepalive
`auth_only`), so the branch is safe to merge/deploy before enabling any lever.

# Failure Modes

Fixed schema per entry. Classification matters more than count: five infrastructure failures
and five account rejections mean different things and must not share a budget.

**Classes:** INFRA (transport, browser, host) · SERVER (portal responses) ·
APP (our logic/state) · OPS (configuration, deployment, human action).

---

## F1 — Our exit IP moves underneath a live session
- **Class:** INFRA
- **Cause:** provider-side. A sticky exit can move **while actively in use**, inside the idle
  window (`CONFIRMED_FACTS.md` §1). Nothing we control prevents it.
- **Detection:** `_note_exit_ip` via a cross-origin in-page fetch to an IP echo. Presents as
  three different portal symptoms — reCAPTCHA rejection, Schedule.jsp losing its markers, a
  refused submit.
- **Current behaviour:** the reCAPTCHA-verdict path checks the exit and, if it moved, rolls
  the session id and retries **without** charging `attempt` or `captcha_fails`.
- **Recovery:** roll the session id; re-warm.
- **Permanent condition:** `LOGIN_IP_MISMATCH_RETRIES` (default 2) exhausted.
- **Observability:** logged with the session id.
- **Improvement:** the hold design still assumes traffic preserves the exit. It does not.
  Holding should be re-examined as "expect to be moved" rather than "stay pinned".

## F2 — Retrying a dead sticky exit
- **Class:** INFRA
- **Cause:** the transient budget retried the same dead tunnel up to six times.
- **Detection:** `ERR_TUNNEL_CONNECTION_FAILED` / `ERR_CONNECTION_CLOSED` / `ERR_TIMED_OUT`.
- **Current behaviour:** roll the session id first, then retry. **Fixed** — 25% → 83%
  reachable.
- **Improvement:** if 83% is not enough, the lever is Webshare **static/ISP** proxies, not
  more retry logic.

## F3 — Opaque origin permanently disables in-page fetches
- **Class:** APP
- **Cause:** a failed navigation leaves the page on `chrome-error://chromewebdata/`; every
  later in-page fetch inherits that origin and throws forever.
- **Detection:** `_on_portal_origin()`.
- **Current behaviour:** `_ensure_portal_origin(why)` re-anchors by navigation.
- **Why it hit apply only:** login/warmup/discovery each *begin* with a navigation.

## F4 — Stale result consumed by the next, unrelated call
- **Class:** APP
- **Cause:** the vendored session is a threaded command queue; some methods retry internally
  longer than the caller waits, so an abandoned call's reply is handed to whoever asks next.
- **Detection:** previously none — it presented as inexplicable JS/state errors.
- **Current behaviour:** `_StampedResultQueue` tags every reply with its command's sequence
  number.
- **Note:** this is why portal *slowness* caused corruption rather than just delay. Caller
  timeouts must keep margin above the callee's internal worst case.

## F5 — Proxy leases leak
- **Class:** APP
- **Cause:** every release path was edge-triggered (terminal event, reap, explicit stop). There
  was **no level-triggered sweep** asking whether an active lease belongs to a live worker.
- **Detection:** none automatic. Found by querying the DB during a 2026-08-25 audit.
- **Historical state:** 107 orphaned leases, median age 101.8 h, max 271.8 h, 0 running workers.
- **Severity:** was 0.3% of a 32,600-row pool — low. Mechanism was unbounded, and had been
  severe at 1,238 rows.
- **Root cause, precisely traced 2026-08-26 (Phase 7):** two distinct code paths, both producing
  exactly the "0 running workers" signature. (1) `apps/hub/events.py`'s `_release_on_terminal`
  runs inside a `_step()` wrapper that catches and logs any exception from
  `release_worker_resources`, with no retry — a transient failure there permanently orphans the
  lease even though the worker reached its correct terminal status. (2) `apps/hub/main.py`'s
  `_do_launch_worker` sets a worker `FAILED` directly if `supervisor.spawn`/command-group
  creation/`send_command` raises *after* a proxy lease already succeeded, without releasing it —
  that worker never touches `RUNNING`, so it is invisible to all three of the scheduler's
  RUNNING-only reap paths forever.
- **Fixed 2026-08-26 (Phase 8, `DECISIONS.md` D-NEW-2 / D37):** `apps/hub/scheduler.py`'s
  `_reconcile_orphaned_leases`, a periodic reconciling sweep in the scheduler tick — exactly the
  shape this entry originally recommended (level-triggered, idempotent, reusing
  `release_worker_resources`) rather than another edge-triggered call site. Orphan predicate:
  `ProxyLease.released_at IS NULL AND Worker.status != RUNNING AND ProxyLease.leased_at <
  now - 300s` (operator-chosen grace period). No new persisted state — `leased_at`,
  `released_at`, and `Worker.status` already existed.

## F6 — Unbounded write-amplifying loop
- **Class:** OPS → APP
- **Cause:** a throttle setting resolving to 0 (`HOLD_POLL_GAP: 0.0`); ~740,000 rows in 66
  minutes, surviving two relaunch attempts.
- **Defences, layered:** `BelowMinimum` raises at resolution; call sites clamp with
  `max(cfg.poll_gap, MIN_POLL_GAP)`; `maxlen~` on every `xadd`; per-worker event-rate breaker
  auto-kills a runaway; `maxmemory` with `noeviction`.
- **Residual gap:** `REDIS_STREAM_MAXLEN` / `REDIS_EVENT_STREAM_MAXLEN` are catalog settings
  that **nothing reads** — the caps are hardcoded. The defence works; the knob is a lie.

## F7 — Unstoppable / falsely-dead workers after a Hub restart
- **Class:** APP
- **Cause:** `Popen`-only tracking; a fresh `Supervisor()` had no handle for pre-restart
  workers. Stop silently no-oped; `running_ids()` reported them dead, so the reaper marked
  healthy holding workers FAILED and released their leases.
- **Current behaviour:** PID tracking + `reconcile()` scanning `/proc` at startup. **Fixed.**

## F8 — Stale command stream poisons the next launch
- **Class:** APP
- **Cause:** stop never cleared the worker's command stream, so a stale `stop` sitting ahead
  of the next `launch` made `wait_launch()` return immediately.
- **Current behaviour:** `_reset_worker_bus_state` clears the stream and the latch. **Fixed.**

## F9 — Signal latch survives a worker's own restart
- **Class:** APP
- **Cause:** the notified-set correctly survives a *Hub* restart and wrongly also survived a
  *worker* restart, starving relaunched workers forever.
- **Current behaviour:** cleared by the same `_reset_worker_bus_state`. **Fixed.**

## F10 — Unbounded slot leases per worker
- **Class:** APP
- **Cause:** a HOLDING worker with no pending command was re-assignable on **every 5 s tick**,
  and nothing checked whether it already held a lease. One worker accumulated 120 active leases
  across the entire pool; every slot then read LEASED, so nobody could be assigned and the
  booking deadlocked.
- **Current behaviour:** inline `already_leased` exclusion in `_assign_slots`. **Fixed.**
- **Debt:** the pure helper `enforce_single_lease` exists, is unit-tested, and is **called from
  nowhere** — the invariant is enforced inline instead.

## F11 — Duplicate unacked commands
- **Class:** APP
- **Cause:** the scheduler re-sent `assign` every tick without checking for an outstanding one;
  56 unconsumed commands and 60 open leases on a single worker.
- **Current behaviour:** `Bus.has_pending_commands()` before sending. **Fixed** — and note it
  recurred in *new* code (`discover` sends) during sim validation, so the guard must be applied
  at every send site, not remembered.

## F12 — Charging our own IP rotation to the account budget
- **Class:** APP
- **Cause:** `Por favor, confirma o reCAPTCHA` caused by *our* exit moving was counted against
  the sub-account's 2-strike budget; burned 3/3 and 2/2 sub-accounts.
- **Current behaviour:** the IP-mismatch check sits beside `transient`, **before**
  `attempt += 1`. **Fixed.**

## F13 — Settings that parse but nothing reads
- **Class:** OPS
- **Cause:** a key is added to the catalog, resolves cleanly, appears in the dashboard, and has
  no read site.
- **Detection:** none automatic. **34 of 140 keys**; only 2 labeled.
- **Improvement:** a test that greps for a read site per catalog key, or a required
  `implemented: bool` field. This trap is *recurring* — it needs a mechanical check, not
  vigilance.

## F14 — Warmup returning True without proving the schedule page
- **Class:** APP
- **Cause:** the happy path was ported without the guards; warmup reported success without
  reading Schedule.jsp.
- **Related:** gating the round-start re-warm on Schedule.jsp markers tests a *page render*,
  not the portal's server-side form cursor. **Every round must re-warm unconditionally.**

## F15 — Anchor provenance confusion
- **Class:** APP
- **Cause:** `posto_pdf` has two writers meaning opposite things; a checkpoint-set anchor
  silently suppressed the keepalive re-warm.
- **Current behaviour:** `_anchor_src` ∈ {chain, checkpoint, None}; the keepalive guard tests
  provenance, not mere presence. **Fixed.**

## F16 — Leaked browser processes
- **Class:** INFRA
- **Precedent:** 2600+ leaked Chromium processes in storm-auto.
- **Current behaviour:** `_kill_proc_tree` on close; `kill_tree` SIGTERM→SIGKILL on every stop
  path; workers spawned with their own session.

## F17 — Unbounded disk growth
- **Class:** OPS
- **Current state:** `var/worker-logs/` 21 MB / 224 files, appended across every launch of a
  worker id; `var/checkpoints/` 28 files.
- **Gap:** `checkpoint.cleanup_stale()` is written, tested and probed but has **no production
  caller**. Log retention exists only as a manual script.
- **Detection:** disk headroom appears in `/ops/status` — which nothing polls.

## F18 — Nothing watches the watchman
- **Class:** OPS
- **Cause:** `/ops/status` exists precisely because a runaway once needed an operator to notice
  it by hand. Nothing polls `/ops/status`.
- **Improvement:** the cheapest real fix in this document — a timer that polls it and alerts on
  Redis memory, `evt` length, disk, and running-worker count.

## F19 — Booking status can outlive its workers
- **Class:** APP / OBSERVED
- **Observed 2026-08-25:** booking "Test 2032" is `RUNNING` with `slot_open_fired_at` set and
  **5 READY / 0 RUNNING** workers, started one minute after a Hub restart.
- **Status:** not yet diagnosed. Either the start path failed to launch, or launch failed
  silently after the status transition. Needs a run to reproduce.

## F20 — Apply has not booked since 2026-08-21
- **Class:** UNKNOWN — this is the open problem, not a diagnosis.
- **What is known:** 17 bookings, all on or before 2026-08-21. Several genuine root causes were
  found and fixed in this window (F3, F12, F14, F15, and the skipped re-warm). None restored
  booking.
- **What is not known:** whether the remaining cause is in the apply path at all. The reverted
  `do_apply` (D21) has never been exercised by a run that produced enough warmed workers.
- **Rule:** measurement taken under a broken regime is not data. Do not tune against it.

## F21 — Three distinct events are not distinguished
- **Class:** APP
- **The three:** (a) **proxy failure** — the tunnel will not carry a request; (b) **proxy exit
  change** — the proxy works fine, its external IP moved; (c) **portal session failure** — the
  portal rejected our authenticated state.
- **Current behaviour:** partially separated. `_note_exit_ip()` detects (b) and the login path acts
  on it. But the hold loop's ladders key off *symptoms*: `_keepalive_dead_streak` counts nav
  failures (a-ish), `_rewarm_fail_streak` counts form-state failures (c-ish), and neither consults
  the exit observation that `_do_keepalive` has *already taken* one line earlier.
- **Consequence, confirmed live 2026-08-22:** a worker held a healthy auth session (28,505-byte
  keepalive page every cycle) while every questionnaire POST returned 403 — the *exit* was flagged,
  not the session. Neither ladder fired: the dead-session ladder never tripped because nav
  succeeded, and the re-warm ladder marked NOT BOOKABLE and returned. It looped to streak 7+ on the
  same blocked IP. `_maybe_escalate_stuck_rewarm` was added to break that specific deadlock.
- **Gap:** the escalation is still driven by a *failure count*, not by the classification. The exit
  observation is taken every keepalive and then discarded.
- **Improvement:** feed the existing `_note_exit_ip` result into the recovery ladder as a
  first-class input. No new measurement is needed — the data is already collected and thrown away.
- **2026-08-26 update — the exit observation now stays deliberately discarded, not accidentally:**
  `classify_session_state` (F22-F26's fix) classifies (a) and (c) from page/URL evidence alone and
  never consults `_note_exit_ip`'s result. This is no longer an oversight: Q16 and Q19
  (`docs/engineering/OPEN_QUESTIONS.md`) since confirmed that an exit change — deliberate or
  involuntary — does not by itself invalidate the portal session (one involuntary webshare move
  survived another 69 min authenticated). Folding "exit changed" into the classifier as a *trigger*
  would reintroduce exactly the misattribution this failure mode describes. What *is* still true:
  the exit-changed flag is logged/emitted but not surfaced next to the session-state verdict in one
  place — the two write to the log independently. Low priority given the flag is not a decision
  input.

## F22 — `sessionLost.jsp` is not recognised in production
- **Class:** APP
- **Cause:** the portal answers a dead server-side session with a dedicated `sessionLost.jsp`
  endpoint. `ops_live.py`'s classifiers know `Authentication.jsp`, the WAF challenge markers, the
  busy markers and the bounce keywords — but not this one.
- **Detection:** found 2026-08-25 while running Q16; four archived checkpoints all landed there and
  the probe's own classifier initially reported them as INCONCLUSIVE.
- **Consequence:** the clearest possible "your session expired" signal the portal offers is
  currently read as an unrecognised page, so the recovery ladder cannot act on it as expiry.
- **Improvement:** classify it, and feed it into F21's three-way split (proxy failure / exit change /
  session failure) as the unambiguous third case. Not implemented — analysis-only phase.
- **Fixed 2026-08-26:** `apps/worker/ops_live.py`'s `classify_session_state()` checks the final
  navigated URL for `sessionlost` before any other evidence and returns `SessionState.SESSION_EXPIRED`
  — the keepalive loop (both pre-warm and post-warm probes) now recognises it. Login-path recognition
  (the vendored session's own classifiers) is unchanged — this fix is scoped to the keepalive loop,
  per the Phase 1 implementation boundary.

## F23 — A pre-warm keepalive cannot see session expiry AT ALL
- **Class:** APP
- **Code path:** `_do_keepalive` with `posto_pdf is None` navigates `HOME_URL`. The portal serves the
  **logged-out** home with HTTP 200 (58,266 B). `browser_nav` returns normally, so
  `_keepalive_dead_streak` is reset to 0 and the worker logs `keepalive: probe OK`.
- **Consequence:** a worker that lost its session before warming holds indefinitely, reporting
  healthy, and is counted by the Hub as ready capacity. Nothing in the loop reads the body.
- **Evidence:** the logged-out home carries `solicitar pedido de visto` but **not** `consultar
  pedidos` and **not** the logout link — the discriminator exists and is simply not consulted.
- **Improvement:** classify the response (§11.1). This is the same defect class as F21: the
  information is already in hand and is discarded.
- **Fixed 2026-08-26:** `_do_keepalive` now reads the pre-warm home page's body (previously it
  never did — a successful `browser_nav` alone was treated as healthy) and classifies it via
  `classify_session_state(page, final_url, warmed=False)`, which applies exactly this
  discriminator (`consultar pedidos` present = authenticated, absent with only the public marker
  present = expired). A logged-out pre-warm home now triggers `_attempt_keepalive_restore`
  instead of being logged as `probe OK`. Zero new HTTP requests — the body is read via
  `browser_eval` on the page already navigated to.

## F24 — Session expiry after warm is misdiagnosed as form-state lapse
- **Class:** APP
- **Code path:** post-warm `_do_keepalive` navigates `Schedule.jsp`, finds no `_SCHED_PAGE_MARKERS`,
  and logs *"form-state lapsed -- re-warming in place"*. It then runs `_run_warmup_steps()`, whose
  in-page POSTs cannot succeed without a session; `_rewarm_fail_streak` climbs and the worker is
  marked NOT BOOKABLE.
- **Consequence:** the correct recovery (re-login) is reached only via
  `_maybe_escalate_stuck_rewarm` after `REWARM_ESCALATE_AFTER` failures **and** only if the
  slot_found / mono+slot_open gate is open. With the gate shut, the worker re-warms into a dead
  session forever. This is the deadlock already recorded for the 2026-08-22 incident, reached by a
  different route.
- **Improvement:** distinguish `SESSION_EXPIRED` from a missing schedule anchor before choosing
  between re-login and re-warm.
- **Fixed 2026-08-26:** `classify_session_state` checks `sessionLost.jsp`/`Authentication.jsp`
  redirects (explicit expiry evidence) before the `_SCHED_PAGE_MARKERS` check. When those are
  absent, marker-absence alone now classifies `UNKNOWN`, not an assumed form lapse — `_do_keepalive`
  still re-warms in that case (there is no positive evidence either way, and re-warming remains a
  legitimate bounded probe, escalating via the existing `_maybe_escalate_stuck_rewarm` ladder
  unchanged), but a genuinely dead session (positive redirect evidence) now routes straight to
  `_attempt_keepalive_restore` and never reaches `_run_warmup_steps()` at all.

## F25 — A portal-busy page during hold is read as form-state lapse
- **Class:** APP
- **Code path:** `_is_portal_busy` is consulted at three sites (gettime classification, the schedule
  probe, login) — **not** in the keepalive's marker check. The "não foi possível de momento" page
  lacks `_SCHED_PAGE_MARKERS`, so the same "form-state lapsed" branch fires.
- **Consequence:** the worker re-warms against a portal that is declining to serve anyone, fails,
  and accrues NOT-BOOKABLE strikes for a condition that is neither its session nor its form.
- **Improvement:** `PORTAL_ERROR` is a distinct state whose correct action is to back off, not to
  re-warm and not to re-login.
- **Fixed 2026-08-26:** `classify_session_state` calls the existing `_is_portal_busy` (already used
  at 3 other sites, never here) before either marker check, returning `SessionState.PORTAL_ERROR`.
  `_do_keepalive` logs it and returns without calling `_run_warmup_steps()` or
  `_attempt_keepalive_restore` — the next keepalive cycle re-probes naturally.

## F26 — There is no UNKNOWN state anywhere in production
- **Class:** APP
- **Cause:** every classifier resolves to a specific verdict or falls through to a failure default —
  `events.py`'s `_TERMINAL_STATUS.get()` maps anything unmapped to FAILED, and the keepalive treats
  "nav returned" as health.
- **Consequence:** unrecognised portal responses are silently absorbed into whichever branch happens
  to catch them, which is how both historical classifier failures (§26, §28-note) produced confident
  wrong answers rather than an admission of ignorance.
- **Improvement:** `UNKNOWN` as an explicit, first-class outcome that backs off and re-probes.
- **Partially fixed 2026-08-26, keepalive only:** `SessionState.UNKNOWN` is now an explicit member
  produced by `classify_session_state` whenever neither positive nor negative evidence is found —
  pre-warm it logs and waits for the next cycle without touching any streak or bookable state;
  warmed it preserves today's bounded re-warm-then-escalate behaviour rather than guessing a
  specific verdict. `events.py`'s `_TERMINAL_STATUS.get()` defaulting unmapped outcomes to FAILED
  is a **separate, still-open** instance of this same defect class, outside this fix's scope
  (worker-terminal-status mapping, not the keepalive loop).

## F27 — The test suite could delete real checkpoint data
- **Class:** OPS
- **Cause:** `tests/test_cleanup_checkpoints_script.py` ran `scripts/cleanup_checkpoints.py` as a real
  subprocess (deliberately — it exercises the actual CLI, not just internal logic) against the real
  `var/checkpoints/` directory, cleaning up only the one fixture file it created itself. This was
  correct when written; it stopped being correct once real workers had run long enough for other
  files in that directory to independently cross the test's own 7-day age threshold.
- **Consequence:** running the full suite on 2026-08-26 invoked `cleanup_checkpoints.py --confirm`
  against production data and deleted 3 real checkpoint files, one of them live. `D15`'s guarantee
  (destructive cleanup is scoped, reviewed, dry-run-by-default tooling) held at the *tool* level —
  the script did exactly what a real operator invocation would have done — but the *test* had no
  equivalent scoping of its own.
- **Fixed 2026-08-26:** `libs/engine/checkpoint.py`'s `_CHECKPOINTS_DIR` now resolves from
  `CHECKPOINTS_DIR_TEST_OVERRIDE` when set, falling back to the unchanged hardcoded production path
  otherwise — mirroring the in-process `monkeypatch.setattr(ckpt, "_CHECKPOINTS_DIR", tmp_path)`
  pattern already used by `tests/engine/test_checkpoint.py`/`tests/hub/test_action_guard_parity.py`,
  extended to reach a subprocess. The test now points every invocation at pytest's own `tmp_path`.
  `cleanup_checkpoints.py` itself still accepts no path argument at all — D15's "no arbitrary path,
  ever" guarantee for the *tool* is unchanged; the override lives one layer below it, is off by
  default, and nothing in production ever sets it. Verified by snapshotting `var/checkpoints/`
  (file list + md5) before and after the isolated test run: byte-for-byte identical.
- **Improvement, not done here (checked, found already handled):** the other two subprocess-invoking
  script tests, `tests/test_reset_run_state_script.py` and `tests/test_cleanup_worker_logs_script.py`,
  already isolate themselves (a `tmp_path`-backed SQLite DB passed via env, and a scratch
  `tmp_path/var/worker-logs/` tree respectively) — `reset_run_state`'s own fixture docstring
  documents having fixed this exact class of bug once before. This was the one remaining gap, not a
  systemic pattern; no broader test-infrastructure change was made.

## F27 — Live credentials committed to git, three of them pushed
- **Class:** OPS (security incident, 2026-08-26)
- **What:** four real credentials existed as plaintext literals in tracked files.

| credential | location | earliest commit | reached remote |
|---|---|---|---|
| `WEBSHARE_PASSWORD` | `importers.py` comment, 2 test files | `ac5645c` 2026-08-19 | **yes** — 3 branches |
| admin `storm` pw **= Postgres pw (reused)** | `user.sh` | `43a9681` 2026-08-14 | **yes** — 3 branches |
| viewer account pw | `user.sh` | `43a9681` 2026-08-14 | **yes** — 3 branches |
| `SOAX_PASSWORD` | `tests/proxies/test_credentials.py` | `5dbfe05` 2026-08-26 | no — unpushed |

- **Why it mattered most for the reused one:** one literal exposed both the database and an admin
  login to an internet-reachable Hub (Caddy → 127.0.0.1:8123, and external scanners are already
  probing it), and that admin can export booked identities' stored passwords via
  `/identities/booked/credentials`.
- **How it was found:** a hash-based scan comparing `.env` values against every ref, run as part of
  a pre-commit review. The viewer password was **not** in `.env`, so the hash scan could not have
  found it — it surfaced only by reading `user.sh` directly.
- **Detection gap:** nothing scans for secrets. This is the same class as `gen_proxy_pool.py`'s own
  warning ("would have put live proxy credentials into git history permanently"), which was written
  after an earlier near-miss and did not prevent this.
- **Remediated:** literals removed from the working tree (`e67f678`); `user.sh` rewritten to take
  credentials from the environment and refuse to run without them.
- **NOT remediated:** rotation (requires provider/dashboard access) and history rewriting (touches
  3 remote branches, 43-60 commits each). `origin/main` is unaffected.
- **Improvement:** a pre-commit secret scan, and stop reusing one password across Postgres and the
  admin login.

## F28 — The apply-recovery ladder poisons its own checkpoint, then restores it
- **Class:** APP
- **What:** on the `do_apply` → `"session"` recovery path, the worker writes the *broken* session's
  cookie jar to `var/checkpoints/` three times and then reloads that file ~22s later, before it is
  allowed to attempt a fresh login. Full evidence, trace and options:
  `REPORT_2026-08-27_checkpoint_self_poisoning.md`.
- **Mechanism:** `WorkerRuntime.on_transition` calls `ops.save_checkpoint_now()` on **every**
  `HOLDING` transition (`runtime.py:36`), and three of `_rebuild_session`'s transitions are
  *failure* transitions (`phases.py:299, 307, 511`). `do_login` then tries
  `_try_restore_from_checkpoint()` before `_fresh_login()` (`ops_live.py:543`).
- **Why the save succeeds on a dead session:** `get_all_cookies()` is a local CDP call; a dead proxy
  tunnel does not make it fail. A broken session always produces a clean-looking save.
- **Detection:** `restoring checkpoint for identity …` immediately following
  `-> holding (re-warm failed; …)` in the worker log. CONFIRMED live on booking `Test2`,
  2026-08-27: 20 restores in 32.5 min, 1 success (14 transport failures, 5 checkpoint deletions,
  1 budget exhaustion) — roughly 8 of 32 minutes.
- **Related defects found with it:** `stage="schedule_jsp"` stamped on a mid-failure save
  (`ops_live.py:2389`; nothing reads `stage`, so misleading not load-bearing); the restore rebuilds
  on the same failed `warm_proxy` and may delete good cookies under its "these cookies are spent"
  rule (OBSERVED, not proven); F17's missing `cleanup_stale()` caller means a poisoned file has no
  expiry.
- **Prior art in this codebase:** the same bug class was already fixed at the *fresh-login* seam —
  see the N4 comment at `ops_live.py:918`, which names "poisoning the next checkpoint save with a
  `stage="schedule_jsp"` that isn't true" and calls `_clear_anchor()`. The apply-failure seam never
  got the same treatment.
- **Not fixed.** Recommended remedy (operator decision pending): gate `save_checkpoint_now()` on
  `_anchor_src == "chain"` **and** `_anchor_age()` under the form-state TTL — the invariant already
  exists (`ops_live.py:641-644, 1453`) and already means "the chain verified this on this session".
  The age check is not optional: a failed re-warm never reaches `_set_anchor`, so `_anchor_src`
  retains the previous *successful* warm's value.

## F29 — The phantom-slot detector is blind to the outcomes we actually produce
- **Class:** APP
- **What:** `_maybe_revert_false_slot_found` decides a pool is phantom by counting **`bounced`**
  leases against `FALSE_SLOT_BOUNCE_LIMIT` (default 12). MEASURED 2026-08-28 across two concurrent
  bookings on posto 5084: **641 leases — `form_stale` 246, `session` 393, `bounced` 0.** The guard
  cannot fire, so a fleet can grind advertised-but-unbookable slots indefinitely with nothing
  noticing.
- **How it got this way, and why the cause was itself correct:** `bounced` used to be the catch-all.
  Reclassifying our own gates and transport failures away from it (2026-08-15 onward) was right —
  it stopped 5,082 bounced leases being charged to a pool that was never phantom, and stopped that
  misattribution driving the false-slot revert on real slots. But it also removed the only signal
  feeding phantom detection, and nothing replaced it.
- **Why it matters at scale:** with 80 workers on a posto advertising 8-20 phantom periods, the
  fleet burns sub-account login budget and portal goodwill against slots no one can book, while the
  dashboard shows healthy `applying` workers. Compounds with the portal throttling already recorded
  in CONFIRMED_FACTS §35.
- **Detection today:** none automatic. Visible only by reading lease outcomes and noticing
  `booked 0 / bounced 0` against a non-empty pool.
- **FIX PLAN (not yet implemented):**
  1. Count `form_stale` toward the phantom verdict on its **own, higher** threshold — a
     `FALSE_SLOT_STALE_LIMIT`, separate from `FALSE_SLOT_BOUNCE_LIMIT`. `form_stale` is weaker
     evidence than `bounced` (it is ambiguous between "slot taken" and "our form lapsed"), so it
     must not share the bounce threshold.
  2. Deliberately EXCLUDE `session`. That is transport dying, genuinely not the slot's fault, and
     counting it would recreate exactly the misattribution the reclassification fixed.
  3. Require the evidence to span **distinct slots**, not repeats of one — the 2026-08-27 defect was
     14 leases on ONE slot, and a per-slot loop must not be mistaken for a phantom pool.
  4. Tests: phantom pool detected from `form_stale` across distinct slots; NOT detected from
     `session`; NOT detected from repeated `form_stale` on a single slot; existing `bounced`
     behaviour unchanged.
- **Do NOT** widen `FALSE_SLOT_BOUNCE_LIMIT` to cover these — that conflates three different
  failures under one counter, which is how this blind spot was created.

## F30 — A launch decided before /stop can still spawn long after it, into a booking with no way back
- **Class:** APP
- **Code path:** `_do_launch_worker` (apps/hub/main.py) checks `bk.status not in
  _LAUNCH_BLOCKED_BOOKING_STATUSES` exactly once, at entry — before `lease_proxy`, which can spend
  up to ~100s in real proxy health probes (its own docstring: `max_probes` candidates at ~20s each,
  twice over with `HOLD_SWAP_AFTER_WARMUP`). Nothing re-checks between that entry gate and the
  actual `supervisor.spawn(w.id)` call tens of seconds to minutes later.
- **CONFIRMED LIVE 2026-08-28/2026-08-31:** two hold-respawn scout relaunches on "Booking
  0828"/"Booking 0828 ws" (posto 5084) passed the entry check while their bookings were still
  RUNNING, then spent the lease-probe window mid-flight while the operator hit `/stop` on both
  (committed 11:31:57 / 11:32:00). Neither relaunch re-checked afterward: both spawned real
  processes at 11:32:25 / 11:32:44 — 25-44s after their booking was already STOPPED — and were
  never in `stop_booking`'s own worker sweep (it only catches rows that are already RUNNING at query
  time; these were still READY, mid-lease). The processes ran for **3 days 9+ hours** before being
  found and killed by hand.
- **Why they could never self-recover, and why this is a consequence of F30, not a second bug:**
  hold-respawn's relaunch path (`_sched_on_relaunch`) correctly calls `_reset_worker_bus_state`
  before every relaunch, removing the worker from the booking's slot_found "already notified" set so
  `_latch_signals` can re-deliver a signal the booking fired before this worker existed (F9's own
  fix, working as designed). But `_latch_signals` — like `_hold_respawn` — only runs `if bk.status in
  _ACTIVE_BOOKING_STATUSES` (scheduler.py `_tick_once`, itself hardening an earlier instance of this
  same defect class, 2026-08-14). A worker that only exists because its booking's STOPPED status
  failed to stop its launch is, from that first tick onward, one `_latch_signals` correctly refuses
  to touch — so `_maybe_escalate_stuck_rewarm`'s gate (`_slot_found_seen()`) can never open, and the
  worker re-warms a lapsed form forever. Confirmed via the Redis event stream: 393 identical "this
  booking has not opened yet" log lines, continuously, from the relaunch through the moment it was
  killed. Fixing the launch race removes the precondition this deadlock needs — no separate fix was
  required for the latch side, which is correct as designed.
- **Fixed 2026-08-31:** a second guard immediately before `supervisor.spawn(w.id)`, using
  `db.refresh(bk)` (not another `db.get`, which would return the same cached identity-map instance
  and never observe a STOP committed by a different session in between). On a match, the proxy
  lease(s) already taken during the probe window are released via `release_proxy(db, w.id)` and the
  function returns without spawning — the worker is left exactly as `_sched_on_relaunch` queued it
  (READY), inert, matching the entry-check's own abort shape.

## F31 — A CAPTCHA solved for a page that had not yet registered its callback
- **Class:** APP
- **Code path:** `_discover_via_slots` (apps/worker/ops_live.py). `browser_nav` returns when the
  *document* loads, but `window.onCaptchaSuccess` is registered by the portal's reCAPTCHA widget
  script, which is still loading. The solve was spent first and the delivery attempted second, so a
  page that was not ready yet cost a token.
- **MEASURED 2026-09-01 on booking "Test 0901":** of 55 `/slots` discovery rounds, **47 died on
  `onCaptchaSuccess not defined`** — and every one of those had *already* solved a CAPTCHA (tokens
  of 2,553–2,702 chars, 7–23s each). The solver was paid 47 times for tokens the page could never
  receive. Exactly one round landed; that one returned **70 slots**, so the endpoint itself works.
- **Fixed 2026-09-01:** an in-page readiness poll (`typeof window.onCaptchaSuccess === 'function'`,
  250ms interval, `_CALLBACK_WAIT_MS = 20000`) runs *before* the solve. If the callback never
  appears the round routes to `_calendar_fallback` having spent nothing. `_SLOTS_WAIT_MS` raised
  12s → 25s, since the one successful round showed the page's own script needs the time. A probe
  that raises (destroyed execution context) is treated as not-ready — also no solve.
- **Note:** this is a cost-and-throughput fix, not a correctness one. The 8 non-callback failures
  are a separate population and are not explained by it.

## F32 — A scout thrashing at the login wall counted as "alive" and suppressed the fallback
- **Class:** APP
- **Code path:** `_maybe_self_scout` (apps/hub/scheduler.py) gate 3 tested
  `any(s.status == WorkerStatus.RUNNING for s in scouts)`. A worker that crashes and respawns every
  ~30s is RUNNING at roughly half of all instants, so the gate read as "a scout is working" while
  the scout was in fact discovering nothing.
- **MEASURED 2026-09-01 on booking "Test 0901":** scout `28b8fe7f` cycled `logging_in → failed`
  **245 times over four hours with zero `slots_found`**. The apply-as-scout fallback — the mechanism
  that exists precisely for when scouts are not working — was suppressed for that entire window,
  while two apply workers sat warmed and idle with no path to discover anything.
- **Fixed 2026-09-01:** `_scout_is_actually_scouting(w)` requires RUNNING **and** a substate in
  `_SCOUTING_SUBSTATES` (LOGGED_IN, WARMING_UP, WARMED, SWAPPED, HOLDING, DISCOVERING, POLLING) —
  i.e. past the login wall. `LOGGING_IN`, `IDLE`, `FAILED` and `substate=None` no longer count.
- **Accepted cost, pinned by a test:** a RUNNING scout that has not yet reported a substate
  (mid-launch) now also fails the gate, so the fallback can fire during a healthy scout's login.
  That is at most one command to one holding apply worker per `_SELF_SCOUT_THROTTLE` (30s) window,
  against a measured alternative of four booking runs with zero bookings. Firing early is the
  cheaper error — see `test_a_scout_with_no_substate_yet_does_not_suppress_the_fallback`.
- **Still open:** gate 1 (`bk.slot_found_fired_at is not None` returns early forever, a one-shot
  latch that permanently disables the fallback once any slot has ever been found) is untouched and
  was not authorised. F29's phantom-slot detection likewise remains open.

## F33 — The self-scout fallback's gate 1 was a one-shot latch on the past, not a question about the pool
- **Class:** APP
- **Code path:** `_maybe_self_scout` (apps/hub/scheduler.py) opened with
  `if bk.slot_found_fired_at is not None: return`. That column is set the first time *any* slot is
  ever discovered for the booking and is never cleared for its lifetime.
- **The defect:** the apply-as-scout fallback exists for "we have no slots and no working scout" —
  but it could not fire in that state, because reaching it requires having had a slot at some point,
  which is exactly what permanently latched the gate shut. One phantom slot, discovered once and
  retired seconds later, disabled discovery recovery for the rest of the run.
- **It compounds the two paths that legitimately empty a pool.** `_maybe_revert_false_slot_found`
  deletes the slots of a pool it has declared phantom, and slot retirement CLOSEs a slot after N
  misses. Both leave a booking with zero applyable slots and a latch still reporting "already found
  some" — the F32 scout-liveness fix alone would not have helped any booking that had reached this
  state, since gate 1 sits *ahead* of gate 3.
- **Fixed 2026-09-01:** gate 1 now counts live inventory — slots in `AVAILABLE` or `LEASED` — and
  suppresses the fallback only while some exists. `LEASED` is deliberately included: a pool with
  every slot leased to a busy worker is at its most productive, and counting only `AVAILABLE` would
  fire the fallback on every busy tick. `CLOSED` (retired) and `TAKEN` (booked) are not inventory,
  because nothing can be applied to them.
- **Note:** the pre-existing `test_maybe_self_scout_skips_once_slot_found` asserted the latch itself
  as a requirement, with no slot rows in the fixture at all. It is renamed to
  `test_maybe_self_scout_skips_while_found_slots_are_still_there` and given a slot; the half it used
  to cover incorrectly is now
  `test_an_exhausted_pool_re_enables_the_fallback_even_though_slot_found_fired`.

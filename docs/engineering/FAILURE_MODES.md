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

## F34 — A per-exit refusal was read as a portal outage, and only a success could clear it
- **Class:** APP
- **Code path:** `_sticky_login_attempts` (apps/worker/ops_live.py) called
  `self._breaker.record_portal_down()` on any `_is_portal_busy(reason)` at login, without first
  testing whether *this exit* was the problem.
- **MEASURED 2026-09-01 (23:18 run of "Test 0901"):** in the 23:30 bucket, **11 "try again later"
  pages coexisted with 4 successful logins**. A page one exit receives while another exit logs in
  successfully is per-exit evidence by definition, not "the portal is down for everyone".
- **Why it could not recover.** `RedisBreaker.record_success()` is reached only by a login that
  succeeded. Once the exit pool degraded, the breaker tripped, suppressed the very attempts that
  could have found a working exit, and nothing could clear the counter. It auto-opens after each
  120s cooldown, the next few refusals re-tripped it, and the cycle repeated: **145 refusals over 91
  minutes, and zero successful logins after 23:37:52** — all 6 of the run's logins landed in its
  first 14 minutes.
- **Not the transport failures.** Those (147 in the same run: `ERR_TIMED_OUT` 53,
  `ERR_TUNNEL_CONNECTION_FAILED` 52, `ERR_CONNECTION_CLOSED` 42) were already classified correctly
  — `_is_transient_login_failure` rolls the exit and never touches the breaker. Only the refusal
  page was mis-attributed.
- **Fixed 2026-09-02:** a refusal is charged to the exit first. `LOGIN_PORTAL_BUSY_ROLLS` (default
  3) refusals each roll to a different exit, uncharged to the sub-account — placed beside
  `transient`/`ip_moves` and *above* `attempt += 1`, since a refusal is not a verdict on the
  credentials. Only once that many **different** exits have refused does the breaker hear about it.
- **The 2026-08-23 finding it revises is preserved, not discarded.** That run (265 of 266 refusals
  at login; 341 attempts for 30 sessions) correctly concluded the fleet should back off together
  when the portal really is refusing everyone. Rolling only delays that until it is *established*
  rather than assumed; `LOGIN_PORTAL_BUSY_ROLLS=0` restores the original straight-to-breaker
  behaviour exactly.
- **Still open:** why zero logins succeeded after 23:37:52 is NOT settled by this. The breaker
  suppressing attempts is one contributor; a genuinely degraded webshare pool is another, and the
  two cannot be separated from this run's data because the breaker prevented the test. The next run
  with this fix is that test.

## F35 — HOLDING still meant "ready" while the session was being rebuilt
- **Class:** APP
- **Code path:** `_assign_slots` (apps/hub/scheduler.py) selects apply workers on
  `substate == HOLDING` plus the absence of `NOT_BOOKABLE_PREFIX`, and nothing else.
  `_attempt_keepalive_restore` (apps/worker/ops_live.py) closes the browser and runs a full
  checkpoint-restore-or-login while the substate stays `HOLDING` — correctly, the worker *is* still
  holding — so the Hub counted a mid-rebuild worker as ready capacity.
- **CONFIRMED LIVE 2026-09-02 (20:45 run of "Test 0901"):** the pool's *only* available slot,
  `2026-09-30 p5`, was leased at 22:22:04 to **two** workers:
  - `41f656e6` was inside the restore chain and did not reach `applying` until **22:24:41**
    (2m37s later); its warm-up then failed `csrf_missing` and it released the slot as `session`.
  - `1d1a9033` reached `applying` immediately and found *"page is not on a live portal origin …
    the tunnel is genuinely down"*, releasing 8 seconds later, also `session`.
  One slot, two workers, neither able to act on it, locked for roughly three minutes.
- **The same defect had already been fixed once, in the neighbouring branch.** The keepalive
  re-warm-failure path has marked NOT_BOOKABLE since 2026-08-21, with a comment describing exactly
  this consequence ("the worker kept reporting a bare `holding`, the Hub counted it as ready
  capacity, and it bounced the first slot it was handed"). The dead-trip branch and the restore
  chain were both missed.
- **Fixed 2026-09-02:** `_attempt_keepalive_restore` marks NOT_BOOKABLE for its duration and clears
  on success; the keepalive dead-trip marks too. Marked at the dead-trip rather than on every probe
  failure, because a single failure is frequently the spurious "Execution context was destroyed"
  nav race (6–8 per worker per hold on the 17:15 run) and marking on those would delete real
  capacity over nothing.
- **The clear had to be re-plumbed.** It was gated on `_rewarm_fail_streak > 0`, which is only true
  when the re-warm branch marked; the dead-trip branch leaves that streak at 0, so a recovered
  worker would have stayed unbookable forever. `_hold_unbookable` now tracks the marker itself. A
  permanently unbookable holding worker is worse than the bug being fixed — it is invisible
  capacity loss.

## F36 — a round that reported nothing still emitted an event, and span while doing it
- **Class:** APP (both introduced by 3d87511, found in its first live run)
- **`report_dates([])`:** `_no_report` returns `[]` for a round that never asked the portal, and
  `phases.py` still called `report_dates` with it. MEASURED on the 20:45 run: **73 of 96
  `dates_reported` events (76%) carried zero outcomes**, all from one scout, roughly every 7
  seconds — stream noise that also made an idle scout look busy. Now only reported when non-empty;
  `failed`/`empty` are real outcomes and still reported, or slot retirement would silently stop.
- **The pacing gate span.** Returning early cost nothing, so the discovery loop cycled at poll_gap
  speed doing no work — **1,566 status changes in four hours**, almost all `DISCOVERING ↔ POLLING`
  flapping. It now waits out the remainder in 1s chunks, checking `is_stopped`. The wait is
  **capped** (`_SLOTS_MAX_PACING_SLEEP = 20s`, not the full 100s interval) because an APPLY worker
  that is self-scouting runs this same path and must stay able to act on a real assignment.

## F37 — an eligibility gate that lived only in the dashboard, over a status that never meant what it was read as
- **Class:** APP
- **Symptom:** identities with nothing to log in with were offered as attachable, attached, and
  launched. The worker leased a proxy, solved a CAPTCHA and attempted a login against a sub-account
  the portal has never verified — there is no confirmed account behind those credentials, so the
  login cannot succeed, and the run pays for the attempt in full.
- **Cause, part 1 — a derived field read as an answer to a different question.** The attach panel
  filtered on `Identity.status === "active"`. `IdentityStatus.ACTIVE`'s own definition is ">=1
  sub-account **exists**", and `gen_subaccounts_bulk` / the CSV import set it the moment sub-account
  rows are GENERATED (`status="new"`). It says nothing about registration or verification. The same
  class as `Identity.status` being stale eight days after a real booking (see `attach_identity`'s
  own note): a derived field is evidence about what it was derived from, never about what a caller
  wishes it meant.
- **Cause, part 2 — the only gate was in the UI.** Nothing behind the panel re-checked.
  `_rank_live_sub_accounts` ranked `REGISTERED` 3rd and `NEW` 4th, i.e. it deliberately HANDED an
  unverified account to a live worker once the verified ones ran out, and `_do_launch_worker` took
  `ranked_subs[0]` whatever it was. A rotation into an account that cannot log in is not a fallback;
  it is a slower failure that costs a proxy lease, a CAPTCHA solve and one slot of the
  sticky/rotating login budget.
- **The first fix was the same bug again.** It rebuilt the tier client-side: fetch `/sub-accounts`,
  filter `["verified","login_ok","held"]` in JSX, gate on that. Correct on the day it was written
  and free to drift from `_rank_live_sub_accounts` the day the enum changes — a second copy of a
  domain rule, in a language that cannot import the first.
- **Fixed 2026-09-04, in three parts:**
  1. `READY_SUB_ACCOUNT_STATUSES` in `libs/domain/enums.py` is the one definition;
     `_rank_live_sub_accounts` filters on it, so NEW/REGISTERED/BOOKED are never candidates.
  2. `GET /identities` reports `ready_sub_accounts` (a count, computed from that set); the dashboard
     renders it and computes nothing.
  3. `_do_launch_worker` refuses a live launch with no ready sub-account, naming how many are still
     unverified. The UI filter is a convenience; the launch gate is the guarantee. Attach stays
     permissive on purpose — registration legitimately finishes after attach.
- **Drift guard:** `tests/hub/test_sub_account_readiness_parity.py` asserts, for every member of
  `SubAccountStatus`, that what `GET /identities` reports is exactly what the launch gate enforces,
  and fails if a sub-account status literal reappears inside the attach panel's JSX.
- **The general rule:** an eligibility rule enforced only where it is displayed is not enforced. Put
  it where the resource is actually spent, publish the result, and bind the two with a test.

### F-swap-probe. `do_swap` validated with a WARMED-session check at a point that is never warmed
**Measured live, booking Test 0916, 2026-09-16: 20/20 swap attempts failed** — 5 workers × 4
attempts, every one logging `do_swap: new session failed health check -- reverting to phase-1
proxy`. Deterministic, not flaky.

`do_swap` runs between `LOGGED_IN` and `KEEPING`. Warmup has been **deferred** to the
slot-open/slot-found signal since 2026-09-09, so a worker at that point is by definition **not
warmed** — the keepalive code says so in its own comment: *"there is no form-state to verify here
even in principle."* But `do_swap` validated the moved session with `_probe_alive()`, which
navigates `Schedule.jsp` and requires `cmbPeriodo`/`getPeriodosOcupados` — markers that only exist
once warmup has built the form-state anchor. The check asked *"is this session warmed?"* at the one
point in the lifecycle where the machine guarantees it is not, so it could never pass.

**Origin:** the swap used to run *after* warmup. When `HOLD_SWAP_AFTER_WARMUP` became
`HOLD_SWAP_AFTER_LOGIN` and moved ahead of warmup, the probe was left pointing at the old lifecycle
— the same class of stale-ordering bug as the substate display fixed the same day.

**Cost:** each failure triggers a `HOLD_SWAP_RETRY_HOLD_SECS` backoff (600 s), so every worker spent
~40 minutes parked at `LOGGED_IN`, unwarmed and with no keepalive running, before falling through to
`KEEPING` still on its phase-1 rotating proxy. The three appointments that run booked were made over
**soax rotating**; the leased ISP exits were allocated, probed, abandoned four times each, and never
carried a session. Static ISP hold has therefore never actually been exercised in production.

**Why no test caught it:** `_FakeSession` returns `"<html>cmbPeriodo</html>"` for *every* URL — a
session that is always warmed. Every `do_swap` test passed against a double that could not express
the failing state. The fix adds `_UnwarmedSession` (authenticated home, bare `Schedule.jsp`) and
asserts through `do_swap` itself rather than on the probe in isolation — the first version of the
test called the new probe directly and passed happily with the old one still wired in.

**Fix:** `_probe_swapped_session()` classifies the **home page** with
`classify_session_state(..., warmed=False)` — the same evidence the auth-only keepalive uses.
`_probe_alive` is unchanged and still correct for its other caller, the checkpoint-restore path,
which restores `posto_pdf` and legitimately expects a warmed session.

**Rule this generalises:** when a step moves in the lifecycle, every check it performs has to be
re-read against the new position. A probe is an assertion about *when* it runs, not only *what* it
tests.

### F-phantom-slots. The calendar path manufactures slots from the portal's fallback template
**Confirmed live, Booking 0918, 2026-09-18, slot-open 10:39.** One scout got a real `Schedule.jsp`
during the ~60 s the portal stayed up, then reported `{3,4}` on 2026-10-14 **and** `{3,4}` on
2026-10-15. The portal's own calendar showed 12/13/14 October available and the 15th greyed out.
Both fired `slot_found`; three apply workers leased them, re-warmed into a 503, and the recovery
ladder escalated them to re-login — discarding authenticated sessions and spending reCAPTCHA strikes
on slots that did not exist.

**Mechanism.** The calendar path derives availability by *subtraction*: `free = active range −
closed prefix`, decoded from `getPeriodosOcupados`. That inference cannot distinguish "the portal
answered this date" from "the backend query failed and the portal served its template with a default
occupied string". Under load the second happens; the `periodos_ocupados='…'` marker is still
present, so the parser succeeds, and **every date decodes to the same thin set**. The codebase named
this exact hazard when it removed the calendar *fallback* on 2026-09-02 ("the exact inference that
manufactures phantom slots") — but the primary calendar path carried the same inference with no
guard.

**Signature (operator-confirmed on earlier runs):** "only 3, 4 periods per every day and fewer
slots than expected". Uniformity across dates is the load-bearing half — a real open answers each
date differently; only a template answers every date identically. Test 0916 booked real appointments
on dates that all showed `{3,4,5}`, which is why the size bound is needed alongside uniformity.

**Guard:** `_reject_phantom_round` — a round with ≥ `PHANTOM_MIN_DATES` (2) found dates, all with the
same period set of ≤ `PHANTOM_MAX_PERIODS` (2) entries, is reported as **failed (unknown)**, never
found and never empty: no slot rows, no `slot_found`, no date retirement; the next round asks again.
The raw occupied string is now logged per phantom round, so the next real open leaves evidence to
refine the rule with — until 2026-09-18 it was discarded the moment it was decoded.

**Definitive check, not yet built:** the portal's calendar widget renders available days from
*something* in the `gettime` response. Only `SPECIAL_DAYS` (closed days) is parsed today. If the
available-days list is there, cross-checking finds against it would replace this heuristic with
ground truth. Needs a live `gettime` body to inspect; the portal was 503 when this was written.

### F-nationality-truncation. Unknown passport demonyms were truncated into invalid portal codes
**Measured 2026-09-19.** Registration failed 100% for every nationality the importer's five-entry
map did not know: `GUINEENS` 0/14, `GUINÉENN` 0/4, `GHANAIAN` 0/2, against ~1% for the mapped `CPV`
(483/489) and `SEN` (12/12). The portal's registration form takes an ISO 3166-1 alpha-3 code; passport
OCR yields the demonym in the passport's own language (Portuguese `CABO-VERDIANA`, French `GUINÉENNE`,
English `GHANAIAN`). The old `_map_nationality` fell back to `value.upper()[:8]` for anything unmapped,
so a demonym was stored as a truncated fragment and sent to the portal as a code -- which it rejects.

**The truncation also destroyed the evidence**, which is the sharper lesson. Both broken Guinea values
were cut at exactly 8 characters, and the 8th-plus letters are what distinguish the two countries:
`GUINEENSE` (Guinea-Bissau, Portuguese, GNB) and `GUINÉENNE` (Guinea-Conakry, French, GIN) both
truncate to something an operator cannot tell apart. The passports settled it (C00-prefixed +
Bissau surnames vs 9-digit + Barry), but a lossy transform must never put the operator there.

**Fix:** `libs/domain/nationality.py::to_iso3` -- a proper demonym→ISO-3 map (diacritic- and
separator-insensitive, masculine/feminine/plural forms, ~40 nationalities relevant to this pool),
returning `(code, error)`. An unrecognised value is a NAMED IMPORT ERROR that skips the record, never
a silent truncated code. The importer (labeled + CSV) surfaces it against the person's name;
`scripts/repair_nationalities.py` re-mapped the 10 already-broken rows in place (GUINEENS→GNB×7,
GUINÉENN→GIN×2, GHANAIAN→GHA×1). ISO-3 is the confirmed contract because SEN/CPV -- the only codes
that ever registered -- are exactly ISO-3.

---

## F38 — An unlocked dict iteration crashed the whole scheduler tick
- **Class:** APP
- **Cause:** `Supervisor.running_ids()` iterated the `_pids` dict while another thread mutated it
  (spawn/reap), raising `RuntimeError: dictionary changed size during iteration`. The exception
  propagated out of `_tick_once`, so the ENTIRE tick aborted — booking drive, signal latch, slot
  assignment, respawn — not just the supervisor read.
- **Detection:** signals firing late or not at all; the tick traceback in the Hub log (~1234×/day
  at peak). The symptom "signals didn't fire at the scheduled time" was this, not the schedule.
- **Current behaviour:** `threading.RLock` guards every `_pids`/`_procs` access; `running_ids()`
  snapshots under the lock then liveness-checks outside it. The tick additionally isolates faults
  **per-booking** (`_drive_booking`) and **per-maintenance-phase**, each wrapped in try/except +
  rollback, so one booking's error can no longer abort the others.
- **Recovery:** none needed at runtime; a crashed tick simply ran again next interval, but late.
- **Related OPS trap:** `systemd` must set `KillMode=process`, or a Hub restart cgroup-kills every
  worker in the control group. See `ARCHITECTURE.md` §12.

## F39 — An in-page fetch slips off-origin DURING a long CAPTCHA solve
- **Class:** INFRA
- **Cause:** a stage that navigates (establishing the portal origin), then solves a CAPTCHA for
  ~15-25 s, then issues an in-page `fetch`/token-inject, can have the browser's proxy tunnel drop
  the PAGE off-origin (to `chrome-error`/opaque) *during* the idle solve **while the exit itself
  stays alive** (MEASURED on the registrar: a raw client survives nav+28 s+POST 5/5; only the
  in-page world slips). The in-page call then throws `TypeError: Failed to fetch` /
  `onCaptchaSuccess not defined`. This is the `opaque_origin` class specialised to a long gap.
- **Detection:** registrar — every register POST failing `Failed to fetch`, cursor frozen (0/11
  registered). Slots discovery — `onCaptchaSuccess not defined` after a page that WAS ready
  pre-solve, every paid token thrown away → discovery goes blind on bad proxy weather.
- **Current behaviour:** both re-anchor on that specific failure. Registrar re-navs
  `Authentication.jsp` before the POST (2026-10-01). Slots re-navs the slots page once and
  re-delivers the SAME still-valid token (~110 s TTL) before giving up (2026-10-02). If the
  re-anchor cannot restore the origin/callback, the page is genuinely gone → the existing
  failed/roll path runs.
- **Recovery:** re-anchor (one navigation) on a live exit; roll the exit only if that fails.
- **Not exposed:** login (submits via a DOM `form.submit()` navigation — immune), warmup/apply
  (no long gap before their in-page fetch), calendar discovery and keepalive (navigation-based).

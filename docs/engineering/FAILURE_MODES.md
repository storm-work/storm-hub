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

## F5 — Proxy leases leak — **OPEN, live**
- **Class:** APP
- **Cause:** every release path is edge-triggered (terminal event, reap, explicit stop). There
  is **no level-triggered sweep** asking whether an active lease belongs to a live worker.
- **Detection:** none automatic. Found by querying the DB during this audit.
- **Current state:** 107 orphaned leases, median age 101.8 h, max 271.8 h, 0 running workers.
- **Severity:** currently 0.3% of a 32,600-row pool — low. Mechanism is unbounded, and was
  severe at 1,238 rows.
- **Improvement:** a periodic reconciling sweep in the scheduler tick. This is the correct
  shape (level-triggered, idempotent) rather than another edge-triggered call site.

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

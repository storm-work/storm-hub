# Hot-Period Booking Reliability — Phase 1 Analysis (2026-10-01)

Reconstructed from the code, the 09/25 run logs/screenshots, and the confirmed-facts memory.
Confirmed-from-evidence vs hypothesis is marked. Companion to the session-health batch shipped the
same day (quest fail-fast, discovery probe-gate, discovery origin-recovery, visible apply re-warm).

## A. Current architecture (confirmed)

Two roles, one OS process each, one state machine (`libs/engine/phases.py`,
`WorkerRole.SCOUT|APPLY`):

```
IDLE → LOGGING_IN → LOGGED_IN → [SWAPPED] → KEEPING
     → (slot-open / slot-found) → WARMING_UP → WARMED
     → DISCOVERING / POLLING / APPLYING → BOOKED      (FAILED from anywhere)
```

- **Scout** `_run_scout`: `await_slot_open` → warm once → loop { `await_discover_task` →
  `do_discover` → push found → `poll_gap` }.
- **Apply** `_run_apply`: `await_slot_open` (skipped if `slot_found_seen`) → loop {
  `await_assignment` → `do_apply` }. `do_apply` **re-warms unconditionally** every round then
  submits (in-page XHR, PT).
- **Keepalive is AUTH-ONLY** by a 2026-09-09 decision (`ops_live._do_keepalive`): while KEEPING it
  probes the authenticated **home** page; it does NOT re-verify/re-warm schedule.jsp form-state.
  `KEEPING = healthy` is now enforced (unhealthy → WARMING_UP).
- Hub/scheduler owns timing, the per-booking slot pool (`M.Slot`), scout rotation, signal latching,
  and the false-slot reverts. Everything keyed by `booking_id`. Signals carry no slot data; the pool
  carries data.

## B. Logging / observability (confirmed)

- **Timestamps already added** (2026-09-26): `_TimestampedStream`/`_ts()` in
  `apps/worker/__main__.py` prefixes every line with UTC `HH:MM:SS.mmm`; `ops._log` carries it into
  the `log_line` event stream. do_apply now emits `warming_up`→`applying` substates.
- **Gaps:** timestamps lack a date; messages are free text, not `key=value`/JSON; no per-line
  correlation fields (worker/booking/role/keepalive-mode/signal); no explicit `duration_ms`; signal
  lifecycle trace is partial (receipt + some actions, not a uniform received→accepted/ignored→
  started→completed).

## C. 09/25 hot-period findings (confirmed + hypothesis)

CONFIRMED (operator + logs): slots DID open on time; scouts surfaced NONE (discovery blindness).
Operator hand-seeded slots + fired a blind apply; some appliers reached the schedule step and got
"já tem um pedido" (spent/collided identities → "By Others"); none else booked. The apply PATH is
healthy (`confirmed_run_0925_3059`, a real %PDF booking).

CONFIRMED from screenshots: keepalive `unrecognised pre-warm response … will re-probe` loops;
`do_discover: gettime nav failed (Execution context was destroyed)`; a 503 storm on quest steps
ending in `csrf_missing after Formulario POST`; `page is not on a live portal origin … re-anchoring`.

Two independent failures (both addressed in the 2026-10-01 session-health batch):
1. Discovery blindness — degraded scouts discovering into dying sessions (503 warm-up storm +
   context-destroyed gettime).
2. Spent identities — "já tem um pedido" is usually a historical username collision
   (`username_collision_already_booked`), not a competitor; no apply change fixes a burned identity.

## D. Root-cause / failure chain (hypothesis, evidence-backed)

```
Portal hot-period instability (503 / WAF / slow render)        [CONFIRMED: 503s]
  → quest-step failures during warm-up; chain plowed past them → csrf_missing   [CONFIRMED, fixed]
  → scouts enter discovery on a degraded/off-origin session     [CONFIRMED: context-destroyed]
  → gettime dies → round FAILED → page off-origin → re-anchor loop  [CONFIRMED]
  → nothing discovered for the whole window = blindness          [CONFIRMED by outcome]
```

Direction: primarily **portal → worker** (503 is server-side). Secondary **worker → portal
amplification** existed (flat-1.5s triple-hit retries, hot re-anchor loops), reduced by this
session's backoff/fail-fast/probe-gate. Login in-window is additionally a global ~1% throttle
lottery (`login_is_a_shot_lottery`) and varies ~8× by exit geo (`proxy_geo_login_rates`).

## E/F/I. The two keepalive modes — the comparison is already decided for APPLY

The portal's schedule-page **form cursor is NAVIGATION-bound, not time-bound**
(`keeping_is_authed_only_warmup_mandatory`, `navigation_vs_inpage_fetch`). A keepalive **nav to
schedule.jsp verifies the page but does not renew the cursor** — **147 form-state lapses vs 42
restores, measured 2026-08-21** (`catalog.py` KEEPALIVE note). So a "pre-warmed, held-ready" apply
worker is NOT bookable; its submit returns the questionnaire. This is why `do_apply` re-warms every
round and why pre-warm-on-slot-found was removed. A/B measured: Test 0907 vs Test 0907-auth matched
2/2 booked each, authed-only running zero schedule re-warms over 21h.

The distinction that actually works is **traffic type, not nav target**: scouts stay warm because
their continuous `getPeriodosOcupados`/getSlots traffic RENEWS the anchor; a bare schedule.jsp nav
does not. So the genuine "Mode B" is periodic discovery-style traffic — which scouts already do and
an idle apply worker does not (the hold problem MONO_BOOKING addresses).

Verdict: for APPLY, AUTH_ONLY is proven; SCHEDULE_JSP-as-readiness is a measured dead end. For
SCOUTS, continuous discovery is the effective always-ready mode, already in place. The hot-period
lever is a FAST, ROBUST mandatory warm-up (the session-health batch) + shot-maximization + identity
freshness, NOT a keepalive-mode switch.

## G/H. Worker responsibilities + signal matrix (confirmed)

slot-open ≠ slot-found. **slot-open** = "window open, start working" (scouts warm+discover; appliers
wait for assignment; booking→RUNNING). **slot-found** = "≥1 AVAILABLE slot in the pool" (appliers get
assigned → re-warm → apply); `slot_found_seen` lets a respawned applier skip the slot-open wait.

| Worker | Signal | State | Action | Warm-up? | Target |
|---|---|---|---|---|---|
| Scout | slot-open | KEEPING | warm once, discover loop | yes (once) | DISCOVERING |
| Scout | slot-found | DISCOVERING | no-op | no | DISCOVERING |
| Apply | slot-open | KEEPING | → await_assignment | no | KEEPING(await) |
| Apply | slot-found | KEEPING(await) | assigned → re-warm → submit | yes, EVERY round | APPLYING→BOOKED |

## J. Recommendation

1. Before hot period: login, swap, reach KEEPING healthy; verify via `session_probe_ok`.
2. During: scouts discover continuously (keeps them warm); appliers re-warm-then-submit per
   assignment; no bare schedule.jsp keepalive (load without bookability).
3. Avoid during: cold re-logins where a re-warm suffices; retry storms (fixed); discovering on an
   unproven session (gated).
4. Keepalive mode: make it configurable to MEASURE, with the documented expectation that Mode B
   won't help apply. Safest level is **booking** (TEMPLATE>BOOKING layering means a template-level
   setting would silently override a booking — the 0925-class trap).
5. Biggest non-keepalive lever: identity freshness + login geo.

## K. Implementation plan (status)

- [shipped 2026-10-01] quest fail-fast + backoff, discovery probe-gate + origin recovery, visible
  apply re-warm.
- [shipped 2026-10-01] configurable `KEEPALIVE_MODE` (booking level) + a non-destructive "schedule"
  mode that MEASURES cursor survival (`cursor=present|absent`) across keepalive cycles — tests the
  147-vs-42 finding on current infra without the risky "trust the maintained cursor" change — plus
  the `round_ms` round-start→submit latency metric on every apply.
- [shipped 2026-10-01] structured logging (Phase-K item 1): ISO-8601 UTC timestamps on every line;
  `_slog(event, **fields)` → `key=value` lines auto-carrying event/role/booking/identity/ka_mode;
  lifecycle events `signal_received` (slot_open/discover/assignment), `warmup_start`/`warmup_end`
  (result/duration_ms/kind), `apply_submit` (status/len/round_ms), `discover_start`/`discover_end`
  (found/empty/failed), `keepalive` (result/cursor); a per-worker `worker context` correlation line.
  The rich free-text diagnostics are KEPT alongside (not rewritten) — deliberate restraint per §18.
- [shipped 2026-10-01] uniform signal trace with a per-signal correlation id (`sig`, minted on
  receipt and auto-stamped on every `_slog` line for that round) threaded end to end:
  `signal_received` → `signal_accepted` | `signal_ignored` (reason=stop/false_slot_open/
  slot_found_already_seen) | `signal_duplicated` (slot_open redelivery) → warmup → apply_submit /
  discover_end → `signal_result` (the apply outcome). One booking attempt is now greppable by its
  `sig` from receipt to verdict.

Phase-K is complete. Remaining future ideas live in OPEN_QUESTIONS / the Manu-advice assessment
(speculative blind apply decoupled from /slots, gated on proving the read-replica-lag hypothesis).

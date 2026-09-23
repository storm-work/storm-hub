# Open Questions

Things we do not know. Each entry states what would settle it. An entry here is not a task —
it is an admission, and several of them block work that would otherwise proceed on a guess.

---

### Q1. How often does a sticky exit move under load? — **ANSWERED 2026-08-25**
Measured: 56 sessions, 4,445 probes, 100% probe success, ~90 session-hours.
**Traffic does not preserve an exit** (`CONFIRMED_FACTS.md` §17). Webshare loses its exit at
0.278/session-hour (95% CI [0.159, 0.451]); 50-67% of sessions still held their original exit at
100 min. soax loses it at a hard ~60-minute ceiling, 20/20, under continuous traffic.
Traffic volume (30x range) and connection reuse both showed no protective effect.
**Residual unknown:** session age at t=0 was not controlled, so the shape of the webshare hazard
(clustered, not constant) is not explained. That is now Q14.

### Q2. Does an established session id actually outperform a fresh one?
The tier ordering in `_candidate_query` rests on a measurement its own file declares invalid
(`CONFIRMED_FACTS.md` §16). Sample sizes were 600 vs 12.
**Why it matters:** ~~it orders selection across a 32,600-row pool.~~
**Settled by:** a clean re-measurement with tier-1 screening off, comparable sample sizes, and
the browser as the only prober.

**PARTLY RETIRED 2026-09-15 — the pool half, by arithmetic rather than experiment.**
The question had two halves. The *pool-ordering* half is gone: soax's own params are
`sessionlength=3600` / `idlettl=900`, and **0 of 42,600 rows had `last_used_at` inside either
window** (10,010 never used; 32,590 last used > 1 h ago). No row held a live provider-side session,
so tier 1 — "established, verdict unknown" — was ranking 32,590 rows on a property *none of them
had*. The ordering could not have been delivering established sessions, because the pool contained
none. That is what made it safe to delete the rotating rows entirely (migration `ci000droprot`);
workers now mint a fresh session id per rotation, so nothing orders them any more.

**Still open: the first-use half.** Whether a *freshly minted* session id connects as well as one
already established remains unmeasured, and it is now on the critical path for every login rather
than for pool ordering. The retracted 0.5%/92% figures say nothing about it (§16).
**Scope caveat:** airtight for soax (31,600 rows, the TTLs are ours to read). Webshare's session
lifetime is provider-determined and undocumented here, so the arithmetic does not transfer — its
11,000 rows were equally dead by `last_used_at`, which is the weaker ground the same conclusion
rests on there.
**Settled by:** the same clean re-measurement, now comparing a minted id's first connection against
a deliberately pre-established one, browser-only.

### Q3. What is the ScheduleController form-state TTL?
Assumed ~90–120 s, inherited from storm-auto. **Never measured in storm-hub.**
**Why it matters:** it sets re-warm cadence and determines whether a bounce means "our form
lapsed" or "the slot is gone" — a distinction the code already encodes as `FORM_STALE` vs
`BOUNCED` but cannot currently justify numerically.
**Settled by:** warm, wait a controlled interval, submit; bisect.

### Q4. What is the portal login session TTL?
UNKNOWN. The keepalive interval (900 s) was chosen against the proxy `idlettl`, not against
any measured portal expiry.
**Settled by:** hold a session with keepalive disabled and probe until it dies.

### Q5. Why has nothing booked since 2026-08-21?
Open. Several real bugs were found and fixed in this window; none restored booking. It is not
established that the remaining cause is in the apply path.
**Blocked on:** a run that produces enough warmed workers to exercise the reverted `do_apply`.
**Do not** tune anything against measurements taken under the current regime.

### Q6. Why is booking "Test 2032" RUNNING with zero running workers?
Observed 2026-08-25 (`FAILURE_MODES.md` §F19), one minute after a Hub restart. Not diagnosed.
**Settled by:** reproducing a start and reading the launch path's logs.

### Q7. Hot holding vs loose holding
Asked by the operator on 2026-08-07 and never answered. It trades portal load and detection
risk against session freshness, and it sets the keepalive cadence defaults.
**Newly urgent:** Q1 changes the calculus — if the exit moves regardless, aggressive keepalive
buys less than assumed.

### Q8. Do concurrent bookings actually work?
Every Redis key is namespaced by `booking_id` (semaphore, breaker, signal channel, latch), so
they *should* work by construction. Never validated live.
**Settled by:** two sim bookings in parallel, asserting no cross-talk.

### Q9. What is the real per-slot capacity?
`SLOT_CAPACITY_PER_PERIOD` defaults to `"auto"`. Multi-capacity is CONFIRMED real — one slot
was leased concurrently by four workers and one booked it — but the actual capacity per period
is inferred, not known.

### Q10. Is `SCOUT_WAIT_TIMEOUT` legacy?
Flagged for audit during the original reconstruction and never resolved. It is one of the 34
keys nothing reads.

### Q11. Which of the 34 unread settings are deferred, and which are simply dead?
`TELEGRAM_*` is plainly Phase 6. `POLL_GAP_HARDENED`, `EMPTY_HOLD_DEAD_CAP`,
`REWARM_EXPIRED_CAP` and others may be storm-auto vestiges that were never ported.
**Settled by:** one pass classifying each as implemented / deferred / vestigial, then deleting
the vestigial and labeling the deferred.

### Q12. Is `libs/engine/orchestration.py` still needed?
It overlaps logic that `scheduler.py` implements inline. Unclear whether it is the intended
seam or leftover scaffolding.

### Q13. What happens at genuine scale?
The architecture targets hundreds of concurrent workers. The largest observed run is far
smaller. Untested: Postgres pool saturation (10 + 20), Chromium memory per host, `evt` stream
throughput, WebSocket fanout cost.
**Settled by:** a sim run at target worker count with `/ops/status` sampled throughout.

### Q14. Why does the webshare hazard cluster rather than stay constant?
Observed changes cluster (1.0 min, then 69-92 min) instead of arriving at a constant rate. Session
age at measurement start was not controlled — an already-bound session has less remaining life than
a fresh one, which would produce exactly this spread.
**Settled by:** repeating the measurement with session ids never used before, so t=0 is a true
binding start.

### Q15. Which holding strategy replaces the invalidated assumption? — OPTIONS ONLY, none chosen
Enumerated 2026-08-25 from the Q1 evidence. **No option is adopted.** Each states what it solves,
what it assumes, and what evidence it needs before it could be chosen.

| | Option | Solves | Assumes | Evidence needed | Cost / risk |
|---|---|---|---|---|---|
| A | **Short holds** — warm close to slot_open | exit outlives the hold (92-100% under 60 min) | warm lead time can be compressed below ~60 min | how long warming N workers actually takes, incl. login-wall retries | cheap; risks arriving unwarmed at slot_open, the failure the long lead exists to prevent |
| B | **Detect exit change, classify** | stops misdiagnosing one event as three | detection is reliable (it already is) | none — `_note_exit_ip` is already called every keepalive and discarded | cheapest; does not by itself keep any session alive |
| C | **Re-establish proxy identity** | recover the *same* exit after a move | provider can re-bind a specific exit | soax `bindttl` does this within its window; webshare offers no equivalent | limited: no mechanism for webshare, and Q1 shows soax's ceiling is absolute |
| D | **Treat exit change as a lifecycle event** — rebuild/re-auth | correctness under instability | a fresh login is affordable at that rate | the login wall rejects ~90% and two reCAPTCHA verdicts burn a sub-account | at 0.278 changes/session-hour this consumes the scarcest resource in the system |
| E | **Static ISP proxies for the hold phase** | removes the instability at its source | portal session survives the migration | **Q16** — plus ISP inventory, which we have none of | this is what storm-auto does across 113 bookings (§19); needs procurement + Q16 |
| F | **Decouple holding from exit continuity** | hold cheaply through moves | the portal does not bind session to IP | **Q16** — the same experiment | free if true; the whole hold design simplifies |

**E and F share one blocking question (Q16), and it is the same question.** Answer it first.

### Q16. Does a proxy exit change invalidate the portal session? — **ANSWERED 2026-08-25: NO**
**3 of 3 valid runs: `SESSION_SURVIVES`** (`CONFIRMED_FACTS.md` §22). An authenticated, warmed
session moved to a fresh browser on a different exit IP stayed authenticated and still rendered all
five scheduling markers. The portal carries the session on `Vistos_sid`, not on the IP.

storm-auto's inherited "validated 0724" claim is now **independently verified here**.

**Limits:** n=3, migration gaps 36.8-70.2 s (inside the form-state window), deliberate clean
migrations only, same provider and geo on both sides. It does not prove an *involuntary* mid-flight
exit change is harmless — see Q19.

**Consequence for Q15:** Option **F** (decouple holding from exit continuity) is no longer blocked
on a portal constraint that does not exist. Option **E** (static ISP proxies) is demoted from
"necessary" to "an optimisation that buys transport reliability, not session survival." Nothing is
adopted — that decision is the operator's.

### Q17. How long does warming actually take, end to end?
Option A's viability depends entirely on this, and it has never been measured. Login dominates —
the wall rejects ~90% of attempts and workers retry sticky-then-rotate — so warm lead time is
mostly *login retry* time, not the ~40 s warm itself. Planned holds of 64-116 min
(`CONFIRMED_FACTS.md` §20) suggest the lead time was chosen generously, but no measurement backs
either the 64 or the 116.
**Settled by:** instrumenting login→warmed duration per worker from existing events; the data may
already be recoverable from `var/worker-logs/`.

### Q18. Does soax support a `sessionlength` above 3600?
Our 3600 is self-imposed and carries no recorded justification (`PROXY_CONTRACTS.md`). If soax
permits a longer session, the measured 60-minute ceiling — which six of eight bookings planned to
hold straight through — may be self-inflicted rather than a constraint.
**Settled by:** provider documentation or a single generated credential at a higher value, probed
with the existing `proxy_exit_stability_probe.py`. **Do not change the pool to find out.**

### Q19. Does an INVOLUNTARY exit change behave like a deliberate migration? — **STILL OPEN**
Attempted 2026-08-26 and **not answered**; the vehicle was invalidated (`CONFIRMED_FACTS.md` §25).
The soax exit transition fired exactly as predicted (t=60.9 min) but the portal session had already
been dead ~16 min, because the session (33-45 min) is shorter than the soax exit (60 min).

**Corrected design:** webshare has no ceiling and loses an exit at 0.278/session-hour, so ~8-17% of
sessions move within their first 30 min — inside the session's own lifetime. Run ~6 webshare
sessions concurrently, probe every 2 min for ~35 min, catch any whose exit moves while still
authenticated. ~40-65% chance of at least one event, so it may legitimately yield nothing.

**Priority note:** this is now a second-order question. It matters for the minority of holds where
the exit moves inside a 35-minute window; it no longer gates the holding architecture, because #1
below does.

### Q20. Portal authentication TTL — **ANSWERED 2026-08-26: 33-45 minutes**
Two independent sessions, different providers, both lost authentication between t=33 and t=45 min
**despite an authenticated GET every 5 minutes** (`CONFIRMED_FACTS.md` §24).

**This is now the binding constraint on hold duration** — shorter than the soax exit lifetime,
shorter than every planned hold in §20 except one.

**Unresolved and important:** this cannot yet distinguish an *absolute* ~35-40 min lifetime from an
*idle* timeout where our home-page GET does not count as activity. See Q21 — and note production's
pre-warm keepalive navigates exactly that URL.

### Q21. Does an auth-gated GET refresh portal authentication? — **ANSWERED 2026-08-26: the premise was wrong**
`/VistosOnline/Pedidos` was discovered and **proven** auth-gated (59,244B with the logout link when
authenticated; 28,717B without it anonymously, same url). Two test arms probed it every 5 min; one
control probed HOME_URL every 5 min.

**All three survived ~175-180 minutes. None expired.** (`CONFIRMED_FACTS.md` §27)

- "auth-gated refreshes while HOME_URL does not" -> **NOT SUPPORTED**. The control held 174.8 min on
  HOME_URL alone, so production's keepalive url is not the inadequate request it was suspected of.
- the mechanism -> **INCONCLUSIVE**. With no expiry on either side, "both refresh" and "TTL > 180
  min" fit equally.

**What it did establish is bigger than what it asked:** Q20's 33-45 min figure does not generalise.
See Q22.

### Q22. Why did two sessions die at 33-45 min? — **ANSWERED 2026-08-26**
A single portal-side invalidation event, not a TTL (`CONFIRMED_FACTS.md` §29). Two sessions on
different providers with different identities transitioned within a 5-second wall-clock window
(01:10:50 / 01:10:55 UTC), both serving truncated bodies. A per-session TTL would have separated
them by ~1 minute.

**What it means architecturally:** a session can be invalidated at any moment by an event unrelated
to its age. That cannot be scheduled around — only detected and recovered from. Sessions are
otherwise capable of 3+ hours (§27), so nothing forces short holds.

**Residual unknown:** the nature of the event (restart / flush / deploy) and whether it recurs on a
schedule. Worth a cheap standing measurement rather than a dedicated experiment — see Q23.


### Q19 (update). Involuntary exit change — one supporting observation
`q21_test_1` moved exit involuntarily at t=109.4 min (178.99.60.149 -> 87.15.27.74) and stayed
AUTHENTICATED on the auth-gated endpoint, surviving another 69 min (`CONFIRMED_FACTS.md` §28).
n=1, and the change was detected between probes ~5 min apart. Q19 stays formally open, but the
evidence now points the same way as Q16.

### Q23. Do portal-side invalidation events recur, and on what cadence?
One event is observed (§29). If these recur nightly, holds spanning that window are futile and the
scheduler should know it; if they are rare, recovery alone suffices.
**Settled by:** the cheapest possible standing measurement — keep 2-3 authenticated sessions alive
across a full 24h and record every simultaneous invalidation. The harness already exists
(`probes/q21_auth_refresh_probe.py`). **Not a blocker for the recovery design**, which must handle
the event whatever its cadence.

## Q24 — The form-state TTL is recorded twice, 3× apart, and nobody has reconciled it

- `libs/domain/settings/catalog.py` (KEEPALIVE_INTERVAL rationale, measured **2026-08-20**, 28
  keepalive cycles on `Test 3018`): *"the portal's scheduling form state dies on a **16.1-minute
  median** clock (min 15.8, max 33.6)"*.
- `CONFIRMED_FACTS` §30 (measured **2026-08-27**, controlled two-arm probe): last ALIVE **4.17 min**,
  first DEAD **6.12 min**.

Both are ours. They disagree by ~3×, and `KEEPALIVE_INTERVAL=600.0` is defensible under one and
indefensible under the other. **It was NOT changed**, because the two bookings in §31 ran at exactly
600.0 — changing the value that just booked, on a contested measurement, is not a fix.

Resolve with a probe (the §30 shape, repeated on posto 3018 for like-for-like), not by editing the
default.

## Q25 — Why does posto 2032 answer form_stale on every slot while 3018 books?

See `CONFIRMED_FACTS` §34. Candidate variables, none isolated: posto, residence (FRA vs IRL — note
the HAR's successful booking was also FRA), `PROXY_GEOS`, and whether 2032's discovered slots were
genuinely taken. The cheapest next step is one 2032 run now that the `PRE-SUBMIT` diagnostic exists:
markers present ⇒ the cursor is fine and the slots were taken (a discovery problem); markers absent
⇒ something posto- or residence-specific destroys the cursor.

## Q26 — Does the scout/apply split model work at all?

Never fairly tested. All four split-mode bookings ran on posto **5084**, which yielded 0–4 slots
(two found none at all); all four booked zero. Every booking that ever succeeded had **no scouts**.
Posto is a total confound.

The structural concern is real and documented in `phases.py`: an idle applier does nothing but a
keepalive nav between slot-open and slot-found, which *verifies* the anchor without *renewing* it.
Measured 2026-08-27 on `Test 2032`: ESPERANCA CUNHA held **8.1 minutes** before slot-open — past the
§30 TTL, though not past the catalog's (see Q24).

Test it on a posto that has slots (3018 has 75) and read the appliers' `PRE-SUBMIT doc` line at
assignment time.

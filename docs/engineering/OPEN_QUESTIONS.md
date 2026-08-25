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
**Why it matters:** it orders selection across a 32,600-row pool.
**Settled by:** a clean re-measurement with tier-1 screening off, comparable sample sizes, and
the browser as the only prober.

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

### Q16. Does a proxy exit change actually invalidate the portal session? — THE BLOCKING QUESTION
Everything in Q15 turns on this, and it has never been tested in storm-hub.

**What we have:** storm-auto's `_swap_to_hold_pool` asserts *"booking form-state is server-side /
Vistos_sid-keyed → transfers to a fresh browser+IP while alive — validated 0724"*, and its whole
holding architecture depends on that being true. That is a **predecessor's claim**, not our
measurement, and our own `do_swap` — which implements the identical migration — has **never run**.

**Smallest safe experiment (no booking attempt, no submit):**
1. Log in and warm one worker on proxy P1 (existing code path, `WARMUP_ONLY`).
2. Capture the browser cookie jar — `get_all_cookies()`, already implemented.
3. Open a fresh browser on proxy **P2** (a different exit) and inject that jar — `get_session(...,
   inject_cookies_full=...)`, already implemented.
4. Call the existing `_probe_alive()`: navigate `Schedule.jsp?posto_id=…` and assert it does not
   redirect to `Authentication.jsp` and still carries `cmbPeriodo` / `getPeriodosOcupados`.

Outcome: still bookable on a different IP → session is **not** IP-bound → F becomes viable and E is
merely an optimisation. Redirected to `Authentication.jsp` → session **is** IP-bound → F is dead, E
becomes necessary, and the hold ceiling is a hard architectural constraint.

**This is exactly `do_swap` executed once, deliberately, against `_probe_alive` — every component
already exists and is tested.** It reaches no further than Schedule.jsp: no `ScheduleController`
submit, no `SubmeterVistoCriaPDF`, so it cannot book anything. Cost: one login, one warm, two
proxies, ~5 minutes. It needs a real login, so it consumes one sub-account login attempt.

**Deliberate limitation:** it tests lifetimes 1 and 2 surviving a *deliberate* migration. It does
not prove an *involuntary* mid-flight exit move behaves identically — the portal may treat a clean
new connection differently from a connection whose address changes underneath it. That is a
follow-up, not a reason to delay this.

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

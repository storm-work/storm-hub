# Open Questions

Things we do not know. Each entry states what would settle it. An entry here is not a task —
it is an admission, and several of them block work that would otherwise proceed on a guess.

---

### Q1. How often does a sticky exit move under load?
We now know it **can** move inside 10 minutes while actively used (`CONFIRMED_FACTS.md` §1).
We do not know the distribution — one credential, one 21-minute window.
**Why it matters:** it determines whether holding is viable at all, or whether the design must
treat exit loss as the normal case. This is the single highest-value unknown in the system.
**Settled by:** N credentials probed on a fixed cadence for several hours, recording
time-to-first-move. Cheap, offline, no portal contact.

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

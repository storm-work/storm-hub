# Decisions

Deliberate engineering choices. A decision is not a fact about the world — it is a commitment
we made, with reasoning, that can be revisited when the reasoning changes. Each entry records
what would reverse it.

---

### D1. Workers never touch the database
The Hub is the only DB client. Everything a worker needs arrives in its `launch` payload;
everything it reports leaves as a bus event.
**Why:** a worker is then cheap to kill, cheap to relaunch, and cannot corrupt shared state or
exhaust the connection pool. It is the property that makes every other resilience mechanism
possible.
**Reverse if:** never, without a redesign of the whole resilience model.

### D2. The session id is the proxy's identity
Hold it to keep an exit; change it to get a different one. Log it. Never invent one.
**Why:** the endpoint is constant across every credential, so it identifies nothing; the
session id is the only thing that selects an exit.
**Reverse if:** a provider is added whose stickiness is not credential-encoded.

### D3. Never raw-probe an exit we intend to browse from
Tier-1 quick-screen stays disabled by default.
**Why:** measured 83% → 0% (`CONFIRMED_FACTS.md` §4).
**Reverse if:** primp becomes our primary transport, so the fingerprint stays consistent —
which is exactly why storm-auto can screen this way and we cannot.

### D4. Submit by navigation, never by in-page fetch
The proven apply round is navigation + navigation.
**Why:** an in-page fetch is origin-dependent and dies permanently on an opaque origin; a
navigation is origin-independent. Also, storm-auto's own comment: a bare POST can be blocked
regardless of proxy quality while a real navigation is trusted.
**Reverse if:** never, on current evidence. A HAR showing the portal's own XHR succeeding is
**not** evidence that *our* XHR is trusted — that inference was made once and was wrong.

### D5. Only `%PDF` bytes prove a booking
Reaching `MostrarPdf` is not success.
**Why:** verified against the portal. Inventing a booking is strictly worse than missing one.
**Reverse if:** never.

### D6. Confirm before marking
A signal is latched as sent only after the send call returns successfully; a worker is marked
RUNNING only after both spawn and consumer-group creation succeed.
**Why:** marking first and acting second produces silent starvation that is invisible after
the fact.
**Reverse if:** never — this is a general correctness pattern, applied in several places.

### D7. The signal latch is durable across a Hub restart but must be cleared on a *worker's*
restart
**Why:** the latch correctly survives a Hub restart. It wrongly also survived a worker's own
restart, starving every relaunched worker of `slot_open`/`slot_found` forever.
**Mechanism:** `_reset_worker_bus_state`, which also clears the stale command stream.

### D8. Every `/start` is a fresh run
It clears the slot latch, retires the previous run's slots, and re-warms. It does not resume
into RUNNING.
**Why:** operator decision, 2026-08-14 — deliberately replaces the earlier resume shortcut.

### D9. Checkpoints live on the filesystem, never in Redis
`var/checkpoints/`, atomic tmp-write + rename, keyed by `identity_id`.
**Why:** payloads carry full cookie jars that have no business inflating the Redis working
set; file storage is what is proven in storm-auto (113 bookings). Keyed by identity because a
worker owns one identity for its whole life, while the sub-account picker can drift.
**Reverse if:** never for Redis. The keying may change — but only via D14.

### D10. A throttle that resolves below its floor raises, never clamps
`BelowMinimum` in the settings resolver.
**Why:** `HOLD_POLL_GAP: 0.0` once drove a worker to 150–280 writes/sec for 66 minutes.
Silently clamping to a working value hides the bug that produced it.
**Reverse if:** never.

### D11. Redis is `noeviction` with an explicit `maxmemory`
**Why:** loud failure beats silent data loss. Eviction on a control-plane stream would drop
commands and events with no error anywhere.
**Reverse if:** never, without a durability design for the streams.

### D12. `held` is not a failure
It means the phase machine ran to a clean end without booking.
**Why:** mapping it to FAILED showed healthy workers red and burned their respawn budget.
**Reverse if:** never — it is a distinct outcome from FAILED (something broke) and STOPPED
(someone decided).

### D13. `STOPPED` is not `COMPLETED`, for both bookings and workers
Stop is resumable; completed is terminal.
**Why:** conflating them made a stopped booking un-restartable — restart silently no-opped.

### D14. Format/keying migrations ship as offline, assert-driven, reversible code first
Never as a live operation against a running booking.
**Why:** a live, unverified checkpoint-keying change against a running booking is what this
project was doing in the last captured minutes before its original catastrophic data loss.
**Pattern:** `probes/checkpoint_format_probe.py`, ported from storm-auto's save/resume probes.

### D15. Destructive cleanup is scoped, reviewed, dry-run-by-default tooling
`scripts/cleanup_checkpoints.py` is the only sanctioned way to clear checkpoints.
**Why:** the original data loss was a human fixing an infrastructure problem by hand, at
speed, with an unscoped `rm`. No ad-hoc `rm -rf` against a production path, ever.

### D16. Live-apply testing books REAL appointments
The assistant does not start bookings autonomously; it hands the operator the exact command.
**Why:** these are real appointments for real people.

### D17. Never attribute a portal failure to portal downtime
Diagnose the proxy exit, the IP, or our own session handling first.
**Why:** standing operator instruction, correct every time it has been tested. `RedisBreaker`
is deliberately zero-HTTP-aware so the classification stays at the call site where this rule
is enforceable.

### D18. Live-vs-sim engine is an explicit per-worker Hub decision
`_engine: "sim" | "live"` in the launch payload — never an env var, never a default-to-live.
**Why:** live execution must be auditable to whoever launched the worker.

### D19. `is_healthy` is not a selection filter
**Why:** a row is a session template, not an IP; a failed probe describes an exit, not the
row. Filtering discarded 639 good rows once.

### D20. A lost claim race is not a health failure
Counted separately in `lease_proxy`.
**Why:** conflating them reported pool exhaustion during ordinary contention.

### D21. The apply path is reverted verbatim to the last commit that booked
`do_apply` is `ad1c570`'s implementation, with exactly one deviation: two
`return "bounced"` → `return "portal_unreachable"` so our own gates are not charged to the
slot.
**Why:** operator instruction after repeated failed fixes. Speculative changes to the apply
path are suspended.
**Status:** **still untested live** — no run since the revert has produced enough warmed
workers to exercise it.

### D22. INVALIDATED — "keepalive preserves the proxy exit"
**This was never written down as a decision; it was an unexamined assumption underlying the hold
design, and it is now measured false** (`CONFIRMED_FACTS.md` §17).

Recorded here explicitly so it cannot be silently re-adopted. Keepalive acts on the portal auth
session and the server-side form state. It has no measurable effect on the proxy exit, and cannot
extend a soax proxy session past its `sessionlength` ceiling.

**What replaces it is not yet decided.** Options are enumerated in `OPEN_QUESTIONS.md` Q15 with
their evidence requirements; none is adopted, and the deciding evidence (Q16) has not been gathered.

### D23. Analysis-phase boundary, 2026-08-25
Q1's result was mapped onto the architecture **without** changing holding logic, proxy selection,
proxy configuration, worker lifecycle, `do_apply`, or the vendored engine — by operator instruction.
Documentation and memory only.
**Why it matters:** the strongest finding (§19-§20) points at an obvious-looking change — enable
`do_swap` onto ISP proxies. That change rests on a claim we have inherited but never verified here
(§18, row 2), and acting on it before Q16 would repeat the pattern this project is trying to break.

### D24. The portal session is cookie-bound, not IP-bound — verified here, not inherited
**CONFIRMED 2026-08-25** (`CONFIRMED_FACTS.md` §22, 3/3). `Vistos_sid` carries the session across a
browser change and an exit change.

This retires an assumption that shaped the whole holding design. **It does not by itself choose a
holding strategy** — Q19 (involuntary changes) and Q20 (session TTL) are still open, and Q20 is now
the binding constraint on how long a worker can usefully hold.

**Reverse if:** Q19 shows involuntary changes behave differently, or the portal changes its session
handling.

### D25. `sessionLost.jsp` is a first-class portal verdict
The portal has a dedicated endpoint for "your server-side session is gone". It is distinct from an
`Authentication.jsp` redirect and was previously unclassified, falling through to "inconclusive"
wherever it appeared.
**Why it matters:** it is the signal that separates *session expiry* from *auth rejection* from *WAF
challenge* — three things F21 says the worker must tell apart. The Q16 probe classifies it; the
production classifier in `ops_live.py` does **not** yet. Recorded, not implemented, per the
analysis-only boundary.

### D26. Persist raw evidence, never conclusions
Every probe in this project writes its own byte counts, marker lists, URLs and errors, and reports
re-derive verdicts from those offline (`reclassify()`).
**Why:** on 2026-08-26 the live classifier accepted any home marker as proof of authentication and
labelled logged-out pages AUTHENTICATED for 25+ minutes on two arms — a wrong result that was
reported to the operator before being caught. Because the raw evidence was persisted, the fix cost
one function and zero re-spent logins. Had the probe stored only its verdicts, the night's data
would have been unrecoverable.

### D27. A measurement whose vehicle is invalidated is reported as NOT ANSWERED
Q19's soax transition fired exactly on schedule and still answered nothing, because the session had
already expired. The result recorded is "not answered, here is why, here is the corrected design" —
not a verdict extracted from an invalid observation (`CONFIRMED_FACTS.md` §25).

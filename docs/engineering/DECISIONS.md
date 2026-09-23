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

**Superseded 2026-08-26 by D32.**

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

**Implemented 2026-08-26 (Phase 1, under D32's recorded exception):** `ops_live.py`'s
`classify_session_state()` now recognises `sessionLost.jsp` in the keepalive loop — see
`FAILURE_MODES.md` F22. Recognition in the login path (the vendored session's own classifiers)
remains as it was; this note covers the keepalive loop only.

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

### D28. A verdict must be positively evidenced, never inferred from "not recognised"
Q21's classifier originally fell back to `bytes > 0 -> LOGGED_OUT`. It fired exactly once, on a
1,920-byte body with no markers, no password field and no redirect — and would have recorded a
session expiry that never happened, at precisely the moment the experiment was looking for one.
**Rule:** AUTHENTICATED requires the auth marker; LOGGED_OUT requires a positive logged-out signal;
everything else is INCONCLUSIVE.

### D29. Hold duration is not a constant, and must not be designed around as one
Measured lifetimes differ 4x across twelve hours with no known cause (Q22). Until that is
understood, the holding architecture must **tolerate a session dying at any moment** rather than
plan around a number. This retires both the "~40 minute ceiling" reading of §24 and any temptation
to rely on the ~180 minute figure from §27.

### D30. Session-state classification is positive-evidence based
**THE RULE:** `SESSION_AUTHENTICATED` and `SESSION_EXPIRED` are explicit states, each established
only by evidence that has been independently validated against a known-authenticated and a
known-logged-out response. An unrecognised response is `UNKNOWN` — **never implicitly LOGGED_OUT,
and never implicitly healthy.**

**Two historical failures are why this rule exists, both from this project, eight hours apart:**

1. *Any-marker instead of all-markers* (Q20, 2026-08-26). The classifier accepted `solicitar pedido
   de visto` as proof of authentication. It is a **public** menu item, present on the logged-out
   page. Logged-out pages were reported AUTHENTICATED for 25+ minutes on two arms, and the wrong
   result was reported to the operator before being caught.

2. *Unrecognised inferred as logged-out* (Q21, 2026-08-26). The classifier fell back to
   `bytes > 0 -> LOGGED_OUT`. It fired once, on a 1,920-byte body with no markers, no password
   field and no redirect — and would have recorded a session expiry that never happened, at exactly
   the moment the experiment was hunting for one.

The two errors point in **opposite directions** — one manufactured health, the other manufactured
failure — which is the argument for the rule rather than for any particular marker. Both were
recoverable only because raw evidence was persisted (D26).

**Validated markers are listed in `ARCHITECTURE.md` §11.1.** Adding a signal to that table requires
measuring it against both a known-authenticated and a known-logged-out response first.

### D31. An exit change is informational, not fatal
Current evidence — Q16 (3/3 deliberate migrations survived) and Q19 (n=1 involuntary change, session
survived a further 69 min on an auth-gated endpoint) — says an IP change does not invalidate a
portal session. **An exit change must therefore not trigger re-login or session rebuild.**
`_note_exit_ip` should feed the classifier as context, not as a failure trigger.
**Reverse if:** a repeat of Q19 at larger n shows involuntary changes behaving differently.

### D32. D23 reconciled: Q16 satisfied, Q22 changes the reason for caution
D23's stated condition (Q16) is satisfied and independently verified here, not merely inherited.
D23 is **superseded, not simply lifted**, because Q22/D29 introduced a new, evidence-backed reason
for caution that D23 could not have anticipated (it predates Q22 by a day): an authenticated
session can be invalidated by a portal-side event unrelated to proxy behavior, of unknown cause and
unknown recurrence.

**Authorized by this decision:** proxy-subsystem ARCHITECTURE DESIGN — specification, evaluation of
prior proposals against evidence, architectural modeling, comparison of alternatives, a written
design, and any experiments needed to answer a design question. **This does not authorize
implementation.**

**Still frozen, and why (each verified against the current repository, not assumed):**
- Enabling `do_swap`, or any change to active proxy selection, active proxy configuration, default
  providers, or static/ISP routing. Two independent reasons, either one sufficient on its own: no
  static/ISP proxy inventory exists to route traffic onto (`PROXY_CONTRACTS.md`: "we own none"),
  and Q22/D29 show proxy behavior is not the sole, or even the dominant, explanation for a held
  session dying — a proxy-focused fix would not address the threat that actually matters most.
- `do_apply` — independently frozen by D21, on D21's own grounds. **D32 does not lift D21.**
- The vendored engine and worker lifecycle beyond the keepalive loop — no evidence anywhere in the
  current ledger argues for touching either; they stay frozen by default, not by a specific new
  argument.
- Holding *duration* — no fixed-duration strategy is adopted. D29 stands: current evidence does not
  support planning around a number.

**The Phase 1 exception, recorded for the historical record:** D23's freeze list named "holding
logic" among the frozen areas. Phase 1 (2026-08-26) nonetheless changed `_do_keepalive`'s
classification logic — implementing `ARCHITECTURE.md §11`'s already-written design — under an
explicit, narrow operator instruction scoped to exactly that one change. This was not a
reinterpretation of D23 by inference; it was a fresh, specific instruction authorizing one carved-out
exception. It does not mean the rest of the holding architecture, or holding duration, was unfrozen
alongside it.

**Reverse if:** Q23 shows the portal-side invalidation event recurs on a predictable cadence (this
would reopen the holding-design conversation on new terms), or static/ISP inventory is procured
(removes one of the two independent reasons `do_swap`/proxy-config changes stay frozen — the other,
Q22/D29, would still apply on its own).

### D33. Proxy architecture evolution, Phase 3: four scoped structural changes, implemented under D32
D32 authorized proxy-subsystem ARCHITECTURE DESIGN only. A separate, explicit operator instruction
(Phase 3, 2026-08-26) then authorized implementing exactly four items drawn from that design — this
decision is the record of what was actually built, not a new authorization in itself.

**Implemented:**
1. **`Proxy.resource_type` ('rotating' | 'static')**, migration `cd000resourcetype`, DB CHECK
   constraint. Every existing row defaults to `'rotating'`. `libs/proxies/lease.py`'s
   `_candidate_query` now scopes D19's "health is never a filter" rule to `resource_type='rotating'`
   only — a `static` row with `is_healthy=False` is excluded, on the reasoning that a static/ISP
   resource is a genuinely finite, individually-owned thing (a bad probe means THIS resource is
   bad), unlike a rotating session template (a bad probe means the exit BEHIND it moved). No static
   inventory exists (`PROXY_CONTRACTS.md`: "we own none"), so this is a no-op today and takes effect
   only once a static row is actually imported.
2. **Worker-level proxy setting overrides**, via the *existing* settings resolver — no second
   precedence system. `PROXY_PROVIDER`/`PROXY_GEOS`/`PROXY_KIND`/`HOLD_PROXY_PROVIDER`/
   `HOLD_PROXY_GEOS`/`HOLD_PROXY_KIND` gained `L.WORKER` to their `override_layers`. This required
   adding the WORKER layer's one missing input mechanism: `Worker.settings_json` (migration
   `ce000workersettings`, mirroring `WorkerTemplate.settings_json`'s existing pattern), validated at
   `attach_identity` via `validate_layer(L.WORKER, ...)`, and wired into `_do_launch_worker`'s
   `layers` dict. This gap (the resolver's precedence was already correct end-to-end, but nothing
   could ever populate the WORKER layer for these six keys) was discovered during implementation,
   not anticipated by the design phase, and was the one point this phase's own stop-condition list
   was checked against before proceeding — it did not create a new precedence model, only wired an
   already-designed, already-unused input layer.
3. **`libs/proxies/credentials.py`**: `build_soax_credential`, `build_webshare_credential`,
   `soax_session_id` — relocated from inline f-strings in `scripts/gen_proxy_pool.py`, which is now
   a thin CLI wrapper (argparse + country-splitting) around the same module `libs/proxies` exports.
   Grammar is byte-for-byte unchanged (golden-sample tests pin the pre-relocation format); the CLI
   still runs as a real subprocess and still writes to `data/proxies_{provider}.txt` by default.
4. **Static-only post-swap validation**: when `HOLD_SWAP_AFTER_FOUND=True`, `_do_launch_worker` now
   requires at least one active `resource_type='static'` proxy matching the resolved
   `HOLD_PROXY_PROVIDER`/`HOLD_PROXY_KIND` *before* attempting to lease one — fails with `409` and
   releases the already-succeeded phase-1 (warm) lease, rather than leasing a rotating proxy as a
   silent fallback. Because no static inventory exists, this rejects every `HOLD_SWAP_AFTER_FOUND`
   launch today; that is judged correct, not a regression — the alternative (silently using a
   rotating exit for the post-swap/hold pool) is exactly the instability the swap exists to move
   away from.

**Explicitly NOT done, per the phase's own scope:** no static/ISP inventory was procured or
enabled; `do_swap` was not enabled by default (D32/D21 stand); `do_apply`, holding duration, the
vendored engine, worker lifecycle, and Redis/Postgres orchestration were untouched; `sessionlength`/
`bindttl`/`idlettl` values were not changed (Q18 remains open, not addressed here); no universal
`ProxyProvider` class hierarchy was introduced (`credentials.py` is functions, not a provider
abstraction — evaluated and rejected as unnecessary for two providers with genuinely different
grammars).

**Backward compatibility, verified by test:** every pre-existing proxy/settings/launch test passes
unchanged in behavior (`resource_type` defaults to `'rotating'`, `Worker.settings_json` defaults to
`{}` and is skipped entirely from `layers` when empty, `HOLD_SWAP_AFTER_FOUND=False` launches never
reach the new validation branch at all). One pre-existing test
(`test_live_launch_leases_hold_proxy_only_when_swap_enabled`) required a fixture update — its
hold-pool proxy fixture had to become genuinely `resource_type='static'` to remain a *valid*
`HOLD_SWAP_AFTER_FOUND` configuration under item 4's new, correct rule; its original intent (hold
proxy gets leased when swap is enabled) is unchanged.

**Reverse if:** none of these four items individually — each is additive and independently
reversible (drop the migration, drop the override_layers widening, revert the script to inline
credential construction, remove the validation branch) without touching the others.

### D34. The static-hold invariant is enforced at every lease/rotation, not only at launch — Phase 5
The Phase 4 audit found D33's `resource_type='static'` guarantee held only at a worker's initial
hold-proxy lease. Re-investigation while implementing this decision found the gap was more precise
than that: even the launch-time lease itself was never actually constrained to `resource_type=
'static'` — `_do_launch_worker`'s pre-lease count check only proved a static row existed somewhere in
the pool; the `lease_proxy` call that followed it had no `resource_type` parameter to enforce with,
and passed only by the coincidence of the existing test fixture's phase-1 lease exhausting the only
matching rotating row. Both the count check's true intent and the mid-hold rotation gap are the same
underlying defect: **`resource_type` was a filter inside `_candidate_query`'s health scoping (D33),
never an explicit requirement any caller could ask for.**

**Implemented:**
1. `lease_proxy`/`_candidate_query` (`libs/proxies/lease.py`) gained an optional `resource_type`
   parameter — an exact-match `WHERE` clause, `None` by default (identical behavior to every
   pre-Phase-5 caller). This is a genuinely new capability, not a reuse of D33's health-scoping
   `or_(...)` clause, which only ever excludes an unhealthy static row from a mixed pool and was
   never a requirement that a lease actually return one.
2. `_do_launch_worker` (`apps/hub/main.py`) now passes `resource_type="static"` on the hold-phase
   lease itself, not only the pre-check — the pre-check remains, for its clearer 409 message, but is
   no longer the only thing standing between a mixed pool and a silently-rotating hold proxy.
3. `_handle_proxy_request` (`apps/hub/events.py`) now (a) includes the WORKER settings layer in its
   `layers` dict, matching `_do_launch_worker` exactly — a worker's override on any of the six Phase 3
   proxy settings now applies identically at launch and at every subsequent rotation, closing the
   configuration-inconsistency half of the Phase 4 finding; and (b) requires `resource_type="static"`
   on a `phase="hold"` lease whenever the resolved `HOLD_SWAP_AFTER_FOUND` is on, mirroring
   `_do_launch_worker`'s own gate exactly, including its use of the deprecated alias specifically (see
   the note below). No rotating fallback: a static-required request with no static candidate fails
   cleanly via the existing `proxy_unavailable` reply, exactly like ordinary pool exhaustion.
4. **A static hold identity is never locally rolled.** `roll_sticky_session` (D2) is a grammar-based
   string transform with no concept of `resource_type` and structurally cannot be given one without
   the worker gaining DB-shaped knowledge (D1 forbids this). Instead, a new runtime-only fact —
   `hold_proxy_static: bool` — rides the existing launch payload (set by the Hub from whether a hold
   proxy was leased at all, since one is now only ever leased static) and the existing
   `proxy_assigned` bus-command payload (`resource_type`, set by the Hub from the leased row, carried
   through `wait_proxy`'s reply and applied by `_request_proxy_rotation` on every successful "hold"
   reply). `do_swap`'s retry ladder (`apps/worker/ops_live.py`) now skips the local roll attempt
   entirely when `self.hold_proxy_static` is true, falling straight through to the existing
   `_request_proxy_rotation("hold")` Hub round-trip — which, per item 3, now itself enforces
   `resource_type='static'`. Nothing about this required a Proxy model, a provider class, or any new
   persistence layer: two plain fields riding two payloads that already exist.
5. `_request_proxy_rotation`'s previously-discarded failure reason (Phase 4 finding) is now logged —
   the Hub already generated and transmitted it; nothing consumed it.

**Explicitly NOT done:** no static inventory was procured or enabled (none exists); `do_swap` was not
redesigned, only its retry ladder's one roll-vs-rotate decision point; no change to `do_apply`,
holding duration, worker lifecycle, or the vendored engine; no migration (both new facts are runtime
payload fields, never persisted); the canonical/deprecated `HOLD_SWAP_AFTER_WARMUP`/
`HOLD_SWAP_AFTER_FOUND` alias semantics were not touched.

**Discovered, not fixed (out of this decision's scope):** `_do_launch_worker`'s hold-lease trigger
(`main.py`) and this decision's new `_handle_proxy_request` gate both key off the *deprecated*
`HOLD_SWAP_AFTER_FOUND` alias specifically — `libs/engine/config.py`'s `EngineConfig` (the worker's
own `do_swap()` gate) correctly OR's both spellings, but the Hub-side launch/rotation gates do not. An
operator setting only the canonical `HOLD_SWAP_AFTER_WARMUP` would make the worker believe swap is on
while the Hub never leases a hold proxy at all. Pre-existing, unrelated to the launch-vs-rotation
invariant this decision closes; flagged here rather than silently reconciled, per this project's own
epistemic discipline (`SYSTEM_MEMORY.md`).

**Corrected 2026-08-26 by D35: this framing was itself wrong.** `HOLD_SWAP_AFTER_FOUND` was not a
legitimate alias whose precedence merely needed reconciling with `HOLD_SWAP_AFTER_WARMUP` — it named
a trigger point the code never had, and was never meant to be a second permanently-supported spelling.
D35 removes it outright. This paragraph's diagnosis (the Hub-side gates and the worker's own
`EngineConfig` gate read different keys) was accurate as a description of the bug; its framing of the
fix — reconciling which of two settings wins — was not the correct resolution. See D35.

**Still unresolved, unchanged by this decision:** static resource reactivation (no mechanism exists
for a static row once marked unhealthy — still whole-pool manual `POST /proxies/reset-health` only);
the static/ISP credential grammar itself (still undefined — no provider to validate against); the
dashboard's lack of a `resource_type` UI surface; F5's lease-leak sweep; Q13; Q18. None of these were
in this decision's scope, and closing the launch-vs-rotation invariant does not make static
procurement safe on its own — see the remaining items above.

**Reverse if:** none of the five items individually — each is additive (an optional parameter with a
backward-compatible default, two new payload fields, one narrowed retry-ladder branch) and
independently reversible without touching the others.

### D35. HOLD_SWAP_AFTER_FOUND is removed outright — it was never a legitimate alias
Every prior phase (D33, D34) treated `HOLD_SWAP_AFTER_FOUND` as a deprecated-but-still-honoured
spelling of `HOLD_SWAP_AFTER_WARMUP`, kept alive by an OR in `libs/engine/config.py` on the theory
that dropping a key an operator had already saved would make the resolver reject the whole layer.
That theory was correct about the *mechanism* (an unknown key does reject the whole layer) but wrong
about the *conclusion*: the fix for a bad key an operator already saved is to remove the key, not to
keep two settings semantically alive indefinitely. `HOLD_SWAP_AFTER_FOUND` was never a second
supported spelling — it named a trigger point ("after found") the code never actually implemented
(the swap fires right after warmup, unconditionally, per `HoldSwapConfig`'s own comment), and its
continued existence was actively harmful: it let `_do_launch_worker`'s hold-lease trigger and (as of
D34) `_handle_proxy_request`'s hold-rotation gate drift onto reading the deprecated name specifically
while `EngineConfig`'s own `do_swap()` gate read both, OR'd — an inconsistency D34 diagnosed but
deliberately did not fix, reading it as a precedence-reconciliation question. It was not one.

**Removed:** the catalog entry, the resolver-visible key, the `EngineConfig` OR-fallback, every
Hub-side (`main.py`, `events.py`) and worker-side (`ops_live.py`) code/comment reference, and the
dashboard's booking-settings checkbox, which read AND wrote `HOLD_SWAP_AFTER_FOUND` specifically
(`apps/dashboard/src/App.jsx`) — a real, functional dependency, not documentation. `HOLD_SWAP_AFTER_WARMUP`
is now the sole recognized key anywhere in the system; setting the old name at any layer now raises
`UnknownSetting`, exactly like any other unrecognized key, with no special-cased rejection message.

**No migration:** `settings_json` (bookings, templates, workers) is an opaque JSON column with no
per-key schema field (`libs/db/models.py`) — there was never a column named after this setting to
drop. This is a pure Python/catalog change.

**Operational risk, disclosed rather than resolved here (repository evidence does not settle it):**
any *already-stored* booking or template `settings_json` blob in the live production database that
still contains a literal `"HOLD_SWAP_AFTER_FOUND"` key (plausible — the dashboard checkbox wrote
exactly this key before this decision) will now fail to resolve at all — `resolve()` validates every
key in every stored layer, so one unrecognized key rejects that booking's *entire* settings
resolution, not just the swap toggle, blocking every future launch for that booking until an operator
edits the stored JSON to drop the key. **UNKNOWN:** whether any such row currently exists in
production — this cannot be determined from the repository, and checking the live database is outside
this phase's read-only/no-production-access boundary. **Recommended precaution before deploying this
change** (not performed here): a one-time read-only query of `bookings.settings_json` and
`worker_templates.settings_json` for the literal string `HOLD_SWAP_AFTER_FOUND`, and if found, an
operator-run `UPDATE` stripping the key (or setting `HOLD_SWAP_AFTER_WARMUP` to the same value it
held) before this code deploys.

**Reverse if:** never, as a reintroduction of the alias — `HOLD_SWAP_AFTER_FOUND` must not come back
as an alias, a fallback, or a second precedence system. If the deployment risk above is ever realized
(a booking fails to launch on this exact `UnknownSetting`), the fix is a one-time data correction on
that row, not restoring the setting.

### D36. Static resource reactivation — Phase 8, D-NEW-1
Before this decision, a `resource_type='static'` row that failed one health probe (D33) was excluded
from `_candidate_query` **permanently** — the only reset was the operator's manual, whole-pool
`POST /proxies/reset-health`. `apps/hub/scheduler.py`'s `_reprobe_static_proxies` (600s interval,
operator decision, Phase 8) now periodically re-probes exactly that excluded set —
`resource_type='static' AND active AND is_healthy=false`, ordered `last_used_at ASC NULLS FIRST` —
using the existing `check_proxy`/`apply_result` unchanged, with no row lock held during the probe.

**No new persisted state.** `last_used_at` is frozen at the last failed-probe timestamp for exactly
these excluded rows (nothing else ever touches it while they're excluded), so it already provides
"oldest still down first" ordering with no new column.

**No new race.** A row stays invisible to `lease_proxy` for as long as it's unhealthy (D33's own
query-level exclusion); the instant this sweep flips it healthy, it becomes an ordinary candidate,
claimed through the unmodified `_claim_locked`/`FOR UPDATE SKIP LOCKED` path — this sweep never
claims or leases anything itself.

**Does not touch `resource_type='rotating'` rows** — structurally, by the query's own `WHERE`
clause, not by convention. D19 is unaffected; proven by regression test
(`test_reprobe_static_proxies_never_touches_rotating_resources`).

**Reverse if:** never expected, but trivially — remove the two scheduler calls and the method;
`find_reactivation_candidates` (`libs/proxies/lease.py`) has no other caller and no side effects of
its own.

### D37. F5 lease reconciliation — Phase 8, D-NEW-2
Closes F5 (`FAILURE_MODES.md`). `apps/hub/scheduler.py`'s `_reconcile_orphaned_leases` (300s grace
period AND 300s sweep interval — one operator-supplied number used for both, not two independently
invented values) periodically releases any `ProxyLease` whose `released_at IS NULL`, whose owning
`Worker.status != RUNNING`, and whose `leased_at` is older than the grace period.

Reuses `release_worker_resources` (via the same `_release_worker_resources` wrapper the three
existing reap paths already call) — no second, lease-only release path. Idempotent by construction:
`release_proxy`'s own `WHERE released_at IS NULL` makes a repeat call on an already-released lease a
harmless no-op, proven by test (`test_reconcile_orphaned_leases_is_idempotent_across_two_runs`).

**No new persisted state.** `ProxyLease.leased_at`/`released_at` and `Worker.status` already existed;
`Worker` still has no `updated_at` (`TimestampMixin` provides only `created_at`) and none was added —
the lease's own `leased_at` is the age reference, not a worker-side timestamp.

**Two confirmed leak paths this closes** (traced in Phase 7, restated in `FAILURE_MODES.md` F5):
`events.py`'s `_release_on_terminal` silently swallowing a `release_worker_resources` exception with
no retry, and `main.py`'s `_do_launch_worker` setting a worker `FAILED` after a successful lease
without releasing it, leaving a worker that never touches `RUNNING` and is therefore invisible to
every one of the three existing reap paths. Both are covered by dedicated regression tests
reproducing the resulting state (not the originating code path, which lives in files this phase does
not touch): `test_reconcile_orphaned_leases_recovers_launch_failure_after_lease_worker` and
`test_reconcile_orphaned_leases_recovers_terminal_release_failure_worker`.

**Operator parameters (Phase 8, explicit decision, not derived from `_DEAD_GRACE` or any other
existing constant):** reactivation interval 600s (D36), reconciliation grace period 300s (this
decision) — both are pure tuning parameters; changing either later is a config change, not an
architecture change.

**Reverse if:** never expected, but trivially — remove the two scheduler calls and the method;
`find_orphaned_worker_ids` has no other caller and no side effects of its own.

### D32. No real credential is ever a literal in the repository — and rotation, not rewriting, is containment
Credentials come from the environment (`.env`, gitignored) or a secret store. Tests use obvious
dummies (`test-password`); no test needs a real value. Scripts read from the environment and refuse
to run when it is unset, and never take a secret as a command-line argument (argv is visible in
`/proc` and in shell history).

**When a credential does leak (F27):** rotate first. Rewriting history reduces *further* exposure
but cannot un-publish what was already fetched, so a rewrite is never a substitute for rotation and
must never be reported as containment.

**Do not reuse one password across systems.** In F27 a single literal exposed both Postgres and an
admin login to an internet-reachable Hub, turning one mistake into two compromises.

### D38. A gate is enforced where the resource is spent, and the dashboard only renders the verdict
Eligibility rules (which identity may launch, which sub-account may be used, which action a worker
status allows) live in `libs/domain`, are enforced by the endpoint that spends something real — a
proxy lease, a CAPTCHA solve, a subprocess — and reach the dashboard as a computed field or a
mirrored table with a parity test behind it. The dashboard filters on that answer; it never derives
one from whatever field looks close enough.
**Why:** every instance of the opposite has cost a live run. `WORKER_ACTIONS` was a UI table the
`detach` endpoint never enforced (F-detach). The attach panel gated on `Identity.status === "active"`,
which only ever meant "sub-account rows exist", and no launch-side check existed at all (F37). A UI
gate is a suggestion: it is bypassed by a stale page, a direct API call, a scheduler path, or a
second panel written later.
**How this shows up in review:** a filter in `App.jsx` that names domain values (statuses, roles,
thresholds) is the smell. Either the server should answer it, or the shared table needs a parity
test — `tests/hub/test_action_guard_parity.py` and
`tests/hub/test_sub_account_readiness_parity.py` are the two worked examples.
**Reverse if:** never for anything that spends a resource. Purely cosmetic grouping (colour, sort
order) is exempt.

### D39. Rotating proxies are not inventory — the worker mints its own session id
**Operator direction, 2026-09-15.** A rotating "proxy" was never an exit; it was a *session
template*, and the session id inside it is a string we choose. Storing 42,600 of them as DB rows
modelled them as if they were finite resources. They are not, and the codebase had said so for
months without acting on it (`libs/proxies/lease.py:153`, D19, `libs/proxies/health.py:121-140`:
"There is no row-level subject to screen").

**The change:** rotating pools have no rows. `libs/proxies/rotating.py::mint_rotating_url` builds a
credential from global settings at launch; `roll_sticky_session` mints a new 16-alphanumeric id
locally whenever a worker needs a different exit. The Hub is not in the path — `_do_launch_worker`
mints instead of leasing, and `_handle_proxy_request` refuses a rotating request outright rather
than silently minting Hub-side. Migration `ci000droprot` deletes the rows.

**Static/ISP proxies keep the entire leasing and health system, unchanged.** That is the point of
the split, not an exception to it: a static row is a genuinely finite, individually-owned IP, so a
failed probe has a real subject and a per-row verdict means something. `lease_proxy` and
`_candidate_query` are byte-unchanged.

**Why it was safe** — arithmetic, not a new experiment. The objection was Q2 (does an established
session id outperform a fresh one?), whose supporting measurement was already retracted
(`CONFIRMED_FACTS.md` §16). Checking the data settled the pool half outright: 0 of 42,600 rows had
been used within soax's own `sessionlength=3600` / `idlettl=900`, so no row held a live session and
tier 1 was ranking 32,590 rows on a property none had. See Q2 for what remains open — the
*first-use* rate of a minted id is still unmeasured, and is now on the critical path for every
login.

**What it removes**, all measured the same week: 46–124 s of Hub proxy-rotation latency per
rotation; the `proxy_request`/`wait_proxy` round-trip that saturated a 4-thread executor; one DB-pool
consumer that fed the audit deadlock; the 42,600-row table that collapsed the Proxies page; and
19,983 `is_healthy=false` condemnations that meant nothing about a per-session exit.

**Relationship to prior decisions.** A carve-out to **D32**, which freezes active proxy selection —
taken on explicit operator instruction. It *fulfils* **D19** (scope health to where it has a
subject) and *extends* **D33 item 1** (which already drew the rotating/static line at the health
filter). It respects **D2**: `roll_sticky_session` still only replaces a value in a *recognised*
credential slot and never appends an id to an unrecognised username.

**Two things this forced, which are the reusable lessons.** First, id width is a correctness
property once minting is decentralised: the old `randint(100000, 999999)` was fine for a rare
recovery step against a pre-generated pool, but as the *only* way any worker changes exit it
collides at roughly even odds by ~1k rolls, and two workers sharing an id share an exit silently.
Second, `max_sessions` was never a bound — every row carried 2, a nominal 85,200-lease ceiling
nothing could reach. The real limit belongs to the *provider*, so `PROVIDER_LOGIN_CONCURRENCY` is
keyed by provider+kind and is deliberately **host-wide, not per-booking**: two bookings each
staying under their own `LOGIN_CONCURRENCY` is exactly how the fleet jointly blew through soax's
limit. Its default (20) is an untuned guess and is labelled as one.

**Reverse if:** a live run shows minted ids logging in materially worse than the pool did. The
generator (`scripts/gen_proxy_pool.py`) still recreates the rows, and the migration is the only
thing that must be undone.

### D40. One module per proxy provider; credentials in the environment, knobs in the dashboard
**Operator direction, 2026-09-16.** Extends D39 from "rotating proxies are not rows" to "each
provider is a self-contained module".

**Structure.** `libs/proxies/providers/` holds `base.py` (the contract), one file per provider
(`soax.py`, `webshare.py`), and a registry in `__init__.py`. A provider module owns exactly four
things: its connection-string format, the credential fields it needs (declared per *pool*), and the
`mint`/`roll`/`read_id` grammar for session ids. `libs/proxies/rotating.py` is now a thin dispatcher.
Adding a provider is one new file plus one registry line.

**What this removed.** `roll_sticky_session`/`session_id` lived in `libs/proxies/lease.py` — the
*static* leasing module — and each carried a hard-coded if/else over both providers' username
grammars. Every new provider meant editing that shared branch. Dispatch now goes through
`providers.for_url()`, so a worker rolls its exit without knowing which provider it is on. The old
names survive as thin aliases because they are called from worker hot paths.

**Two namespaces, resolved from different sources — this is the security half.**
*Secrets* (login/password/package) come from the ENVIRONMENT only, as `{env_prefix}_{FIELD}`.
*Options* (the soax TTLs) come from dashboard settings, then env, then a declared default.

Credentials were briefly catalogued as GLOBAL settings, and that was wrong for two concrete
reasons, not taste. A setting is editable from a web dashboard and readable by anything with a DB
connection — inappropriate for a proxy password. And because `apps/hub/config.py` loads `.env` at
import, credential resolution that consulted ambient `os.environ` meant the TEST SUITE resolved
live production credentials: one appeared verbatim in a pytest failure message. `resolve_credentials`
now takes `env=` so tests are hermetic, and secrets are unreachable from `settings` — asserted by
`test_secrets_never_come_from_settings`.

**A pool is the unit, not a provider.** SOAX sells `rotating` (352438) and `mobile` (350716) as
separate purchases with SEPARATE passwords. Conflating them silently authenticates one pool with
the other's password — which has happened here for real, twice: 11,000 rows generated against a
rotating package using the mobile password, and 10,000 mobile rows carrying a retired rotating
package's password. Hence one `Pool` per purchase, each with its own `env_prefix`. TTL names are
deliberately *not* pool-prefixed: they describe the provider's behaviour, not one purchase.

**Operational note learned the hard way.** The `ci000droprot` migration ran before the credential
backfill did, deleting the only DB copy. The credentials were recovered from
`scripts/gen_proxy_pool.py`'s own output files in `data/`, which is why that script is kept working
and documented-legacy rather than deleted. `backfill_rotating_credentials.py` now tries the rows
first and falls back to those files — and the files are the *better* source, holding exactly one
package and one password each, with no generation ambiguity to resolve.

**Reverse if:** never for the module split. The env-only rule for secrets could be relaxed only if
a real secret store replaced it — not by moving them back into `settings`.

### D41. A named ISP pool is a label on a row, and a booking may draw from several
**Operator requirement, 2026-09-16.** Two halves, both about capacity being *real* — which is what
separates ISP pools from the rotating "pools" that D39/D40 deleted, where a pool was a bag of
strings we minted on demand.

**Import names the pool.** `proxies.pool_name` (`cj000poolname`). New name → pool created; existing
name → appended. No create endpoint, because a pool *is* the set of rows carrying its name. The
dashboard offers existing names from `GET /proxies/isp-pools` via a datalist, and says which of the
two is about to happen before the operator commits.

**A booking assigns several pools.** `PROXY_POOLS` / `HOLD_PROXY_POOLS`, comma-separated, mirroring
the `PROXY_GEOS` pair. Usable capacity is the union. This exists because one purchase is normally
smaller than the fleet, so requiring one-pool-per-booking would cap a booking at the size of its
smallest supplier.

**Three rules that are easy to get wrong, and why they are what they are.**
*NULL is not admitted by a pool filter* — a booking that named its pools must not receive an
unrelated ad-hoc import; these are finite IPs someone else may be using. *Dedup is global, not
per-pool* — the same IP in two pools would be leased twice at once, defeating one-worker-one-IP.
*Capacity is reported as `leasable`, not `total`* — a condemned static row is genuinely out of
service (D33), so `total` overstates capacity exactly when the operator is deciding whether to buy
more.

**Delivery, not just storage.** `pool_name` was added to `ProxyImport` and `ProxyOut` first and the
create path silently dropped it — the row looked right in the table and was invisible to every
booking that assigned the pool. Caught by asserting that `PROXY_POOLS` *reaches `lease_proxy`*
rather than that the setting round-trips, which is the only form of this assertion that catches the
delivery-gap class (see `delivery_gaps`).

**Reverse if:** a pool ever needs attributes of its own. Then `pool_name` becomes a foreign key and
this entry is superseded — but not before, because the join is on the lease hot path.

### D42. Proxy health has two tiers, and only tier 1 is a verdict about the proxy
**Operator requirement, 2026-09-16:** "2 tier healthy check — one with normal ping and another with
reachability check to the portal", plus bulk selection, filtering, and deletion of dead proxies.

**Tier 1** (raw → neutral url) asks *can this proxy carry traffic*. It writes `is_healthy`/`health`,
gates selection for static rows (D33), and is the only thing deletion keys off.
**Tier 2** (browser → portal) asks *can this IP reach the portal right now*. It writes
`portal_ok`/`portal_health` and is diagnostic only.

**Why they cannot share a column.** On 2026-08-28 a throttling portal flipped 4,489 rows to
`is_healthy=False` and 73 of 120 workers never launched; re-probed against a neutral url those rows
connected 20/20 — "the flag was recording the portal, not the proxy". That was recoverable when the
rows were regenerable session strings. It is not recoverable now: these are purchased ISP IPs and
the dashboard can delete them, so a portal outage must never be able to present a healthy pool as
deletable garbage.

**Deletion guards.** Active lease → refuse (live FK; the same one that produced the Detach 500 and
broke `ci000droprot`'s first run). Tier 1 passed → refuse. Never checked → refuse, tested *before*
the health test because `is_healthy` defaults to `True` and an unprobed row would otherwise be
reported as having passed a check that never ran. `force` overrides all three, deliberately
explicit.

**Bounded and off-session.** 200 proxies per run, 4 concurrent browsers; probes run with the DB
connection released and re-acquired to write. Both limits are drawn from real incidents — the
executor saturation that took the Hub down on 2026-09-15, and the audit deadlock caused by holding a
pooled connection across slow work.

**Reverse if:** never for the tier split. The neutral-url target may change if ipify proves
unreliable; it must stay a host we do not otherwise depend on, and it must never be the portal.

### D43. Blind apply — seed the pool on sight, reuse the machinery, retire by evidence
**Operator direction, 2026-09-18.** When the operator has confirmed availability by eye on the
portal's calendar and discovery finds nothing, seed the slot pool with the (date × period) grid and
let apply workers try the combinations. Two records say discovery can miss what is there: `/slots`
is on file as under-reporting (`slots_reference_implementation`, superseded 2026-07-06), and the
calendar path manufactures phantoms from the portal's template under load (F-phantom-slots). On
Booking 0918 the calendar showed 12/13/14 October open while fifteen sessions got `[]`.

**Design: no new mode.** `POST /bookings/{id}/blind-apply` writes one `Slot` per combination with
`source='blind'`. Everything downstream is unchanged: `_fire_slot_found_if_new` fires because an
`AVAILABLE` row exists, apply workers warm (mandatory every round), `_assign_slots` hands out
combinations under multi-capacity leasing, a bounce releases and the worker gets the next one, a
booking lands in `booked_appointments`. A blind slot that books is real by evidence.

**Exactly three things treat a blind row differently**, each a one-line rule with a seam test:
1. `phases.py` — `form_stale` on a blind slot → `no_slot` (release with the bounce penalty, next
   combination). Never the stale ladder, whose end is re-login: `apply_path_proven_healthy` records
   that the portal rejects a well-formed submit for a gone slot, which lands as `form_stale` with
   the cursor intact — routing that through the ladder spends a session and a reCAPTCHA strike on
   a combination that never existed.
2. `events.py` — a BOUNCED release counts against the blind slot; at `BLIND_MAX_BOUNCES` (3) it is
   CLOSED. A discovered slot retires via the scouts' miss count; a blind one was never discovered
   and would otherwise be reassigned forever. `form_stale`/`session` releases do not count.
3. `scheduler.py` — blind bounces are excluded from the false-slot-open revert. A grid is mostly
   phantom by construction; counted, one sweep would hit the limit of 12 and wipe the pool.

**The period table is measured, not assumed** (`libs/domain/blind.py`): every PDF-verified booking
and every discovered slot as of 2026-09-18. Periods are posto-specific — 3 only at 2032, 7/8 only at
3018/3059. Capacity per (date, period) ≥ 3 (three verified bookings on 2026-10-09 p5). Weekends are
never seeded.

**Deliberately manual.** A blind sweep is the densest submit load this fleet generates, and every
attempt is a warmup chain + a CAPTCHA solve + a submit. It is a decision at a moment the operator
has verified, not a background behaviour — and it refuses while discovery still has `AVAILABLE`
slots unless forced, because seeding phantoms on top of real finds only dilutes the pool.

**Reverse if:** the portal starts answering a bad-slot submit in a way the bounce classifier reads as
a session fault. Then rule 1 stops being safe and blind apply needs its own verdict, not a reuse.

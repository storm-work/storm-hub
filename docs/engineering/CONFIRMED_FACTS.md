# Confirmed Facts

Every entry states its evidence and date. An entry without evidence does not belong here —
it belongs in `OPEN_QUESTIONS.md`. Entries whose supporting measurement is later invalidated
are downgraded in place, with the invalidation recorded.

---

## 1. A Webshare sticky exit can move while actively in use — NEW, 2026-08-25

**CONFIRMED.** Measured directly, one credential, probes 5 minutes apart:

| t | exit IP | inside the 15-min idle window? |
|---|---|---|
| 0 | 86.43.123.45 | baseline |
| 5 m | 86.43.123.45 | yes — held |
| **10 m** | **191.110.69.88** | **yes — moved anyway** |
| 21 m | 191.110.69.88 | after a deliberate >15 min gap, unchanged |

The bind was lost **between two probes five minutes apart**, well inside the configured
15-minute Proxy Idle Timeout. The connection was never idle long enough for idle timeout to
be the cause.

**Consequences.**
- Raising Proxy Idle Timeout would **not** have prevented this. An earlier recommendation in
  this project ("if the bind does not survive an idle gap, raising the timeout is the fix")
  rested on the assumption that idleness kills the bind. That assumption is now disproven.
- The hold/keepalive design assumes traffic preserves the exit. **It does not.** Keepalive
  still serves the *portal* session; it does not pin the *proxy* exit.
- Every portal symptom caused by our own IP moving — `Por favor, confirma o reCAPTCHA`,
  Schedule.jsp losing its markers, a refused submit — can occur without any idle period and
  without anything being wrong with the proxy, the account, or the portal.

**Supersedes** the previous claim that keeping `KEEPALIVE_INTERVAL + JITTER` under `idlettl`
preserves the exit. That guidance is necessary but **not sufficient**, and was never the
whole mechanism.

**Caveat (honest scope):** one credential, one 21-minute window, one provider. It proves the
bind *can* move under activity; it does not establish how often. See `OPEN_QUESTIONS.md` §1.

---

## 2. The session id is the proxy's identity

**CONFIRMED** by measurement, 2026-08-25, five requests per credential:

| credential | distinct exit IPs | behaviour |
|---|---|---|
| `sodade2026-51` | 1 | sticky |
| `sodade2026-PT-777` | 1 | sticky |
| `sodade2026-rotate` | 5 | rotating |

Webshare grammar is `{username}-{country}-{geo}-{session_id}`; a numeric suffix pins an exit,
`-rotate` requests a new IP per request. **The endpoint (`p.webshare.io:80`) is identical for
every credential we own** — identifying a proxy by host:port identifies nothing.

Operator rule, adopted as a DECISION: hold the session id to keep an exit, change it to get
a different one.

---

## 3. Retrying a dead sticky exit cannot succeed

**CONFIRMED.** Rolling the session id off an unreachable exit rather than retrying it took
portal reachability from **3/12 to 10/12** (25% → 83%), measured 2026-08-25. Before the fix,
the login transient budget retried the *same* dead tunnel up to six times
(`ERR_TUNNEL_CONNECTION_FAILED` / `ERR_CONNECTION_CLOSED`), visible in every run as
"transient 1/6, 2/6, 3/6" back to back.

Because a session id is just a number we choose, the supply of exits is effectively unlimited
and costs no Hub round-trip and no lease. **Pool size was never the constraint.**

---

## 4. Never raw-probe an exit you intend to browse from

**CONFIRMED.** Same rows, same day, sequential:

| approach | result |
|---|---|
| browser only | 5/6 ok (83%) |
| raw primp probe, then browser | 0/6 ok (0%) |

DataDome flags the IP on the non-browser TLS handshake; the subsequent Chromium navigation
gets `ERR_CONNECTION_CLOSED`. Tier-1 quick-screen is therefore **disabled by default** in
`lease_proxy`. storm-auto can screen this way because primp is its *primary* transport, so
its fingerprint stays consistent — we use primp only as a probe and then switch to a browser
on the same exit, which is exactly the flagged combination.

---

## 5. An opaque origin permanently disables in-page fetches

**CONFIRMED** (root cause of apply-only failure). A failed navigation leaves the page on
`chrome-error://chromewebdata/`. Any `browser_fetch` / `browser_eval`+fetch inherits that
opaque origin and throws `TypeError: Failed to fetch` for the life of the page. Navigations
are origin-independent and unaffected.

Login, warmup and discovery each *begin* with a navigation and so self-heal. Apply did not.
The proven apply round is **navigation + navigation**.

---

## 6. Exit geography is the largest measured lever on login success

**CONFIRMED**, 1392 real attempts, 2026-08-18:

| geo | login success |
|---|---|
| DE | 23.7% |
| IT | 23.4% |
| PT | 10.8% |
| NL | 10.6% |
| ES | 8.1% |
| LU | 4.9% |
| CZ | 2.9% |

An ~8x spread. Solver score is not the gate, and proxy load speed does **not** predict it.

**Related CONFIRMED bug:** the 2026-08-22 pool regeneration wrote every row with a NULL geo,
and `_candidate_query` deliberately never excludes NULL geo (a filter must not empty the
pool). So `PROXY_GEOS=DE` matched all 30,000 rows — geo targeting *looked* configured and did
nothing. Fixed by injecting the country at lease time, per provider.

---

## 7. Reaching MostrarPdf is not a booking

**CONFIRMED** against the portal. Only `%PDF` bytes prove a booking succeeded. Inventing a
booking is strictly worse than missing one.

---

## 8. "Already has an application" is a username collision

**CONFIRMED.** The portal's "already has an application" response is a sub-account username
collision with an unenumerable historical account, not evidence about the identity. The
generation rule was widened 3-3-4 → 4-4-6 in response.

---

## 9. Booking history

**CONFIRMED** from the database, 2026-08-25: **17 booked appointments**, the most recent
**2026-08-21 05:30:45 UTC**. Eleven of the seventeen landed on 2026-08-20 alone.

**No booking has succeeded since 2026-08-21.** This is the central operational fact about the
system's current state.

---

## 10. Test suite

**CONFIRMED**, run 2026-08-25 at `dc133fb`: **883 tests pass**, 183 s, one deprecation
warning (`starlette.testclient` / httpx). 58 test files across bus, domain, engine, hub,
proxies, worker.

---

## 11. 24% of the settings catalog is inert — NEW, 2026-08-25

**CONFIRMED** by exhaustive grep: of **140** catalog keys, **34 have zero read site**
anywhere in `apps/` or `libs/`. Only **2** of those 34 are labeled `NOT IMPLEMENTED`.

The other 32 appear in the dashboard as ordinary configurable knobs and silently do nothing:

```
BOOKING_CIRCUIT_BREAKER  BREAKER_HOLD_GAP      BROWSER_INJECT_ROTATIONS  DETECT_INTERVAL
DETECT_PROBE_DAYS        DETECT_RECHECK        EMPTY_HOLD_DEAD_CAP       EPHEMERAL_PROFILE
EXECUTOR_THREADS         FORMSTATE_CHECK_BROWSER HOLD_CKPT_SECS          HOLD_VERIFY_MINS
NO_RETIRE_LOGIN          POLL_BACKOFF_AFTER    POLL_CONCURRENCY          POLL_GAP_HARDENED
POLL_GAP_LIGHT           POLL_GAP_MAX          PRESERVE_BACKOFF          PROXY_FAULT_BACKOFF
PROXY_FAULT_BACKOFF_CAP  REDIS_EVENT_STREAM_MAXLEN  REDIS_STREAM_MAXLEN  REWARM_EXPIRED_CAP
SCOUT_HEARTBEAT_TTL      SCOUT_WAIT_TIMEOUT    SESSION_REFRESH_STAGGER   SIGNAL_TOKEN
SIGNAL_TRIGGER_FILE      SIM_SLOTS_OPEN_AT     SIMULATE_RELEASE_AT       TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID         TEST_POSTO_ID
```

**Worst case in the set:** `REDIS_STREAM_MAXLEN` and `REDIS_EVENT_STREAM_MAXLEN` carry notes
asserting they cap the streams. They do not — `libs/bus/bus.py` hardcodes
`CMD_STREAM_MAXLEN = 200` and `EVENT_STREAM_MAXLEN = 5000` and never reads the settings. A
setting whose note claims a behaviour it does not have is worse than no setting at all,
because it defeats the very audit that would catch it.

Some entries are legitimately deferred (`TELEGRAM_*` is Phase 6) — but they are not marked,
so the catalog gives an operator no way to tell a deferred knob from a live one.

---

## 12. Proxy leases leak — live defect, NEW 2026-08-25

**CONFIRMED** from the production database:

- **107 active (unreleased) proxy leases**
- **0 RUNNING workers** — every one of the 107 is held by a non-running worker
- lease age: min 1.4 h, **median 101.8 h, max 271.8 h** (11+ days)
- 94 distinct proxy rows pinned, each `max_sessions = 2`
- 1 orphaned slot lease

**Current severity: low.** 94 of 32,600 rows is 0.3% of the pool. **Mechanism severity:
high** — growth is unbounded and monotonic, and this was catastrophic when the pool held
1,238 rows.

**Cause (CONFIRMED by reading the code):** every release path is **edge-triggered** — the
event consumer's terminal handler, the scheduler's dead/stall/runaway reaps, explicit stop.
There is **no level-triggered reconciliation** that asks "does this active lease belong to a
worker that still exists?" A worker whose terminal event was missed, or that ended across a
Hub restart, leaks its lease permanently with nothing to sweep it.

Note this persists *despite* `release_worker_resources` having been written specifically to
close this gap and being correctly called from every path that was known about. The defect is
not a missing call — it is the absence of a reconciling sweep.

---

## 13. Process tracking must be by PID, not Popen

**CONFIRMED bug, fixed.** `Popen` can only represent a process *this* Python process started.
A fresh `Supervisor()` after a Hub restart had no handle for any live worker, so `stop()`
silently no-oped (the Stop button did nothing) and `running_ids()` reported every such worker
as dead — so the next tick past the grace window marked healthy holding workers FAILED and
released their leases. Now tracked by PID with `os.kill(pid, 0)`, plus `reconcile()` scanning
`/proc` at startup.

---

## 14. A throttle setting resolving to zero once ran a worker at 150–280 writes/sec for 66 minutes

**CONFIRMED** from the recovered database dump: `HOLD_POLL_GAP: 0.0` in a worker's frozen
`effective_settings_json`, ~740,000 rows written in one episode, surviving two relaunch
attempts. Now guarded by `BelowMinimum` in the settings resolver, which raises loudly rather
than clamping — a throttle silently "fixed" to a working value hides the bug that produced it.

---

## 15. Infrastructure baseline

**CONFIRMED**, 2026-08-25: Redis `maxmemory` 2 GB, policy `noeviction`, currently using
2.19 MB. Postgres pool explicitly sized (10 + 20 overflow). `storm-hub.service` active since
17:19:41 UTC. Proxy pool: 32,600 rows (21,600 soax / 11,000 webshare), all `max_sessions=2`.
186 identities, 548 sub-accounts. `var/worker-logs/` 21 MB across 224 files;
`var/checkpoints/` 116 KB across 28 files.

---

## 16. Downgraded: "a fresh session id is 0.5% healthy"

**WAS** treated as a measurement justifying proxy selection order (600 never-used sessions →
0.5% healthy, versus 12 already-used → 92%).

**NOW INVALIDATED**, by this project's own later finding (§4): that sweep ran the tier-1 raw
probe across all 600 rows *first*, poisoning every exit it then measured. `libs/proxies/lease.py`
states the invalidation in one comment while a comment a few lines above still cites the
figure as the justification for the tier ordering.

**Status: the tier ordering in `_candidate_query` rests on an invalidated measurement.** The
ordering may still be correct — an established session plausibly does outperform a fresh one —
but that is now a **HYPOTHESIS**, not a fact, and the sample sizes (600 vs 12) were never
comparable. See `OPEN_QUESTIONS.md` §2.

---

## 17. Traffic does NOT preserve a proxy exit — Q1 ANSWERED, 2026-08-25

**CONFIRMED.** 56 sessions, 4,445 probes, **100% probe success** (zero failures, so no
measurement-failure confound anywhere). ~90 session-hours. Config under test: Webshare
Session Timeout 12 h, **Idle Timeout 1 h**; soax credentials `sessionlength-3600 bindttl-600
idlettl-900`. Raw data: `var/exit_stability.jsonl`; re-derive with
`probes/proxy_exit_stability_probe.py --report`.

**Positive control fired exactly.** All 20 soax sessions (12 rotating + 8 mobile) changed exit
between **60.2 and 63.4 minutes** — precisely at the declared `sessionlength-3600` — while
being probed every 60 s. 100% survival through 60 min, **0% at 75 min, 20/20, no exceptions.**
The instrument detects a known boundary to ~1-minute precision, which is what licenses trust
in the webshare numbers below.

**Exit-change rate (95% Poisson CI):**

| arm | contract | changes / session-hours | rate/h | 95% CI |
|---|---|---|---|---|
| `active_ws` | webshare, 60 s, fresh conn | 6 / 19.82 | 0.303 | [0.111, 0.659] |
| `persist_ws` | webshare, 60 s, **pooled conn** | 5 / 19.79 | 0.253 | [0.081, 0.589] |
| `lowtraffic_ws` | webshare, **30 min** | 5 / 18.01 | 0.278 | [0.089, 0.648] |
| **webshare combined** | | **16 / 57.62** | **0.278** | **[0.159, 0.451]** |
| `active_soax` | soax/rotating | 12 / 19.73 | 0.608 | [0.314, 1.062] |
| `active_mob` | soax/mobile | 8 / 13.12 | 0.610 | [0.263, 1.202] |

**Survival, still on the original exit at T:**

| arm | 15m | 30m | 60m | 75m | 90m | 100m |
|---|---|---|---|---|---|---|
| `active_ws` | 92% | 92% | 92% | 83% | 58% | **50%** |
| `persist_ws` | 92% | 83% | 75% | 75% | 67% | **67%** |
| `lowtraffic_ws` | – | – | 83% | 83% | 83% | **58%** |
| `active_soax` | 100% | 100% | **100%** | **0%** | 0% | **0%** |
| `active_mob` | 100% | 100% | **100%** | **0%** | 0% | **0%** |

**Three findings:**

1. **Traffic volume has no measurable effect.** A 30x difference in probe frequency (60 s vs
   30 min) produced 0.303 vs 0.278 changes/session-hour — CIs almost entirely overlapping.
2. **Connection reuse has no protective effect.** The pooled-connection arm produced the
   *earliest* changes observed (14.0, 15.0, 18.2, 30.0 min) and the only session to change
   twice (#9639: two exits in 3.2 minutes).
3. **soax cannot be protected by traffic at all.** Continuous 1/min traffic did not extend a
   single session past its sessionlength ceiling.

**The webshare hazard is not constant.** Changes cluster (1.0, then 69–92 min for `active_ws`),
so a mean-time-between-changes of ~3.6 h from the pooled rate would misdescribe the risk.
Session age at t=0 is unknown — these session ids may have carried prior bindings — which is a
plausible explanation for the spread and a real limitation.

**Also observed:** several exits moved from IPv4 to **IPv6** (e.g. `98.17.236.7` →
`2a02:810d:...`). For a portal that binds a session to its requesting IP, that is a move like
any other.

**Supersedes** §1's framing. §1 established that an exit *can* move under activity; this
establishes that traffic is not the mechanism that holds it, in either provider.

**The soax 60-minute ceiling is self-imposed.** `sessionlength-3600` is generated by our own
`scripts/gen_proxy_pool.py` (`_SOAX_SESSIONLENGTH = 3600`), not a provider limit.

---

## 18. Five lifetimes, not one — the corrected model, 2026-08-25

**CONFIRMED architectural correction.** These were being treated as one thing. They are five
independent lifetimes, and none guarantees another:

| # | Lifetime | Bound to | Ends when | Refreshed by | Status |
|---|---|---|---|---|---|
| 1 | **Portal auth session** (`Vistos_sid`) | a cookie | portal-side expiry / invalidation | any authed GET | TTL **UNKNOWN** |
| 2 | **Server-side form/draft state** (ScheduleController) | `Vistos_sid`, server-side | ~90–120 s idle | any authed GET on that sid | TTL **HYPOTHESIS** (storm-auto figure, never measured here) |
| 3 | **Proxy session** (credential/session id) | the credential string | provider policy | nothing we control | soax: **CONFIRMED 60 min**; webshare: no hard ceiling observed |
| 4 | **Proxy exit / IP** | the provider's binding | independently of 1–3 | **nothing — traffic does not hold it** (§17) | **CONFIRMED unstable** |
| 5 | **Browser session** (context/page) | the worker process | process exit / crash / `close_browser` | — | CONFIRMED process-scoped |

The invalidated assumption, stated plainly: **"keeping the session alive also keeps the proxy exit
stable" is FALSE.** Keepalive traffic acts on lifetimes 1 and 2. It has **no measurable effect on
lifetime 4** (§17), and cannot extend lifetime 3 at all.

A second conflation worth naming: lifetime 3 ≠ lifetime 4. A soax session can hold a *usable*
proxy whose *exit* has changed; and a proxy can fail at the transport level while its session id is
still perfectly valid. Three distinct events — proxy failure, exit change, portal-session failure —
which the worker does not currently distinguish (see `FAILURE_MODES.md` F21).

---

## 19. The predecessor's holding strategy is a STATIC ISP IP — and we have none

**CONFIRMED** by reading `/srv/projects/storm-auto/app/worker.py` (113 real bookings).

storm-auto's validated design (its own comment, "REAL-FLOW split (validated 0724)"): warm on the
cheap disposable rotating pool, then **migrate the warm session onto an ISP hold proxy — one IP per
held session — before the keepalive-hold.** Its stated reason for the migration is exactly the
failure this project just measured: *"the 0729 root cause: 'no live source browser' → the session
stayed on the ROTATING login proxy (SOAX) → died on exit rotation."*

**storm-hub has the mechanism and cannot use it:**

1. `LiveEngineOps.do_swap()` implements the same migration — and is **OFF by default**
   (`HOLD_SWAP_AFTER_WARMUP`, catalog default `False`).
2. It has **never executed in production**: zero occurrences of `do_swap` in 21 MB / 224 worker log
   files, and zero `swapped to hold proxy` lines.
3. Its default target is wrong for the purpose: `HOLD_PROXY_PROVIDER` defaults to **`webshare`**,
   which is *rotating*. Enabling it as configured would migrate from one unstable exit to another.
4. **We own no static/ISP proxies at all.** Every one of the 30,000 active rows is
   `soax/rotating`, `soax/mobile`, or `webshare/rotating`. `Proxy.provider`'s own column comment
   reads `soax | isp` — the `isp` case has never been populated.

So the predecessor's answer to exit instability exists in our code, disabled, pointed at the wrong
pool, with no ISP inventory behind it.

---

## 20. Planned hold durations exceed the soax exit lifetime

**CONFIRMED** from the bookings table, cross-referenced against §17's survival data:

| booking | planned hold (warmup → slot_open) | soax exit survival at that duration |
|---|---|---|
| Booking 0814 | 7 min | ~100% |
| Booking 0821 mobile | 64 min | **0%** |
| Test 2032 | 74 min | **0%** |
| Test2 | 85 min | **0%** |
| Booking 0821 webshare | 85 min | (webshare ~58%) |
| Test 3018 | 90 min | **0%** |
| Booking 0821 soax | 109 min | **0%** |
| Test 3059 | 116 min | **0%** |

**Six of eight bookings planned a hold longer than the soax 60-minute ceiling.** For those runs,
every soax-based worker was *guaranteed* to lose its exit before slot-open fired — not at some
probability, but by construction.

This is a strong candidate contributor to Q5 (nothing booked since 2026-08-21; the three bookings
that day planned 64/85/109-minute holds). It is **not** established as the cause, and must not be
recorded as one.

---

## 21. Proxy session age is not knowable — Q14 assessment

**CONFIRMED** by inspection. Nothing in the system records when a proxy session id was first bound
to an exit:

- `Proxy.last_used_at` is stamped on lease and probe attempts — it is a **selection cursor**, not a
  binding timestamp.
- `Proxy.exit_ip` exists as a column, has **no write site anywhere**, and is populated on **0 of
  32,600 rows**. Dead column.
- `ProxyLease.leased_at` records when *we* took the row, which says nothing about when the provider
  bound that session id to an address — possibly in an earlier run, possibly never.

So session age is: **not known, not tracked, not persisted, not observable, and not usable by lease
selection.** This is exactly why Q14's hazard-shape question is open, and it is the one piece of
missing instrumentation that a follow-up experiment would need — obtainable without schema changes
by using session ids that have provably never been used.

---

## 22. An authenticated portal session SURVIVES a proxy exit change — Q16 ANSWERED, 2026-08-25

**CONFIRMED. 3 of 3 valid runs: `SESSION_SURVIVES`.** Raw: `var/q16_session_migration.jsonl`;
probe: `probes/session_ip_migration_probe.py`.

Method: authenticate and warm on P1, capture the browser cookie jar, open a **fresh browser on P2**
with a **different exit IP**, inject the jar, navigate `Schedule.jsp?posto_id=…`. The experiment
stops there — it never POSTs `ScheduleController` and never touches `SubmeterVistoCriaPDF`, so no
booking is reachable from it.

| run | P1 exit | P2 exit | gap | verdict | `_probe_alive` |
|---|---|---|---|---|---|
| `cc0395d3` | 79.26.199.8 | 81.57.97.124 | 39.5 s | SESSION_SURVIVES | agrees |
| `f8df92c8` | 37.159.125.149 | 79.45.135.224 | 36.8 s | SESSION_SURVIVES | agrees |
| `713c1928` | 146.241.99.100 | 5.77.106.70 | 70.2 s | SESSION_SURVIVES | agrees |

Each used a different identity and sub-account. Every migrated session returned a ~55 KB page
carrying **all five** scheduling markers (`getSlots`, `captchaDiv`, `SubmeterVistoCriaPDF`,
`f_date_c`, `cmbPeriodo`) — not merely "still logged in", but still in the bookable state.

**So the portal does NOT bind its authenticated session to the establishing IP.** The session is
carried by the `Vistos_sid` cookie, exactly as storm-auto's inherited claim asserted. That claim is
now independently verified in storm-hub rather than inherited.

**5 further runs were INVALID and are not counted** — 4 could not establish P1 at all (login
refused) and 1 had an unreachable P2. Per the experiment's own rule, a failed login or a dead P2 is
a transport/account fact and says nothing about session/IP binding.

### Limitations — what this does NOT establish

1. **n = 3**, all within one hour on one day.
2. **All gaps were 36.8–70.2 s**, inside the ~90–120 s form-state window. It does not test whether a
   session survives an exit change *plus* a longer gap — and the negative checkpoint results below
   suggest elapsed time is the thing that actually kills it.
3. **All migrations were deliberate and clean**: a new browser dialling out from a new address. An
   *involuntary* exit change underneath a live connection may behave differently, and this does not
   prove otherwise.
4. Same provider (webshare) and same requested geo (IT) on both sides — cross-provider and
   cross-geo migration untested.

### Corroborating negative, and why it is not counted as evidence

Four archived checkpoints (ages 4.7 h, 47.2 h, 103.4 h, 103.8 h) were loaded onto fresh exits. All
four returned the portal's dedicated **`sessionLost.jsp`** endpoint. This was recorded as
INCONCLUSIVE, never as invalidation: those jars are far older than any plausible session TTL, so
expiry fully explains them and the design cannot separate expiry from IP-binding. storm-auto reports
hitting the same page during its swaps and attributes it to form-state ageing out during the
browser-open window — i.e. to **time**, not to the address change.

Read together with the positive result, the picture is that **elapsed time ends a portal session;
an IP change does not.**

---

## 23. The login wall — not the portal — is what blocks work right now

**CONFIRMED** while attempting Q16, 2026-08-25 22:00–22:40 UTC.

The portal is **up**: an unauthenticated browser nav to the home page returned 58,266 bytes with
login markers present, no DataDome challenge, no throttle text.

But 5 of 8 login attempts were refused, across **both providers**, **many distinct exits**
(95.81.16.219, 93.208.48.66, 185.203.123.40, 93.192.211.59, 91.67.178.218, 37.4.250.119,
93.255.249.197, 91.17.225.21 …) and several sub-accounts. The dominant refusal was the portal's
explicit throttle: *"Não foi possível de momento concretizar o seu pedido, por favor tente mais
tarde."*

**Correction to an earlier belief recorded in this project:** the throttle message was previously
noted as rare (8 occurrences) against 554 reCAPTCHA rejections. Across the full worker-log corpus it
is in fact the **most common** login rejection: **1416 throttle vs 988 reCAPTCHA**. Today's mix is
therefore not anomalous — it is normal for this system, and the earlier "8" was a small early
sample generalised too far.

Because many distinct exits were refused identically, this is **not** exit-reputation and must not
be diagnosed as one. Successful logins did still occur (3 of 8, in 50.8 s, 63.1 s and 214.1 s), so
the wall is probabilistic, not closed.

---

## 24. ~~The portal authentication session lives ~33-45 minutes~~ — **CORRECTED by §27, see below**

> **THIS ENTRY'S HEADLINE NUMBER IS WRONG AS A GENERAL FACT.** Q21 (§27) ran three sessions for
> ~180 minutes on 2026-08-26 and **none expired** — 4x longer than the figure below, one of them
> using the very same HOME_URL probe. What §24 records is a real observation of *those two sessions
> on that night*, not the portal's authentication TTL. The measurement stands; the generalisation
> does not. Read §27 first.

### Original entry (2026-08-26, Q20), preserved

**CONFIRMED, two independent sessions.** Raw: `var/session_lifetime.jsonl`; probe:
`probes/session_lifetime_probe.py --report` (verdicts are re-derived from raw evidence, never
trusted from the live run — see the classifier bug below).

| arm | provider | probe cadence | last AUTHENTICATED | first LOGGED_OUT | lifetime |
|---|---|---|---|---|---|
| `q19_soax_involuntary` | soax | every 5 min | t = 34.2 min | t = 45.0 min | **33-45 min** |
| `q20_active_ws` | webshare | every 5 min | t = 32.9 min | t = 43.5 min | **33-44 min** |
| `q20_idle_ws` | webshare | gaps 10/20/30 min | t = 30.8 min | not observed | > 30.8 min |

**Both active sessions died at essentially the same elapsed time despite an authenticated GET every
five minutes.** Traffic at that cadence does not keep the session alive. Each arm also showed a
partial page (20,972 B / 9,651 B) at t≈38-40 min, right at the transition.

**The discriminator (measured):** the portal home renders at **59,690 B carrying BOTH**
`solicitar pedido de visto` and `consultar pedidos` while authenticated, and at **58,266 B carrying
only** `solicitar pedido de visto` once the session is gone. `solicitar pedido de visto` is a
PUBLIC menu item; only `consultar pedidos` is auth-gated. There is no inline password field on the
logged-out home — the login form lives on `Authentication.jsp` — so a password field is useless as
a negative signal on this page.

### The unresolved confound — state it before using this number

This measurement **cannot yet distinguish**:
  (a) an **absolute** session lifetime of ~35-40 min that no activity extends, from
  (b) an **idle** timeout of ~35-40 min where our probe *does not count as activity* — the home
      page may be served without touching the server-side session.
Both fit the data exactly. Settling it needs a probe against an **auth-gated** page (the
`consultar pedidos` target) rather than the home page — see `OPEN_QUESTIONS.md` Q21.

Note what is already true either way: **the keepalive as production runs it navigates `HOME_URL`
before a worker has warmed**, which is precisely the request whose session-refreshing power is now
in doubt.

### Limitations
n=2 for the lifetime figure; one night; both sessions unwarmed (no `ScheduleController` POST, which
this phase forbids), so this is the **auth** session only. Form-state TTL is untouched and remains a
HYPOTHESIS. The idle arm produced no expiry observation: its 61.4 min probe was a transport failure
(its proxy exit had died), so the idle-vs-active question is not settled by it.

---

## 25. Q19 could not be answered — the vehicle was invalidated by fact #24

**NOT ANSWERED. Reported as such rather than forced.**

The plan was sound on the evidence available: Q1 measured that every soax credential loses its exit
at its `sessionlength` ceiling (20/20, 60.2-63.4 min), so holding an authenticated browser on soax
past 60 min should produce a genuine involuntary exit transition under a live session.

The transition happened exactly as predicted — **t = 60.9 min, 151.59.4.106 -> 93.44.90.203** — but
by then the portal session had been dead for ~16 minutes (last authenticated probe t = 34.2 min).
The observation says nothing about Q19.

**The reason is the finding:** the portal session (~35-45 min) is **shorter than the soax exit
lifetime (~60 min)**. On soax, an involuntary exit change can therefore *never* occur under a live
session — the session always dies of old age first. That is a structural fact about the two
lifetimes, not a failed experiment.

**Corrected design for Q19** (see `OPEN_QUESTIONS.md` Q19): webshare has no fixed ceiling and loses
an exit at 0.278/session-hour, so from Q1's survival curve roughly 8-17% of sessions move within
their first 30 minutes — inside the portal session's lifetime. Run N webshare sessions concurrently,
probe every ~2 min for ~35 min, and catch the sessions whose exit moves while still authenticated.
At n=6 that is a ~40-65% chance of at least one event, so it is a real experiment with a real chance
of yielding nothing.

---

## 26. A classifier bug, and why the raw data survived it

**Recorded as a method note, because the recovery is the point.**

The live classifier accepted **any** home marker as proof of authentication. Because
`solicitar pedido de visto` is public, it reported logged-out pages as AUTHENTICATED for 25+ minutes
on two arms, and I reported that to the operator before catching it. `ops_live.py`'s own
`_classify_submit_response` already had the correct rule — `all(m in low for m in _HOME_MARKERS)`.

It was caught by noticing that a "successful" probe was 58,266 B — the exact byte count measured for
the *logged-out* home page during Q16 — and carried one marker where earlier probes carried two.

**No re-run and no re-spent logins were needed**, because every probe persists its own raw evidence
(byte count, marker list, password field, final URL). Verdicts are now re-derived offline by
`reclassify()` and the report never trusts the recorded verdict. **Persisting raw evidence rather
than conclusions is what made a bad classifier a recoverable error instead of a lost night.**

---

## 27. The portal session survived ~180 minutes on all three arms — Q21, 2026-08-26

**CONFIRMED. No session expired in 3 hours.** Raw: `var/q21_auth_refresh.jsonl`; probe:
`probes/q21_auth_refresh_probe.py --report` (verdicts re-derived offline).

| arm | endpoint | probes | last AUTHENTICATED | expired? |
|---|---|---|---|---|
| `q21_test_1` | `/VistosOnline/Pedidos` (auth-gated) | 34 | t = 178.6 min | **no** |
| `q21_test_2` | `/VistosOnline/Pedidos` (auth-gated) | 34 | t = 177.7 min | **no** |
| `q21_control_1` | `/VistosOnline/` (production keepalive url) | 34 | t = 174.8 min | **no** |

### The auth-gated endpoint, and how it was proven

No auth-gated url existed in this codebase. `/VistosOnline/Pedidos` was discovered from a live
authenticated page and then **verified** against a cookie-less browser on the same proxy:

| `GET /VistosOnline/Pedidos` | bytes | `Terminar Sessão` | `Consultar pedidos` |
|---|---|---|---|
| authenticated | 59,244 | **present** | present |
| no cookies | 28,717 | absent | absent |

Same url, no redirect either way. The marker is the **logout link** — semantically it can only exist
when there is a session to end. This replaces the page-size and public-menu-item heuristics that
produced §26's wrong verdicts.

### What this answers, and what it does not

**"An auth-gated GET refreshes authentication while HOME_URL does not" — NOT SUPPORTED.** The
control held a session for 174.8 minutes on HOME_URL alone. Production's pre-warm keepalive url is
not the inadequate request it was suspected of being.

**The underlying mechanism — INCONCLUSIVE.** No arm expired, so "both requests refresh the session"
and "the TTL is simply longer than 180 minutes" fit the data equally. A differential test cannot
resolve this when neither side dies. Distinguishing them needs a run long enough to reach an actual
expiry, or a true no-traffic idle arm.

### The important consequence: §24's number does not generalise

Two sessions died at 33-45 min on the night of 2026-08-26; three sessions survived ~180 min the
next morning, one of them under the identical HOME_URL probe. **Whatever ended the first pair was
not a fixed authentication TTL.** The cause is **UNKNOWN** — a portal-side invalidation event, a
time-of-day policy, and per-node session configuration are all consistent with the evidence, and
none is established.

**Operationally this is the more useful fact:** a held session is capable of lasting at least 3
hours, which is longer than every planned hold on record (§20, max 116 min). But it is not
*reliable* — the same setup produced 40-minute sessions twelve hours earlier. Hold duration cannot
be treated as a constant.

---

## 28. An INVOLUNTARY exit change did not invalidate the session — Q19 partial, n=1

**OBSERVED, once, and it is genuine Q19 evidence** — the event Q19's own experiment failed to
capture (§25).

`q21_test_1` held one exit for 109 minutes, then moved **involuntarily**, provider-driven, under a
live browser:

```
t=  2.2m   178.99.60.149   AUTHENTICATED
t=109.4m   87.15.27.74     AUTHENTICATED   <- exit changed, session unaffected
...
t=178.6m   87.15.27.74     AUTHENTICATED
```

The probe immediately after the change returned 59,246 bytes from the **auth-gated** endpoint with
the logout marker present, and the session went on to survive another 69 minutes. This is stronger
than §22's deliberate migration: the same browser, the same session, an address change nobody asked
for.

**Limits: n = 1.** The change was detected between two probes ~5 minutes apart, so this shows an
exit moving between requests rather than mid-request. Q19 remains formally open; this is supporting
evidence, not the answer.

**Also worth noting against Q1:** the other two sessions held a single webshare exit for the full
~180 minutes. Q1's rate of 0.278 changes/session-hour predicts ~2.5 changes across 9 session-hours;
one was observed. Small numbers, but a reminder that the rate is an average over a wide CI.

---

## 29. The 33-45 min "TTL" was a single portal-side invalidation event — Q22-A, 2026-08-26

**SUPPORTED, and it retires the TTL reading entirely.** The two Q20 sessions did not expire
independently. Compared on **wall clock** rather than elapsed time:

| arm | provider | identity | last AUTHENTICATED | first LOGGED_OUT | page served at the transition |
|---|---|---|---|---|---|
| `q19_soax_involuntary` | **soax** | 2ac3d5ba | 01:05:18 | 01:10:50 | **20,972 B** |
| `q20_active_ws` | **webshare** | 7f04af2e | 01:05:39 | 01:10:55 | **9,651 B** |

Two sessions on **different providers**, with **different identities**, created a minute apart,
transitioned within a **5-second window** — each serving a truncated page against a normal ~59 KB.
A per-session TTL fires at each session's own age and would have separated them by ~1 minute. This
did not.

**The third arm corroborates:** `q20_idle_ws` was still authenticated at 01:09:31 and never probed
again inside the window.

**Conclusion:** a portal-side event invalidated live sessions simultaneously at ~01:10:50 UTC. The
"33-45 minutes" was the distance from each session's creation to that single event, nothing more.

### Candidate explanations, classified

| explanation | verdict | why |
|---|---|---|
| Portal-side simultaneous invalidation (restart / session-store flush / deploy) | **SUPPORTED** | 5-second co-timing across two providers and identities, plus truncated response bodies at exactly that moment |
| Fixed authentication TTL | **UNSUPPORTED** | would separate the two by ~1 min; contradicted outright by 3/3 sessions at ~180 min (§27) |
| Proxy / provider differences | **UNSUPPORTED** | soax and webshare died together |
| Account / identity differences | **UNSUPPORTED** as the cause | different identities cannot synchronise to 5 seconds |
| Session-creation or authentication path | **UNSUPPORTED** | identical code (`acquire_session`) in both runs |
| Request pattern | **UNSUPPORTED** | Q20's active arm and Q21's control both probed HOME_URL every 5 min |
| Time-of-day / maintenance window | **PLAUSIBLE** | 01:10 UTC is a plausible window and all Q20 arms were nocturnal — but n=1 event, and this project's standing rule is not to charge failures to "downtime" without evidence. **Not established.** |
| Which portal node served us | **UNKNOWN** | no visibility |
| Cookie differences | **UNKNOWN** | `Vistos_sid` presence was not recorded in Q20 (it was in Q21). Irrelevant given the simultaneity |

### The architectural lesson, which is stronger than a TTL would have been

**A portal session can be invalidated at any moment, by an event unrelated to its age, with no
warning.** That is a worse world than a known TTL — you cannot schedule around it — and it is the
single fact the holding design must absorb. It is also good news in one respect: sessions are
capable of lasting 3+ hours (§27), so nothing forces short holds; what is required is *detection and
recovery*, not a duration budget.

---

## 30. getPeriodosOcupados does NOT renew the form state — 2026-08-27

**CONFIRMED, and it disproves a belief recorded in this project's own settings catalog.**

Two arms, same warm session shape, same 2-minute cadence, different request type. Raw:
`var/formstate_ttl_discover.jsonl`.

| arm | request polled | last ALIVE | first DEAD |
|---|---|---|---|
| `schedule_poll` | `Schedule.jsp` | 4.17 min | **6.12 min** |
| `discover_poll` | `getPeriodosOcupados` | 4.08 min | **6.08 min** |

**They expire at the same instant** (6.12 vs 6.08 min). Polling the discovery endpoint every two
minutes preserves the server-side form state no better than polling Schedule.jsp — which
`CONFIRMED_FACTS.md` §17-adjacent work already showed preserves it not at all.

**What this disproves.** `MONO_BOOKING`'s catalog note claims it is "the practical fix for anchor
lapse during hold: a worker running its own getPeriodosOcupados rounds keeps the server-side
booking anchor alive, which a bare keepalive nav does not (147 lapses vs 42 restores, measured
2026-08-21)." The 147:42 figure is real but **correlational**; this is the controlled test, and the
causal claim does not survive it.

The likely real mechanism: scouts do not lapse *less* — their discovery **fails fast** once the
anchor dies (the response degrades in one poll), which trips the keepalive's re-warm sooner. They
are re-warmed more often, not held alive.

**Post-mortem detail worth keeping.** After the form died the discovery endpoint's response
degraded in two stages: `192B` (valid, marker present) -> `168B` (marker absent) -> `28,513B` from
8.1 min onward. That last size matches the portal's "try again later" refusal page measured at
28,506-28,529B, so a long-dead context eventually gets refused outright rather than merely
answered emptily.

**`Vistos_sid` was present in every row of both arms, alive and dead** — further confirmation that
cookie presence is not evidence of a usable form state.

**Architectural consequence.** "Discovery-as-keepalive" is not available. The existing design --
`discover -> detect expiry -> re-warm` -- is the correct one, and the only mechanism shown to
restore a lapsed anchor remains a real `_run_warmup_steps()`.

## 31. FIRST BOOKINGS SINCE 2026-08-21 — two, verified on %PDF bytes, 2026-08-27

**CONFIRMED.** The strict rule was applied: reaching `MostrarPdf` is not a booking; only `%PDF`
magic bytes are.

| | AMILCAR LOPES SILVA | SOFIA FILOMENA COSTA |
|---|---|---|
| booked at | 19:15:42 UTC | 19:20:44 UTC |
| PDF magic | `%PDF-1.6` | `%PDF-1.6` |
| PDF size | 250,821 B | 250,824 B |
| slot | 2026-09-02 / period 7 | 2026-09-02 / period 8 |
| slot status after | `taken` | `taken` |
| booking / posto | `Test 3018` / **3018** | `Test 3018` / **3018** |
| residence | FRA | FRA |
| proxy | webshare / rotating, `PROXY_GEOS=''` | same |
| `MONO_BOOKING` | True | True |
| `KEEPALIVE_INTERVAL` | **600.0** | **600.0** |

Artifacts: `var/artifacts/aa4f2d44-…_1787858142.pdf`, `var/artifacts/8fc207be-…_1787858444.pdf`.

**The healthy submit signature**, now the reference for the `PRE-SUBMIT` diagnostic:

```
19:15:29  ScheduleController -> 200, 54,171B
19:15:38  do_apply: PRE-SUBMIT doc 54,865B markers=['getSlots','captchaDiv',
          'SubmeterVistoCriaPDF','f_date_c','cmbPeriodo'] title='Visa Application Scheduling' origin_ok=True
19:15:42  do_apply: BOOKED -- verified PDF (250,821B) at …/MostrarPdf?
```

Four seconds from pre-submit to booked.

**Both booked with NO `/slots` call at all** — the workers spawned at 19:10, after the primer was
removed at 16:59. This **disproves** the reading of `data/new_booking.har` that `/slots` is required
for a submit to be accepted (see §33).

## 32. A rejected `/slots` POST destroys the server-side booking cursor — 2026-08-27

**CONFIRMED 3/3**, controlled, fresh session per arm, only the primer differing. Probe:
`probes/cursor_survives_primer_probe.py`. Raw: `scratchpad/cursor_survives_primer.json`.

| run | CONTROL (6s wait, no primer) | PRIMER (`captcha=""`) |
|---|---|---|
| 1 | 55,194 B / 5 markers → 55,196 B / 5 | 55,196 B / 5 → **39 B / 0** |
| 2 | 55,198 B / 5 → 55,198 B / 5 | 55,194 B / 5 → **39 B / 0** |
| 3 | 55,200 B / 5 → 55,200 B / 5 | 55,208 B / 5 → **39 B / 0** |

`_SCHED_PAGE_MARKERS` on `Schedule.jsp` is this codebase's definition of a live cursor (the exact
check `_run_warmup_steps` uses). The identical 39-byte answer across three runs is deterministic,
not flaky.

**Endpoint → cursor matrix** (`probes/endpoint_cursor_matrix_probe.py`) — exactly ONE endpoint in
the pipeline destroys it:

| endpoint | response | cursor |
|---|---:|---|
| `GET /VistosOnline/` | 57,488 B | preserves |
| `GET Questionario` | 72,128 B | preserves |
| `GET profile.jsp` | 75,276 B | preserves |
| `GET gettime` | 673 B | preserves |
| `GET getPeriodosOcupados` | 189 B | preserves |
| `GET Schedule.jsp` | 54,861 B | preserves |
| **`POST /slots` (`captcha=""`)** | 28,514 B | **DESTROYS** |

Removing the primer was correct and is committed (`39bfd1c`). It was **necessary but not
sufficient** — `form_stale` continued at posto 2032 afterwards (see §34).

## 33. What `data/new_booking.har` does and does not prove — 2026-08-27

A manually captured **successful** booking (posto 3059, residence FRA, 2026-08-24, 33 entries,
request-side only — response bodies were not recorded).

The real flow: `POST /login` → 9 × `GET QuestNextQuestion` → `POST /slots?posto_id=3059` (200,
21,191 B, `captcha` **2,617 chars**) → `POST /SubmeterVistoCriaPDF?posto_id=3059` (200, 1,572 B).

**CONFIRMED differences from storm-hub:**

| | HAR (successful) | storm-hub |
|---|---|---|
| submit transport | **XHR** — `X-Requested-With: XMLHttpRequest`, `Sec-Fetch-Mode: cors`, `Dest: empty` | **navigation** (`browser_form_post` → `form.submit()`) |
| submit `lang` | **`PT`** | **`ENG`** |
| `Referer` (both calls) | `…/Schedule.jsp?posto_id=N` | whatever page the nav is on |
| quest steps | **9** (incl. `id_pergunta=3 valor=TRAT`, `id_pergunta=22 valor=N`) | **7** — those two missing |
| `/slots` captcha | solved, 2,617 chars, **accepted** (21 KB slot list) | `""` → rejected |

The 2,617-char token length is **exactly** what our own solver produces, so our tokens are the right
shape; the difference is acceptance, not generation.

**DISPROVEN by §31:** that `/slots` is *required*. Two real bookings were made with no `/slots` call
at all, five hours after this HAR was analysed. A single capture of one successful path shows what
that path did — not what is necessary.

## 34. Posto 3018 books; posto 2032 answers form_stale on every slot — 2026-08-27, UNEXPLAINED

Same code, same night, same `MONO_BOOKING=True`, same webshare/rotating provider:

| | `Test 3018` | `E2E VALIDATION 0827` |
|---|---|---|
| posto | 3018 | 2032 |
| residence | **FRA** | **IRL** |
| `PROXY_GEOS` | `''` | `'FR, DE, PT, IT'` |
| result | **9 booked** | **22 × form_stale, 0 booked** |

The 2032 run leased **10 distinct slots** (after the form_stale ordering fix) and every one answered
`form_stale` — the questionnaire page, 72,211 B, carrying the authenticated Logout / Change-Password
nav, so the SESSION was never the problem. Captured bodies in `var/submits/`.

Per storm-auto's own semantics (quoted at `ops_live.py` `_BOUNCE_KEYWORDS`) that response means
*"the slot was taken, session is FINE"* — so ten genuinely-taken slots is a live reading, not only a
defect. **The `PRE-SUBMIT` diagnostic (`8c1049f`) did not exist during that run**, so there is no
cursor measurement at the instant of those submits. One 2032 run now would settle it.

## 35. The portal throttles gettime, login and /slots with one page — 2026-08-27

`portal_try_again_later`, **~28.5 KB**, `Não foi possível de momento concretizar o seu pedido, por
favor tente mais tarde`. The same page answers all three endpoints during a throttle window.

Measured on `Test 2032`: one worker went `-> discovering` **twenty times in ~10 minutes**, every one
blocked, five workers wide, for hours — asking again 30 s after the portal said to try later. Fixed
by `4bc66d9` (`DISCOVERY_BLOCKED_BACKOFF`, doubling, capped, jittered, resets on a healthy gettime).
storm-auto's precedent: *"~2.5h of empty polling drew 77 DataDome pages → fixed by bounded backoff."*

## 36. The apply path is HEALTHY at the moment of submit — 2026-08-28, posto 5084

**CONFIRMED, and it eliminates every cursor-side hypothesis.** This is the first direct measurement
of the document the submit is built in, taken inside the real round rather than in a probe. Raw:
`scratchpad/run0828_dump.json` (5,007 events), 291 bodies in `var/submits/`.

Two concurrent bookings, 80 workers, posto 5084. **All 8 `PRE-SUBMIT` lines carried the identical
signature to the two %PDF-verified bookings of 2026-08-27 (§31):**

```
do_apply: PRE-SUBMIT doc 55,198B markers=['getSlots','captchaDiv','SubmeterVistoCriaPDF',
          'f_date_c','cmbPeriodo'] title='Visa Application Scheduling' origin_ok=True
```

Cursor intact, all five markers, live portal origin, authenticated session — and the portal answered
with the questionnaire page anyway (64,241 B, `Logout` x4, zero scheduling markers).

**Therefore NOT any of:** a lost booking cursor, the `/slots` primer (§32 — already removed), a dead
or expired session, an opaque `chrome-error` origin, a malformed payload. The submit is well-formed
and the portal rejects it on its merits.

Per storm-auto's semantics quoted at `ops_live.py` `_BOUNCE_KEYWORDS`, that response means **"the
slot was taken, session is FINE."**

**Supporting numbers:** 641 leases across both bookings — `form_stale` 246, `session` 393,
**`booked` 0, `bounced` 0** — against only **8 and 20** advertised slots, with the same handful
hammered repeatedly (`2026-09-17/3` x7, `2026-09-18/4` x5). Consistent with the long-recorded
portal behaviour that *"most postos permanently advertise 3-4 phantom periods that cannot be booked
by anyone"* (`_maybe_revert_false_slot_found` docstring).

Also observed: 3 x `This applicant already has an application registered with a future schedule`
(identities that can never book and will retry until stopped), and 88 `no scheduling markers`
warm-up failures against 101 ScheduleController calls.

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

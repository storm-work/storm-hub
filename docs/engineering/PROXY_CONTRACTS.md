# Proxy Contracts

Proxies are a **first-class subsystem**, not an implementation detail behind a generic
abstraction. Providers differ in credential grammar, session semantics, rotation mechanism and
failure modes, and those differences are load-bearing. This file documents the contract for
each supported provider. A "universal proxy" abstraction that hides these differences would be
a bug, not a simplification.

---

## Shared model

A `Proxy` row is a **session template, not an IP.** The exit address behind a row changes over
time. This single fact drives most of the design below.

Consequences:
- `is_healthy` is deliberately **not** a selection filter. A failed probe means "that exit was
  bad a moment ago", never "this row is bad". Filtering permanently discards a good template
  over a transient IP. (Once cost 639 rows to a misconfigured prober.)
- The only recovery that works for a rotating pool is **rotation** — which the LRU ordering
  provides.
- Health/score are still recorded and still break ties, so a consistently bad row sinks without
  being cut.

### Selection order (`_candidate_query`)

Tiered, then LRU within each tier:

| tier | condition | intent |
|---|---|---|
| 0 | `health == "ok"` | proven good |
| 1 | `health IS NULL AND last_used_at IS NOT NULL` | session established, verdict unknown |
| 2 | `health IS NULL AND last_used_at IS NULL` | never established |
| 3 | otherwise | known bad |

`last_used_at` is stamped on **every attempt, not just success**, so a candidate that just
failed rotates to the back instead of being retried first. Tier 1 exists because
`POST /proxies/reset-health` wipes every verdict — on 2026-08-24 one click emptied tier 0
across all 21,600 rows and left a single tier ordered NULLS FIRST, i.e. *never-used sessions
first*, the opposite of the intent.

> **Caveat (see `CONFIRMED_FACTS.md` §16):** the measurement originally justifying tiers 1-vs-2
> was invalidated by the tier-1 poisoning discovery. The ordering is currently a **HYPOTHESIS**.

Candidates are shuffled inside a bounded window (`_spread_window`, capped at pool/4) so
concurrent leasers do not all walk the same head of the queue. The window must stay a fraction
of the pool, or shuffling defeats the "just-failed rotates to the back" guarantee.

### Lease accounting

A **lost claim race** (someone committed first) is counted separately from a **failed health
probe**. Conflating them let a worker burn its whole probe budget on healthy candidates and
then report pool exhaustion — blaming the pool for contention. Confirmed live: 4 of 9 workers
stuck at READY naming five candidates that were all `is_healthy=True`.

Probes run **outside** any open transaction; only the winning candidate is re-locked
(`FOR UPDATE SKIP LOCKED`, single row). Holding a row lock across a multi-second network probe
would serialise a whole fleet launch against itself.

### Screening

**Tier-1 raw (primp) screening is DISABLED by default and must stay that way.** Raw-probing an
exit you intend to browse from takes it from 83% to 0% (`CONFIRMED_FACTS.md` §4). Read that
entry before re-enabling it.

---

## Webshare

| Property | Value |
|---|---|
| Endpoint | `p.webshare.io:80` — **identical for every credential we own** |
| Credential grammar | `{username}-{country}-{geo}-{session_id}:{password}` |
| Sticky | a **numeric** session-id suffix pins one exit |
| Rotating | the literal suffix `-rotate` gives a new IP per request |
| Geo | injected into the **username**: `sodade2026-17` → `sodade2026-DE-17` |
| Rotation mechanism | **change the session id** — no API call, no lease, no Hub round-trip |
| Sticky TTL (dashboard) | 24 h |
| Proxy Idle Timeout (dashboard) | 15 min |
| Actual observed bind stability | **can break in under 10 min while actively used** |

**The session id is the identity.** Because host:port is constant, identifying a webshare
proxy by its endpoint identifies nothing — every worker in every run logged the same string
until this was fixed. Logs now carry `proxy=p.webshare.io:80#51`.

**Rules:**
1. Hold the session id to keep an exit; change it to get a different one.
2. Any retry after a *transport* failure must change the id — retrying a dead tunnel cannot
   succeed. Wired into: both login transient paths, portal-unreachable, the reCAPTCHA
   IP-mismatch path, and `do_swap`'s retry.
3. **Never invent a session id.** `roll_sticky_session` returns the URL **unchanged** when it
   does not recognise a session id to roll. Appending one blindly rewrites the *username* — it
   authenticates against a suffix-parsing gateway and fails outright anywhere else (`u` would
   become `u-276569`). The caller falls back to a real proxy rotation.
4. A credential whose session id reads `rotate` is worth alarming on — it breaks every
   IP-bound session we hold.

**The hard constraint (`CONFIRMED_FACTS.md` §1):** a sticky exit can move *while in use*, well
inside the idle window. Nothing we control prevents this. Keepalive preserves the portal
session; it does **not** pin the exit. Design accordingly — treat "our IP moved" as a routine
event to detect and recover from, not an anomaly.

---

## SOAX

| Property | Value |
|---|---|
| Endpoint | `proxy.soax.com:5000` |
| Credential grammar | `package-{id}-country-{cc}-sessionid-{sid}-sessionlength-{n}` |
| Sticky | `sessionid` segment, bounded by `sessionlength` (3600 s for us) |
| Geo | a **parameter**: `package-335959-country-de-...` |
| Idle release | exit released after `idlettl` of silence |
| Extra failure mode | residential exits simply go offline |
| Rotation mechanism | replace the `sessionid-<x>` segment |

**Status: country-fr is burned** — DataDome ASN block after 488 IPs.

SOAX has a *third* way to lose an exit that webshare does not: the underlying residential IP
can drop off the network entirely, independent of any timer.

---

## Failure semantics (both providers)

| Symptom | Meaning | Correct response |
|---|---|---|
| `ERR_TUNNEL_CONNECTION_FAILED` / `ERR_CONNECTION_CLOSED` / `ERR_TIMED_OUT` | this exit is dead | **roll the session id** — never retry the same one |
| `Por favor, confirma o reCAPTCHA` | token minted on one IP, sent from another — **our exit moved** | roll; do **not** charge the sub-account's strike budget |
| Schedule.jsp loses its markers | often the same IP move | re-warm; check the exit before blaming form state |
| Submit refused | often the same IP move | re-warm; check the exit |
| `chrome-error://` page | failed navigation, now an opaque origin | re-anchor by **navigation** before any in-page fetch |

**One event, three portal symptoms.** Before `exit_ip.py` existed, nothing in this codebase
ever looked at its own exit address, and a single IP move was diagnosed as three different
portal problems — then "recovered" by re-warming or re-logging in *from the new IP*, which is
why those recoveries so rarely worked.

**Charging our own rotation to the account budget burned 3/3 and 2/2 sub-accounts** before
the IP-mismatch check was moved to sit beside `transient`, *before* `attempt += 1`.

`exit_changed()` is biased to False on purpose: a false positive costs a needless session
rebuild — a fresh login, a CAPTCHA solve, and a slice of the binding constraint — so an unknown
reading must never trigger one.

---

## Standing operator instruction

**Do not attribute failures to proxy quality.** Webshare works for login, warmup and slot
detection. When applying fails while those succeed, the cause is in our own browsing/session
handling, not the proxy. This instruction has been correct every time it was tested — it
caught three separate misdiagnoses.

The measured exception, recorded for completeness: a *dead exit* is real and is fixed by
rolling the session id, not by blaming the pool. That is a transport fact, not a quality
judgement about the provider.

---

## soax parameter contract (analysed 2026-08-25)

Our credentials are generated by `scripts/gen_proxy_pool.py` as
`package-{PKG}-country-{cc}-sessionid-{16 random}-sessionlength-{LEN}-bindttl-{T}-idlettl-{T}-opt-wb`.

| parameter | meaning | our value | basis |
|---|---|---|---|
| `sessionlength` | how long the session id may hold one exit | **3600** | **no recorded justification** |
| `bindttl` | how long soax keeps re-binding the SAME exit after it drops | 600 | measured; reverted from 5 on 2026-08-22 |
| `idlettl` | how long a node stays bound with NO traffic | 900 | measured 2026-08-21 |

**All three are OUR values, not provider requirements.**

`bindttl` and `idlettl` each carry a detailed, measurement-backed comment explaining the exact
value. **`sessionlength = 3600` carries none.** It sits under a generic "non-secret defaults
confirmed working as of 2026-08-13" header and entered the repo wholesale in the single Phase-2
reconstruction commit (`e273ef9`) — never separately introduced, justified, or measured.

Interaction: `idlettl` releases an exit for *silence*; `bindttl` governs re-binding after a *drop*;
`sessionlength` is a **hard ceiling that overrides both**. Q1 measured it directly — 20/20 sessions
changed at 60.2–63.4 min under continuous 1/min traffic. No amount of traffic, and no `bindttl`
value, extends it.

**Whether soax supports a longer `sessionlength` is UNKNOWN** — not documented anywhere in this
repo, and not something to assume. That question, not the value itself, is the actionable item.

**What the provider guarantees about exit persistence: nothing we have in writing.** Every stability
property we rely on is inferred from our own measurements.

## What no provider here guarantees

Neither webshare nor soax offers us a contractual exit-persistence guarantee. Webshare exposes
*connection*-scoped controls (Session Timeout, Idle Timeout) which do not govern the session-id →
exit mapping our workers actually depend on — Q1 showed exits moving inside the idle window, and
showed connection reuse conferring no protection.

**The only configuration in this project's lineage with a real per-session exit guarantee is a
STATIC ISP proxy** — one IP per held session, which is what storm-auto migrates onto before holding
(`CONFIRMED_FACTS.md` §19). We own none.

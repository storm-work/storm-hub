# Report — Checkpoint Self-Poisoning in the Apply-Recovery Ladder

**Date:** 2026-08-27
**Trigger:** operator observation — "restore checkpoint happens during applying re-warm, does it
restore the stale session which was not able to book?"
**Evidence base:** direct code read + the live event stream of booking `Test2` (`5d2204ed`,
posto 2032, 5 workers, 07:37:59 → 08:12:13 UTC, stopped by operator).
**Companion artifact:** https://claude.ai/code/artifact/66985de6-e470-4010-b506-4e76685ff314

Labels used throughout: **CONFIRMED** = verified by direct code read or live measurement ·
**OBSERVED** = seen in the data, mechanism not proven.

---

## 1. Answer

**Yes — and the stale state is not left over from an earlier session. The ladder writes it
itself, three times, in the ~9 seconds before it reads it back.**

The write-to-read gap measured live was **22 seconds**.

---

## 2. The chain — CONFIRMED

| # | Step | Site |
|---|---|---|
| 1 | Apply round starts, `_run_warmup_steps()` fails, `do_apply` returns `"session"` (correctly refusing to blame the slot) | `apps/worker/ops_live.py:2168` |
| 2 | The `session` branch transitions `_to(HOLDING, "re-warm failed; rebuilding session")` — **every** `HOLDING` transition fires `on_transition` → `ops.save_checkpoint_now()` | `libs/engine/phases.py:511` → `apps/worker/runtime.py:36` |
| 3 | `_rebuild_session` opens with `_to(HOLDING, "re-warming: …")` — **save #2** | `libs/engine/phases.py:299` |
| 4 | Its `do_warmup()` fails too → `_to(HOLDING, "re-warm failed; re-login: …")` — **save #3** | `libs/engine/phases.py:307` |
| 5 | `_to(LOGGING_IN)` → `ops.do_login()` | `libs/engine/phases.py:308` |
| 6 | `do_login` tries `_try_restore_from_checkpoint()` **before** `_fresh_login()` — loading the file written at step 4 | `apps/worker/ops_live.py:543` |

### Live trace — worker EDER CUNHA BORGES, Test2, t = 27 min

```
t=26.94m  -> holding (portal unreachable; retrying)
t=27.16m  do_apply: re-warm at round start failed -- session problem, not the slot
t=27.16m  -> holding (re-warm failed; rebuilding session)      <- checkpoint SAVE #1
t=27.16m  -> holding (re-warming: apply re-warm failed)        <- checkpoint SAVE #2
t=27.25m  -> holding (re-warm failed; re-login: ...)           <- checkpoint SAVE #3
t=27.25m  -> logging_in
t=27.62m  restoring checkpoint for identity 274de050...        <- RESTORES save #3
t=27.77m  checkpoint restore: session rebuild failed after 9s
          (Page.goto: net::ERR_CONNECTION_CLOSED)
```

### What was on disk — CONFIRMED

Read from `var/checkpoints/` while the run was live:

| Identity | `stage` | `posto_pdf` | Age | Cookies |
|---|---|---|---|---|
| `274de050` | `schedule_jsp` | 2032 | 11.4 min | `Vistos_sid`, `cookiesession1` |
| `030e41c2` | `schedule_jsp` | 2032 | 12.3 min | `Vistos_sid`, `cookiesession1` |

Both were written *during* the failure loop, and both are stamped `schedule_jsp` — the marker
meaning "this session reached a bookable schedule page." Neither had.

---

## 3. Defects

### D1 — the save has no health gate — CONFIRMED

`save_checkpoint_now()` (`ops_live.py:2376`) snapshots whatever cookies the browser holds. Its own
docstring says *"Call after reaching a healthy holding state… the caller decides when a state is
healthy enough"* — but `_rebuild_session`'s **failure** transitions are also `HOLDING`, so the
recovery ladder violates the contract its caller was trusted to keep.

`get_all_cookies()` is a local CDP call, so it succeeds perfectly well when the proxy tunnel is
dead. A broken session always produces a clean-looking save.

**The identical bug class was already found and fixed at the fresh-login seam.** The N4 comment at
`ops_live.py:918` names it verbatim — *"poisoning the next checkpoint save with a
`stage="schedule_jsp"` that isn't true"* — and calls `_clear_anchor()`. The apply-failure seam
never got the same treatment.

### D2 — the stage stamp lies — CONFIRMED, currently harmless

`stage = "schedule_jsp" if self.posto_pdf else "login"` (`ops_live.py:2389`). `posto_pdf` is never
cleared on the apply ladder — `_clear_anchor()` is called at only two sites (`ops_live.py:919`
and `:1935`), neither on this path — so a checkpoint written mid-failure claims a bookable anchor.

Mitigating: **nothing reads `stage`.** A grep across `apps/` and `libs/` finds one occurrence — the
writer. It is misleading to an operator, not yet load-bearing.

### D3 — a dead proxy can be misfiled as spent cookies — OBSERVED

`_try_restore_from_checkpoint` rebuilds on `self.warm_proxy` — the same proxy that just failed. Its
discard rule is *"it rebuilt fine but isn't bookable, so these cookies are spent"*, and on that
verdict it **deletes the checkpoint permanently**.

In Test2 that verdict fired 5 times, on a run whose dominant failure was transport
(40 × `ERR_CONNECTION_CLOSED`, 30 × origin-lost, 10 confirmed exit-IP changes).

**Not proven.** 14 of the 20 restores *did* land in the exception branch, which correctly keeps the
checkpoint. The logs cannot show that the 5 deletions were transport rather than genuinely dead
cookies — only that the rule has no way to tell them apart, and that the run supplied ample
transport failures for it to misread. Restoring onto a proxy that has already failed is also, on
its own, a step that cannot succeed.

### D4 — no retention sweep — CONFIRMED, pre-existing (F17)

`cleanup_stale()` still has zero callers outside its own docstring. 30 checkpoint files are on
disk; 12 are 6–7 days old, for postos 3018 and 3059 that appear in no current run. Restated here
because it compounds D1: a poisoned file has no expiry.

---

## 4. Cost — CONFIRMED

20 restore attempts inside a 32.5-minute run. Each pays `RESTORE_STAMPEDE_JITTER`
(`random.uniform(0, 30)`, ~15 s mean, `ops_live.py:576`) **before** the
`CHECKPOINT_RESTORE_BUDGET` clock starts, then a browser launch and a navigation.

| Restore outcome | Count | Effect |
|---|---:|---|
| session rebuild failed (`ERR_CONNECTION_CLOSED`) | 14 | Checkpoint kept. Cookies never tested. |
| unusable — *discarding this checkpoint* | 5 | Checkpoint deleted. See D3. |
| budget spent, skipping re-warm | 1 | Straight to login. |
| re-warm succeeded (after 125 s) | 1 | The one success. |

Roughly **8 of 32 minutes** spent restoring a session the worker had itself just declared broken,
onto the proxy that had just failed, for a 1-in-21 success rate.

---

## 5. The decision

### Option A — gate the write — RECOMMENDED FIRST

Make `save_checkpoint_now()` refuse to write unless the anchor was established *by the chain, on
this session, recently*:

```
_anchor_src == "chain"  AND  _anchor_age() < form-state TTL (~4-6 min, CONFIRMED_FACTS §30)
```

That invariant already exists and already means exactly this — `_set_anchor("chain")` is set only
on a successful `_run_warmup_steps()` (`ops_live.py:1453`), while `_set_anchor("checkpoint")`
deliberately leaves `_anchor_at = None` (`ops_live.py:641-644`).

Self-contained inside one method in `ops_live.py`. No `phases.py` change, no new settings, no new
portal traffic.

**The age check is not optional.** A failed re-warm never reaches `_set_anchor`, so `_anchor_src`
keeps its previous `"chain"` value from the last *successful* warm. Source alone would still let
the poisoned write through.

### Option B — don't read the checkpoint inside the recovery ladder

Give `do_login()` a `prefer_fresh` flag that the apply/keepalive rebuild paths pass, sending them
straight to `_fresh_login()`. The restore chain stays intact for launch and respawn — which is what
it was built for, and where a checkpoint is genuinely older than the process reading it.

Costs more per recovery (a CAPTCHA solve and a slice of the sub-account budget), so it is the
second move, not the first.

### Option C — a transition-detail gate in `runtime.py`

Skip the save whenever the `HOLDING` transition carries a `detail` string. One line, and it does
cover all three saves in the trace above. **But it leaks:** `phases.py:572` reaches a detail-less
`HOLDING` after a bounce re-warm that may itself have failed. Weaker than A for the same effort.

### Not proposed

Rotating the proxy inside the restore chain. It would address D3 directly, but it is
proxy-selection work and that remains frozen under **D23**. Flagged, not done.

### Recommendation

**A now, B next.** A fixes what gets written, B fixes when it is read, and A alone already breaks
the self-poisoning loop. Neither touches `do_apply`, `do_swap`, proxy selection, or the vendored
engine.

---

## 6. Recent runs

### Test2 — posto 2032, 5 workers, soax rotating (DE / IT)

Started 07:37:59 UTC, stopped by operator 08:12:13 UTC. 32.5 minutes.

| Measure | Value | Reading |
|---|---:|---|
| Slot leases | 48 | 1.48 / min overall |
| Lease outcomes | 48 × `session` | **Zero bounced.** No real slot was blamed for our failure. |
| `portal origin could not be restored` | 30 | Fix A firing as designed |
| `Failed to fetch` | 0 | Was 96 pre-fix |
| Parks | 4 | Fix B firing at exactly x5, twice per worker |
| `ERR_CONNECTION_CLOSED` | 40 | Underlying failure, unchanged |
| Exit-IP changes | 10 | Mostly our own rolls after a dead tunnel |
| Bookings | 0 | Total still 17; last real booking 2026-08-21 05:30 UTC |

**Both fixes are live-proven.** Fix A eliminated the `Failed to fetch` class outright and cut
failed rounds from 30–43 s to 4–11 s. Fix B classified every one of 48 leases as `session` rather
than `bounced` — the direct counter to the 2026-08-15 run that manufactured 5,082 bounced leases
against a pool that was never phantom.

**What is still wrong — the park does not brake.** `_PARK_POLL_GAPS = 3` (`phases.py:41`) ×
`HOLD_POLL_GAP = 5.0` ≈ 15–20 s, against a failure that recurs on a multi-minute cycle:

| Worker | Parks | Interval |
|---|---:|---|
| `9fd63b7f` | 2 (t=25.5m, t=31.0m) | 5.5 min |
| `a7101efc` | 2 (t=29.9m, t=31.0m) | **66 seconds** |

Churn accelerated rather than converging — 1.0 leases/min over the first 28.5 min, **5.8/min**
across the final 3.3 min, as more workers converged on `applying` simultaneously. That final-window
rate is worse than the pre-fix run's 2.3/min.

### Apply-Recovery Revalidation — posto 2032, 1 worker

Started 05:17:14 UTC, ran ~10.4 minutes. **0 slot leases** — it never reached the apply loop. One
exit change, one checkpoint restore. It validated the pre-apply pipeline and produced no apply-path
evidence; every apply-path finding in this report comes from Test2.

---

## 7. Unchanged

Nothing has booked since **2026-08-21 05:30:45 UTC**. The total remains 17 appointments. The soax
exits continue to die under warmed sessions after roughly 4–6 minutes, and no fix in this report
addresses that — they make failure cheaper and better-classified, not less frequent.

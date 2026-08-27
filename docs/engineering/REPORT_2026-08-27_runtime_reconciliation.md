# Report — Fresh Runtime Reconciliation + Apply-Path Validation

**Date:** 2026-08-27
**Scope:** Phases A–H of the runtime-reconciliation directive.
**Outcome:** Phases A, B and the Phase G baseline are complete. Phases C–F are **BLOCKED** — not by
state, but by the single-operate-seat control, which the operator currently holds.

Labels: **CONFIRMED** = direct code read or measurement · **OBSERVED** = seen in data, mechanism
unproven · **UNAVAILABLE** = evidence does not exist to check.

---

## 1. Runtime identity — CONFIRMED

| Fact | Value |
|---|---|
| HEAD | `a063125` on branch `apply-revert-proven` |
| Working tree | clean of production changes (2 doc files only) |
| Last commit touching `apps/` or `libs/` | `6d3c8fc`, 2026-08-27 **05:05:19** UTC |
| Diff `6d3c8fc..HEAD` | `CONFIRMED_FACTS.md` + `probes/formstate_ttl_probe.py` only — **no production code** |
| Hub PID | 2214565, started 2026-08-27 **07:36:16** UTC, cwd `/srv/projects/storm-hub` |
| Production `.py` newer than the Hub process | **0** |
| Stale `.pyc` newer than the Hub process | **0** |
| Orphan Hub processes | none (restarts today: 03:52:03, 05:13:02, 07:36:17 — only the last is alive) |
| Playwright/Chromium processes | **0** (every `chrome` match was the Claude Code CLI, not a browser) |
| Ahead of `origin/main` | 131 commits, unpushed |

**Worker code revision is not a separate question.** `Supervisor` spawns each worker as
`subprocess.Popen([sys.executable, "-m", module, key], cwd=_ROOT)` (`apps/hub/supervisor.py:153`),
so a worker always imports the tree as it stands at spawn time, independent of how old the Hub
process is.

⇒ **Everything that has run today executed the same production code: `6d3c8fc`.**

---

## 2. Configuration — CONFIRMED, no discrepancy

Settings layers in the DB: **7 rows, all `global`** (`CAPSOLVER_KEYS`, `TELEGRAM_*`, `CF_*`,
`EMAIL_PROVIDER`). No booking-layer or worker-layer rows exist.

Fidelity check — every key in `booking.settings_json` landed verbatim in the worker's
`effective_settings_json`:

| Booking | Result |
|---|---|
| Booking 0821 soax | OK — all booking keys landed |
| Test2 | OK — all booking keys landed |
| APPLY-RECOVERY REVALIDATION | OK — all booking keys landed |

`worker.settings_json` is `{}` for all three — the worker override layer is unused.

⇒ **dashboard → DB → worker settings_json → effective config has no divergence.** Phase A's
"STOP and report if there is a discrepancy" condition is not met.

**Security note (not part of the directive, recorded because it was encountered):** `CAPSOLVER_KEYS`
and `TELEGRAM_BOT_TOKEN` are stored as plaintext in the `settings` table. Same class as F27.

---

## 3. Database state

| | Count |
|---|---:|
| bookings | 8 (all `stopped`) |
| workers | 130 |
| identities | 186 |
| appointments | 17 (most recent **2026-08-21 05:30:45** UTC) |
| slots | 172 — `available` 106, `closed` 51, `taken` 15 |
| slot leases | 174 — `session` 154, `bounced` 6, `booked` 5, `expired` 5, open 4 |
| **open slot leases (unreleased)** | **0** |
| proxy leases | 2354 — **active (unreleased): 0** |
| orphaned proxy leases (worker row gone) | 0 |

**Stale operational rows found:**

- **53 workers sit at `ready` on bookings that are `stopped`** — Booking 0821 webshare (21),
  0821 soax (18), Booking 0814 (10), Test2 (2), 0821 mobile (2). `ready` is a non-terminal status;
  these are leftovers of the stop path, not live workers (no processes, no leases).
- **4 `SlotLease` rows with `outcome=NULL`** from 2026-08-14 (Booking 0814, workers `stopped`).
  They hold no live slot — all 4 target slots are `available` — but the rows are 13 days orphaned.
- 1 worker `stopped/holding`, 1 `completed/holding` — substate not cleared on terminal status.

**Identity ↔ appointment disagreements: 9** — this is the finding with real operational risk:

| Direction | Count | Consequence |
|---|---:|---|
| has a `BookedAppointment` but `Identity.status = active` | 3 | **re-selectable for a new booking → double-booking risk** |
| `Identity.status = booked` but no appointment row | 6 | excluded from selection despite being free — shrinks the usable pool |

The 3 at risk: `35b31acb` EDSON MENDES CORREIA, `dbb8a0fb` NELSON ALMEIDA,
`d134fe8e` RICARDO RONALDO CORREIA.

---

## 4. Checkpoint state — CONFIRMED

30 files. Provenance classified by correlating each `saved_at` against the worker event stream
(±90 s) rather than by trusting the `stage` field.

| Class | Count | Notes |
|---|---:|---|
| **FAILURE-PRODUCED** | 2 | `030e41c2` FELICIANO PIRES, `274de050` EDER CUNHA BORGES — both written inside Test2's recovery ladder, ~30 min old, both stamped `schedule_jsp` |
| **HEALTHY** | 1 | `2c4eb57e` — see correction below |
| **UNKNOWN** | 4 | no correlating events in the retained stream |
| **STALE** (> 3 days) | 23 | postos 5084 / 3018 / 3059, no current run; oldest 7.75 days |

22 of 30 carry `stage="schedule_jsp"`. Per the confirmed self-poisoning finding, that stamp is not
evidence of health, and the classification above deliberately ignores it.

**Correction to the first pass.** `2c4eb57e` (ALEXSANDRO PAITRICK) was initially filed UNKNOWN.
Its `saved_at` is **05:27:37.007**, which is REVAL's `t=10.38m` — the `HOLDING` transition of the
`no_book` gate, taken from a session that had just warmed, discovered 32 slots and been selected to
apply. That is a genuine healthy save. Reclassified **HEALTHY**.

No checkpoint was deleted or moved during this investigation.

---

## 5. Dashboard vs Claude — the runs ARE equivalent

The premise under test was that Claude-driven and dashboard-driven runs might differ in software,
configuration or state. **For the two runs on current code, they do not.**

| | APPLY-RECOVERY REVALIDATION | Test2 |
|---|---|---|
| Started | 2026-08-27 05:17:14 | 2026-08-27 07:37:59 |
| Hub PID | 2203652 (started 05:13:02) | 2214565 (started 07:36:17) |
| Worker code | tree at 05:17 = `6d3c8fc` | tree at 07:37 = `6d3c8fc` |
| **Commits to `apps/`+`libs/` between them** | **0** | |
| posto | 2032 | 2032 |
| proxy | soax / rotating / DE, IT | soax / rotating / DE, IT |
| workers | 1 | 5 |
| `MONO_BOOKING` | **True** | **False** |
| `no_book` | **True** | **False** |
| booking type | `real` | `test` |
| Effective-config differences | **exactly 3 keys**: `MONO_BOOKING`, `no_book`, `_sub_account_id` | |
| Checkpoint at launch | present, restored, rejected in 53 s | present for 2 of 5 identities |
| Discovery | gettime + 32 slots at t=10.38m | gettime + slots at t=4.84m |
| Slot leases | 0 | 48 (over 2 distinct slots) |
| Furthest stage | **APPLYING → `dry_run`** | APPLYING → re-warm failed ×14 |

**Why the lease counts differ, and it is not a defect.** `phases.py:412-414` consults
`await_assignment()` *before* the self-discovery branch. Under `MONO_BOOKING=True` with one worker
and an empty pool, REVAL got no assignment, discovered its own slots and applied `found[0]`
directly — **no Hub lease is taken on that path**. Test2's five workers were assigned from the
shared pool, so every attempt cost a lease. The two runs exercised *different halves* of the
pipeline: REVAL covered DISCOVER→APPLY without LEASE/ASSIGN; Test2 covered ASSIGN/LEASE without
ever completing a re-warm.

**`Booking 0821 soax` (2026-08-21) IS genuinely divergent** — 27 commits to `apps/`+`libs/`
separate it from now, plus catalog drift (`CAPTCHA_FAILURE_BUDGET`, `LOGIN_IP_MISMATCH_RETRIES`,
`MONO_BOOKING`, `REWARM_ESCALATE_AFTER` added; `HOLD_SWAP_AFTER_FOUND` renamed). Its behavioural
evidence is **UNAVAILABLE**: the Redis `evt` stream retains 5,003 entries back to 2026-08-25 18:18,
so its events are trimmed. Its marker counts are unknown, **not zero**.

### A hypothesis raised and then disproved

Mid-investigation the leased slots looked like 13-day-old rows (`created_at = 2026-08-14`), which
would have meant Test2 applied against a stale pool. **That is wrong and is retracted.** The slot
row is upserted on rediscovery: `created_at` is the row's first-ever insert, `last_seen` is the
observation. Both slots show `last_seen = 2026-08-27 07:44:46`, `discovered_by = 9fd63b7f` (a Test2
worker of this run), and dates `2026-08-28` — the same dates REVAL found independently. The
`slots_found` event at t=4.84m confirms it. Test2 applied against **real, freshly observed slots**.

---

## 6. E2E result

**Furthest stage reached: `DO_APPLY`.**

```
LOGIN ✓ → WARM ✓ → HOLD ✓ → DISCOVER ✓ → PERSIST ✓ → ASSIGN ✓ → LEASE ✓
      → HANDOFF ✓ → RE-WARM ✓ → DO_APPLY ✓ → SUBMIT ✗ → PDF_VERIFY ✗ → BOOKED ✗
```

Evidenced by REVAL's own terminal event:

```
t= 8.70m [status_change] warmed → holding → discovering
t= 8.78m [gettime_result] special_days populated
t=10.38m [slots_found]    9+ slots, 2026-08-28 p3/p4/p5, 2026-08-31 p3...
t=10.38m [status_change] applying
t=10.38m [terminal] {"outcome":"dry_run","detail":"no_book: not submitted","substate":"holding"}
```

`LEASE`/`ASSIGN` are ticked from Test2 (48 leases, all correctly classified), not from REVAL.

**Untested, explicitly:** `SUBMIT`, `PDF_VERIFY`, `BOOKED`. Reaching them requires a real
submission against the live portal. Posto 2032 is production consulate infrastructure, not test
infrastructure, and the portal does not sanction automated submission there — so per the
directive's own Phase E rule, the run stopped at the `no_book` boundary. Nothing was manufactured.

Also worth recording per the "do not stop at the first login failure" rule: REVAL's **first worker
failed login** (3 reCAPTCHA rejections across all 3 sub-accounts, then the portal's "try again
later" page, terminal `failed/login` at t=6.80m). The supervisor respawned it, and the second
process logged in at t=7.92m and completed the chain. **Retry/recovery behaved correctly.**

---

## 7. Earliest causal failure

**Test2 — INFRASTRUCTURE, not an application defect.** The first cause is the soax exit dying
under a warmed session: 40 × `ERR_CONNECTION_CLOSED`, 30 × origin-lost, 10 confirmed exit-IP
changes across 32.5 minutes. Every downstream symptom (14 round-start re-warm failures, 14 session
rebuilds, 20 checkpoint restores, 48 `session` leases, 4 parks) follows from it. The application
classified the condition correctly at every step — **zero of 48 leases were charged to a slot** —
and recovered as designed. It simply never had a working tunnel.

**REVAL — SERVER.** The first worker's login died on portal-side reCAPTCHA rejection and the "try
again later" page. Recovered by respawn.

Neither run's earliest cause is an application defect.

---

## 8. Checkpoint behaviour

**Self-poisoning occurred, and the gate is not implemented.** Two failure-produced checkpoints sit
on disk right now (`030e41c2`, `274de050`), written inside Test2's recovery ladder and stamped
`schedule_jsp`. Option A was **not** applied during this investigation, per the directive.

The required invariant is **not** currently held:

```
failed re-warm  →  healthy checkpoint written anyway  →  recovery restores the just-failed session
```

One counter-observation worth recording: the **rejection** path does work. REVAL restored
`2c4eb57e`, found it not bookable, and discarded it — `checkpoint restore: unusable after 53s`.
So a genuinely dead checkpoint is caught; the defect is that a dead session is allowed to *become*
a checkpoint in the first place, and that catching it costs 53 s.

No evidence was deleted. Full provenance recorded in §4 before anything else was considered.

---

## 9. Resource integrity — baseline only

No run was executed, so there is no "after". Baseline recorded 2026-08-27 ~08:45 UTC:

```
appointments 17   slots 172   slot_leases 174   proxy_leases 2354
open slot leases 0   active proxy leases 0
workers 130   bookings 8   identities 186   checkpoint files 30
Playwright/Chromium processes 0
```

Nothing was mutated by this investigation except two documentation files.

---

## 10. Code changes

**NO PRODUCTION CODE CHANGED.**

`git diff 6d3c8fc..HEAD` touches only `docs/engineering/CONFIRMED_FACTS.md` and
`probes/formstate_ttl_probe.py`. The working tree adds only `FAILURE_MODES.md` (F28) and two
report files under `docs/engineering/`.

---

## 11. Decision

### BLOCKED — the operate seat, not the runtime state

The state is **clean and reconciled**: code identity proven, config chain intact, zero active
leases, zero orphan processes. There is no runtime discrepancy to resolve.

The block is a permission control working as designed. `require_operator` (`apps/hub/auth.py:118`)
demands the single live operate seat, and the operator holds it — session `f7b839ac`, `storm`
(admin), `purpose=operate`, `status=active` since 07:36:33, with a viewer session opened at
08:37:19. Creating and starting a validation booking requires that seat. The only ways past it are
to mint a token bound to the operator's live session (impersonation) or to open a second operate
session and request handover (which evicts the operator's dashboard). **Neither was done.**

Writing booking/worker rows straight into the DB was also rejected: it would bypass the
create/start path that Phase D exists to exercise.

### Ready-to-run specification for the validation booking

Everything needed has been selected and verified. Recommended parameters:

| Field | Value | Why |
|---|---|---|
| posto | `2032` | designated test posto; REVAL's 32 fresh slots for 2026-08-28+ are still `available` |
| type | `test` | keeps it out of the real-booking accounting |
| workers | 1 | isolates the trace |
| `MONO_BOOKING` | `true` | one worker must discover for itself |
| `no_book` | **`true`** | production portal; see §6 |
| `ENGINE_MODE` | `live` | |
| proxy | soax / rotating / `DE, IT` | holds the variable constant against both prior runs |
| `CAL_START_DATE` / `CAL_END_DATE` | `2026-08-28` / `2026-09-25` | matches both prior runs |
| identity | any of 98 verified-clean candidates, e.g. `c11bf37d` RICARDO FERREIRA | no appointment, no checkpoint file, 3 verified sub-accounts, not attached to any live worker |

Selecting an identity with **no checkpoint file** is deliberate: it removes the restore chain from
the first login and makes the run's checkpoint behaviour observable from a known-empty start.

To make Phase F conclusive, the run should be watched for the sequence
`re-warm at round start failed` → `-> holding (re-warm failed; rebuilding session)` →
`restoring checkpoint`. Under current code that sequence is expected to appear; the write gate is
what would prevent it.

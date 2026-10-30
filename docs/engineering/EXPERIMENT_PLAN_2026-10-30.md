# Hot-window experiment plan — booking day 2026-10-30

Everything below is decided BEFORE the window. The window is the only time the portal is in its stress
posture, so an experiment that is not set up in advance is lost. Times are UTC and assume slot-open **T0 =
10:00 UTC** (confirmed by the operator 2026-10-30).

**Rule zero: the real booking comes first.** Experiments ride on it; none may reduce its chance of booking by
more than the share of workers it uses (≤ 30 % in total), and nothing is changed after T0−10 min.

## 1. What we do not know yet (the questions)

| # | Question | Why it matters |
|---|---|---|
| X1 | Does the `quest_hold` hold survive the strict window and the stress cycle better than the `schedule` re-warm (what booked 12 on 1002)? | It is the new default. Probes only ran outside the window. |
| X2 | Which hold interval is best: 60–120 s, 60–300 s (default) or 240–420 s? | Less traffic vs. margin to the ~10.4 min idle expiry. |
| X3 | Does the resume at the signal work live, and how much time does it save? | Unit-tested only. Full chain ≈ 40 s. |
| X4 | Where do the minutes go between slot-open and the first submit? (open → first `/slots` answer → found → warm → submit) | Last run: slot-open → found took 3.5 min. The re-warm-each-round fix is untested live. |
| X5 | Does `PRIMP_IMPERSONATE=chrome_148` get detached primp through the strict window? | Decides whether a primp/hybrid arm is ever viable. Needs a strict window. |
| X6 | When exactly does the login wall rise, and does it hit held sessions? | Sets how early everyone must be logged in. |
| X7 | How long does a warmed cursor stay bookable (Q29), and does an exit change under a held login matter (Q28)? | Gates pre-warm timing; decides whether rolling a dead exit is cheap. |
| X8 | What does ~60+ concurrent Donut profiles cost the host (CDP throttle, load, "context destroyed")? | Scale limit for the fleet size. |
| X9 | Blind apply (periods seeded by the operator) vs discovery-driven apply. | Only if a blind booking is created. |
| X10 | What is the real portal up/down cycle? | PortalPulse records it passively. |

## 2. Layout: bookings are the arms

Settings are frozen into each worker at launch, so an arm is a **booking** with its own overrides, all on the
same posto. The identity pools must not overlap (an identity belongs to one active booking). Use the identity
**group** label to keep pools apart and to avoid mixing quality between arms (shuffle, then split).

| Booking name | Share | Overrides | Answers |
|---|---|---|---|
| `W-main` (control) | ~70 % | none (defaults: `quest_hold`, 60–300 s) | the real run; baseline for X1–X4 |
| `W-sched` | ~10 % | `KEEPALIVE_MODE=schedule` | X1 (old default) |
| `W-short` | ~10 % | `QUEST_HOLD_MIN_S=60`, `QUEST_HOLD_MAX_S=120` | X2 |
| `W-long` | ~10 % | `QUEST_HOLD_MIN_S=240`, `QUEST_HOLD_MAX_S=420` | X2 |
| `W-blind` (optional) | 5–10 % | `BLIND_APPLY_MODE=on`, periods seeded by you | X9 |

Slots found by any arm feed the others through the existing cross-booking feed (`discovery_feeder_booking_ids`
on the apply bookings), so the arms cooperate on discovery and compete only for the same slots. Keep every
booking's `slot_open_at` / `warmup_start_at` identical. Keep `HYBRID_MODE` off everywhere (browser path).

Minimum sample: an arm under ~15 workers can only be read as "no disaster / clear disaster", not as a rate.
If the pool is small, run **only** `W-main` + `W-sched` + one interval arm and skip the rest.

## 3. Side probes (spare identities, never part of a booking)

Reserve **6 spare ACTIVE identities with a Donut profile** and label their group `EXP`. They are touched only
by these probes, one login shot per account per hour at most (the per-account cap is 3/h). Each probe runs its own login, so give each probe its own spare and do not reuse one inside the hour.

| Probe | When | How | Reads |
|---|---|---|---|
| X6 login wall | T0−100, −80, −60, −40, −20, 0, +15 | one `probes/quest_hold_probe.py ... --login-only --tag wall-HHMM` per spare (login attempt, then exit) | login success/"try again later" vs. time to open |
| X5 primp A/B | from T0−70 (strict flips ≈ T0−76) every 10 min until T0+30 | `probes/primp_impersonate_probe.py <posto>` on a logged-in spare | interstitial (44 KB) vs real form, default vs `chrome_148`, same exit/cookies |
| X7 cursor life (Q29) | T0−120 and T0−60 | `probes/formstate_ttl_probe.py` (usage in its header) on a spare | cursor still bookable after Δ without navigating |
| X7 exit change (Q28) | any quiet 20 min before T0−90 | `probes/session_ip_migration_probe.py` (usage in its header) | auth survives an exit roll |
| X1/X2 long soak | T0−150 → T0+60 | `probes/quest_hold_probe.py --min-s 60 --max-s 300 --minutes 200 --tag window-soak` on one spare | uninterrupted hold through the whole window, event by event |

The soak probe is the cleanest single measurement of the hold: it books nothing and logs every event with its
verdict, so a failure has a timestamp that can be matched to the pulse.

## 4. Instrumentation (all passive, start before anything else)

* `python -m scripts.window_recorder` in its own terminal from T0−180 until T0+120: pulse state and counts,
  fleet by booking/role/substate, appointments per booking, load, browser processes, running Donut
  profiles → `var/window/<date>.jsonl` (every 10 s).
* PortalPulse (`PORTAL_PULSE=true`): the closed up/down windows → `GET /portal/pulse?n=1000` saved at T0+120.
* Hub event stream and worker logs (`var/worker-logs`) stay as they are. **Do not run
  `scripts/cleanup_worker_logs.py` or `cleanup_checkpoints.py` today.**
* Save at T0+120: `/portal/pulse?n=1000`, each booking's worker and appointment counts (dashboard), the soak probe's jsonl.
* Preflight at T0−150 and again at T0−30: `python -m scripts.window_preflight` (add `--create-test` once).

## 5. Timeline (T0 = 10:00 UTC)

| Time | Action |
|---|---|
| before 07:00 | Arm bookings created, identities attached, 6+ `EXP` spares registered (§9). `window_link_arms` run. |
| 07:00 (T0−180) | Preflight (`--create-test`). Start the recorder. Start `window_probes` (it sleeps until 07:30). |
| 07:30 (T0−150) | `warmup_start_at`: the fleet logs in and parks on the Questionario hold. Soak probe starts. Preflight again. |
| 08:00–09:00 | Login wall expected from ≈ 08:30 (T0−90). Formstate probe 08:00 and 09:00; wall probes 08:20 → |
| 08:44 (T0−76) | **Strict flip expected.** Primp A/B 08:50, 09:05, 09:20, 09:35. |
| 09:20 | **Freeze.** No setting changes, no restarts, no new workers. |
| 09:30 | Scheduled slot-open fires on all five bookings (the portal's claimed 10:00 is the late bound). |
| 10:00 (T0) | Claimed open. Do nothing by hand. Watch the recorder only. |
| 12:00 (T0+120) | Stop the recorder, save the artifacts above, then analyse. |

## 6. What each result decides (pre-registered)

* **X1:** *alive-at-signal* = logged in, last hold/keepalive `ok` within 2 × the arm's max interval of the
  signal, no re-login since. If `W-main` < `W-sched` by more than 15 points, set `KEEPALIVE_MODE=schedule` as
  the default before the next window; if equal or better, keep `quest_hold`.
* **X2:** the longest interval whose alive-at-signal is not worse than the default's becomes the default.
  If `W-long` loses sessions to idle expiry, cap `QUEST_HOLD_MAX_S` at 300 for good.
* **X3:** log lines `RESUMING the held questionnaire` vs full chain, with the warm-up duration and outcome.
  Resume failing more than the full chain → turn the resume off (`_qh_take` returns None) and keep the hold.
* **X4:** per signal round: open → first `/slots` answer → found → warm start → submit → answer. The biggest gap
  is the next thing to fix.
* **X5:** `chrome_148` passes where the default is challenged ≥ 80 % of the time → a primp arm is worth
  building; both challenged → primp stays out of the strict window for good.
* **X6:** the time at which new-login success falls under 5 % is "everyone logged in by" next time.
* **X7:** cursor still bookable at Δ → the longest safe pre-warm lead; auth survives a roll in ≥ 8 of 10 →
  rolling a dead exit under a held login is cheap and F41's hold matters less.
* **X8:** peak concurrent profiles, load1, CDP "context destroyed" count by minute → the fleet size ceiling.

## 7. Abort rules (protect the real run)

* A probe or arm that is damaging the control (load1 > 15, > 5 % of workers in a restart loop) is stopped
  immediately; the control is never touched.
* If Donut starts answering 429 on creation, stop creating anything new; existing profiles keep working.
* If the Hub stops answering, do not restart it during the window unless workers have already lost their
  signal path; restarting re-clears the slot-open latch by design (fresh-run semantics).

## 8. Known unknowns and how they are handled

* Settings are frozen at worker launch: an arm cannot be switched mid-window. That is why §2 is fixed up front.
* `QUEST_HOLD_*` and `KEEPALIVE_MODE` live at the booking layer; a template cannot override them.
* The login throttle (3 shots/account/h) shapes how many workers are logged in at T0−90, whatever the arm.
* The resume path has run only against fakes. It falls back to the full chain, so a failure costs ≈ 40 s,
  not the session.

## 9. Operator runbook (dashboard) -- exact settings

Identity budget: 80 real (apply) + test identities (scout). **Real identities** are split by shuffling them
first (so no arm gets the better accounts), **test identities** all go into `W-main`.

| Booking | type | real (apply) identities | test (scout) identities | Settings (BOOKING layer) |
|---|---|---|---|---|
| `W-main` | real | 50 | all (scouts) | *(none -- defaults: `KEEPALIVE_MODE=quest_hold`, 60-300 s)* |
| `W-sched` | real | 7 | none | `KEEPALIVE_MODE=schedule` |
| `W-short` | real | 7 | none | `QUEST_HOLD_MIN_S=60`, `QUEST_HOLD_MAX_S=120` |
| `W-long` | real | 7 | none | `QUEST_HOLD_MIN_S=240`, `QUEST_HOLD_MAX_S=420` |
| `W-blind` | real | 7 | none | `BLIND_APPLY_MODE=true`, `BLIND_MAX_BOUNCES=0` |

2 real identities stay unassigned as reserve. Every booking: **posto 5084**, same nationality/residence,
`warmup_start_at` = **07:30 UTC**, `slot_open_at` = **09:30 UTC** (identical on all five bookings). The portal
claims 10:00 but has opened up to ~15 min early before; discovery only runs once slot-open has fired, so
`slot_open_at` is set 30 min ahead of the claimed time (scouts then poll from 09:30; the apply workers
re-warm each round anyway).
`HYBRID_MODE` stays off. Do not copy any setting that is not in the table.

Steps (dashboard, Bookings page):
1. Create `W-main` (type real, posto 5084, times above, no settings). Attach the 50 real + all test identities.
2. For each arm: **Duplicate** `W-main` (copies posto/nationality/residence/settings, not identities or times),
   rename, then set the arm's settings in the booking's settings editor and the two times. Attach its identities.
   Check the booking's *effective settings* show the arm's keys and nothing else different.
3. `W-blind`: create the same way (identical times), then seed the 5084 periods (`POST /bookings/{id}/blind-apply`).
4. Link the arms to the control (the dashboard has no field for it):
   `.venv/bin/python -m scripts.window_link_arms` then `--check`. `W-blind` stays unlinked.
5. Schedule all five bookings (they warm up by themselves at 07:30). Blind mode with a `slot_open_at` set HOLDS
   the seeded pool until that instant (scheduler `_drive_booking`: the immediate blind fire only applies when
   no `slot_open_at` is set), so `W-blind` is scheduled exactly like the others and warms up before the login
   wall. `BLIND_MAX_BOUNCES=0` keeps the 09:30-10:00 bounces from retiring the grid.
6. Nothing is started by hand. Freeze at 09:20 (T0_sched-10): no setting changes or restarts after that.
7. Test identities for the spares: register at least 6, set their **group to `EXP`**, leave them unattached to
   any booking. They need an ACTIVE identity, a VERIFIED sub-account and (donut backend) a profile.

Probes (automatic): `.venv/bin/python -m scripts.window_probes --t0 10:00 --booking W-main`
(`--dry-run` prints the plan and the spares it found). Output: `var/window/probes/`. Stop everything with
`touch var/window/ABORT`. Not automated: the exit-change probe (`session_ip_migration_probe.py`, X7/Q28), which
does not take the EXP scope -- run it by hand in a quiet period if wanted.

# Stress-Window Booking — Run 1023 Analysis and Target Design (2026-10-23)

Run 1023 booked nothing. The failure modes are catalogued as F40-F49 in `FAILURE_MODES.md`. This
report gives the timeline, separates what we measured from what we don't know, and states the
behaviour we want: **every worker should do what PA508829's worker did, as early as the portal
allows.**

## A. Timeline (event stream, UTC)

| Time | Portal | Fleet |
|---|---|---|
| 08:33-08:48 | healthy | 120/123 apply workers logged in (3 never: reCAPTCHA rejections) |
| 08:45-10:00 | healthy | ~150 ok auth_only keepalives / 15 min. Browser "keeping" mostly unverified (F43) |
| ~10:15 | first `Servico Indisponivel` | keepalive ok rate 55 -> 36 -> 23 -> 1 by 11:00 |
| 10:39 / 10:47-10:52 | 503 on `QuestNextQuestion` | slot_open + slot_found fired; every warm-up fails |
| 10:47-10:53 | | **75 workers drop their login** after one failed re-warm (F40) |
| 11:06-11:40 | 503s stop; maintenance ~11:17; then mostly not accepting connections | ~9,800 exit rolls in total (F41); logins fail |
| 11:23-12:06 | recovering | 25 workers log back in; Browser ones warm 1-2 min later |
| 11:41-12:11 | up | 13 Browser workers warm, 77 submits, all to blind phantoms (F48). Primp: 0 warmed (F42) |
| 12:05 | up | PA508829 fully warmed -> "applicant already has a request": a competitor got there first (F47) |

## B. What the reference success proves

Worker 689db64b (PA508829): login 12:02:37 -> questionnaire 12:03:46 -> every quest step ->
`Formulario?copy=true` 12:05:13 -> ScheduleController 200 at 12:05:29. **Once the portal answers,
our warm-up works in ~2 minutes.** The same holds for every Browser worker that was logged in after
recovery (warm 1-2 min after login, 13/13).

So the run was not lost in the warm-up code. It was lost in:
1. **Login availability at recovery:** we discarded the logins at the start of the outage (F40/F41)
   and had to win the login lottery again during recovery, while competitors were submitting.
2. **Transport fitness:** the hybrid fleet's 10 s connect timeout could not reach a portal that
   took ~25 s to accept a connection (F42).
3. **Targets:** no real slots were discovered, so even warmed workers could only fire at phantoms
   (F48).

## C. Confirmed vs unknown

**Confirmed:** F40-F49 as measured. A login can live 3+ hours with keepalives (CONFIRMED_FACTS §27).
A portal-side event can invalidate every login at once (§29).

**Unknown (OPEN_QUESTIONS Q27-Q29):**
- Q27: does a login survive an outage like 10:15-11:40 (no successful keepalive for ~80 min,
  maintenance at ~11:17)? No worker carried a pre-outage login through it. Every post-recovery
  warm-up followed a fresh login.
- Q28: does an exit change invalidate the login? n=1 says no (Q19 update); the code assumes yes.
- Q29: how long a warmed cursor stays bookable without re-warming.

The target design must be correct **whichever way Q27/Q28 resolve**. It must never lose time by
assuming the answer.

## D. Target design — "every worker like PA508829, as early as possible"

**1. Before stress: be really ready.**
- KEEPING must mean authenticated on a live exit: fix F43 (browser dead-exit detection) and F44
  (primp auth check).
- Warm before the portal degrades (F45): schedule-mode keeping or a timed pre-warm, so the
  signal-time action is the submit, not the 7-step chain.

**2. During stress: spend nothing irreplaceable.**
- **Never discard a login because the portal is slow or down (F40).** Hold it. Retry the warm-up
  on the same login with backoff paced to the portal's up-windows (`STRESS_RETRY_GAP`).
- **Never roll an exit the portal didn't kill (F41).** Before rolling, check the exit against the
  IP echo. Exit alive + portal failing = portal down: keep the exit and the login. Bound the hold,
  because a webshare tunnel failure can be exit-specific, then fall back to rolling.
- **Timeouts long enough to catch a slow accept (F42)**, still bounded so a dead tunnel cannot hang
  a worker.
- **No browser storms (F49):** kill the hung init; don't start new browsers to re-login into a
  down portal.

**3. At recovery: first request decides.**
- The held worker's first successful request answers Q27 for itself:
  - warmed -> apply immediately;
  - logged out -> re-login immediately (pre-solved token pool to cut solve latency).
- This is optimal whether logins survive outages or not. Today's ladder is optimal for neither.

**4. Targets.**
- Real discovery under stress (F48): a warmed applier already sits on Schedule.jsp and could read
  the calendar itself instead of waiting on a detector that cannot get in.
- Blind fallback follows the operator rule (F46): an invalid seeded slot means take the next slot,
  never a session verdict, never a park.

**5. Identity race (F47).** Measure and minimise the time from portal recovery to first submit per
identity. Save the "already has a request" page as evidence.

## E. Implementation order

1. **F40 + F41: hold the login through stress; don't roll a live exit.** DONE 2026-10-23
   (`STRESS_HOLD`, `STRESS_HOLD_MAX_S`, `STRESS_HOLD_GAP`, `PORTAL_DOWN_EXIT_ROLL_S`); primp quest steps
   now retry 503s like the browser chain. Live once workers relaunch on a restarted Hub.
2. F42: connect timeout ~40 s (setting, no code).
3. F43 + F44: KEEPING truthfulness.
4. F46: blind bounces don't park.
5. F49: init-timeout chrome kill.
6. F45 / F48: design work (pre-warm timing, self-discovery). Needs Q29 measured first.

## F. Afternoon addendum (12:30-14:55): the F40/F41 code ran live

Worker processes import the working tree at every spawn, so the uncommitted F40/F41 change went live
at 12:31 through respawns. That made the afternoon an unplanned first test.

- **The hold worked.** On the Primp fleet, 948 stress holds ended with the worker applying on its next
  assignment, and only 31 fell back to re-login. 1,298 warm-ups failed `[slow]` on a primp connect
  error between 12:30 and 13:20 (F42: 10 s connect timeout), and none of them threw the login away.
  When warm-ups started passing (13:2x: 180 ok), held workers submitted at once: 134 submits in 10 min.
- **Nothing to book.** All 228 submits (54 Browser at 12:3x, 174 Primp at 12:4x-13:3x) hit seeded
  slots and got the 66 KB form_stale. All 100 blind slots ended CLOSED after 3-7 rejections each.
  Browser assignments stopped ~12:40 and Primp ~13:35.
- **"Stuck in keeping" = idle on an empty pool.** From then on the 36 Browser + 56 Primp KEEPING
  workers had no slot to try and no detector feeding one. On top of that, 26/36 Browser keepers' last
  keepalive was `inconclusive` (F43: dead exits invisible). The Primp keepers' `ok` is reachability,
  not a verified login (F44). 6 Primp workers were parked by blind bounces (F46).
- **Competitors were booking.** Five of our identities were turned away with "already has a request":
  PA508829, PA514893, PA455411, PA560851, PA534371. Real slots existed; we never found one (F48).
- **Login remained the other wall:** 366 Browser and 123 Primp process exits on `login` after 12:30.

**Conclusion:** with F40/F41, stress no longer costs us logins, and warm-up recovers as soon as the
portal does. The binding constraint is now **targets (F48)**, then login supply and KEEPING
truthfulness (F43/F44).

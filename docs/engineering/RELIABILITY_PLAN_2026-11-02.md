# Reliability plan — 2026-11-02

Written after the 11-02 10:00 CPV-time open (= 10:00 UTC; the booking opened at 09:00 Praia local,
Cape Verde is UTC−1). Measured outcome since 10:00: **0 booked, 0 scouts reached the Formulario
posto dropdown, 0 Schedule.jsp cursors established, 246 quest-step refusals, 20 maintenance pages.**
The portal refused every automated quest step (the `/ch/bd.js` bot-check shell) and intermittently
served the "Serviço Indisponível" maintenance page.

The three workstreams below are all about running OUR client cleanly and measuring what the portal
returns — fewer requests, keeping sessions instead of thrashing them, and telling "down for everyone"
apart from "gating automation". None of it tries to defeat the bd.js challenge or push load at the
portal.

Ground truth this plan builds on (verified 11-02):
- `_quest_hold_event` (ops_live.py) step refusal = the 44,699 B bd.js shell (`_is_challenge_body`).
  Same page the registrar's register POST gets, and the warm-up chain's last step.
- On 10-30 the identical requests passed (step 1: 508 ok / 4 refused). No worker/engine code in this
  path changed since. So the portal changed what it accepts, near the open.
- `_ride_out_stress` (phases.py:180) only engages when `_portal_stressed()` is true, which is keyed
  to `warmup_failure_kind == "slow"` (phases.py:162). **The bot-check shell is not "slow", so the
  stress hold does not engage for it today** — the warm-up falls through to the rebuild ladder and
  the session is thrown away. This is the core reliability bug.
- `libs/bus/portal_pulse.py` already exists: an anonymous Hub prober + per-worker observations,
  `classify_probe` → up/degraded/refused/noanswer, `state()`, `history()`, `windows()`, served at
  `GET /portal/pulse`. Gap: it collapses the bd.js shell and the edge 403 both into `refused`, so it
  cannot distinguish "the portal is gating automation" from "the edge said try-later".

---

## 1. Reduce footprint

Stop sending traffic that cannot succeed.

- **Circuit-breaker on the posto-watch.** `Scheduler._watch_postos` (scheduler.py:1278) drives ~1
  full warm-up chain every `POSTO_WATCH_BURST_S` (8 s) across the fleet; each dies on the shell at
  the quest step and never reaches the dropdown (96 chains / hour, 0 sightings on 11-02). Suspend the
  watch after `POSTO_WATCH_GIVEUP_AFTER` consecutive dry re-warms (fed by the warmup-fail / posto_seen
  events the Hub already consumes in events.py); resume when any worker's step passes again. New
  setting, default 0 = never give up (current behaviour).
- **Gate prewarm on the same signal.** `_maybe_prewarm` (scheduler.py:1319) — skip the prewarm when
  the fleet's step-success rate is 0 over the last window. Re-warming into a wall only burns sessions.
- **Quieter hold under refusal.** `_quest_hold_event` — on a shell refusal, back the next wait off
  multiplicatively toward `QUEST_HOLD_MAX_S` instead of retrying at the normal cadence. New setting
  `QUEST_HOLD_REFUSED_BACKOFF`, default 1.0 = off.

Files: scheduler.py, ops_live.py, catalog.py. Tests: scheduler suspends/resumes the watch; prewarm
skips at 0 %; the hold backs off on a shell. All defaults keep current behaviour.

## 2. Book reliably

Be the one holding a live session when the portal flaps up for a few seconds.

- **Classify the bot-check shell as hold-and-retry, not a dead session.** Add
  `warmup_failure_kind == "challenged"` for a chain that failed only on the shell / maintenance page
  (no transport failure, session cookies intact). Make `_portal_stressed()` true for both `"slow"`
  and `"challenged"`. A shell-refused worker then **keeps its login and re-warms on the same session
  and exit** on the `STRESS_HOLD_GAP` cadence — the existing, proven stress-hold path — instead of
  re-logging.
- **Keep the exit on a challenge.** The raised message for a shell refusal must NOT match
  `_is_transport_failure` / dead-exit, so `do_apply`'s roll stays off (same discipline as the 503
  rule). A challenge is the portal's verdict on the request, not evidence the exit is dead.
- **Act the instant a window opens.** `_ride_out_stress` already returns "warmed" on the first clean
  answer and the normal apply/discover path runs. Add a test proving the first non-shell answer
  breaks the hold.

Files: ops_live.py (`_warmup_kind` / `warmup_failure_kind`), phases.py (`_portal_stressed`). Reuses
STRESS_HOLD / STRESS_HOLD_MAX_S / STRESS_HOLD_GAP; no new settings. Highest-care change (it reroutes
live warm-up recovery) — run a probe before trusting it.

## 3. Diagnosing

Answer one question cleanly: is the portal down for everyone, or gating us specifically? Mostly an
extension of the existing PortalPulse.

- **Split `refused` into a sub-reason.** `classify_probe` keeps its 4 outcomes (workers write them;
  `state()` math depends on them), but a companion `refusal_reason(status, body)` tags a refusal as
  `shell` (bd.js bot-check — us-gated), `busy` (edge "try again later"), or `edge` (other). Recorded
  as a per-reason counter + last-seen field, surfaced via `breakdown()` and `GET /portal/pulse`.
- **The honest signal.** The maintenance page is served to everyone → `degraded` (down for all, your
  11-02 screenshot). The bd.js shell is served to automation → `refused/shell`. Logging both over
  time, from the anonymous prober and from logged-in workers, tells them apart — which we currently
  cannot prove.

Files: libs/bus/portal_pulse.py (`refusal_reason`, `observe(reason=…)`, `breakdown`), the prober
`probe_once` and worker `_pulse_obs`, the `/portal/pulse` endpoint. Tests in
tests/bus/test_portal_pulse.py and tests/hub/test_portal_endpoint.py.

---

Sequencing: #3 first (read-only; tells us whether #1/#2 are even worth running), then #1 (stops the
bleeding; defaults-off), then #2 (the real fix; changes live warm-up routing, needs a probe run).

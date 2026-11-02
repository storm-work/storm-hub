# POSTO_AUTO -- a booking whose posto is not known in advance

Built 2026-11-02 for the CCV Praia "Permanencia" at Sao Nicolau (online booking from 2026-11-02 09:00
Cape Verde time = **10:00 UTC**, Cape Verde is UTC-1 all year). Off by default.

## How it works
1. The Formulario page's `f0sf1` dropdown lists the postos the portal accepts for the declared residence
   (`_posto_options`). Every warm-up chain passes through it.
2. With `POSTO_AUTO` on, a worker reports the dropdown to the Hub (`posto_seen` event) whenever it
   CHANGED. A worker never picks a posto.
3. From `POSTO_WATCH_LEAD_S` before `slot_open_at`, the Hub asks ONE holding scout per
   `POSTO_WATCH_INTERVAL_S` (fleet-wide, rotating) to re-warm (`posto_watch` signal) so the dropdown is read.
   Within `POSTO_WATCH_BURST_WINDOW_S` (600s) either side of `slot_open_at` the pace is `POSTO_WATCH_BURST_S` (8s),
   because the list changes with time and the posto most likely shows at the open.
   After slot-open the scouts' own warm-ups keep reading it.
4. `Scheduler._posto_auto` picks the new posto: an option not in `POSTO_KNOWN` and not the booking's current
   posto. A `POSTO_HINT` match wins; a single new option is taken; several new options with no hint match are
   NOT guessed (logged as a warning -- use set-posto).
5. `Scheduler.switch_posto` sets `posto_id` on this booking AND every other POSTO_AUTO booking on the same old
   posto, sends `set_posto` to their running workers, and fires slot-open (marked operator-grade so the
   false-open guard does not revert it).
6. A worker's `posto_id` is now a property: it reads the Hub's override, so every use follows it, and the
   posto-bound state (`posto_pdf`, the anchor) is cleared. Respawned workers read the updated booking row.

## Operating it (booking settings, dashboard, booking layer)
| setting | value today |
|---|---|
| `POSTO_AUTO` | true |
| `POSTO_KNOWN` | `5084,5086,5087,5088` (+ the French ids if the residence is French) |
| `POSTO_HINT` | `nicolau` |
| booking `posto_id` | `5084` -- must be a posto the portal offers for the residence, so the chain can run |
| `slot_open_at` | 10:00 UTC |
| `SELF_SCOUT_FALLBACK` | false |

Set `POSTO_AUTO` etc. BEFORE starting: workers freeze it at launch. Restart the Hub first (scheduler code).
Manual override, the same code path: `POST /bookings/{id}/set-posto {"posto_id": "5099"}`.

## What to look for
- worker log / event stream: `posto-watch (do_warmup): the dropdown offers N: ...`
- Hub log: `booking <id>: POSTO 5084 -> 5xxx (dropdown shows new posto ...); told N worker(s)`
- or the warning `POSTO_AUTO sees N new posto(s) ... but cannot choose`.

## Not covered / risks
- The hypothesis that the posto appears when slots open is untested. If it only appears AFTER the slots
  are gone, the watch gives no head start, only the right posto.
- If the new posto is not in the `f0sf1` dropdown, nothing is seen. Use set-posto when you see it.
- Slots already in a booking's pool belong to the old posto; clear them if any exist.
- A sighting is only reported when a worker's dropdown CHANGED; a Hub restart forgets which bookings it
  resolved (the new posto is then the current one, so it is not re-picked, but watching resumes).

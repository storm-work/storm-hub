"""Worker phase machine — the confirmed scout/apply control model (operator spec 2026-08-04).

Browser logic is IDENTICAL for scout and apply (one EngineOps implementation); the ONLY difference
is phase sequencing after the shared login:

  common:  login -> swap-to-ISP -> KEEPING
  apply :  KEEPING -> (slot-found) -> (await slot ASSIGNMENT) -> applying/book
           (warm-up is INSIDE do_apply now -- it re-warms unconditionally right before the submit,
            so the separate slot-found _warm_if_needed was removed 2026-09-26 to avoid a double warm)
           fallback: if all scouts are invalid/dead -> warmup -> self-discover -> book
  scout :  KEEPING -> (slot-open) -> warmup -> discovering -> push pool (on found)
           -> polling (loop) until booking finishes / all apply workers closed / manual stop

KEEPING (renamed from HOLDING, 2026-09-11) is keeping the AUTHENTICATED session alive -- nothing
more. It has never meant a warmed session, and since warmup became deferred it cannot: a KEEPING
worker is by definition NOT warmed yet. Keepalive during KEEPING is auth-only (it probes the home
page, never the booking form), so the state can be held for hours cheaply.

WARMUP IS DEFERRED TO THE SIGNAL THAT NEEDS IT -- scouts warm on slot-open, apply workers on
slot-found, self-scouting apply workers on the self-scout command. Warmup is what PRODUCES the
booking form-state anchor (Questionnaire -> Formulario -> ScheduleController -> Schedule.jsp, i.e.
`posto_pdf`); discovery and apply CONSUME it. That anchor expires (FORM_STATE_TTL), which is why
`is_warmed()` gates on both its provenance and its age -- see LiveEngineOps.is_warmed.

The orchestrator (hub+supervisor) owns: firing slot-open, firing slot-found (when a scout pushes),
assigning pooled slots to KEEPING apply workers, and tracking scout validity for the fallback.

Pure + injectable: the EngineOps calls block on real signals/assignments in production; stubs drive
them in tests, so the whole control flow is unit-tested with no browser.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable

from libs.domain.enums import WorkerRole, WorkerSubstate
from libs.domain.state_machines import worker_substate_transition

from . import pool_manager
from .config import EngineConfig

# False-slot-open revert (operator spec 2026-08-14): await_discover_task's third possible return,
# distinct from a real date list and from None (stop). The Hub decided the slot-open it fired was
# false (a long window with zero slots found) and is dropping the booking back to holding -- the
# scout returns to await_slot_open with its session alive, ready for a genuinely new window.
# Defined here, next to the EngineOps Protocol that returns it, so apps/worker imports from libs
# (the established direction) rather than libs reaching into apps.
INACTIVATE = object()

# How many poll gaps a worker sits out when it parks (see PhaseMachine._park). Deliberately a small
# constant rather than a setting: it is a back-off shape, not an operator knob, and every unit of it
# is a poll_gap() carrying the enforced minimum floor.
_PARK_POLL_GAPS = 3


def _flatten_found(results: list[dict]) -> list[dict]:
    """do_discover's per-date outcome list -> a flat list of {"date","period"} found slots
    (push_slots/the assignment shape stays scalar-per-period, matching apps/hub/events.py)."""
    return [
        {"date": r["date"], "period": p}
        for r in results
        if r.get("outcome") == "found"
        for p in r.get("periods", [])
    ]


@runtime_checkable
class EngineOps(Protocol):
    """Live operations the phase machine invokes (same impl for both roles)."""

    # ── shared warm-up ────────────────────────────────────────────────────────
    def do_login(self) -> bool: ...
    # Legacy stub, always False since the booking breaker was removed 2026-09-23. Kept so _warmup's
    # getattr(ops, "breaker_tripped") still resolves to a callable across the Protocol's test doubles.
    def breaker_tripped(self) -> bool: ...
    # True when the portal reported this applicant ALREADY HAS a request -- the identity is unusable
    # for this run, so the phase machine terminates (never respawns) instead of grinding it.
    def identity_unavailable(self) -> bool: ...
    def do_warmup(self) -> bool: ...
    # Whether the booking form-state anchor (posto_pdf) is currently established. Warmup is
    # deferred to the signal that needs it (slot-open for scouts, slot-found for apply) rather than
    # done unconditionally before KEEPING -- this lets each call site skip a redundant re-warm when
    # PREWARM_BEFORE_SLOT_OPEN (or a prior round) already did it. True only for an anchor this
    # session's own chain built AND that is still inside FORM_STATE_TTL -- see
    # LiveEngineOps.is_warmed for why neither half of that is optional.
    def is_warmed(self) -> bool: ...
    def do_swap(self) -> str | None: ...          # ISP exit ip if swapped, else None
    def await_slot_open(self) -> str: ...         # block until the slot-open signal

    # ── scout ─────────────────────────────────────────────────────────────────
    # Confirmed 4-phase model (operator spec 2026-08-05): SINGLE-mode probe (one scout at a time)
    # before the first slot is found, then SPLIT-mode polling (date range partitioned across scouts)
    # after. The orchestrator (Hub) owns which dates go to which scout each round; do_discover's
    # per-date outcome must stay 3-way (found/empty/failed) -- bug fix #6: a WAF-blocked/failed scan
    # must never be silently treated as 'empty' and pruned from the pool.
    # Returns a date list, None (stop), or the INACTIVATE sentinel (apps/worker/bus_signals.py) --
    # the Hub's false-slot-open revert: go BACK to await_slot_open and keep holding, don't terminate.
    def await_discover_task(self) -> list[str] | None: ...
    # do_discover: one entry per requested date, {"date": d, "outcome": "found"|"empty"|"failed",
    # "periods": [...]} ("periods" present only when outcome == "found").
    def do_discover(self, dates: list[str]) -> list[dict]: ...
    def push_slots(self, slots: list[dict]) -> None: ...   # push to pool (orchestrator fires slot-found)
    def report_dates(self, outcomes: list[dict]) -> None: ...  # [{"date":..., "outcome":...}, ...]
    def poll_gap(self) -> None: ...               # wait between discovery polls

    # ── apply ─────────────────────────────────────────────────────────────────
    def await_assignment(self) -> dict | None: ...  # block until a pooled slot is assigned (None = none)
    def scouts_invalid(self) -> bool: ...           # True when all scouts dead -> apply must self-scout
    # True once a slot_found signal has been observed for this booking. Lets a worker that respawns
    # mid-run skip the slot_open wait it has already missed -- see _run_apply.
    def slot_found_seen(self) -> bool: ...
    def do_apply(self, slot: dict) -> str: ...      # 'booked' | 'bounced'
    # Tell the orchestrator this slot was not booked, so it releases the lease and can hand the
    # worker a different one. Without it a bounced worker keeps its lease and stays unassignable
    # until the scheduler's SLOT_LEASE_TTL reclaim notices.
    def release_slot(self, slot: dict, reason: str = "bounce") -> None: ...

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def is_stopped(self) -> bool: ...  # True if the last wait ended because of an explicit stop --
    # distinguishes "operator stopped me" (-> outcome "stopped") from "genuinely no slot assigned"
    # (-> outcome "held") when await_assignment() returns None, which is ambiguous between the two.


@dataclass
class PhaseResult:
    outcome: str                       # booked | discovered | dry_run | warmed | failed | held | stopped
    final_substate: WorkerSubstate
    detail: str = ""
    visited: list[WorkerSubstate] = field(default_factory=list)


class PhaseMachine:
    def __init__(self, cfg: EngineConfig, ops: EngineOps, on_transition=None):
        self.cfg = cfg
        self.ops = ops
        self._on_transition = on_transition
        self._sub = WorkerSubstate.IDLE
        self.visited: list[WorkerSubstate] = [WorkerSubstate.IDLE]

    def _to(self, nxt: WorkerSubstate, detail: str = "") -> None:
        worker_substate_transition(self._sub, nxt)
        self._sub = nxt
        self.visited.append(nxt)
        if self._on_transition is not None:
            self._on_transition(nxt, detail)

    def _fail(self, step: str) -> PhaseResult:
        self._to(WorkerSubstate.FAILED)
        return PhaseResult("failed", self._sub, step, self.visited)

    def _warm_with_recovery(self, why: str) -> bool:
        """Recover a FAILED warm-up WITHOUT discarding the authenticated session.

        A warm-up failure says the server-side booking workflow anchor isn't where we need it. It
        does NOT say the auth session is dead -- login has already succeeded by this point, and that
        session is the expensive thing: a fresh one costs a new proxy lease, a new CAPTCHA solve, and
        a new login against a portal that rate-limits sub-accounts. Re-warming reuses all of it.

        Confirmed live 2026-08-20, worker 8fc9bdfe (identity ERNESTO PIRES, booking "Test 3018"):
        login succeeded, Formulario?copy=true returned 862KB, the ScheduleController POST came back
        200 -- and then `browser closed - primp session active` and the process exited, to be
        relaunched three minutes later and redo the whole login from scratch. The recovery ladder
        already existed for exactly this failure in the hold/apply loop (_rebuild_session,
        form_stale); the INITIAL warm-up was simply never given it, so the one moment the session is
        newest and most valuable was the one moment a single failure threw it away.

        Same ladder, same in-process/same-proxy principle: re-warm up to REWARM_MAX_ATTEMPTS, and
        only if re-warming is genuinely not rebuilding the anchor escalate to a fresh login. Leaves
        substate at WARMING_UP on success so the caller owns what happens next.
        """
        max_rewarms = max(1, int(self.cfg.raw.get("REWARM_MAX_ATTEMPTS", 1)))
        for attempt in range(max_rewarms):
            self._to(WorkerSubstate.WARMING_UP,
                     detail=f"re-warm {attempt + 1}/{max_rewarms}: {why}")
            if self.ops.do_warmup():
                return True
        # A SLOW portal is not a broken session, and a re-login cannot make the portal faster --
        # it just spends a CAPTCHA solve and a slice of the sub-account rate limit, which is already
        # the binding constraint, to arrive back at the same slow portal. Keep re-warming instead;
        # systemic slowness is now surfaced to the operator via PortalLiveness (the booking-wide
        # breaker that used to back the fleet off was removed 2026-09-23).
        kind = getattr(self.ops, "warmup_failure_kind", lambda: "unknown")()
        if kind == "slow":
            self._to(WorkerSubstate.KEEPING,
                     detail=f"portal slow, not broken -- holding rather than re-logging in: {why}")
            self.ops.poll_gap()
            return False

        # Re-warm alone has stopped rebuilding the anchor -- only a fresh login will (storm-auto
        # abc5). Still in-process: same worker, same proxy, same sub-account.
        self._to(WorkerSubstate.KEEPING, detail=f"re-warm exhausted; re-login in place: {why}")
        self._to(WorkerSubstate.LOGGING_IN)
        if not self.ops.do_login():
            return False
        self._to(WorkerSubstate.LOGGED_IN)
        self._to(WorkerSubstate.WARMING_UP)
        return self.ops.do_warmup()

    # ── shared warm-up: login -> swap -> hold ─────────────────────────────────
    # Warmup itself is NOT done here on the main path (2026-09-09) -- it is deferred to the moment
    # it's actually needed (slot-open for scouts, assignment for apply, see _run_scout/_run_apply),
    # since do_apply already unconditionally re-warms every round regardless and a pre-hold warmup
    # can simply go stale over a long hold for no benefit (confirmed live, "Test 3059": a scout
    # warmed once before holding, the warm state went stale, and the first post-slot-open discover
    # failed for 4.5 minutes before the reactive re-warm ladder in _run_scout recovered it -- see
    # that function's own comment). `warmup_only` is the one exception: it's a diagnostic/capture
    # mode that explicitly wants to stop once warmed and report that state.
    def _warmup(self) -> PhaseResult | None:
        self._to(WorkerSubstate.LOGGING_IN)
        # The booking breaker was REMOVED 2026-09-23, so a failed do_login() is always a real login
        # failure now -- there is no "the fleet is gated, wait" case any more. (do_login handles a
        # 'try again later' throttle per-worker with exit rolls, and a maintenance page by backing
        # THIS worker off; neither freezes the fleet.) breaker_tripped() survives as a stub that
        # always returns False, so this stays a plain fail-on-failure.
        if not self.ops.do_login():
            return self._fail("login")
        self._to(WorkerSubstate.LOGGED_IN)
        if self.cfg.warmup_only:
            self._to(WorkerSubstate.WARMING_UP)
            # NOT terminal on the first failure any more -- see _warm_with_recovery. Terminating
            # here threw away a session that had just authenticated successfully.
            if not self.ops.do_warmup() and not self._warm_with_recovery("initial warm-up failed"):
                return self._fail("warmup")
            self._to(WorkerSubstate.WARMED)
            return PhaseResult("warmed", self._sub, "warmup_only", self.visited)
        # HOLD_SWAP_AFTER_LOGIN (renamed 2026-09-09 from HOLD_SWAP_AFTER_LOGIN): swap now
        # genuinely happens right after login, since warmup no longer runs at this point in the
        # lifecycle on the main path.
        if self.ops.do_swap():
            self._to(WorkerSubstate.SWAPPED)
        self._to(WorkerSubstate.KEEPING)
        return None

    def _warm_if_needed(self, why: str) -> PhaseResult | None:
        """Warm up now, only if not already warmed (covers both: PREWARM_BEFORE_SLOT_OPEN or a
        prior round already did it -- skip, no redundant work; or nothing did -- do it now, right
        when it's actually needed). Returns a terminal PhaseResult on failure, None on success/
        already-warmed, matching _warmup()'s own None-means-continue convention."""
        # getattr, not a bare call -- EngineOps is a Protocol with many partial test doubles, so we
        # probe defensively. An ops that cannot answer "am I warmed" has
        # no anchor concept at all, so it keeps its pre-existing behaviour (this step is a no-op for
        # it) rather than crashing or being forced through a warmup it never modelled.
        _warmed = getattr(self.ops, "is_warmed", None)
        if not callable(_warmed) or _warmed():
            return None
        self._to(WorkerSubstate.WARMING_UP)
        if not self.ops.do_warmup() and not self._warm_with_recovery(why):
            # ALREADY-BOOKED is terminal: the warmup found the portal saying this applicant already
            # has a request, so the identity is unusable and re-warming/respawning only grinds it.
            # do_warmup already emitted `already_booked` (Hub retires the identity); STOP, never fail
            # (a failure respawns). Both scouts and appliers reach warmup through here.
            if getattr(self.ops, "identity_unavailable", lambda: False)():
                return PhaseResult("already_booked", self._sub,
                                   "portal: applicant already has a request", self.visited)
            return self._fail("warmup")
        self._to(WorkerSubstate.WARMED)
        return None

    def run(self) -> PhaseResult:
        warm = self._warmup()
        if warm is not None:
            return warm
        if self.cfg.role == WorkerRole.SCOUT:
            return self._run_scout()
        return self._run_apply()

    # ── scout: single-probe -> split-poll loop, driven by the orchestrator's task assignment ──
    def _run_scout(self) -> PhaseResult:
        # "stopped" is a distinct outcome from "held" -- an explicit operator stop must map to
        # WorkerStatus.STOPPED (events.py's _TERMINAL_STATUS already has this key; it just wasn't
        # ever emitted here), never to the FAILED the "held" bucket falls back to. Conflating the two
        # was a confirmed bug: the Hub sets the worker STOPPED, the worker then reports "held", the
        # event consumer overwrites STOPPED with FAILED, and hold-respawn relaunches a worker the
        # operator explicitly stopped.
        if self.ops.await_slot_open() == "stop":
            return PhaseResult("stopped", self._sub, "stopped before slot_open", self.visited)
        # Warm up NOW (2026-09-09) rather than relying on a pre-hold warmup that may have gone
        # stale over the hold -- see _warm_if_needed and _warmup's own comment. Skips cleanly if
        # PREWARM_BEFORE_SLOT_OPEN already did it this round.
        warm = self._warm_if_needed("slot-open warm-up failed")
        if warm is not None:
            return warm
        pushed_total = 0
        failed_rounds = 0
        # Operator-specified ladder (2026-08-20), and the FIRST round is deliberately cheaper:
        #
        #   step 1  slot-open -> discover x1   fail -> re-warm
        #   step 2  re-warm   -> discover x3   fail -> re-warm, repeat step 2
        #   step 3  on success -> slot-found, normal discover/poll loop
        #
        # The threshold used to be a flat 3, which is right for a session that has ALREADY proven it
        # can discover and then degrades, and wrong for the very first round after slot-open -- when
        # the likeliest explanation for failure is simply that this session was never in a warm
        # enough state to call gettime at all.
        #
        # Traced live on booking "Test 3059", scout 9e505283 (PEDRO TEIXEIRA RAMOS): slot-open fired
        # at 11:05:16, the first gettime returned special_days={} (empty -- an unwarmed session), and
        # three full rounds then reported EVERY date "failed" at ~68s each. Only at 11:10:31 did the
        # flat-3 threshold permit a re-warm; that re-warm took 40s, gettime came back populated, and
        # discovery found 5 slots immediately. slot-found landed at 11:12:35 -- 7m18s after
        # slot-open, of which roughly 4.5 minutes was spent re-running a probe that could not work.
        # One failed round is all the evidence needed to justify a 40s re-warm.
        steady_rebuild_after = max(1, int(self.cfg.raw.get("SCOUT_REBUILD_AFTER_FAILED_ROUNDS", 3)))
        rebuild_after = 1
        while True:
            dates = self.ops.await_discover_task()
            if dates is None:               # orchestrator says stop (booking done / applies closed)
                break
            if dates is INACTIVATE:
                # False slot-open (operator spec 2026-08-14): the Hub decided this open was false --
                # drop back to plain holding and wait for a genuinely new slot-open rather than
                # grinding discovery (which risks rate-limiting for nothing). Not a stop: the
                # session stays alive and this scout is fully reusable in the next window.
                self._to(WorkerSubstate.KEEPING, "slot-open inactivated")
                if self.ops.await_slot_open() == "stop":
                    return PhaseResult("stopped", self._sub, "stopped while re-holding after "
                                       "slot-open inactivation", self.visited)
                continue
            # PROVE the session healthy BEFORE discovering (finding #2, 2026-10-01). The keepalive
            # between rounds only logs an inconclusive/dead probe and re-probes -- it does not stop
            # this discover. So a degraded scout went into do_discover and gettime died with
            # "Execution context was destroyed", wasting the whole ~68s round before the reactive
            # all-failed ladder below noticed. session_probe_ok() is a cheap positive check (authed +
            # on a live portal origin); if it fails, rebuild the session FIRST, then discover on it.
            # getattr-guarded so a test double without the method keeps its old behaviour.
            _probe = getattr(self.ops, "session_probe_ok", None)
            if callable(_probe) and not _probe():
                _relogin = getattr(self.ops, "discovery_needs_relogin", None)
                _skip = bool(callable(_relogin) and _relogin())
                if not self._rebuild_session("session not proven healthy before discover",
                                             skip_rewarm=_skip):
                    return self._fail("scout session unrecoverable")
            self._to(WorkerSubstate.DISCOVERING)
            results = self.ops.do_discover(dates)
            found = _flatten_found(results)
            if found:
                self.ops.push_slots(found)     # orchestrator fires slot-found on the first push
                pushed_total += len(found)
            # ALWAYS report round completion -- even an empty round. Under the ROTATION model
            # (2026-09-21) the Hub's dates_reported drain is the ONLY signal that clears a scout's
            # in-flight slot and advances the round-robin; a round that reported nothing left the
            # Hub stuck on that scout until the 180s hang-timeout, which is exactly the "advance to
            # the next one immediately on failure" the model is supposed to give. An empty list is
            # classified as a failed round (advance now, no pacing wait) by on_round_reported. This
            # was previously suppressed as stream noise (73/96 empty on the 2026-09-02 run), but the
            # rotation runs one scout at a time paced by DISCOVERY_MIN_INTERVAL, so it is at most one
            # report per round now, and the report is FUNCTIONAL rather than noise.
            self.ops.report_dates([{"date": r["date"], "outcome": r["outcome"]} for r in results])

            # A round where EVERY date came back "failed" is a session-level problem, not news about
            # the calendar. Confirmed live 2026-08-14: a scout whose browser page had died span this
            # loop 631 times over 53 minutes at 0.6s a round (a real gettime takes ~15s), reporting
            # failures nobody could act on, while the dashboard showed it healthily "polling". Same
            # bug class as the apply path: a failure that should rebuild the session instead loops.
            if results and all(r.get("outcome") == "failed" for r in results):
                failed_rounds += 1
                if failed_rounds >= rebuild_after:
                    failed_rounds = 0
                    # Step 1 spends ONE round before re-warming; every cycle after that gets the
                    # steady budget, so a session that is genuinely working is not re-warmed over a
                    # single unlucky round.
                    rebuild_after = steady_rebuild_after
                    self._to(WorkerSubstate.POLLING)
                    _relogin = getattr(self.ops, "discovery_needs_relogin", None)
                    _skip = bool(callable(_relogin) and _relogin())
                    if not self._rebuild_session("discovery failing every round",
                                                 skip_rewarm=_skip):
                        return self._fail("scout session unrecoverable")
                    continue
            elif results:
                failed_rounds = 0
            # A round that reported NOTHING (results == []) neither counts toward the rebuild nor
            # clears it. ops_live._no_report returns that when our own tooling failed before the
            # portal was asked -- a CapSolver outage says nothing about the session either way.
            # Resetting here would let a genuinely dead session hide behind an intermittent solver:
            # alternate failed/empty rounds and the counter never reaches the threshold.

            self._to(WorkerSubstate.POLLING)
            self.ops.poll_gap()
        # dates is None both on an explicit stop and (in unit-test/no-bus-wired mode) on genuinely
        # running out of configured discover tasks -- is_stopped() disambiguates rather than
        # guessing from the None alone.
        if self.ops.is_stopped():
            return PhaseResult("stopped", self._sub, f"stopped while discovering, pushed {pushed_total}",
                               self.visited)
        return PhaseResult("discovered", self._sub, f"pushed {pushed_total}", self.visited)

    # ── apply: hold -> await assignment -> book; fallback self-scout ──────────
    def _rebuild_session(self, why: str, *, skip_rewarm: bool = False) -> bool:
        """Recover a degraded session IN PROCESS, cheapest step first.

        RE-WARM, then re-login only if that fails. This went straight to do_login(), which is the
        wrong order and the expensive one: a re-warm is ~40s and costs nothing scarce, while a
        re-login is minutes, burns a CAPTCHA solve, and puts the sub-account back through the wall
        that rejects ~90% of attempts. Confirmed live 2026-08-19 -- scouts whose gettime failed
        three rounds went straight back to logging_in, and so did every holding apply worker.

        Nearly always the auth session is still fine and only the booking form-state has lapsed,
        which is exactly what a re-warm restores. Same ladder the apply path already uses for
        form_stale (REWARM_MAX_ATTEMPTS) and the one storm-auto uses. Returns False only when even a
        fresh login can't recover -- the one genuinely terminal case."""
        # skip_rewarm: the caller has POSITIVE evidence the cheap rung cannot help.
        #
        # The ladder's premise -- "nearly always the auth session is fine and only the form-state
        # lapsed" -- is exactly inverted for a /slots session that has been burned. MEASURED on the
        # 2026-09-02 17:15 run: scout 28b8fe7f re-warmed successfully FOUR times and still answered
        # {"data":{}} six times out of six. Because a successful re-warm returns True here, the
        # ladder never reached the re-login rung at all, and the worker sat in that loop for two
        # hours looking healthy. The escalation only ever fired when the re-warm happened to fail.
        if skip_rewarm:
            self._to(WorkerSubstate.KEEPING, detail=f"re-warm cannot fix this; re-login: {why}")
            self._to(WorkerSubstate.LOGGING_IN)
            if not self.ops.do_login():
                return False
            self._to(WorkerSubstate.LOGGED_IN)
            self._to(WorkerSubstate.WARMING_UP)
            if not self.ops.do_warmup():
                return False
            self._to(WorkerSubstate.WARMED)
            self._to(WorkerSubstate.KEEPING)
            self.ops.poll_gap()
            return True
        self._to(WorkerSubstate.KEEPING, detail=f"re-warming: {why}")
        self._to(WorkerSubstate.WARMING_UP)
        if self.ops.do_warmup():
            self._to(WorkerSubstate.WARMED)
            self._to(WorkerSubstate.KEEPING)
            self.ops.poll_gap()
            return True
        # Re-warm couldn't rebuild the workflow anchor -- only a fresh login will (abc5).
        self._to(WorkerSubstate.KEEPING, detail=f"re-warm failed; re-login: {why}")
        self._to(WorkerSubstate.LOGGING_IN)
        if not self.ops.do_login():
            return False
        self._to(WorkerSubstate.LOGGED_IN)
        self._to(WorkerSubstate.WARMING_UP)
        if not self.ops.do_warmup():
            return False
        self._to(WorkerSubstate.WARMED)
        self._to(WorkerSubstate.KEEPING)
        self.ops.poll_gap()
        return True

    def _log_release(self, slot, why: str) -> None:
        emit = getattr(self.ops, "_log", None)
        if callable(emit):
            emit(f"apply: releasing slot {slot} -- {why}")

    def _park(self, why: str) -> None:
        """Session-preserving back-off. Keeps the authed session and the proxy; just stops this
        worker consuming (and bouncing) pooled slots for a few poll gaps. Paced entirely by
        poll_gap(), which carries the enforced minimum floor -- never a bare sleep."""
        self._to(WorkerSubstate.KEEPING, detail=f"parked: {why}")
        for _ in range(_PARK_POLL_GAPS):
            if self.ops.is_stopped():
                return
            self.ops.poll_gap()

    def _run_apply(self) -> PhaseResult:
        # apply holds through slot-open, awaiting assignment. See _run_scout's comment above --
        # "stopped" (-> WorkerStatus.STOPPED) must never be reported as "held" (-> FAILED, which
        # hold-respawn then relaunches, undoing the operator's stop).
        #
        # ...unless the booking is ALREADY past slot-found. A worker that respawns mid-run (a
        # bounced apply, a hold-respawn) has re-warmed into a booking where slots are actively being
        # discovered and assigned; making it sit through another slot_open handshake wastes a warm
        # session on a signal that has already happened. Confirmed live 2026-08-14: appliers cycled
        # failed -> rewarm -> WARMED -> HOLDING and stalled there while the booking was in the
        # slot-found phase with slots waiting. The scheduler does re-latch slot_open to relaunched
        # workers, so this was a delay rather than a permanent hang -- but it is a phase the worker
        # provably no longer needs to be in.
        if not self.ops.slot_found_seen():
            if self.ops.await_slot_open() == "stop":
                return PhaseResult("stopped", self._sub, "stopped before slot_open", self.visited)
        else:
            # slot-found already observed for this booking (a mid-run respawn), so this worker
            # deliberately skips the slot_open handshake it has already missed -- record it in the
            # signal trace rather than leaving a silent gap. getattr-guarded: SimEngineOps and other
            # partial doubles have no _slog.
            _slog = getattr(self.ops, "_slog", None)
            if callable(_slog):
                _slog("signal_ignored", signal="slot_open", reason="slot_found_already_seen")
        # A bounce must NEVER end the run, and must never cost the warm session. Operator, 2026-08-14:
        # "each process should keep running just retying, rewarm - against the failures. logged in
        # auth session is very precious - why close the process for a failure?"
        #
        # This is storm-auto's confirmed apply_book contract, which distinguishes two bounces that
        # this file used to flatten into one terminal "held":
        #   form_stale -- back on the questionnaire/home/auth page. The server-side workflow anchor
        #                 lapsed; THE SLOT IS STILL REAL. Re-POSTing is futile -- RE-WARM (and, past
        #                 REWARM_MAX_ATTEMPTS, re-login) IN-PROCESS, then retry the SAME slot.
        #   no_slot    -- back on the schedule/slots page. Someone took it, or it was never real.
        #                 The session is fine: hand the slot back and ask for another.
        # Neither outcome ends the process. The login wall and the CAPTCHA solve are the scarce
        # resources; portal traffic is not. The only terminal states here are booked, an operator
        # stop, and a session that cannot be rebuilt even by a fresh login.
        #
        # PACING: every non-productive path through this loop MUST hit poll_gap(). An unthrottled
        # retry loop is precisely the shape that produced the 66-minute/740k-row runaway that cost
        # this project four days of work -- see the HOLD_POLL_GAP: 0.0 finding.
        max_rewarms = max(1, int(self.cfg.raw.get("REWARM_MAX_ATTEMPTS", 1)))
        # storm-auto's MAX_CONSECUTIVE_BOUNCES + SESSION_PRESERVING_FANOUT, and the reason
        # APPLY_MAX_ATTEMPTS still earns its place now that it no longer bounds the process's life:
        # many DISTINCT slots bouncing in a row means THIS session is degraded, not that the slots
        # are taken. soak10 (storm-auto): one broken session bounced ~28 good slots off the shared
        # pool. So past the limit the worker PARKS -- keeps its session, backs off, re-polls -- which
        # is strictly gentler than a re-login and stops it churning the pool.
        #
        # This also protects the Hub's false-slot revert below from drawing the wrong conclusion: a
        # single degraded session must not be able to manufacture the bounce volume that convinces
        # the Hub its genuinely-real slots are false.
        max_bounces = max(1, int(self.cfg.raw.get("APPLY_MAX_ATTEMPTS", 5)))
        rewarm_after_bounces = max(1, int(self.cfg.raw.get("APPLY_REWARM_AFTER_BOUNCES", 2)))
        mono = bool(self.cfg.raw.get("MONO_BOOKING", False))
        # Consecutive `session` outcomes. The bounce cap above deliberately does NOT cover these --
        # treating a session failure as a bounce is what let one dead proxy manufacture 5082
        # "bounced" leases and park every applier (2026-08-15). But uncapping it entirely left the
        # other failure mode wide open: if _rebuild_session keeps REPORTING success while the
        # session stays broken, this loop never exits.
        #
        # That is not hypothetical. Traced live 2026-08-20 on "Test 3018", worker CARLOS FURTADO:
        # 121 leases in 55 minutes, 120 of them `session`, ~26s per cycle, still going when found.
        # The specific cause (do_warmup short-circuiting forever on a consumed checkpoint-restore
        # flag) is fixed in ops_live, but "a rebuild that always claims success" is a class, and an
        # unbounded retry loop is the exact shape that cost this project four days once already.
        # Generous on purpose -- 3x the bounce budget -- so a genuinely transient dead proxy still
        # gets many chances; failing here is terminal and hold-respawn relaunches with backoff.
        max_session_failures = max_bounces * 3
        # Same budget as the bounce storm: both are "this keeps failing for a reason that is not
        # the slot's fault", and both are answered by parking rather than by burning a re-login.
        max_unreachable = max_bounces
        attempts = 0
        stale_streak = 0
        bounce_streak = 0
        session_streak = 0
        unreachable_streak = 0
        slot = None                      # a form_stale retry KEEPS its slot across iterations
        while True:
            if self.ops.is_stopped():
                return PhaseResult("stopped", self._sub, "stopped while holding", self.visited)
            # NO slot-found pre-warm here (removed 2026-09-26). It was meant to have the worker READY
            # when a slot lands, but do_apply ALREADY re-warms UNCONDITIONALLY right before the submit
            # (keeping-is-authed-only: the Schedule-page cursor is NAVIGATION-bound, and the keepalive
            # navs between here and the submit destroy it -- so a pre-warm's cursor is dead by submit
            # time and provides no readiness). Keeping both meant a genuine DOUBLE warm-up whenever
            # is_warmed() was false at slot-found (measured ~47s each = ~94s), which only slows the
            # apply and loses more slot races. do_apply's own re-warm is the authoritative one and
            # also detects already-booked (returned as the "already_booked" outcome, handled below),
            # so nothing is lost. The self-scout path below KEEPS its _warm_if_needed -- do_discover
            # has no unconditional re-warm of its own.
            if slot is None:
                slot = self.ops.await_assignment()
            if slot is None and self.ops.is_stopped():
                return PhaseResult("stopped", self._sub, "stopped while awaiting assignment",
                                   self.visited)
            if slot is None and (mono or self.ops.scouts_invalid()):
                # MONO_BOOKING: the worker discovers its OWN slots and books them, instead of idling
                # until a scout hands it one. This is storm-auto's confirmed architecture -- there is
                # no separate scout fleet there, every warmed worker scans a slice of the date window
                # each round and books what it finds.
                #
                # It is also the fix for the hold problem, which is why it is worth having as more
                # than a fallback. The split model leaves an applier doing NOTHING but a 600s
                # keepalive nav between slot-open and slot-found, and a bare nav VERIFIES the
                # server-side booking anchor without RENEWING it -- so the anchor lapses, the worker
                # reports a healthy-looking `holding`, and it cannot book when a slot finally
                # arrives. Measured 2026-08-21: 147 "form-state lapsed" against 42 re-warms that
                # restored it. Note which workers do NOT suffer this: scouts, whose continuous
                # getPeriodosOcupados traffic is exactly what keeps their anchor alive. storm-auto
                # never needed a keepalive at all ("keepalive" appears once in its whole apply loop,
                # in a docstring) because no worker is ever idle.
                #
                # Same code path as the scouts_invalid fallback that was already here; MONO_BOOKING
                # just stops it being conditional on the scouts having died first.
                # Warm up before self-discovering (2026-09-09): do_discover's slots-mode path
                # prefers posto_pdf and only degrades to bare posto_id when it's unset -- unlike
                # do_apply, it has no unconditional re-warm of its own, so a self-scouting apply
                # worker needs the same on-demand warmup a real scout gets before its first
                # discover.
                warm = self._warm_if_needed("self-scout warm-up failed")
                if warm is not None:
                    return warm
                self._to(WorkerSubstate.DISCOVERING)
                d = self.cfg.discovery
                dates = pool_manager.default_date_window(
                    start_date=d.start_date, end_date=d.end_date, num_days=d.num_days,
                    today=date.today(),
                )
                found = _flatten_found(self.ops.do_discover(dates))
                if found:
                    # SHARE the find. The fallback kept it private, so the Hub never learned the
                    # slot existed, slot_found never fired, and no other worker could use it --
                    # every applier re-scanned the same window to find the same thing. storm-auto
                    # merges finds into the shared pool and fires slot-found on the FIRST free date
                    # rather than at the end of the scan.
                    self.ops.push_slots(found)
                slot = found[0] if found else None
            if slot is None:
                # Nothing to apply right now. That is what KEEPING is for -- it is not a failure and
                # it is emphatically not a reason to discard a warm authed session. Wait and re-ask.
                self._to(WorkerSubstate.KEEPING)
                self.ops.poll_gap()
                continue

            self._to(WorkerSubstate.APPLYING)
            if self.cfg.no_book:
                self._to(WorkerSubstate.KEEPING)
                return PhaseResult("dry_run", self._sub, "no_book: not submitted", self.visited)
            attempts += 1
            outcome = self.ops.do_apply(slot)
            # Terminal event of the assignment signal round (shares the `sig` minted when the
            # assignment was received): the booked/form_stale/no_slot/session/... verdict, so one
            # round traces received -> accepted -> warmup -> apply_submit -> result end to end.
            _slog = getattr(self.ops, "_slog", None)
            if callable(_slog):
                _slog("signal_result", signal="assignment", outcome=outcome)
            # `portal_unreachable` is EXCLUDED from this reset on purpose. The comment this replaces
            # read "any other answer means the session reached the portal" -- which is exactly
            # inverted for that one outcome: it means the request never reached the portal at all,
            # so it is not evidence the session is healthy and must not clear the session cap.
            if outcome not in ("session", "portal_unreachable"):
                session_streak = 0      # the portal actually answered -- the session is healthy
            if outcome != "portal_unreachable":
                unreachable_streak = 0
            if outcome == "booked":
                self._to(WorkerSubstate.BOOKED)
                return PhaseResult("booked", self._sub, str(slot), self.visited)

            if outcome == "already_booked":
                # The portal reports this applicant ALREADY HAS a request (a competitor booked it, or
                # it applied within 5 days). The identity is permanently unusable for this run, so
                # STOP -- terminal, never respawn (respawning re-runs the same doomed sub-account).
                # do_warmup already emitted `already_booked` so the Hub retires the identity. The slot
                # is untouched and blameless: hand it back WITHOUT a penalty.
                if slot is not None:
                    self.ops.release_slot(slot, "session")
                return PhaseResult("already_booked", self._sub,
                                   "portal: applicant already has a request", self.visited)

            if outcome == "portal_unreachable":
                # The request never left the machine (Chrome network-error page / blank / DataDome).
                # The SESSION is fine -- do_apply re-warms at the top of every attempt, so simply
                # taking the next assignment is the whole recovery. Rebuilding here threw away a
                # healthy login and ~10 minutes of warmup for a transient dropped POST.
                #
                # BUT IT MUST BE BOUNDED. This branch had no counter and no cap, and it also reset
                # the session cap above, so nothing anywhere could stop it: measured live
                # 2026-08-27 on booking "Test2", worker 9fd63b7f took 64 of these in a SINGLE
                # process -- leasing a slot, failing, releasing it and immediately re-leasing --
                # producing 70 slot leases on its own and 102 across the booking in 45 minutes,
                # with zero bookings. That is the churn shape this file's own PACING note warns
                # about, reached through the one outcome that had no brake.
                #
                # _park is the right brake rather than a rebuild or a failure: the session really
                # is fine, so this keeps it and simply stops consuming pooled slots for a few poll
                # gaps -- the same session-preserving back-off the bounce storm already uses.
                self.ops.release_slot(slot, "session")
                slot = None
                unreachable_streak += 1
                if unreachable_streak >= max_unreachable:
                    self._park(f"portal unreachable x{unreachable_streak}")
                    unreachable_streak = 0
                else:
                    self._to(WorkerSubstate.KEEPING, detail="portal unreachable; retrying")
                self.ops.poll_gap()
                continue

            if outcome == "session":
                # do_apply never got as far as talking to the portal about this slot -- its own
                # re-warm failed (dead proxy, dropped tunnel). The slot is untouched and blameless:
                # hand it back WITHOUT a penalty and rebuild the session, exactly as for form_stale.
                # Treating this as a bounce is what let a dead proxy manufacture 5082 "bounced"
                # leases against a pool that was never phantom, park every applier on "5 consecutive
                # bounces", and drive the Hub's false-slot revert on real slots (live 2026-08-15).
                self.ops.release_slot(slot, "session")
                slot = None
                session_streak += 1
                self._to(WorkerSubstate.KEEPING, detail="re-warm failed; rebuilding session")
                if session_streak >= max_session_failures:
                    # The rebuild keeps claiming success and the session keeps failing. Stop
                    # churning slot leases over it -- respawn gets a clean process.
                    return self._fail(
                        f"session unrecoverable ({session_streak} consecutive re-warm failures)")
                if not self._rebuild_session("apply re-warm failed"):
                    return self._fail("session unrecoverable")
                self.ops.poll_gap()
                continue

            if outcome == "form_stale" and (slot or {}).get("blind"):
                # BLIND SLOT: a form_stale verdict means "this combination is not bookable", never
                # "our form lapsed, re-warm and retry it". The operator seeded this (date, period)
                # on sight, not on evidence; most of a blind grid is phantom by construction, and
                # apply_path_proven_healthy records that the portal rejects a WELL-FORMED submit
                # for a slot that is gone -- which lands here, as form_stale with the cursor
                # intact. Routing that through the stale ladder is the failure the operator named:
                # stale_streak reaches max_rewarms and the worker re-logs in, spending a good
                # session (and a reCAPTCHA strike) on a slot that never existed. So it takes the
                # no_slot path below: release with the bounce penalty, get the next combination.
                # The next round re-warms anyway (rewarm_every_round_is_mandatory), so a genuinely
                # lapsed form costs nothing extra by being treated this way.
                outcome = "no_slot"

            if outcome == "form_stale":
                # storm-auto releases the lease here too -- with a DIFFERENT penalty, not by
                # hoarding it. My previous version kept the slot to avoid "poisoning the pool";
                # apply_book shows the real mechanism is the penalty tier: release(lease,
                # "form_stale") doesn't count toward retiring the slot, release(lease, "bounce")
                # does. Holding the lease just starved this worker of new assignments while it
                # re-logged in, and hid the failure from the Hub entirely.
                bounce_streak = 0
                stale_streak += 1
                self.ops.release_slot(slot, "form_stale")
                slot = None
                if stale_streak >= max_rewarms:
                    # Re-warm alone has stopped rebuilding the anchor (storm-auto abc5); only a
                    # fresh login will. In-process: same worker, same proxy.
                    stale_streak = 0
                    self._to(WorkerSubstate.KEEPING,
                             detail="form_stale: re-login in place (re-warm not rebuilding anchor)")
                    self._to(WorkerSubstate.LOGGING_IN)
                    if not self.ops.do_login():
                        return self._fail("form_stale re-login")
                    self._to(WorkerSubstate.LOGGED_IN)
                    self._to(WorkerSubstate.WARMING_UP)
                    if not self.ops.do_warmup():
                        return self._fail("form_stale re-warm after re-login")
                    self._to(WorkerSubstate.WARMED)
                self._to(WorkerSubstate.KEEPING, detail=f"form_stale x{stale_streak}: re-warming")
                self.ops.poll_gap()
                continue          # do_apply re-warms on the way into the next attempt

            # no_slot / bounced: hand the lease back so another worker can try it and so the
            # orchestrator will assign this one something new. The Hub counts these releases -- past
            # FALSE_SLOT_BOUNCE_LIMIT it stops believing the pool and reverts the whole booking.
            self.ops.release_slot(slot, "bounce")
            slot = None
            stale_streak = 0
            bounce_streak += 1
            self._to(WorkerSubstate.KEEPING)
            if bounce_streak >= max_bounces:
                bounce_streak = 0
                self._park(f"{max_bounces} consecutive bounces")
            elif bounce_streak % rewarm_after_bounces == 0:
                # Two bounces in a row is much more likely a stale server-side form anchor than two
                # genuinely taken slots -- and re-warming is ~38s, against a re-login's CAPTCHA solve
                # and a slice of the sub-account budget the portal is already penalising us on.
                # Previously this just retried until it hit max_bounces and parked, so a worker whose
                # anchor had lapsed spent its whole budget bouncing good slots off the pool.
                self._to(WorkerSubstate.WARMING_UP,
                         detail=f"{bounce_streak} consecutive bounces: re-warming before the next try")
                if self.ops.do_warmup():
                    self._to(WorkerSubstate.WARMED)
                self._to(WorkerSubstate.KEEPING)
            self.ops.poll_gap()

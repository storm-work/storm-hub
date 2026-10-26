"""Event consumer + WebSocket broadcaster.

A background thread tails the shared `evt` Redis stream. For each worker event it (1) updates the
worker's status/substate in the DB and (2) fans the event out to every connected dashboard WebSocket.
The status mapping is a pure function so it's unit-tested without Redis or a DB.
"""
from __future__ import annotations

import asyncio
import threading
import logging
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

from libs.bus import Bus, connect
from libs.domain.enums import WorkerStatus, WorkerSubstate

from .config import settings
from .db import SessionLocal
from libs.db import models as M

logger = logging.getLogger(__name__)

# PhaseResult.outcome -> terminal worker status. MUST cover every outcome libs/engine/phases.py can
# emit: an unmapped one silently becomes FAILED via the .get() default below, which is exactly how
# `held` (emitted twice by _run_apply -- "no slot assigned" and "apply bounced") spent this whole
# reconstruction being reported as a failure. Keep this table and PhaseResult's outcomes in lockstep;
# tests/hub/test_events.py asserts they match.
_TERMINAL_STATUS = {
    "booked": WorkerStatus.COMPLETED,
    "discovered": WorkerStatus.COMPLETED,
    "dry_run": WorkerStatus.COMPLETED,
    "warmed": WorkerStatus.COMPLETED,
    # The portal reported this applicant already has a request -- the identity is retired (via the
    # already_booked event) and the worker is DONE, not respawned (COMPLETED, never HELD/FAILED).
    "already_booked": WorkerStatus.COMPLETED,
    "held": WorkerStatus.HELD,
    "stopped": WorkerStatus.STOPPED,
    "failed": WorkerStatus.FAILED,
}


def map_event_to_update(event_type: str, payload: dict) -> dict:
    """Return the Worker fields to update for an event (empty dict = no DB change). Pure."""
    if event_type == "started":
        return {"status": WorkerStatus.RUNNING, "substate": WorkerSubstate.IDLE}
    if event_type == "status_change":
        sub = payload.get("substate")
        upd: dict = {}
        if sub:
            upd["substate"] = sub
            # The detail describes THIS substate, so a substate change always writes it -- None when
            # empty. It used to be written only when non-empty, so a worker that recovered to KEEPING
            # (`_mark_keep_bookable(True)` sends detail="") kept its old "not bookable: ..." text, and
            # _assign_slots excludes KEEPING workers carrying that prefix: a healthy worker invisible to
            # slot assignment. 29 such episodes on 1023 / 3059 w slots (2026-10-23..26).
            upd["substate_detail"] = payload.get("detail") or None
        elif payload.get("detail"):
            upd["substate_detail"] = payload["detail"]
        return upd
    if event_type == "terminal":
        outcome = payload.get("outcome", "")
        upd = {"status": _TERMINAL_STATUS.get(outcome, WorkerStatus.FAILED)}
        if payload.get("substate"):
            upd["substate"] = payload["substate"]
        if payload.get("detail"):
            upd["substate_detail"] = payload["detail"]
        return upd
    if event_type == "account_rotated":
        # Sub-account rotation (do_login()'s sticky-then-rotate loop, ops_live.py): keep the DB's
        # Worker.sub_account_id in sync with which candidate the worker actually logged in with, so
        # the dashboard/booked-PDF persistence (_persist_booked_pdf) reflect reality, not the
        # sub-account picked at launch time.
        sub_account_id = payload.get("sub_account_id")
        return {"sub_account_id": sub_account_id} if sub_account_id else {}
    return {}


class EventHub:
    def __init__(self, redis_url: str | None = None):
        self._redis_url = redis_url or settings.redis_url
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Stall watchdog (D3) needs "when did we last hear from this worker"; the runaway-loop
        # circuit breaker (F16) needs a rolling per-worker event timestamp window to compute rate.
        # Both are process-local caches the scheduler polls -- not a system of record (Phase F9: a
        # Hub restart just starts these empty again, which is fine, it's advisory/reactive state).
        self.last_seen: dict[str, float] = {}
        self._event_times: dict[str, list[float]] = {}
        # P4: a proxy_request's health-probe-then-lease can take several seconds (possibly longer if
        # the first candidate(s) are dead and it has to try another). _consume() is the ONLY reader
        # of the shared `evt` stream for every worker in every booking -- handling a proxy_request
        # inline would stall every other worker's event processing for that whole time. Dispatched
        # onto this small bounded pool instead so _consume() keeps moving.
        self._proxy_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="proxy-lease")
        # D6: a scout's dates_reported event (per-date found/empty/failed outcomes) needs to reach
        # Scheduler._scout_pools' ScoutPoolState so empty dates get pruned from that scout's
        # persistent slice -- but ScoutPoolState lives only in the Scheduler (a different thread).
        # Buffered here and drained by Scheduler._drive_scouts every tick, matching last_seen/
        # _event_times' existing "EventHub owns process-local advisory state, Scheduler polls it"
        # pattern rather than giving EventHub a direct reference into Scheduler's internals.
        self.dates_reported: dict[str, list[list[dict]]] = {}
        # Shared gettime cache (storm-auto's proven app/core/slot_manager.py._gettime_cache pattern,
        # ported): booking_id -> (special_days JSON dict, cached_at). One scout's fresh gettime fetch
        # gets handed to the rest of the fleet by _drive_scouts so, within GETTIME_CACHE_SECS, only
        # ONE real gettime fetch happens fleet-wide per booking instead of one per scout per round.
        self.gettime_cache: dict[str, tuple[dict, float]] = {}

    def pop_dates_reported(self, worker_id: str) -> list[list[dict]]:
        """Return and clear this worker's buffered dates_reported outcome-lists (each element is
        one report's full outcomes list, oldest first)."""
        return self.dates_reported.pop(worker_id, [])

    def get_gettime_cache(self, booking_id: str, max_age: float) -> dict | None:
        """Returns the cached special_days JSON dict if still within max_age (GETTIME_CACHE_SECS),
        else None (nothing cached yet, or stale -- caller must fetch fresh)."""
        entry = self.gettime_cache.get(booking_id)
        if entry is None:
            return None
        special_days, cached_at = entry
        if time.time() - cached_at >= max_age:
            return None
        return special_days

    def _cache_gettime(self, worker_id: str, special_days: dict) -> None:
        db = SessionLocal()
        try:
            w = db.get(M.Worker, worker_id)
            if not w:
                return
            self.gettime_cache[w.booking_id] = (special_days, time.time())
        finally:
            db.close()

    def event_rate(self, worker_id: str, window: float = 10.0) -> float:
        """Events/sec for `worker_id` over the trailing `window` seconds. The confirmed-incident
        circuit breaker (F16): a worker sustaining an anomalous rate gets auto-killed by the
        scheduler well before it can run unthrottled for an hour."""
        now = time.time()
        times = self._event_times.get(worker_id, [])
        times = [t for t in times if now - t <= window]
        self._event_times[worker_id] = times
        return len(times) / window if times else 0.0

    # ── WS subscriptions ──────────────────────────────────────────────────────
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    #: Pushed to every subscriber on shutdown so their `await q.get()` returns instead of blocking.
    CLOSE = {"__close__": True}

    def close_subscribers(self) -> None:
        """Wake every open WS reader so it can exit.

        ws_events is `while True: await q.get()`, which never returns on its own. Uvicorn will not
        finish shutting down while a socket is still being served, so an open dashboard tab was
        enough to hang the whole Hub on "Waiting for background tasks to complete" until systemd's
        stop timeout expired and SIGKILLed it -- leaving the unit `failed` and, because the
        scheduler never got to tick again, a worker row stranded in RUNNING with no process behind
        it. Confirmed live 2026-08-21 03:42.

        This is the F16 hazard the reconstruction plan names directly: never let an event-loop
        pattern include a forever-blocking coroutine without an explicit exit path.
        """
        for q in list(self._subs):
            try:
                q.put_nowait(self.CLOSE)
            except asyncio.QueueFull:
                pass
        self._subs.clear()

    def _broadcast(self, item: dict) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                pass

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def start(self) -> None:
        self._loop = asyncio.get_event_loop()
        self._thread = threading.Thread(target=self._consume, name="event-consumer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._proxy_executor.shutdown(wait=False)

    def _apply_db(self, worker_id: str, upd: dict) -> None:
        if not upd:
            return
        db = SessionLocal()
        try:
            w = db.get(M.Worker, worker_id)
            if w:
                for k, v in upd.items():
                    setattr(w, k, v)
                db.commit()
        finally:
            db.close()

    def _release_on_terminal(self, worker_id: str, bus) -> None:
        """Regression (P1): a worker that ends normally (booked/discovered/dry_run/warmed/failed/
        stopped -- i.e. every path through here that ISN'T one of the scheduler's dead/stall/runaway
        reap paths) never released its proxy/slot lease before this existed. Only a worker the
        scheduler had to forcibly reap got cleaned up; the common case -- a worker finishing its own
        run -- leaked its lease permanently. With Proxy.max_sessions defaulting to 2, a handful of
        ordinary completed registrations/bookings was enough to exhaust the pool for everyone else."""
        from libs.proxies.lease import release_worker_resources
        db = SessionLocal()
        try:
            release_worker_resources(db, worker_id, bus=bus)
        finally:
            db.close()

    def _handle_proxy_request(self, worker_id: str, phase: str, bus, health_check=None) -> None:
        """P3 -- proxy rotation, spec'd directly by the operator: a worker asks the Hub ("the proxy
        provider") for a rotation and gets back a proxy drawn from the pool matching its CURRENT
        phase -- PROXY_PROVIDER/PROXY_KIND (the same settings _do_launch_worker's phase-1 lease
        uses) during "warmup", HOLD_PROXY_PROVIDER/HOLD_PROXY_KIND (its phase-2 lease settings)
        during "hold" -- with identical provider/kind/geo selection whether or not
        HOLD_SWAP_AFTER_LOGIN is actually on (LiveEngineOps.do_swap() flips the worker's tracked
        phase to "hold" unconditionally, per the operator's explicit requirement: "it will not be
        different when swap is off"). The worker process is deliberately DB-less (apps/worker/
        __main__.py's own constraint), so it can't lease a row itself -- this round-trips through the
        same event/command bus slot assignment already uses. Called off the event-consumer's own
        thread (see __init__'s _proxy_executor) since the live health probe below can take several
        seconds.

        Phase 5: settings resolution now includes the WORKER layer (matching _do_launch_worker), and
        a "hold" request additionally requires `resource_type='static'` on the lease whenever the
        resolved HOLD_SWAP_AFTER_LOGIN is on -- mirroring _do_launch_worker's own launch-time
        requirement exactly, so the guarantee "the post-swap pool is static" survives every
        subsequent rotation, not just the worker's first hold-proxy assignment. No rotating fallback:
        if no static candidate exists, the request fails cleanly via `proxy_unavailable` (below) --
        the SAME failure shape as any other pool exhaustion, just with the constraint named in the
        reason string.

        P4 -- live pre-lease screening (operator-specified, 2026-08-13): before handing back a
        candidate, check it against the real portal (libs/proxies/health.check_proxy by default,
        overridable via `health_check` for tests); if unhealthy, mark it and try the next candidate
        (lease_proxy's own job -- see its docstring for why the probe never holds a DB row lock).
        Essential for rotating pools specifically: the actual IP a provider hands out on this
        request can differ entirely from whatever was checked historically, so only a check done
        AT lease time, not a periodic sweep, actually proves this specific proxy works right now.

        Excludes the worker's existing same-pool lease(s) from the new lease's candidate set (a
        rotation must not just hand back the same still-has-capacity proxy it's rotating off of),
        and leases the new proxy FIRST, releasing the old one(s) only after the new one is confirmed
        -- never leaves the worker fully proxy-less if the pool is momentarily exhausted (falls back
        to re-including the excluded proxy if that's genuinely the only one left, rather than failing
        a rotation the worker doesn't strictly need). A pre-existing lease from the OTHER pool (e.g.
        a pre-leased hold proxy while still in warmup) is left untouched throughout."""
        from sqlalchemy import select

        from libs.db import models as M
        from libs.domain.enums import SettingsLayer as L
        from libs.domain.settings import resolve
        from libs.proxies.health import check_proxy, quick_screen
        from libs.proxies.lease import ProxyLeaseError, lease_proxy

        from .settings_store import get_global

        # `hc` is resolved per-request BELOW, once the pool `kind` is known -- see the _static_pool
        # gate at the lease call. Injected health_checks (tests) still win outright.
        # Tier 1 only when tier 2 is the real browser probe: a caller injecting a fake health_check
        # is describing a fake environment and must not get a real network call underneath it.
        _quick_screen = None      # DISABLED -- see lease_proxy's TIER 1 comment (it poisons the exit)

        db = SessionLocal()
        try:
            w = db.get(M.Worker, worker_id)
            if w is None:
                return
            bk = db.get(M.Booking, w.booking_id) if w.booking_id else None
            layers = {L.GLOBAL: get_global(db), L.BOOKING: (bk.settings_json or {}) if bk else {}}
            if w.template_id:
                tpl = db.get(M.WorkerTemplate, w.template_id)
                if tpl:
                    layers[L.TEMPLATE] = (tpl.settings_json or {})
            # Phase 5: the WORKER layer's one input source (same as _do_launch_worker) -- without
            # this, a worker's proxy-setting override applied at launch silently stopped applying the
            # instant a mid-hold rotation ran, resolving BOOKING/TEMPLATE/GLOBAL only.
            if w.settings_json:
                layers[L.WORKER] = w.settings_json
            eff = resolve(layers)

            # Phase 5 (D33 follow-up): a hold-phase request whose resolved configuration requires the
            # swap must also require resource_type='static' on the lease -- otherwise a mid-hold
            # rotation can silently hand back a rotating proxy for a pool that was static-only at
            # launch, defeating the guarantee _do_launch_worker enforces at the very first lease.
            # Matches _do_launch_worker's own gate (main.py) exactly.
            hold_required = bool(eff.get("HOLD_SWAP_AFTER_LOGIN", False))
            if phase == "hold":
                provider = str(eff.get("HOLD_PROXY_PROVIDER", "soax"))
                kind = eff.get("HOLD_PROXY_KIND") or None
                geo_setting = eff.get("HOLD_PROXY_GEOS")
                pool_setting = eff.get("HOLD_PROXY_POOLS")
                resource_type = "static" if hold_required else None
            else:
                provider = str(eff.get("PROXY_PROVIDER", "soax"))
                kind = eff.get("PROXY_KIND") or None
                geo_setting = eff.get("PROXY_GEOS")
                pool_setting = eff.get("PROXY_POOLS")
                resource_type = None
            # A rotation must honour the same geo restriction as the original lease -- otherwise the
            # first proxy failure quietly drops the worker back into the full pool, and the geos
            # that clear the login wall stop being the ones it uses.
            from apps.hub.main import _geos, _pools, _static_pool
            geos = _geos(geo_setting)
            # Same reasoning as geos directly above: a rotation must stay inside the NAMED ISP pools
            # the booking was assigned. Without this a worker that loses its static exit would be
            # re-leased out of some other booking's purchase -- and an ISP proxy is a finite,
            # individually-owned IP, so that is a real conflict, not just untidy accounting.
            pool_names = _pools(pool_setting)

            # STATIC ONLY (2026-09-15). A ROTATING pool has nothing to assign: the session id is a
            # string the worker mints itself (libs/proxies/rotating.py), so a worker asking the Hub
            # for one is asking for something that no longer exists on this side. Answer honestly
            # rather than silently minting here -- a Hub-side mint would put the round-trip straight
            # back, and the round-trip is what this change removes.
            #
            # WHY IT HAD TO GO, measured on booking 0911: worker 59df999b took 46s / 97s / 124s
            # between emitting proxy_request and getting a usable exit. Workers emitted ~12.7
            # requests/min against a 4-thread executor whose handler ran a ~25s browser probe per
            # candidate -- ~2.6x oversubscribed, so every rotation got slower, and each slow
            # rotation made a worker roll MORE exits and emit more requests. A feedback loop, on top
            # of an already-degraded portal, right through slot-open.
            # Two independent ways a request is legitimately STATIC, and both must pass the gate:
            #   - `kind` names a static pool (PROXY_KIND / HOLD_PROXY_KIND = static|isp), or
            #   - `resource_type == "static"`, which the hold phase sets from HOLD_SWAP_AFTER_LOGIN
            #     REGARDLESS of kind -- a row can legitimately be kind='rotating' with
            #     resource_type='static' (that is exactly what the static hold pool looks like, and
            #     what test_handle_proxy_request_hold_succeeds_and_returns_static_when_available
            #     seeds). Gating on `kind` alone would have refused every real static hold
            #     assignment -- caught by that test.
            if resource_type != "static" and not _static_pool(kind):
                bus.send_command(
                    worker_id, "proxy_unavailable",
                    reason=(f"{provider}/{kind or 'rotating'} is a rotating pool -- workers mint "
                            "their own session ids now (roll_sticky_session); the Hub assigns "
                            "STATIC/ISP proxies only"))
                return
            hc = health_check if health_check is not None else check_proxy

            old_q = select(M.ProxyLease).join(M.Proxy, M.ProxyLease.proxy_id == M.Proxy.id).where(
                M.ProxyLease.worker_id == worker_id, M.ProxyLease.released_at.is_(None),
                M.Proxy.provider == provider,
            )
            if kind:
                old_q = old_q.where(M.Proxy.kind == kind)
            old_leases = db.scalars(old_q).all()
            old_proxy_ids = {l.proxy_id for l in old_leases}

            try:
                proxy, url = lease_proxy(db, worker_id, provider=provider, kind=kind,
                                         exclude_ids=old_proxy_ids, health_check=hc, quick_screen=_quick_screen,
                                         geos=geos, pool_names=pool_names,
                                         resource_type=resource_type)
            except ProxyLeaseError:
                try:
                    proxy, url = lease_proxy(db, worker_id, provider=provider, kind=kind,
                                             health_check=hc, quick_screen=_quick_screen, geos=geos,
                                             pool_names=pool_names, resource_type=resource_type)
                except ProxyLeaseError as e:
                    detail = str(e)
                    if resource_type:
                        detail = f"{detail} (resource_type={resource_type!r} required, no rotating fallback)"
                    bus.send_command(worker_id, "proxy_unavailable", reason=detail)
                    return

            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            released_any = False
            for old_lease in old_leases:
                if old_lease.proxy_id == proxy.id:
                    continue   # the exhausted-pool fallback re-leased the same proxy -- keep it live
                old_lease.released_at = now
                released_any = True
            if released_any:
                db.commit()

            # Phase 5: carries the fact, not the row -- lets the worker refuse to locally roll a
            # static hold identity (D1: no DB access) without giving it any DB-shaped state.
            bus.send_command(worker_id, "proxy_assigned", proxy=url, resource_type=proxy.resource_type)
        finally:
            db.close()

    def _persist_slots(self, worker_id: str, slots: list) -> None:
        """A scout pushed slots -> upsert into the booking's DB pool (scheduler then assigns them).

        A REDISCOVERY of an existing row must reopen it, not be discarded. SlotStatus.CLOSED means
        "absent from N consecutive scans" and the transition table has said CLOSED -> AVAILABLE
        ("reappearance reopens") from the start -- but this method simply `continue`d on any existing
        row, so nothing ever performed that transition.

        Harmless while slots were only ever created, and fatal once /start began retiring the
        previous run's slots to CLOSED (2026-08-14). Confirmed live on booking Test2: a scout pushed
        24 slots at 09:21 and again at 09:23, every one of them matching a CLOSED row from the
        earlier run -- all 48 pushes were dropped, slot_found never fired, and the apply workers sat
        holding with nothing to do while the pool showed "24 slots, all closed".

        A TAKEN row is ALSO reopened (2026-08-20). A slot is a date+period carrying multiple
        capacity, not one seat, so a booking landing on it does not mean it is full -- and a scout
        seeing it again afterwards is the portal itself saying capacity remains. That re-sighting is
        the only evidence of spare capacity we can ever get, and it was being thrown away.

        Confirmed on booking "Test 3059": slot f94b0b47 was leased CONCURRENTLY by four workers and
        one booked it. The scout then re-found 09-15 and 09-18 at 11:13:02, both already TAKEN, and
        both reports were dropped here -- while the last apply worker (EDER LUIS VIEIRA NEVES), out
        of leasable slots, reverted to holding with that capacity sitting invisible in front of it.

        LEASED is still left alone: a lease in flight is managed by the lease lifecycle, and a
        re-sighting says nothing new about it -- only its freshness, which is updated below."""
        from sqlalchemy import select
        from libs.domain.enums import SlotStatus
        db = SessionLocal()
        try:
            w = db.get(M.Worker, worker_id)
            if not w:
                return
            bk = db.get(M.Booking, w.booking_id)
            now = datetime.now(timezone.utc)
            for s in slots or []:
                date, period = s.get("date"), s.get("period")
                if date is None or period is None:
                    continue
                exists = db.scalar(select(M.Slot).where(
                    M.Slot.booking_id == w.booking_id, M.Slot.date == str(date),
                    M.Slot.period == int(period)))
                if exists is not None:
                    if exists.status in (SlotStatus.CLOSED, SlotStatus.TAKEN):
                        reopened_from = exists.status
                        exists.status = SlotStatus.AVAILABLE
                        exists.discovered_by = worker_id
                        exists.miss_count = 0
                        if reopened_from == SlotStatus.TAKEN:
                            logger.info(
                                "slot %s %s/%s re-discovered after a booking -- reopening as "
                                "AVAILABLE (spare capacity confirmed by the portal)",
                                exists.id, date, period)
                    if exists.status == SlotStatus.AVAILABLE:
                        exists.last_seen = now      # keep a live slot's freshness current
                    continue
                db.add(M.Slot(booking_id=w.booking_id, posto_id=bk.posto_id if bk else "",
                              date=str(date), period=int(period), status=SlotStatus.AVAILABLE,
                              discovered_by=worker_id, first_seen=now, last_seen=now))
            db.commit()
        finally:
            db.close()

    def _release_slot_lease(self, worker_id: str, slot: dict, reason: str = "bounce") -> None:
        """An apply worker handed a slot back: release its lease and reopen the slot.

        `reason` carries storm-auto's two-tier penalty. "form_stale" means OUR form-state expired
        and the slot is still real, so it is recorded as FORM_STALE rather than BOUNCED and never
        counts against the slot -- the false-slot revert must be driven by evidence the POOL is
        phantom, not by our own sessions lapsing.

        Without this a worker that keeps its warm session after a bounce (phases.py's apply loop)
        would also keep the lease, so _assign_slots' one-active-lease rule would never hand it
        another slot -- it would sit warm and idle until SLOT_LEASE_TTL reclaimed it minutes later."""
        from sqlalchemy import func, select

        from libs.domain.enums import SlotLeaseOutcome, SlotStatus
        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            leases = db.scalars(select(M.SlotLease).where(
                M.SlotLease.worker_id == worker_id, M.SlotLease.released_at.is_(None))).all()
            if not leases:
                return
            slot_ids = {le.slot_id for le in leases}
            for le in leases:
                le.released_at = now
                le.outcome = {"form_stale": SlotLeaseOutcome.FORM_STALE,
                              "session": SlotLeaseOutcome.SESSION}.get(
                                  reason, SlotLeaseOutcome.BOUNCED)
            db.flush()
            for s in db.scalars(select(M.Slot).where(M.Slot.id.in_(slot_ids))).all():
                if s.status == SlotStatus.LEASED:
                    s.status = SlotStatus.AVAILABLE   # freed a unit -- bookable again
                # BLIND SLOT RETIREMENT. A discovered slot retires through the scouts' miss-count
                # (dates_reported); a blind slot was never discovered, so nothing would ever retire
                # it -- it would be reassigned and bounced off forever. Each BOUNCED release counts
                # against it here, and past BLIND_MAX_BOUNCES it is CLOSED. form_stale/session
                # releases do not count: those are about our session, not the combination.
                if getattr(s, "source", "scout") == "blind" and reason not in ("form_stale", "session"):
                    s.miss_count = (s.miss_count or 0) + 1
                    # A BOOKING PROVES THE COMBINATION IS REAL and that its real capacity is at least
                    # `already_booked`. Measured on Test 0919 (2026-09-19): three workers racing a
                    # genuinely-open unit bounced and drove miss_count to 3, which CLOSED a real slot
                    # mid-run -- so a booked slot must get HEADROOM for racing multi-capacity.
                    #
                    # But "never retire a booked slot" (the old `if already_booked: continue`) is the
                    # OPPOSITE failure, CONFIRMED live 2026-10-02 (Test 1002): worker 8fac5d27 bounced
                    # ONE booked slot (2026-11-02 p3) EIGHTEEN times in 25 minutes while other workers
                    # starved -- the default cap of 5 left 4 phantom units behind a single real
                    # booking, and nothing could ever close them. A bounce AFTER the real inventory is
                    # taken is not racing; it is an endless loop. So a booked slot DOES retire, as
                    # TAKEN (the appointment(s) exist) rather than CLOSED (phantom), once its bounces
                    # exceed BLIND_MAX_BOUNCES + already_booked -- i.e. the phantom budget plus one
                    # racing-bounce of headroom per real booking.
                    already_booked = db.scalar(select(func.count()).select_from(M.BookedAppointment)
                                               .where(M.BookedAppointment.slot_id == s.id)) or 0
                    limit = self._blind_max_bounces(db, s.booking_id)
                    if limit > 0:
                        threshold = limit + already_booked
                        if s.miss_count >= threshold and s.status not in (SlotStatus.CLOSED,
                                                                          SlotStatus.TAKEN):
                            s.status = SlotStatus.TAKEN if already_booked else SlotStatus.CLOSED
                            logger.info("blind slot %s %s/p%s %s after %d bounce(s) (booked=%d)",
                                        s.id, s.date, s.period,
                                        "TAKEN" if already_booked else "CLOSED",
                                        s.miss_count, already_booked)
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _blind_max_bounces(db, booking_id: str) -> int:
        from apps.hub.main import _resolve_booking_setting
        bk = db.get(M.Booking, booking_id)
        if bk is None:
            return 3
        return int(_resolve_booking_setting(db, bk, "BLIND_MAX_BOUNCES", 3) or 0)

    def _persist_booking(self, worker_id: str, slot: dict | None,
                         pdf_path: str | None = None,
                         appointment_ref: str | None = None) -> None:
        """Record a confirmed booking. The PDF is an ARTIFACT, not the proof.

        This used to be reachable only via `booked_pdf`, so a real appointment whose PDF retrieval
        failed -- or returned a non-PDF, which do_apply deliberately refuses to save -- produced NO
        BookedAppointment at all. do_apply's own log said "booking IS confirmed, PDF missing" and
        then the Hub recorded nothing: the one artefact this whole system exists to produce, lost to
        a failed follow-up fetch. Found by the full-flow sim, where a sim booking (which has no PDF
        by construction) left the appointments table empty.

        Idempotent: `booked` and `booked_pdf` both land for a healthy live booking, and the second
        must attach the artifact to the first's row rather than insert a duplicate."""
        return self._persist_booked_pdf(worker_id, pdf_path, slot, appointment_ref)

    def _mark_identity_booked(self, db, w, sub_account_id: str, slot_id: str | None) -> None:
        """Record the result where it SURVIVES: on the identity.

        The worker row is not a durable record of a booking. Detaching an identity deletes its
        worker outright (and nulls the artifact's worker_id), so re-attaching gives a fresh READY
        worker with no trace that this person already has an appointment -- exactly what was
        reported for RICARDO RONALDO CORREIA MATOS: a booked PDF on disk, an appointment row, and a
        worker showing "ready" whose log had never seen a booking.

        Two writes, both of which the schema was built for and neither of which had a single caller:
          - IdentityStatus.BOOKED. The enum documents it as "a sub-account booked -> auto-retired to
            booked list" and state_machines allows ACTIVE -> BOOKED, but no code performed it, so an
            identity holding a confirmed appointment stayed ACTIVE and freely re-runnable -- risking
            a SECOND real appointment for the same person.
          - An IdentityEvent row. identity_events is described as "C6: durable per-identity run
            history" and has never had a writer anywhere in the codebase; booking-delete even goes
            out of its way to preserve a history that was always empty.

        Best-effort: a bookkeeping failure must never propagate into the booking record itself,
        which is why this runs after the appointment row is added and swallows its own errors."""
        from libs.domain.enums import IdentityEvent, IdentityStatus
        from libs.domain.state_machines import IllegalTransition, identity_transition
        try:
            db.add(M.IdentityEvent(
                identity_id=w.identity_id, sub_account_id=sub_account_id,
                booking_id=w.booking_id, event=IdentityEvent.BOOKED,
                step="booked", detail=f"slot={slot_id}", ts=datetime.now(timezone.utc),
            ))
            ident = db.get(M.Identity, w.identity_id)
            if ident is not None:
                try:
                    ident.status = identity_transition(ident.status, IdentityStatus.BOOKED)
                except IllegalTransition:
                    pass    # already booked/retired/archived -- leave the stronger state alone
            db.commit()
        except Exception:
            logger.exception("identity bookkeeping failed for worker %s (booking still recorded)",
                             w.id)

    def _mark_already_booked(self, worker_id: str, detail: str) -> None:
        """The portal told a worker its applicant already holds a future appointment.

        Retires the identity so it stops being attached to new bookings. Deliberately does NOT
        create a BookedAppointment: we have no idea when that appointment is, and a fabricated
        booking record is far worse than a missing one (see the success-detection rule -- only %PDF
        bytes prove a booking WE made). The IdentityEvent carries the portal's own wording, so the
        reason survives in history rather than living only in a worker log that rolls away.
        """
        from libs.domain.enums import IdentityEvent, IdentityStatus
        from libs.domain.state_machines import IllegalTransition, identity_transition
        db = SessionLocal()      # module-level, matching every other handler in this file
        try:
            w = db.get(M.Worker, worker_id)
            if w is None:
                return
            ident = db.get(M.Identity, w.identity_id)
            if ident is None:
                return
            db.add(M.IdentityEvent(
                identity_id=ident.id, sub_account_id=w.sub_account_id, booking_id=w.booking_id,
                event=IdentityEvent.BOOKED, step="already_booked_at_portal",
                detail=detail[:500], ts=datetime.now(timezone.utc),
            ))
            # BY OTHERS, not BOOKED (operator 2026-09-26). The portal says an appointment EXISTS,
            # but we did not make it and hold no BookedAppointment for it -- so it is a competitor's
            # booking, not ours. Marking it BOOKED (as before) both inflated our own booked count and
            # mislabelled the worker/identity as a success we achieved. RETIRE it instead (it is
            # unusable -- the portal blocks a second appointment) and record WHY in the comment, so
            # the dashboard can show "By Others" and COMPLETED stays reserved for bookings WE made.
            try:
                ident.status = identity_transition(ident.status, IdentityStatus.RETIRED)
                logger.warning("identity %s (%s) retired as BY OTHERS: the portal reports an "
                               "existing appointment we did not make", ident.id, ident.full_name)
            except IllegalTransition:
                pass    # already booked/retired/archived -- leave the stronger state alone
            ident.comment = ("By Others — portal reports this applicant already holds an "
                             "appointment; not booked by us (no BookedAppointment).")[:512]
            db.commit()
        except Exception:
            logger.exception("mark_already_booked failed for worker %s", worker_id)
        finally:
            db.close()

    def _persist_booked_pdf(self, worker_id: str, pdf_path: str | None, slot: dict | None,
                            appointment_ref: str | None = None) -> None:
        """A live apply confirmed a booking and saved its PDF to disk (Phase: dashboard "open pdf"
        feature) -- create the Artifact + BookedAppointment rows the schema already had (models.py's
        Artifact/BookedAppointment) but that nothing wrote to before this."""
        from datetime import datetime, timezone

        from sqlalchemy import func, select

        from libs.domain.enums import SlotLeaseOutcome, SlotStatus
        from libs.engine import pool_manager

        db = SessionLocal()
        try:
            w = db.get(M.Worker, worker_id)
            if not w:
                return
            artifact = None
            if pdf_path:
                artifact = M.Artifact(worker_id=worker_id, kind="pdf", path=pdf_path)
                db.add(artifact)
                db.flush()
            slot_id = None
            if slot and slot.get("date") is not None and slot.get("period") is not None:
                slot_row = db.scalar(select(M.Slot).where(
                    M.Slot.booking_id == w.booking_id, M.Slot.date == str(slot["date"]),
                    M.Slot.period == int(slot["period"])))
                if slot_row:
                    slot_id = slot_row.id
                    # Close this worker's lease on the booked slot as BOOKED. SlotLeaseOutcome.BOOKED
                    # existed in the enum from the start and was never once written: the terminal
                    # path's release_worker_resources sets released_at but no outcome, so every real
                    # booking left its lease reading `None` -- indistinguishable from an in-flight
                    # one in any report or query.
                    for le in db.scalars(select(M.SlotLease).where(
                            M.SlotLease.slot_id == slot_row.id,
                            M.SlotLease.worker_id == worker_id,
                            M.SlotLease.released_at.is_(None))).all():
                        le.released_at = datetime.now(timezone.utc)
                        le.outcome = SlotLeaseOutcome.BOOKED
                    # Multi-capacity leasing (operator spec 2026-08-14): one physical (date, period)
                    # can absorb SLOT_CAPACITY_PER_PERIOD concurrent bookings before it's exhausted.
                    # This used to set TAKEN unconditionally -- SlotStatus has no BOOKED member, and
                    # the 2026-08-14 fix above (this comment's previous version) only fixed the
                    # AttributeError crash, not the missing capacity check. So the FIRST booking on
                    # a slot resolved to capacity 5 discarded the other 4 leasable units, permanently:
                    # TAKEN is excluded from every reconsideration path in this codebase BY DESIGN
                    # (_assign_slots' own candidate query is `status == AVAILABLE`;
                    # _reclaim_stale_slot_leases explicitly refuses to touch it -- "genuinely
                    # booked -- never reopen"). The only way back was a scout independently
                    # rediscovering that exact (date, period) later, which is opportunistic, not
                    # guaranteed. CONFIRMED LIVE 2026-09-0X: 9 previously-taken slots sat invisible
                    # for a whole booking run because nothing ever re-touched them.
                    #
                    # db.flush(), not autoflush (SessionLocal is built with autoflush=False): the
                    # count below must see the released_at/outcome writes just made above, in the
                    # SAME transaction, or it undercounts by exactly the booking that just landed.
                    db.flush()
                    bk_for_cap = db.get(M.Booking, w.booking_id)
                    cap_setting = str((bk_for_cap.settings_json or {}).get(
                        "SLOT_CAPACITY_PER_PERIOD", "auto")) if bk_for_cap else "auto"
                    cap = pool_manager.resolve_slot_capacity(cap_setting, slot_row.capacity_estimate)
                    booked_count = db.scalar(select(func.count()).select_from(M.SlotLease).where(
                        M.SlotLease.slot_id == slot_row.id,
                        M.SlotLease.outcome == SlotLeaseOutcome.BOOKED)) or 0
                    # Below cap: leave the slot exactly as it was (AVAILABLE, or LEASED if other
                    # in-flight leases already filled the rest) -- _assign_slots' own query already
                    # excludes it correctly once genuinely full, no extra write needed here for that
                    # case. Only the exhausted case needs an explicit transition.
                    if booked_count >= cap:
                        slot_row.status = SlotStatus.TAKEN
            sub_account_id = (w.effective_settings_json or {}).get("_sub_account_id")
            if not sub_account_id:
                return  # sim-engine or pre-upgrade worker with no recorded sub-account; nothing to link
            booked_now = None
            existing = db.scalar(select(M.BookedAppointment).where(
                M.BookedAppointment.booking_id == w.booking_id,
                M.BookedAppointment.identity_id == w.identity_id))
            if existing is not None:
                # Second event for the same booking (the `booked_pdf` following a `booked`):
                # attach what it adds, never insert a duplicate appointment.
                if artifact is not None and existing.pdf_artifact_id is None:
                    existing.pdf_artifact_id = artifact.id
                    # The PDF finally arrived -- clear the provisional "PDF missing" flag set below.
                    _idn = db.get(M.Identity, w.identity_id)
                    if _idn is not None and _idn.comment and "PDF missing" in _idn.comment:
                        _idn.comment = None
                if existing.slot_id is None:
                    existing.slot_id = slot_id
                # Backfill the portal-confirmed appointment date+window if the first event (booked)
                # didn't carry it but this one (booked_pdf) does -- never overwrite a value already set.
                if appointment_ref and not existing.appointment_ref:
                    existing.appointment_ref = appointment_ref
            else:
                db.add(M.BookedAppointment(
                    booking_id=w.booking_id, identity_id=w.identity_id,
                    sub_account_id=sub_account_id, slot_id=slot_id,
                    pdf_artifact_id=artifact.id if artifact is not None else None,
                    appointment_ref=appointment_ref,
                    booked_at=datetime.now(timezone.utc),
                ))
                booked_now = (w, sub_account_id, slot_id)
                if artifact is None:
                    # PDF MISSING (operator 2026-09-26). The submit confirmed the booking (do_apply's
                    # "booking IS confirmed, PDF missing" -- e.g. route_never_captured on the MostrarPdf
                    # fetch), but we captured no %PDF, so the appointment has no proof. Flag the
                    # identity so the operator VERIFIES it on the portal; cleared above if a later
                    # booked_pdf attaches the artifact.
                    _idn = db.get(M.Identity, w.identity_id)
                    if _idn is not None:
                        _idn.comment = ("PDF missing — submit confirmed the booking but no PDF "
                                        "was captured; VERIFY this appointment on the portal.")[:512]
            db.commit()
            # AFTER the commit, in its own transaction. The appointment is the thing this system
            # exists to produce; identity bookkeeping must not be able to take it down, and
            # ordering makes that structural rather than a matter of one try/except holding.
            if booked_now is not None:
                self._mark_identity_booked(db, *booked_now)
        finally:
            db.close()

    def _persist_registration_result(self, sub_account_id: str, outcome: str, payload: dict) -> None:
        """A registration attempt settled (registered/exists/verified/failed) -- update that
        sub-account's latest RegistrationJob row and, for a durable state change, SubAccount.status
        via subaccount_transition(). 'failed' never touches SubAccount.status: SubAccountStatus has
        no FAILED rung by design (see state_machines.py's note) -- only RegistrationJob.status
        regresses, so a failed account stays wherever it last settled and is naturally re-selectable
        for another registration run."""
        from sqlalchemy import select

        from libs.domain.enums import RegistrationStatus, SubAccountStatus
        from libs.domain.state_machines import IllegalTransition, subaccount_transition

        db = SessionLocal()
        try:
            sa = db.get(M.SubAccount, sub_account_id)
            if not sa:
                return
            job = db.scalar(
                select(M.RegistrationJob).where(M.RegistrationJob.sub_account_id == sub_account_id)
                .order_by(M.RegistrationJob.created_at.desc())
            )
            job_status = {
                "registered": RegistrationStatus.REGISTERED, "exists": RegistrationStatus.REGISTERED,
                "verified": RegistrationStatus.VERIFIED, "failed": RegistrationStatus.FAILED,
            }.get(outcome)
            if job is not None and job_status is not None:
                job.status = job_status
            if job is not None and payload.get("proxy_id"):
                job.proxy_id = payload["proxy_id"]
            if job is not None and payload.get("solver_used"):
                job.solver_used = payload["solver_used"]

            if payload.get("email"):
                sa.email = payload["email"]
            if payload.get("email_pass"):
                sa.email_pass = payload["email_pass"]
            if payload.get("detail"):
                sa.notes = str(payload["detail"])[:2000]
            elif outcome in ("registered", "exists", "verified"):
                # A prior attempt's failure detail must not linger next to a now-successful status --
                # confirmed confusing in practice (an account showing "verified" next to a stale
                # "ReCaptchaError" note from attempt 1, after attempt 3 succeeded).
                sa.notes = None

            sub_target = {"registered": SubAccountStatus.REGISTERED, "exists": SubAccountStatus.REGISTERED,
                         "verified": SubAccountStatus.VERIFIED}.get(outcome)
            if sub_target is not None:
                try:
                    sa.status = subaccount_transition(sa.status, sub_target)
                except IllegalTransition:
                    pass  # already there (e.g. a duplicate/stale event) -- leave status as-is
            db.commit()
        finally:
            db.close()

    def _consume(self) -> None:
        bus = Bus(connect(self._redis_url))
        last_id = "$"   # only new events from Hub start
        while not self._stop.is_set():
            try:
                msgs = bus.read_events(last_id=last_id, block_ms=1000, count=200)
            except Exception:
                continue
            for m in msgs:
                last_id = m.id
                wid = m.payload.get("worker_id")
                if wid:
                    now = time.time()
                    self.last_seen[wid] = now
                    self._event_times.setdefault(wid, []).append(now)
                    # Each side-effect is isolated. Confirmed live 2026-08-14: _persist_booked_pdf
                    # raised AttributeError on SlotStatus.BOOKED (no such member), which aborted the
                    # rest of THIS message's handling -- so a real, PDF-verified booking produced no
                    # Artifact, no BookedAppointment, and, because the `terminal` branch below never
                    # ran, left the worker showing running/applying after it had already exited.
                    # A failure in one persistence path must never silently swallow another; log it
                    # loudly and keep going, so a status update is never lost to an unrelated bug.
                    def _step(name, fn, *a):
                        try:
                            fn(*a)
                        except Exception:
                            logger.exception("event handler %s failed for worker %s (event=%s)",
                                             name, wid, m.type)

                    _step("apply_db", self._apply_db, wid, map_event_to_update(m.type, m.payload))
                    if m.type == "slots_found":
                        _step("persist_slots", self._persist_slots, wid, m.payload.get("slots", []))
                    if m.type == "booked":
                        _step("persist_booking", self._persist_booking, wid, m.payload.get("slot"),
                              None, m.payload.get("appointment_ref"))
                    if m.type == "already_booked":
                        _step("mark_already_booked", self._mark_already_booked,
                              wid, m.payload.get("detail", ""))
                    if m.type == "booked_pdf" and m.payload.get("pdf_path"):
                        _step("persist_booked_pdf", self._persist_booked_pdf,
                              wid, m.payload["pdf_path"], m.payload.get("slot"),
                              m.payload.get("appointment_ref"))
                    if m.type == "reg_result" and m.payload.get("sub_account_id"):
                        _step("persist_registration_result", self._persist_registration_result,
                              m.payload["sub_account_id"], m.payload.get("outcome", ""), m.payload)
                    if m.type == "terminal":
                        _step("release_on_terminal", self._release_on_terminal, wid, bus)
                    if m.type == "proxy_request":
                        self._proxy_executor.submit(
                            self._handle_proxy_request, wid, m.payload.get("phase", "warmup"), bus)
                    if m.type == "dates_reported":
                        self.dates_reported.setdefault(wid, []).append(m.payload.get("outcomes", []))
                    if m.type == "slot_released":
                        _step("release_slot_lease", self._release_slot_lease,
                              wid, m.payload.get("slot") or {}, m.payload.get("reason", "bounce"))
                    if m.type == "gettime_result":
                        _step("cache_gettime", self._cache_gettime, wid,
                              m.payload.get("special_days", {}))
                item = {"id": m.id, "type": m.type, "ts": m.ts, **m.payload}
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(self._broadcast, item)

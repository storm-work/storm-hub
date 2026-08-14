"""BusSignals — the worker's blocking wait-points on the bus (slot-open, assignment, launch, discover).

The phase machine's await_slot_open / await_assignment / await_discover_task are orchestration
waits, not browser ops — both sim and live workers block here for the SAME scheduler-driven commands:
  - launch   : carries the frozen effective settings
  - signal(name=slot_open|slot_found) : the orchestrator fired a signal
  - assign(slot=..., scouts_invalid=...) : the orchestrator assigned a pooled slot to this (apply)
    worker. `scouts_invalid` rides along on the SAME command (resolving the open design question
    from the reconstruction plan) rather than needing a second round-trip/command type -- the
    scheduler already knows scout liveness when it decides what to assign, so it's free to include.
  - discover(dates=[...]) : the orchestrator assigned a date-chunk to this (scout) worker
  - stop/shutdown          : abort

Commands persist in the per-worker Redis stream (consumer group), so ordering vs the worker's own
progress doesn't matter — each wait reads until its command arrives (or a timeout / stop).
"""
from __future__ import annotations

import time

# Distinguishes "this window elapsed with nothing to report" from a REAL result of None -- an
# `assign` command legitimately carries `slot=None` (the self-scout fallback signal, scouts_invalid
# rides along on it) and must not be swallowed as if it were just a timeout. Confirmed regression:
# before this sentinel existed, wait_assignment() returned bare None for both cases, so the
# fallback's `assign slot=None` was indistinguishable from "still waiting" and silently discarded,
# making PhaseMachine's `scouts_invalid()` self-discover path unreachable.
TIMEOUT = object()

# Re-exported from libs.engine.phases (defined there, next to the EngineOps Protocol that returns
# it) so both the signal layer and the phase machine compare against the SAME object identity.
from libs.engine.phases import INACTIVATE  # noqa: E402


class BusSignals:
    def __init__(self, bus, worker_id: str, consumer: str):
        self.bus = bus
        self.wid = worker_id
        self.consumer = consumer
        self.stopped = False
        self.launch_payload: dict = {}   # full launch command payload (cfg + _engine/account/... -- C6)
        self._last_scouts_invalid = False
        # Passively observed regardless of which wait_* call sees it in a batch -- the keepalive
        # loop (LiveEngineOps) needs to know "has a slot actually been found yet" to decide whether
        # a session blip is worth burning a re-login over (storm-auto's LOOSE HOLD rule: before
        # slot_found there's nothing to book, so a proxy/conn flicker must never cost a re-login).
        self.slot_found_seen = False
        self.slot_open_seen = False
        # Set by an `inactivate` signal (the Hub's false-slot-open revert). Passively observed in
        # _drain like slot_found_seen, so whichever wait_* call happens to see the batch records it.
        self.inactivated = False
        # An `assign` that arrives while some OTHER wait_* is blocked. _drain acks the whole batch,
        # so a command not matched by the currently-running wait used to be acked and dropped on the
        # floor. Confirmed live 2026-08-14: the scheduler assigns a slot (creating a SlotLease) to
        # any HOLDING apply worker -- including one still sitting in await_slot_open after a respawn
        # -- and that assign was silently discarded, stranding its lease forever. One worker
        # accumulated 120 stranded leases, consuming every slot in the booking.
        self.pending_assignment = TIMEOUT   # TIMEOUT = "nothing stashed" (None is a legal slot)
        bus.ensure_command_group(worker_id)

    def _drain(self, block_ms: int = 2000) -> list:
        """Fetch AND ack the whole batch (up to read_commands' own page size) before returning --
        never a lazy generator. Every wait_* below does `return` from partway through iterating this
        result once it finds the command it wants, which used to abandon a lazily-acking generator
        mid-batch: any messages already delivered by XREADGROUP but not yet reached in the loop were
        never acked and (since `>` never redelivers an already-delivered entry) sat in the consumer
        group's PEL forever. Two confirmed consequences (X1): a `stop` batched behind an `assign` in
        the same fetch was silently lost, and the orphaned pending entries made
        Bus.has_pending_commands() report True for that worker permanently, cutting it off from all
        future scout/slot assignments. Acking the full batch up front, before any dispatch logic
        runs, closes both."""
        msgs = self.bus.read_commands(self.wid, self.consumer, block_ms=block_ms)
        for m in msgs:
            self.bus.ack_command(self.wid, m.id)
            if m.type == "signal" and m.payload.get("name") == "slot_open":
                # STICKY, like slot_found_seen. A signal is only ever sent once per worker: the Hub
                # marks it notified after a confirmed send and _latch_signals then skips it forever.
                # But _drain acks the WHOLE batch regardless of which wait_* is currently running,
                # so a slot_open arriving while the worker was still logging in/warming up -- which
                # is exactly what happens when slot_open_at is already in the past at launch -- was
                # acked and dropped. The worker then reached await_slot_open() and waited forever
                # for a signal that had already been delivered and would never be re-sent.
                # Confirmed live 2026-08-14: scouts stuck in HOLDING while the booking was already
                # past slot-found. Same class as the `assign` drop fixed earlier the same day.
                self.slot_open_seen = True
            if m.type == "signal" and m.payload.get("name") == "slot_found":
                self.slot_found_seen = True
            if m.type == "assign":
                # Stash rather than drop -- wait_assignment consumes it. Keeping the newest is
                # correct: the scheduler only ever has one outstanding assignment per worker now
                # (Phase F2's one-active-lease rule, enforced in _assign_slots).
                self.pending_assignment = m.payload.get("slot")
                self._last_scouts_invalid = bool(m.payload.get("scouts_invalid", False))
            if m.type == "signal" and m.payload.get("name") == "inactivate":
                self.inactivated = True
                # A re-fired slot_open is a genuinely new window -- clear the stale slot_found so a
                # scout that saw one in a PREVIOUS (now-inactivated) window doesn't skip loose-hold
                # protection in the new one.
                self.slot_found_seen = False
                self.slot_open_seen = False   # the window is over; a new one must be signalled again
        return msgs

    def wait_launch(self, timeout: float = 3600) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline and not self.stopped:
            for m in self._drain():
                if m.type == "launch":
                    self.launch_payload = m.payload
                    return m.payload.get("cfg", {})
                if m.type in ("stop", "shutdown"):
                    self.stopped = True
                    return None
        return None

    def take_inactivated(self) -> bool:
        """Consume-and-clear the inactivate flag (one revert = one transition back to holding; a
        stale flag must not immediately kick the scout out of the NEXT slot-open window too)."""
        was = self.inactivated
        self.inactivated = False
        return was

    def wait_slot_open(self, timeout: float = 900) -> str:
        """Blocks for one bounded window; returns "timeout" if nothing arrived. Deliberately bounded
        (not looped internally) -- the caller (LiveEngineOps/SimEngineOps's await_slot_open) is the
        one with something useful to DO between windows (a real keepalive portal ping for live, a
        heartbeat log line for sim), and must never let "timeout" escape to the phase machine, which
        doesn't handle it (that was the original bug: an unhandled "timeout" fell through as if
        slot_open had fired). Looping-until-stop lives one layer up now, wrapped around a real action."""
        # Already delivered while some other wait_* was running -- see _drain.
        if self.slot_open_seen:
            return "slot_open"
        deadline = time.time() + timeout
        while time.time() < deadline and not self.stopped:
            for m in self._drain():
                if m.type == "signal" and m.payload.get("name") == "slot_open":
                    return "slot_open"
                if m.type in ("stop", "shutdown"):
                    self.stopped = True
                    return "stop"
            if self.slot_open_seen:
                return "slot_open"
        return "timeout"

    def wait_assignment(self, timeout: float = 900):
        """Same bounded-per-window contract as wait_slot_open -- see its docstring. Returns
        `TIMEOUT` (the sentinel, not None) if the window elapsed with nothing to report; returns a
        real `dict | None` for an actual `assign` command (None is legitimate there -- the
        self-scout fallback signal) or `None` for stop. Callers distinguish stop from a real
        null-slot assign via `self.stopped` after the call."""
        # An assignment that landed while a different wait_* was running is already stashed by
        # _drain -- take it before blocking, or it would be waited for a second time and the
        # scheduler would have to issue (and lease) another one.
        if self.pending_assignment is not TIMEOUT:
            slot, self.pending_assignment = self.pending_assignment, TIMEOUT
            return slot
        deadline = time.time() + timeout
        while time.time() < deadline and not self.stopped:
            for m in self._drain():
                if m.type == "assign":
                    slot, self.pending_assignment = self.pending_assignment, TIMEOUT
                    return slot
                if m.type in ("stop", "shutdown"):
                    self.stopped = True
                    return None
        return TIMEOUT

    def scouts_invalid(self) -> bool:
        """Set from the most recent `assign` command's `scouts_invalid` flag -- see the module
        docstring for why this rides along on `assign` rather than being a separate round-trip."""
        return self._last_scouts_invalid

    def wait_proxy(self, timeout: float = 30):
        """Bounded single-window wait for the Hub's reply to a `proxy_request` event (P3 -- proxy
        rotation: a worker asks the Hub, the only side with DB access, for a fresh proxy drawn from
        the pool matching its current phase). Mirrors wait_assignment's TIMEOUT-sentinel contract:
        `TIMEOUT` if the window elapses with nothing to report, `{"ok": True, "proxy": <url>}` /
        `{"ok": False, "reason": ...}` for a real reply, or `None` for stop. Default timeout is short
        (30s, not 900s like the orchestration waits) -- this blocks a specific failure-recovery call
        site (a login retry, a keepalive restore), never the worker's main loop."""
        deadline = time.time() + timeout
        while time.time() < deadline and not self.stopped:
            for m in self._drain():
                if m.type == "proxy_assigned":
                    return {"ok": True, "proxy": m.payload.get("proxy")}
                if m.type == "proxy_unavailable":
                    return {"ok": False, "reason": m.payload.get("reason", "")}
                if m.type in ("stop", "shutdown"):
                    self.stopped = True
                    return None
        return TIMEOUT

    def wait_discover_task(self, timeout: float = 900) -> list[str] | None:
        """Same bounded-per-window contract as wait_slot_open -- see its docstring. Returns None on
        BOTH timeout and stop; callers distinguish via `self.stopped` after the call.

        Also stashes the command's optional `special_days` payload onto
        `self.last_discover_special_days` (side channel, matching `launch_payload`/
        `slot_found_seen`'s existing pattern in this class) -- the Hub's shared gettime cache
        (apps/hub/scheduler.py's _drive_scouts), when fresh, rides along on the SAME `discover`
        command so a scout given a cache hit never needs its own gettime fetch at all."""
        deadline = time.time() + timeout
        while time.time() < deadline and not self.stopped:
            for m in self._drain():
                if m.type == "discover":
                    self.last_discover_special_days = m.payload.get("special_days")
                    return m.payload.get("dates") or []
                if m.type in ("stop", "shutdown"):
                    self.stopped = True
                    return None
            if self.take_inactivated():
                return INACTIVATE   # false slot-open -- caller returns to await_slot_open, not stop
        return None

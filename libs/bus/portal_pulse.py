"""PortalPulse -- a real-time, fleet-wide answer to "is the portal answering RIGHT NOW?"

WHY (run 1023, 2026-10-23). Under booking-day stress the portal is up for 30-90 s, then down. Riding
those windows ("hammering" the last step) needs to know, within seconds, when the portal answers
again. PortalLiveness (the per-booking dashboard badge) cannot: it records only logins, bookings and
the maintenance page, over 3-minute windows, and never sees a timeout or a 503. And each worker on
its own cannot tell "my exit is dead" from "the portal is down" without the rest of the fleet --
which is what made run 1023 roll exits ~9,800 times into a down portal.

ONE PORTAL, ONE PULSE. Not per booking: every booking talks to the same site. Redis-backed so every
worker process and the Hub read and write the same view within a second.

OUTCOMES an observation can report:

  up         the portal APPLICATION served a real page (a 200 with portal markup, a quest step, a
             keepalive that came back authenticated, a submit that got an answer).
  degraded   the portal answered but is not serving: 5xx or its maintenance page.
  refused    the EDGE answered with its 'try again later' page or a bot gate. Reachable, but says
             nothing about the application (the Hub server's own IP always gets this -- datacenter IPs
             are refused, measured 2026-10-23: 403 in 0.8 s).
  noanswer   no answer at all, from a vantage whose control (an IP echo through the same exit)
             works -- the portal or its edge is down. A vantage whose control ALSO fails reports
             nothing: that is a dead exit, not evidence about the portal.

STATES over the last `window` seconds (state()):

  up         the application is answering (>=1 up, and up is the newest app-level outcome or >=30%
             of them).
  flapping   some up, but mostly failing -- the stress cycle in progress. Hammer: fire on each up.
  degraded   answering with errors only.
  down       no answers from live vantages.
  reachable  only edge refusals seen -- the edge is up, the application unknown.
  unknown    nothing observed recently.

WHAT EACH PRODUCER CAN SEE (measured 2026-10-23 ~15:30 UTC). The Hub's anonymous probe can NOT see
the application: the server IP gets the edge's 403, and residential (soax) exits get the 44,699 B
bot-challenge in ~1-2 s -- the same gate that stops detached primp in the strict window. So the
prober reports `refused` (edge up, and how fast) or `noanswer` (edge down) -- exactly the line
between run 1023's outage (11:15-11:40, no answers) and a portal that is merely gating. Only logged-in
WORKERS, whose requests carry a real session, report application-level `up`. Webshare exits at the
same moment reached ipify but failed the portal tunnel in 3.3 s ("tunnel error: unsuccessful"): a
no-answer from a live exit while the edge answers others is that EXIT's problem, which only the
fleet view can tell (ops_live._portal_down_not_exit consults state()).

Producers: workers (passively, from requests they make anyway) and the Hub's prober thread
(run_pulse_prober). Consumers: held workers wake on `last_up_at()` advancing (the hammer trigger);
the portal-down/exit-dead check asks state(); the dashboard shows state() + history().
"""
from __future__ import annotations

import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

OUTCOMES = ("up", "degraded", "refused", "noanswer")
_KEY = "portal:pulse"
_LOG = "portal:pulse:log"

# Mirrors apps/worker/ops_live.py's marker lists (kept dependency-free so the Hub need not import
# the worker). Maintenance = down for everyone; busy = the edge's per-client 'try again later'.
_MAINTENANCE = ("serviço em manutenção", "servico em manutencao", "service under maintenance",
                "serviço indisponível", "servico indisponivel",
                "service unavailable. please visit again later")
_BUSY = ("não foi possível de momento concretizar o seu pedido",
         "nao foi possivel de momento concretizar o seu pedido",
         "we were unable to complete your request at this time")
_CHALLENGE = ("/ch/bd.js", "captcha-delivery", "datadome", 'name="robots" content="noindex,nofollow"')
_PORTAL_MARKUP = ("vistosonline", "pedidodevistos")


def classify_probe(status: int | None, body: str) -> str:
    """The outcome of one anonymous GET of the portal home page."""
    low = (body or "").lower()
    if any(m in low for m in _MAINTENANCE):
        return "degraded"
    if any(m in low for m in _BUSY) or any(m in low for m in _CHALLENGE):
        return "refused"
    if status is not None and status >= 500:
        return "degraded"
    if status == 200 and any(m in low for m in _PORTAL_MARKUP):
        return "up"
    return "refused"          # any other edge answer (403 etc.): reachable, application unknown


class PortalPulse:
    def __init__(self, r, *, window: float = 20.0, keep: float = 900.0, key: str = _KEY):
        self.r = r
        self.window = float(window)
        self.keep = float(keep)
        self.key = key
        self.log_key = key + ":log" if key != _KEY else _LOG

    def _zkey(self, outcome: str) -> str:
        return f"{self.key}:{outcome}"

    # ── recording ─────────────────────────────────────────────────────────────────────────────
    def observe(self, outcome: str, *, ms: float | None = None, src: str = "") -> None:
        """Record one observation. Cheap (a pipeline of O(log n) ops); never raises to the caller's
        hot path -- liveness must not be able to break the work it measures."""
        if outcome not in OUTCOMES:
            return
        now = time.time()
        try:
            p = self.r.pipeline()
            z = self._zkey(outcome)
            p.zadd(z, {f"{now:.3f}:{uuid.uuid4().hex[:6]}": now})
            p.zremrangebyscore(z, "-inf", now - self.keep)
            p.expire(z, int(self.keep) + 60)
            fields = {f"last_{outcome}_at": now}
            if src:
                fields[f"last_{outcome}_src"] = src
            if outcome == "up" and ms is not None:
                fields["last_up_ms"] = round(float(ms))
            p.hset(self.key, mapping=fields)
            p.expire(self.key, 86400)
            p.execute()
        except Exception:
            pass

    # ── reading ───────────────────────────────────────────────────────────────────────────────
    def _h(self) -> dict:
        raw = self.r.hgetall(self.key) or {}
        return {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
                for k, v in raw.items()}

    def last_up_at(self) -> float:
        """When ANY vantage last saw the application answer. The hammer trigger: a held worker
        compares this with when it started waiting."""
        try:
            v = self.r.hget(self.key, "last_up_at")
            return float(v) if v else 0.0
        except Exception:
            return 0.0

    def counts(self, window: float | None = None) -> dict:
        w = self.window if window is None else float(window)
        now = time.time()
        return {o: int(self.r.zcount(self._zkey(o), now - w, "+inf") or 0) for o in OUTCOMES}

    def _newest_app_outcome(self, window: float) -> str | None:
        """Which of up/degraded/noanswer happened most recently inside the window."""
        now = time.time()
        best, best_ts = None, 0.0
        for o in ("up", "degraded", "noanswer"):
            top = self.r.zrevrangebyscore(self._zkey(o), "+inf", now - window, start=0, num=1,
                                          withscores=True)
            if top and top[0][1] > best_ts:
                best, best_ts = o, top[0][1]
        return best

    def state(self) -> dict:
        c = self.counts()
        h = self._h()
        app = c["up"] + c["degraded"] + c["noanswer"]
        if c["up"] and (self._newest_app_outcome(self.window) == "up" or c["up"] / app >= 0.3):
            st = "up"
        elif c["up"]:
            st = "flapping"
        elif c["degraded"]:
            st = "degraded"
        elif c["noanswer"]:
            st = "down"
        elif c["refused"]:
            st = "reachable"
        else:
            st = "unknown"
        now = time.time()
        up_at = float(h.get("last_up_at", 0) or 0)
        return {
            "state": st,
            "since": float(h.get("state_since", 0) or 0) or None,
            "window_s": self.window,
            "counts": c,
            "last_up_at": up_at or None,
            "up_age_s": round(now - up_at, 1) if up_at else None,
            "last_up_ms": int(h["last_up_ms"]) if h.get("last_up_ms") else None,
            "last_up_src": h.get("last_up_src"),
        }

    # ── transitions (written by ONE process: the Hub prober's tick) ──────────────────────────
    def tick(self) -> str | None:
        """Compute the state and, if it changed since the last tick, record the transition. Returns
        the new state on a change, else None. The log is what turns 'it feels like 30-90 s' into
        measured up/down window lengths."""
        st = self.state()["state"]
        prev = self._h().get("state")
        if st == prev:
            return None
        now = time.time()
        self.r.hset(self.key, mapping={"state": st, "state_since": now})
        self.r.xadd(self.log_key, {"state": st, "prev": prev or "", "ts": f"{now:.3f}"},
                    maxlen=5000, approximate=True)
        return st

    def history(self, n: int = 100) -> list[dict]:
        out = []
        for _id, f in self.r.xrevrange(self.log_key, count=n):
            f = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
                 for k, v in f.items()}
            out.append({"state": f.get("state"), "prev": f.get("prev"), "ts": float(f.get("ts", 0))})
        return out

    def windows(self, n: int = 200) -> list[dict]:
        """Closed state intervals, newest first: [{state, start, end, seconds}] -- the measured up /
        down cycle."""
        h = list(reversed(self.history(n)))
        return [{"state": a["state"], "start": a["ts"], "end": b["ts"],
                 "seconds": round(b["ts"] - a["ts"], 1)} for a, b in zip(h, h[1:])][::-1]

    def wait_for_up(self, after: float, timeout: float, *, poll: float = 1.0,
                    stop: Callable[[], bool] = lambda: False) -> bool:
        """Block up to `timeout` s; True as soon as the application is seen answering after `after`."""
        end = time.time() + timeout
        while time.time() < end:
            if stop():
                return False
            if self.last_up_at() > after:
                return True
            time.sleep(min(poll, max(0.0, end - time.time())))
        return False


# ── the Hub's active prober ───────────────────────────────────────────────────────────────────
PORTAL_HOME = "https://pedidodevistos.mne.gov.pt/VistosOnline/"
IP_ECHO = "https://api.ipify.org"


@dataclass
class Vantage:
    """One place the prober looks from. `mint` returns a proxy URL (None = the server itself);
    a vantage whose own control fails is re-minted, never reported as evidence."""
    name: str
    mint: Callable[[], str | None]
    proxy: str | None = None
    inflight: int = 0
    control_ok_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def ensure(self) -> None:
        if self.proxy is None:
            self.proxy = self.mint()

    def reroll(self) -> None:
        try:
            self.proxy = self.mint()
        except Exception:
            pass
        self.control_ok_at = 0.0


def _client(proxy: str | None, connect_t: float, read_t: float):
    import primp
    kw = dict(verify=False, follow_redirects=True, impersonate="chrome_148",
              connect_timeout=connect_t, read_timeout=read_t)
    if proxy:
        kw["proxy"] = proxy
    return primp.Client(**kw)


def probe_once(v: Vantage, pulse: PortalPulse, *, connect_t: float = 30.0, read_t: float = 30.0,
               log: Callable[[str], None] = lambda _m: None) -> str | None:
    """One anonymous GET of the home page from `v`. Returns the outcome recorded (None = vantage
    dead, nothing recorded). Connect timeout is deliberately LONG: the stressed portal took 25.6 s
    to accept a connection (run 1023), and a 10 s timeout read that as down (F42)."""
    t0 = time.time()
    try:
        r = _client(v.proxy, connect_t, read_t).get(PORTAL_HOME, timeout=connect_t + read_t)
        out = classify_probe(getattr(r, "status_code", None), r.text or "")
        pulse.observe(out, ms=(time.time() - t0) * 1000, src=f"probe:{v.name}")
        # NOT re-minted on `refused`: MEASURED 2026-10-23 ~15:30 UTC, three fresh soax exits all got
        # the same 44,699 B bot-challenge in 0.9-1.9 s -- the anonymous gate is portal-wide, so a new
        # exit would see the same thing. A refusal still proves the edge is up and how fast.
        return out
    except Exception as e:
        # No answer. Portal down, or our exit dead? Ask the exit (cached 30 s).
        if time.time() - v.control_ok_at > 30:
            try:
                ip = (_client(v.proxy, 8, 8).get(IP_ECHO, timeout=10).text or "").strip()
                v.control_ok_at = time.time() if ip and len(ip) <= 45 and "<" not in ip else 0.0
            except Exception:
                v.control_ok_at = 0.0
        if v.control_ok_at:
            pulse.observe("noanswer", src=f"probe:{v.name}")
            return "noanswer"
        log(f"portal_pulse: vantage {v.name} is dead ({str(e)[:80]}) -- re-minting")
        v.reroll()
        return None


def run_pulse_prober(redis_client, *, stop_event: threading.Event, vantages: list[Vantage],
                     interval: float = 3.0, max_inflight: int = 3,
                     active: Callable[[], bool] = lambda: True,
                     log: Callable[[str], None] = lambda _m: None) -> None:
    """Background loop: every `interval` s (+-20%), start a probe from each vantage that has fewer
    than `max_inflight` in flight -- probes overlap, so a 25 s slow accept never blinds the prober
    for 25 s. Ticks the state machine every loop, so transitions are recorded even when only the
    workers are reporting. Probes only while `active()` (a booking is running): residential bytes
    cost money, and nobody needs the answer otherwise."""
    pulse = PortalPulse(redis_client)
    log(f"portal_pulse: prober started ({len(vantages)} vantages, interval={interval}s)")
    last_active_check, is_active = 0.0, False
    while not stop_event.is_set():
        try:
            if time.time() - last_active_check > 30:
                is_active, last_active_check = bool(active()), time.time()
            if is_active:
                for v in vantages:
                    with v.lock:
                        if v.inflight >= max_inflight:
                            continue
                        v.inflight += 1
                    try:
                        v.ensure()
                    except Exception as e:
                        log(f"portal_pulse: vantage {v.name} cannot mint ({str(e)[:80]})")
                        with v.lock:
                            v.inflight -= 1
                        continue

                    def _run(v=v):
                        try:
                            probe_once(v, pulse, log=log)
                        finally:
                            with v.lock:
                                v.inflight -= 1
                    threading.Thread(target=_run, name=f"pulse-{v.name}", daemon=True).start()
            new = pulse.tick()
            if new:
                log(f"portal_pulse: portal is now {new.upper()}")
        except Exception as e:
            log(f"portal_pulse: loop error ({str(e)[:100]})")
        stop_event.wait(interval * random.uniform(0.8, 1.2))
    log("portal_pulse: prober stopped")

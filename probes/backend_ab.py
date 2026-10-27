"""Chromium vs Donut A/B report for one or more bookings (read-only: DB, Redis event stream, worker logs).

Usage:
    .venv/bin/python -m probes.backend_ab "Donut AB 1" ["Donut AB 2" ...] [--since 2026-10-27T06:00]

Each worker's backend comes from its own session log ("[session] launch begin backend=..."), so a
booking that mixes a chromium and a donut template splits cleanly. Per backend it reports:

  launch      sessions started, start-to-ready seconds (p50/p90), start failures (init timeout,
              Donut API errors, Donut 429s)
  login       attempts (/login form POST), landed (at_home=True), verified (landed minus the
              "WITHOUT the logged-in menu" retries), bot challenge on the login POST
  hold        per verified login: minutes until the first keepalive that was not ok (expired,
              dead exit, dead-trip). Reported as the share still alive at 15/30/60/120 min among
              sessions observed at least that long (censored sessions are not counted as losses).
  warm-up     warmup_end ok / fail, p50 seconds
  discovery   discover_end rounds and how many reported slots

Nothing here touches the portal or changes state.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

import redis
from sqlalchemy import select

from apps.hub.db import SessionLocal
from libs.bus.names import EVENT_STREAM
from libs.db import models as M

_TS = re.compile(r"^(\d{4}-\d\d-\d\dT[\d:.]+)Z ")


def _ts(line: str) -> float | None:
    m = _TS.match(line)
    if not m:
        return None
    return dt.datetime.fromisoformat(m.group(1)).replace(tzinfo=dt.timezone.utc).timestamp()


def _pct(xs, p):
    if not xs:
        return "n/a"
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(p * len(xs)))], 1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bookings", nargs="+")
    ap.add_argument("--since", default=None, help="UTC ISO time; default = earliest booking start")
    a = ap.parse_args()

    db = SessionLocal()
    bks = {b.id: b for b in db.scalars(select(M.Booking)).all() if b.name in a.bookings}
    if not bks:
        raise SystemExit(f"no booking named {a.bookings}")
    workers = {w.id: w for w in db.scalars(select(M.Worker)).all() if w.booking_id in bks}
    db.close()
    since = (dt.datetime.fromisoformat(a.since).replace(tzinfo=dt.timezone.utc).timestamp() if a.since
             else min((b.started_at or b.created_at).timestamp() for b in bks.values()))

    # ── worker logs: backend, launch timing, login POSTs ────────────────────────────────────────
    backend: dict[str, str] = {}
    S = defaultdict(lambda: defaultdict(list))          # backend -> metric -> values
    C = defaultdict(Counter)                            # backend -> counter
    verified_at = defaultdict(list)                     # worker -> [login ts]
    for f in glob.glob("var/worker-logs/worker-*.log"):
        m = re.search(r"worker-([0-9a-f-]{36})\.log$", f)
        if not m or m.group(1) not in workers:
            continue
        wid = m.group(1)
        lines = [l for l in Path(f).read_text(errors="replace").splitlines() if (_ts(l) or 0) >= since]
        kinds = Counter(re.findall(r"launch begin backend=(\w+)", "\n".join(lines)))
        if not kinds and not any("[session]" in l for l in lines):
            continue
        # Logs written before the backend marker existed can only be the Chromium engine.
        be = backend[wid] = kinds.most_common(1)[0][0] if kinds else "chromium"
        for l in lines:
            if m2 := re.search(r"\[session\] ready backend=\w+ in ([\d.]+)s", l):
                S[be]["ready_s"].append(float(m2.group(1)))
            if "launch begin backend=" in l or "ANTIDETECT flags:" in l:
                C[be]["sessions"] += 1
            if "init timed out" in l:
                C[be]["start_fail_init_timeout"] += 1
            if "HTTP 429" in l and "Donut" in l:
                C[be]["donut_429"] += 1
            if "Donut API unreachable" in l or "could not attach to Donut" in l:
                C[be]["donut_start_fail"] += 1
            if "/login form POST" in l:
                C[be]["login_attempts"] += 1
                if "challenge=True" in l:
                    C[be]["login_bot_challenge"] += 1
                if "at_home=True" in l:
                    C[be]["login_landed"] += 1
                    verified_at[wid].append(_ts(l))

    # ── event stream: verification retries, keepalives, warm-ups, discovery ──────────────────────
    r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
    first_loss = {}                                     # (worker, login_ts) -> ts of first non-ok
    ka = defaultdict(list)
    for eid, fld in r.xrange(EVENT_STREAM, min=f"{int(since * 1000)}-0"):
        try:
            d = json.loads(fld.get("data", "{}"))
        except Exception:
            continue
        wid = d.get("worker_id") or ""
        if wid not in backend:
            continue
        be, t = backend[wid], d.get("text") or ""
        ts = int(eid.split("-")[0]) / 1000
        if "WITHOUT the logged-in menu" in t:
            C[be]["login_landed_but_logged_out"] += 1
        if m := re.search(r"event=keepalive .*?result=(\w+)", t):
            ka[wid].append((ts, m.group(1)))
        if m := re.search(r"event=warmup_end .*?result=(\w+)(?:.*?duration_ms=(\d+))?", t):
            C[be][f"warmup_{m.group(1)}"] += 1
            if m.group(1) == "ok" and m.group(2):
                S[be]["warmup_s"].append(int(m.group(2)) / 1000)
        if "event=discover_end" in t:
            C[be]["discover_rounds"] += 1
            if re.search(r"found=([1-9]\d*)", t):
                C[be]["discover_rounds_with_slots"] += 1

    # ── hold survival per verified login ─────────────────────────────────────────────────────────
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    BAD = {"expired", "dead_exit", "probe_failed", "rewarm_failed"}
    for wid, logins in verified_at.items():
        be = backend[wid]
        logins = sorted(x for x in logins if x)
        for i, lt in enumerate(logins):
            end = logins[i + 1] if i + 1 < len(logins) else now     # next login ends this session
            loss = next((ts for ts, res in ka.get(wid, []) if lt < ts < end and res in BAD), None)
            S[be]["hold"].append(((loss or end) - lt, loss is not None))

    for be in sorted(C):
        c = C[be]
        att, landed = c["login_attempts"], c["login_landed"]
        verified = landed - c["login_landed_but_logged_out"]
        print(f"\n=== backend={be}  workers={sum(1 for w in backend.values() if w == be)}")
        print(f"  launch : sessions={c['sessions']}  ready p50={_pct(S[be]['ready_s'], .5)} s "
              f"p90={_pct(S[be]['ready_s'], .9)} s  init-timeouts={c['start_fail_init_timeout']}  "
              f"donut start fails={c['donut_start_fail']}  donut 429={c['donut_429']}")
        print(f"  login  : attempts={att}  landed={landed}  verified={verified}  "
              f"rate={100 * verified / att:.1f}%  bot-challenge on POST={c['login_bot_challenge']}"
              if att else "  login  : no attempts")
        line = []
        for mins in (15, 30, 60, 120):
            obs = [(d, lost) for d, lost in S[be]["hold"] if d >= mins * 60 or lost]
            alive = [1 for d, lost in obs if d >= mins * 60]
            if obs:
                line.append(f"{mins}m {100 * len(alive) / len(obs):.0f}% (n={len(obs)})")
        print(f"  hold   : still logged in at " + (", ".join(line) or "no data"))
        print(f"  warm-up: ok={c['warmup_ok']} fail={c['warmup_fail']}  p50={_pct(S[be]['warmup_s'], .5)} s")
        print(f"  discover: rounds={c['discover_rounds']}  with slots={c['discover_rounds_with_slots']}")


if __name__ == "__main__":
    main()

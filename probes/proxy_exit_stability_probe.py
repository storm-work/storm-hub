#!/usr/bin/env python3
"""Does a proxy exit IP stay stable while its session is ACTIVELY kept alive?

WHY THIS EXISTS (Q1, docs/engineering/OPEN_QUESTIONS.md). The holding architecture assumes that a
worker's keepalive traffic preserves its proxy exit. One observation on 2026-08-25 showed a webshare
exit moving at t=10m while being probed every 5 minutes -- inside the then-15-minute idle window, so
idleness could not explain it. That is evidence of POSSIBILITY, not of FREQUENCY. This probe measures
frequency.

Sibling to probes/proxy_idle_hold_probe.py, which measures the IDLE case (silence -> does the exit
survive). This one measures the ACTIVE case, longitudinally: hold a session, probe it on a schedule,
and record every exit it presents over time.

WHAT IT DELIBERATELY IS NOT:
  - It never touches the portal. Exit observation is an IP echo, not a portal nav, so the WAF is
    irrelevant and no booking traffic is generated.
  - It writes NOTHING to the database and changes no production behaviour. It reads Proxy rows to
    obtain credentials and nothing else.
  - It builds no second proxy abstraction: credentials come from libs.proxies.lease.conn_url and
    identity from libs.proxies.lease.session_id, the same functions production leases use.

ARMS. Exit stability may be provider- and configuration-specific, so arms are never pooled:

  active_ws     webshare/rotating   probe every PROBE_S, a FRESH connection each probe
  active_soax   soax/rotating       same cadence -- also a POSITIVE CONTROL: these credentials carry
                                    sessionlength-3600, a provider-declared 1h ceiling, so a sound
                                    instrument should see them change near 60 minutes
  active_mob    soax/mobile         same cadence, different provider contract
  lowtraffic_ws webshare/rotating   probe every LOWTRAFFIC_S (default 30m) -- far less traffic, but
                                    still under the 1h Proxy Idle Timeout, so it is never idle-released.
                                    Isolates TRAFFIC VOLUME as a variable against active_ws.
  persist_ws    webshare/rotating   probe every PROBE_S reusing ONE pooled connection (requests.Session)
                                    Isolates CONNECTION REUSE: the provider's Idle/Session Timeouts are
                                    documented in terms of a *connection*, and a fresh-connection probe
                                    never keeps one alive between samples.

Each session is pinned to ONE echo endpoint for its whole run (falling back only on failure), so a
difference between echo services can never masquerade as an exit change.

A FAILED PROBE IS NEVER AN EXIT CHANGE. Failures are recorded with ok=false and excluded from
transition counting; a change is only ever counted between two SUCCESSFUL observations.

Output: JSONL, one line per probe, to --out. Every conclusion is recomputable from that file.

    .venv/bin/python probes/proxy_exit_stability_probe.py --verify
    .venv/bin/python probes/proxy_exit_stability_probe.py --minutes 100 --out var/exit_stability.jsonl
    .venv/bin/python probes/proxy_exit_stability_probe.py --report var/exit_stability.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Reused from libs/proxies/exit_ip.py rather than redefined -- same endpoints production observes with.
ECHO_URLS = ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")
_MAX_IP_LEN = 45


def _looks_like_ip(body: str) -> bool:
    return bool(body) and len(body) <= _MAX_IP_LEN and " " not in body and "<" not in body


def observe(getter, echo: str, timeout: float = 30.0) -> tuple[bool, str, float, str]:
    """(ok, ip_or_error, latency_seconds, echo_used). Never raises."""
    order = [echo] + [u for u in ECHO_URLS if u != echo]
    last = "no attempt"
    for url in order:
        t0 = time.time()
        try:
            body = getter(url, timeout)
            dt = time.time() - t0
            if _looks_like_ip(body):
                return True, body, dt, url
            last = f"non-ip body: {body[:60]!r}"
        except Exception as e:
            dt = time.time() - t0
            last = f"{type(e).__name__}: {e}"[:160]
    return False, last, dt, order[-1]


def _fresh_getter(conn: str):
    import requests
    def get(url: str, timeout: float) -> str:
        r = requests.get(url, proxies={"http": conn, "https": conn}, timeout=timeout,
                         headers={"User-Agent": UA})
        return (r.text or "").strip() if r.status_code == 200 else f"HTTP {r.status_code}"
    return get


def _persistent_getter(conn: str):
    """One pooled requests.Session reused for every probe -- keeps a connection alive between
    samples, which a fresh-connection probe never does."""
    import requests
    s = requests.Session()
    s.proxies.update({"http": conn, "https": conn})
    s.headers.update({"User-Agent": UA})
    def get(url: str, timeout: float) -> str:
        r = s.get(url, timeout=timeout)
        return (r.text or "").strip() if r.status_code == 200 else f"HTTP {r.status_code}"
    return get


def run_session(rec: dict, interval: float, deadline: float, sink, lock) -> None:
    """Probe one proxy session on a fixed cadence until `deadline`, appending a JSONL row per probe."""
    getter = (_persistent_getter if rec["persistent"] else _fresh_getter)(rec["conn"])
    echo = rec["echo"]
    seq = 0
    started = rec["started"]
    next_at = time.time()
    while time.time() < deadline:
        ok, val, latency, echo_used = observe(getter, echo)
        now = time.time()
        row = {
            "arm": rec["arm"], "provider": rec["provider"], "kind": rec["kind"],
            "session_id": rec["session_id"], "endpoint": rec["endpoint"],
            "persistent": rec["persistent"], "interval_s": interval,
            "seq": seq, "started_at": started, "ts": now,
            "elapsed_s": round(now - started, 2),
            "ok": ok, "ip": val if ok else None, "error": None if ok else val,
            "latency_s": round(latency, 3), "echo": echo_used,
        }
        with lock:
            sink.write(json.dumps(row) + "\n")
            sink.flush()
        seq += 1
        next_at += interval
        sleep_for = next_at - time.time()
        if sleep_for > 0:
            time.sleep(min(sleep_for, max(0.0, deadline - time.time())))
        else:
            next_at = time.time()   # fell behind (slow probe) -- resync rather than burst


def build_sessions(args) -> list[dict]:
    from sqlalchemy import select
    from apps.hub.db import SessionLocal
    from libs.db import models as M
    from libs.proxies.lease import conn_url, session_id

    plan = [
        ("active_ws",     "webshare", "rotating", args.n_ws,   args.probe,      False),
        ("active_soax",   "soax",     "rotating", args.n_soax, args.probe,      False),
        ("active_mob",    "soax",     "mobile",   args.n_mob,  args.probe,      False),
        ("lowtraffic_ws", "webshare", "rotating", args.n_ws,   args.lowtraffic, False),
        ("persist_ws",    "webshare", "rotating", args.n_ws,   args.probe,      True),
    ]
    db = SessionLocal()
    out: list[dict] = []
    try:
        seen: set[str] = set()
        for arm, provider, kind, n, interval, persistent in plan:
            if n <= 0:
                continue
            rows = db.scalars(
                select(M.Proxy).where(M.Proxy.provider == provider, M.Proxy.kind == kind,
                                      M.Proxy.active.is_(True)).limit(n * 40)
            ).all()
            picked = 0
            for p in rows:
                if picked >= n:
                    break
                url = conn_url(p)
                sid = session_id(url)
                # Every arm must use DISTINCT sessions: two arms sharing one session id would be
                # measuring the same exit twice and would correlate their results by construction.
                key = f"{provider}/{sid}"
                if not sid or key in seen:
                    continue
                seen.add(key)
                out.append({"arm": arm, "provider": provider, "kind": kind, "session_id": sid,
                            "endpoint": p.endpoint, "conn": url, "interval": interval,
                            "persistent": persistent,
                            "echo": ECHO_URLS[len(out) % len(ECHO_URLS)]})
                picked += 1
            if picked < n:
                print(f"  ! {arm}: only {picked}/{n} distinct sessions available", file=sys.stderr)
    finally:
        db.close()
    return out


def verify(sessions: list[dict]) -> int:
    """Prove the instrument before trusting it (report section 13): the observed IP must actually be
    the proxy's, the session id must be stable, and repeated probes must agree."""
    import requests
    print("== instrument verification ==")
    ok_all = True

    try:
        direct = requests.get(ECHO_URLS[0], timeout=20, headers={"User-Agent": UA}).text.strip()
        print(f"  direct (no proxy) exit: {direct}")
    except Exception as e:
        print(f"  direct probe failed: {e} -- cannot verify proxying")
        return 1

    for rec in sessions[:4]:
        g = _fresh_getter(rec["conn"])
        ok1, ip1, lat1, _ = observe(g, rec["echo"])
        ok2, ip2, lat2, _ = observe(g, rec["echo"])
        tag = f"{rec['arm']}/{rec['provider']}#{rec['session_id']}"
        if not (ok1 and ok2):
            print(f"  {tag}: probe failed ({ip1} / {ip2}) -- not a verification failure, but no signal")
            continue
        via_proxy = ip1 != direct
        consistent = ip1 == ip2
        print(f"  {tag}: ip={ip1} via_proxy={via_proxy} back_to_back_consistent={consistent} "
              f"lat={lat1:.2f}s/{lat2:.2f}s")
        if not via_proxy:
            print("     FAIL: observed IP equals the direct IP -- traffic is NOT going through the proxy")
            ok_all = False

    from libs.proxies.lease import session_id
    for rec in sessions[:4]:
        if session_id(rec["conn"]) != rec["session_id"]:
            print(f"  FAIL: session id unstable for {rec['session_id']}")
            ok_all = False
    print(f"  session ids stable across reads: {'yes' if ok_all else 'NO'}")
    print("== verification", "PASSED" if ok_all else "FAILED", "==")
    return 0 if ok_all else 1


def report(path: str) -> int:
    rows = [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]
    if not rows:
        print("no rows")
        return 1
    by_sess: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        by_sess[(r["arm"], r["provider"], r["kind"], r["session_id"])].append(r)

    print(f"\n=== proxy exit stability -- {len(rows)} probes, {len(by_sess)} sessions ===\n")
    arms: dict[str, list[dict]] = defaultdict(list)

    for key, rs in by_sess.items():
        rs.sort(key=lambda r: r["ts"])
        good = [r for r in rs if r["ok"]]
        fails = len(rs) - len(good)
        ips = [r["ip"] for r in good]
        # A change is only ever counted between two SUCCESSFUL observations.
        changes = [(good[i - 1], good[i]) for i in range(1, len(good)) if good[i]["ip"] != good[i - 1]["ip"]]
        span = (good[-1]["elapsed_s"] - good[0]["elapsed_s"]) / 3600.0 if len(good) >= 2 else 0.0
        if len(good) < 2:
            cls = "unknown"
        elif not changes:
            cls = "stable"
        elif len(changes) == 1:
            cls = "changed_once"
        else:
            cls = "unstable"
        arms[key[0]].append({
            "session_id": key[3], "provider": key[1], "kind": key[2], "class": cls,
            "probes": len(rs), "ok": len(good), "fail": fails,
            "distinct_ips": len(set(ips)), "changes": len(changes),
            "span_h": span,
            "ttfc_s": changes[0][1]["elapsed_s"] if changes else None,
            "transitions": [(c[0]["ip"], c[1]["ip"], round(c[1]["elapsed_s"], 1)) for c in changes],
        })

    for arm, ss in sorted(arms.items()):
        n = len(ss)
        usable = [s for s in ss if s["class"] != "unknown"]
        stable = [s for s in usable if s["class"] == "stable"]
        once = [s for s in usable if s["class"] == "changed_once"]
        unst = [s for s in usable if s["class"] == "unstable"]
        sess_h = sum(s["span_h"] for s in usable)
        chg = sum(s["changes"] for s in usable)
        ttfc = sorted(s["ttfc_s"] for s in usable if s["ttfc_s"] is not None)
        probes = sum(s["probes"] for s in ss)
        okp = sum(s["ok"] for s in ss)
        print(f"-- {arm}  ({ss[0]['provider']}/{ss[0]['kind']}, n={n} sessions)")
        print(f"   probes {probes}, successful {okp} ({100*okp/probes:.1f}%)")
        if not usable:
            print("   INSUFFICIENT: no session had 2+ successful probes\n")
            continue
        print(f"   usable sessions      : {len(usable)}")
        print(f"   stable / once / unstable: {len(stable)} / {len(once)} / {len(unst)}")
        print(f"   session stability rate : {100*len(stable)/len(usable):.1f}%")
        print(f"   observation           : {sess_h:.2f} session-hours")
        print(f"   exit changes          : {chg}"
              + (f"  ->  {chg/sess_h:.2f} changes per session-hour" if sess_h > 0 else ""))
        if ttfc:
            mid = ttfc[len(ttfc)//2]
            print(f"   time-to-first-change  : min {ttfc[0]/60:.1f}m  median {mid/60:.1f}m  "
                  f"max {ttfc[-1]/60:.1f}m   (n={len(ttfc)})")
            print(f"   ttfc distribution (min): {[round(t/60,1) for t in ttfc]}")
        d = defaultdict(int)
        for s in usable:
            d[s["distinct_ips"]] += 1
        print(f"   distinct IPs per session: {dict(sorted(d.items()))}")
        print()

    print("-- endpoint comparison (first vs last successful observation) --")
    for arm, ss in sorted(arms.items()):
        usable = [s for s in ss if s["class"] != "unknown"]
        if not usable:
            continue
        moved = sum(1 for s in usable if s["distinct_ips"] > 1)
        print(f"   {arm:<14} {moved}/{len(usable)} sessions presented more than one exit")
    print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=float, default=100.0)
    ap.add_argument("--probe", type=float, default=60.0, help="active-arm probe interval (s)")
    ap.add_argument("--lowtraffic", type=float, default=1800.0,
                    help="low-traffic arm interval (s); must stay under the Proxy Idle Timeout")
    ap.add_argument("--n-ws", type=int, default=12)
    ap.add_argument("--n-soax", type=int, default=12)
    ap.add_argument("--n-mob", type=int, default=8)
    ap.add_argument("--out", default="var/exit_stability.jsonl")
    ap.add_argument("--verify", action="store_true", help="verify the instrument and exit")
    ap.add_argument("--report", help="re-report from an existing JSONL file and exit")
    args = ap.parse_args()

    if args.report:
        return report(args.report)

    sessions = build_sessions(args)
    if not sessions:
        print("no usable proxy sessions")
        return 1
    if args.verify:
        return verify(sessions)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + args.minutes * 60
    lock = threading.Lock()
    per_arm: dict[str, int] = defaultdict(int)
    for s in sessions:
        per_arm[s["arm"]] += 1
    print(f"exit-stability probe: {len(sessions)} sessions, {args.minutes:.0f} min, out={out}")
    for a, n in sorted(per_arm.items()):
        iv = next(s["interval"] for s in sessions if s["arm"] == a)
        print(f"   {a:<14} n={n:<3} every {iv:.0f}s")

    started = time.time()
    with out.open("w", encoding="utf-8") as sink:
        threads = []
        for rec in sessions:
            rec["started"] = started
            t = threading.Thread(target=run_session,
                                 args=(rec, rec["interval"], deadline, sink, lock), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
    print(f"done in {(time.time()-started)/60:.1f} min -> {out}")
    return report(str(out))


if __name__ == "__main__":
    raise SystemExit(main())

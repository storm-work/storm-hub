"""Login-success % by soax exit GEO (country) -- so you can pick the winning geo before a booking.

Scans var/worker-logs/*.log. Each login attempt is preceded by a
`browser_login: ... proxy=...country-XX-sessionid-...` line carrying the requested geo; the outcome
line that follows (at_home=True / 'tente mais tarde' / 'confirma o reCAPTCHA' / ERR_TUNNEL...) is
attributed to that geo. Prints attempts, successes and the failure breakdown per country, sorted by
success %. Read-only.

Measured basis: login success varies ~8x by exit geo (proxy_geo_login_rates), and exit quality -- not
apply logic -- is what separated our 78-booking day from the 4-booking runs. This is the instrument
for that lever.

    python -m scripts.exit_geo_login                 # all worker logs
    python -m scripts.exit_geo_login --mtime 3600    # only logs modified in the last hour
"""
from __future__ import annotations

import glob
import os
import re
import sys
import time
from collections import defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOGDIR = os.path.join(_ROOT, "var", "worker-logs")

_COUNTRY = re.compile(r"country-([a-z]{2})", re.I)
_BROWSER_LOGIN = re.compile(r"browser_login:.*proxy=")


def _classify(line: str) -> str | None:
    low = line.lower()
    if "at_home=true" in low or "-> logged_in" in low:
        return "success"
    if "tente mais tarde" in low or "unable to complete your request" in low:
        return "throttle"
    if "confirma o recaptcha" in low or "confirma o recaptcha" in low:
        return "recaptcha"
    if any(k in low for k in ("err_tunnel", "err_connection", "err_timed_out", "err_proxy",
                              "chrome-error", "err_empty_response")):
        return "dead_exit"
    if "login-rejected" in low or "auth-redirect" in low or "login_failed" in low:
        return "rejected"
    return None


def main() -> None:
    mtime_s = None
    if "--mtime" in sys.argv:
        mtime_s = float(sys.argv[sys.argv.index("--mtime") + 1])
    now = time.time()

    # per-country outcome tallies
    tally: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    files = 0
    for path in glob.glob(os.path.join(_LOGDIR, "worker-*.log")):
        if mtime_s is not None and (now - os.path.getmtime(path)) > mtime_s:
            continue
        files += 1
        cur = None
        try:
            with open(path, errors="ignore") as fh:
                for line in fh:
                    if _BROWSER_LOGIN.search(line):
                        m = _COUNTRY.search(line)
                        cur = m.group(1).lower() if m else "??"
                        continue
                    if cur is None:
                        continue
                    out = _classify(line)
                    if out is not None:
                        tally[cur][out] += 1
                        tally[cur]["_attempts"] += 1
                        cur = None            # one outcome per browser_login
        except Exception:
            continue

    rows = []
    for geo, t in tally.items():
        att = t["_attempts"] or 1
        rows.append((geo, att, t.get("success", 0), t.get("throttle", 0), t.get("recaptcha", 0),
                     t.get("dead_exit", 0), t.get("rejected", 0), 100.0 * t.get("success", 0) / att))
    rows.sort(key=lambda r: (-r[7], -r[1]))

    print(f"\nLogin success by soax exit geo  ({files} worker logs"
          + (f", last {mtime_s:.0f}s" if mtime_s else "") + ")")
    print("=" * 78)
    print(f"{'geo':4}{'attempts':>10}{'login OK':>10}{'success%':>10}{'throttle':>10}"
          f"{'recaptcha':>11}{'deadexit':>10}")
    print("-" * 78)
    tot = defaultdict(int)
    for geo, att, ok, thr, rec, dead, rej, pct in rows:
        print(f"{geo:4}{att:>10}{ok:>10}{pct:>9.1f}%{thr:>10}{rec:>11}{dead:>10}")
        for k, v in (("att", att), ("ok", ok)):
            tot[k] += v
    print("=" * 78)
    if tot["att"]:
        print(f"overall login success: {100*tot['ok']/tot['att']:.1f}%  ({tot['ok']}/{tot['att']})")
    print("Pick the geo with the highest success% AND enough attempts to trust it.")


if __name__ == "__main__":
    main()

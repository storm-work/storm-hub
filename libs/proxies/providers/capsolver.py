"""Capsolver — mobile / residential rotating pools (proxy.capsolver.com).

CONNECTION STRING (operator-provided 2026-10-14):

    http://{PACKAGE}-{TYPE}-country_{CC}-rotate_{N}m-session_{ID}:{PASSWORD}@proxy.capsolver.com:36583

  * everything that identifies the session lives in the USERNAME; the host:port is a constant for
    every capsolver credential we own, so the session id is the identity (D2).
  * geo is `-country_{CC}`, UPPERCASE (the sample uses `country_NL`), omitted entirely for "any exit".
  * `{TYPE}` is the product: `mobile` or `residential`. It is the ONLY thing that differs between the
    two pools -- see "ONE ACCOUNT" below.
  * `rotate_{N}m` is the sticky-session window in MINUTES (N=120 in the sample): the same session id
    holds one exit for that long. It is OUR value, not a provider requirement, and -- like soax's
    idlettl -- it bounds how long an exit can be held, so keep KEEPALIVE_INTERVAL under it.
  * `session_{ID}` is the session id -- the proxy's identity. Rolling it moves to a new exit; the
    grammar we rewrite is the `session_` slot, never the username around it (D2).

ONE ACCOUNT, MANY TYPES. Unlike SOAX (separate packages AND passwords per pool), capsolver's mobile
and residential share ONE account id (PACKAGE) and ONE password; only the `{TYPE}` segment differs.
So both pools declare the same secrets and differ only in their fixed type segment, carried as a
non-secret option so `mint()` (which only receives the resolved creds, never the pool) can read it.
"""
from __future__ import annotations

import random
import re
import string

from .base import Pool, RotatingProvider, split_url

# The session id rides a `session_<ID>` slot. Capsolver issues ~10-char alphanumeric labels (sample),
# and accepts letters (unlike webshare); 62^10 ~ 8e17 makes independent minters collide negligibly.
_SESSION_RE = re.compile(r"(session_)([A-Za-z0-9]+)")
_MINTED_ID_LEN = 10


def _capsolver_session_id(seen: set[str] | None = None) -> str:
    seen = seen if seen is not None else set()
    while True:
        sid = "".join(random.choices(string.ascii_letters + string.digits, k=_MINTED_ID_LEN))
        if sid not in seen:
            seen.add(sid)
            return sid


class Capsolver(RotatingProvider):
    name = "capsolver"
    endpoint = "proxy.capsolver.com:36583"

    # mobile is the default (first) pool -- the confirmed sample. residential shares the account and
    # grammar, differing only in the TYPE segment. CAPSOLVER_TYPE is baked per pool (its default is
    # the product name) and is not something an operator tunes; CAPSOLVER_ROTATE_MIN is a real knob.
    pools = {
        "mobile": Pool(
            name="mobile", env_prefix="CAPSOLVER_MOBILE", secrets=("PACKAGE", "PASSWORD"),
            options={"CAPSOLVER_TYPE": "mobile", "CAPSOLVER_ROTATE_MIN": 120}),
        "residential": Pool(
            name="residential", env_prefix="CAPSOLVER_RESIDENTIAL", secrets=("PACKAGE", "PASSWORD"),
            options={"CAPSOLVER_TYPE": "residential", "CAPSOLVER_ROTATE_MIN": 120}),
    }

    def new_session_id(self, seen: set[str] | None = None) -> str:
        return _capsolver_session_id(seen)

    def mint(self, creds: dict, *, session_id: str, geo: str | None = None,
             scheme: str = "http") -> str:
        ptype = str(creds.get("CAPSOLVER_TYPE", "mobile")).strip() or "mobile"
        rotate = int(creds.get("CAPSOLVER_ROTATE_MIN", 120) or 120)
        geo_seg = f"-country_{geo.upper()}" if geo else ""
        user = f"{creds['PACKAGE']}-{ptype}{geo_seg}-rotate_{rotate}m-session_{session_id}"
        return f"{scheme}://{user}:{creds['PASSWORD']}@{self.endpoint}"

    def roll(self, url: str) -> str:
        parts = split_url(url)
        if parts is None:
            return url
        scheme, user, sep, pwd, hostport = parts
        if "session_" not in user:
            return url          # not our grammar -- never rewrite a username we do not understand
        user = _SESSION_RE.sub(lambda m: m.group(1) + _capsolver_session_id(), user, count=1)
        return f"{scheme}://{user}{sep}{pwd}@{hostport}"

    def read_id(self, url: str) -> str:
        parts = split_url(url)
        if parts is None:
            return ""
        m = _SESSION_RE.search(parts[1])
        return m.group(2) if m else ""

    def owns(self, url: str) -> bool:
        """Recognise by HOST: every capsolver credential we own points at proxy.capsolver.com, and
        the host is distinctive (soax=proxy.soax.com, webshare=p.webshare.io). Grammar-matching on
        `session_` would be ambiguous nowhere today but the host is unambiguous and cheaper."""
        return bool(url) and "proxy.capsolver.com" in url

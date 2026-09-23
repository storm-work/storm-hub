"""SOAX — residential/rotating and mobile pools.

CONNECTION STRING (confirmed, docs/engineering/PROXY_CONTRACTS.md):

    http://package-{PKG}[-country-{cc}]-sessionid-{ID}-sessionlength-{N}-bindttl-{N}-idlettl-{N}
           -opt-wb:{PASSWORD}@proxy.soax.com:5000

  * geo is `-country-{cc}`, lowercase, and is OMITTED entirely for "any exit" -- never the literal
    string "random" (D2).
  * the three TTLs are OUR values, not provider requirements (PROXY_CONTRACTS.md:225-231). They
    also bound how long a session id can possibly be "established", which is what settled the
    pool-ordering half of Q2: 0 of 42,600 pooled rows had been used inside either window.
  * KEEPALIVE_INTERVAL must stay under `idlettl` or a keeping worker's exit dies between probes.

TWO POOLS, TWO PASSWORDS. `rotating` (package 352438) and `mobile` (package 350716) are separate
purchases with SEPARATE passwords. Collapsing them onto one credential silently authenticates one
pool with the other's password -- which has already happened here for real: 11,000 rows were
generated against a rotating package using the mobile password, and a further 10,000 mobile rows
carried a retired rotating package's password. Hence one Pool per purchase, each with its own
env prefix.
"""
from __future__ import annotations

import re

from .base import Pool, RotatingProvider, new_session_id, split_url

_SESSIONID_RE = re.compile(r"(sessionid-)([A-Za-z0-9]+)")


class Soax(RotatingProvider):
    name = "soax"
    endpoint = "proxy.soax.com:5000"

    # The first entry is the default pool for a blank/absent kind.
    pools = {
        "rotating": Pool(
            name="rotating",
            env_prefix="SOAX_ROTATING",
            secrets=("PACKAGE", "PASSWORD"),
            # Not pool-prefixed: SOAX_IDLETTL describes SOAX's behaviour, not one purchase, so both
            # pools share it. Operator-tunable from the dashboard; see Pool's docstring.
            options={"SOAX_SESSIONLENGTH": 3600, "SOAX_BINDTTL": 600, "SOAX_IDLETTL": 900},
        ),
        "mobile": Pool(
            name="mobile",
            env_prefix="SOAX_MOBILE",
            secrets=("PACKAGE", "PASSWORD"),
            options={"SOAX_SESSIONLENGTH": 3600, "SOAX_BINDTTL": 600, "SOAX_IDLETTL": 900},
        ),
    }

    def mint(self, creds: dict, *, session_id: str, geo: str | None = None,
             scheme: str = "http") -> str:
        geo_seg = f"-country-{geo.lower()}" if geo else ""
        user = (f"package-{creds['PACKAGE']}{geo_seg}-sessionid-{session_id}"
                f"-sessionlength-{int(creds['SOAX_SESSIONLENGTH'])}"
                f"-bindttl-{int(creds['SOAX_BINDTTL'])}"
                f"-idlettl-{int(creds['SOAX_IDLETTL'])}-opt-wb")
        return f"{scheme}://{user}:{creds['PASSWORD']}@{self.endpoint}"

    def roll(self, url: str) -> str:
        parts = split_url(url)
        if parts is None:
            return url
        scheme, user, sep, pwd, hostport = parts
        if "sessionid-" not in user:
            return url          # not our grammar -- never rewrite a username we do not understand
        user = _SESSIONID_RE.sub(lambda m: m.group(1) + new_session_id(), user, count=1)
        return f"{scheme}://{user}{sep}{pwd}@{hostport}"

    def read_id(self, url: str) -> str:
        parts = split_url(url)
        if parts is None:
            return ""
        m = _SESSIONID_RE.search(parts[1])
        return m.group(2) if m else ""

    def owns(self, url: str) -> bool:
        return bool(url) and "sessionid-" in url.rpartition("@")[0]

"""Observe the exit IP a proxy is actually giving us.

WHY THIS EXISTS. Until 2026-08-23 nothing in this codebase ever looked at its own exit address, and
that blindness sits underneath several failures we spent days attributing elsewhere.

The proxies are ROTATING. A SOAX credential pins an exit for at most `sessionlength` (3600s for us),
releases it after `idlettl` of silence, and loses it outright whenever a residential IP simply goes
offline. So the exit under a live worker changes -- MEASURED: 50% of workers that reached `warmed`
subsequently died on the network, median ~25 minutes after warming, and three of those had been
alive longer than sessionlength so their exit had certainly rotated.

The portal binds a session to the IP that established it. When the exit changes underneath us:

    Schedule.jsp stops showing the scheduling markers   -> we read it as "form-state lapsed"
    the submit is refused                               -> we read it as "the portal is declining"
    login returns "Por favor, confirma o reCAPTCHA"     -> token minted on one IP, sent from another

We have been diagnosing one event as three different portal problems, and re-warming or re-logging
in from the NEW ip, which is why those recoveries so rarely worked.

This module does not fix any of that. It makes it VISIBLE, which is the precondition: with the exit
recorded at establishment and re-checked cheaply afterwards, "our IP moved" becomes a fact the
recovery ladder can act on instead of a portal symptom it has to guess at.

Deliberately raw HTTP (primp), never a browser: it must be cheap enough to run on every keepalive,
and it is asking an IP-echo service, not the portal, so the WAF is irrelevant here.
"""
from __future__ import annotations

# Plain-text IP echoes. More than one because this must not become a single point of failure on a
# path that runs every keepalive -- if the first is down we simply try the next.
_ECHO_URLS = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
)

_MAX_IP_LEN = 45          # an IPv6 address in full form


def observe_exit_ip(conn_url: str, timeout: float = 8.0) -> str | None:
    """The exit address this proxy is currently presenting, or None if it could not be determined.

    None is NOT the same as "changed" -- callers must treat it as "unknown" and never act on it as
    evidence of rotation, or a flaky echo service would trigger session rebuilds.
    """
    try:
        import primp
    except ImportError:
        return None
    for url in _ECHO_URLS:
        try:
            c = primp.Client(proxy=conn_url, verify=False, follow_redirects=True, timeout=timeout)
            body = (c.get(url).text or "").strip()
        except Exception:
            continue
        # Guard against an echo that answers with an HTML error page rather than an address.
        if body and len(body) <= _MAX_IP_LEN and " " not in body and "<" not in body:
            return body
    return None


def exit_changed(before: str | None, after: str | None) -> bool:
    """True only when BOTH addresses are known and they differ.

    Biased to False on purpose. A false positive here costs a needless session rebuild -- a fresh
    login, a CAPTCHA solve, and a slice of the sub-account budget that is already the binding
    constraint -- so an unknown reading must never be able to trigger one.
    """
    return bool(before and after and before != after)

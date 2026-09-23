"""Webshare — rotating gateway pool.

CONNECTION STRING (confirmed, docs/engineering/PROXY_CONTRACTS.md):

    http://{PREFIX}[-{CC}]-{ID}:{PASSWORD}@p.webshare.io:80

  * everything that identifies the session lives in the USERNAME; the host:port is a constant for
    every Webshare credential we own. So identifying one of these by endpoint identifies nothing --
    the session id is the identity (D2).
  * geo is `-{CC}-`, UPPERCASE, omitted entirely for "any exit".
  * the trailing segment is the rotation unit. A value pins one exit (sticky); the literal `rotate`
    gives a new exit per request. Our credentials are already sticky, not rotating
    (CONFIRMED_FACTS.md) -- rolling the session id off a dead exit took portal reachability from
    25% to 83%.

THE SESSION ID MUST BE ALL DIGITS. Confirmed live 2026-09-21 against the real gateway with valid
credentials: `Mylist1234-123456` and `Mylist1234-US-6514037027661333` authenticate and pin a sticky
exit (same id -> same IP across requests), while `Mylist1234-<any letters>` is rejected outright
(407/400 Bad Request). The provider refactor had switched this to the shared 16-ALPHANUMERIC SOAX
shape, so EVERY webshare mint 400'd, the worker's login failed on it, and it rolled onto another
provider's exits -- a whole fleet configured for webshare ran on soax with webshare never once
appearing in its logs. So webshare mints a numeric id (see new_session_id below). `_is_session_
segment` still also accepts the legacy 16-alphanumeric shape, so a worker launched on one of the old
broken URLs can be ROLLED onto a valid numeric exit rather than stranded.
"""
from __future__ import annotations

import random
import string

from .base import Pool, RotatingProvider, split_url

_MINTED_ID_LEN = 16


def _is_session_segment(seg: str) -> bool:
    """Is this trailing username segment a session id we may roll?

    Deliberately narrow, and the narrowness is the point (D2 / PROXY_CONTRACTS.md:131). Rolling
    something that is NOT a session id rewrites the username: an arbitrary `u` would become
    `u-<id>`, which authenticates fine against a gateway that parses suffixes and fails outright
    against one that does not. Accepted shapes: all-digits (legacy generator) or exactly our minted
    16-alphanumeric shape.
    """
    return bool(seg) and (seg.isdigit() or (len(seg) == _MINTED_ID_LEN and seg.isalnum()))


class Webshare(RotatingProvider):
    name = "webshare"
    endpoint = "p.webshare.io:80"

    pools = {
        "rotating": Pool(name="rotating", env_prefix="WEBSHARE",
                         secrets=("PREFIX", "PASSWORD")),
    }

    def new_session_id(self, seen: set[str] | None = None) -> str:
        """A fresh ALL-DIGITS session id -- webshare's gateway rejects any letters in the session
        segment (see the module docstring). 16 digits is 10^16 values, so independent minters
        collide far below any rate this system already tolerates, same as the alphanumeric default."""
        seen = seen if seen is not None else set()
        while True:
            sid = "".join(random.choices(string.digits, k=_MINTED_ID_LEN))
            if sid not in seen:
                seen.add(sid)
                return sid

    def mint(self, creds: dict, *, session_id: str, geo: str | None = None,
             scheme: str = "http") -> str:
        geo_seg = f"-{geo.upper()}" if geo else ""
        user = f"{creds['PREFIX']}{geo_seg}-{session_id}"
        return f"{scheme}://{user}:{creds['PASSWORD']}@{self.endpoint}"

    def roll(self, url: str) -> str:
        parts = split_url(url)
        if parts is None:
            return url
        scheme, user, sep, pwd, hostport = parts
        segs = user.split("-")
        if len(segs) > 1 and _is_session_segment(segs[-1]):
            segs[-1] = self.new_session_id()
        elif len(segs) > 1 and segs[-1].lower() == "rotate":
            # An explicitly-rotating credential becomes sticky, deliberately: we need a HELD exit,
            # and "new exit per request" is not something a portal session can survive.
            segs[-1] = self.new_session_id()
        else:
            return url          # unrecognised -- leave it alone rather than corrupt the username
        return f"{scheme}://{'-'.join(segs)}{sep}{pwd}@{hostport}"

    def read_id(self, url: str) -> str:
        parts = split_url(url)
        if parts is None:
            return ""
        segs = parts[1].split("-")
        if len(segs) > 1 and _is_session_segment(segs[-1]):
            return segs[-1]
        if len(segs) > 1 and segs[-1].lower() == "rotate":
            return "rotate"
        return ""

    def owns(self, url: str) -> bool:
        """Recognise by USERNAME GRAMMAR, not by hostname.

        Matching on `webshare.io` looks tighter and is wrong: the credential grammar outlives the
        canonical endpoint. Hold proxies and imported rows legitimately carry this username shape on
        another host (`http://sodade2026-51:pw@hold.example:80` is a real case in the worker tests),
        and the roll_sticky_session this replaced was always grammar-based. Host-matching silently
        made those unrollable -- `for_url` would find no owner, `roll()` would return the URL
        unchanged, and a worker would sit on a dead exit forever rather than rotating off it.

        Exclusive against SOAX in practice: a SOAX username ends `-opt-wb`, which is not a session
        segment, and SOAX is consulted first anyway.
        """
        parts = split_url(url)
        if parts is None:
            return False
        segs = parts[1].split("-")
        return len(segs) > 1 and (_is_session_segment(segs[-1]) or segs[-1].lower() == "rotate")

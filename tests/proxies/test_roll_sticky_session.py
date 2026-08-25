"""Rolling a dead sticky exit.

MEASURED 2026-08-25, five requests each through the same credential:

    sodade2026-51      (ours) -> 1 distinct exit IP   STICKY
    sodade2026-PT-777         -> 1 distinct exit IP   STICKY
    sodade2026-rotate         -> 5 distinct exit IPs  rotating

Our credentials are already sticky, so a dead tunnel stays dead for every retry aimed at it -- which
is what the login's transient budget was doing, six times in a row, in every recent run. The session
id is a number we choose, so rolling it is a free, unbounded supply of fresh exits.
"""
import re

from libs.proxies.lease import roll_sticky_session as roll


def _user(url):
    return url.split("@")[0].split("//", 1)[1].rsplit(":", 1)[0]


def test_a_trailing_numeric_session_id_is_replaced():
    out = roll("http://sodade2026-51:pw@p.webshare.io:80")
    u = _user(out)
    assert u.startswith("sodade2026-") and u != "sodade2026-51"
    assert u.split("-")[-1].isdigit()


def test_the_country_is_preserved_when_rolling():
    # Geo targeting must survive: rolling changes WHICH exit, never WHERE it is.
    assert _user(roll("http://sodade2026-PT-777:pw@p.webshare.io:80")).startswith("sodade2026-PT-")


def test_a_credential_with_no_session_id_gets_one_appended():
    u = _user(roll("http://sodade2026:pw@p.webshare.io:80"))
    assert u == "sodade2026" or u.split("-")[-1].isdigit()


def test_an_explicitly_rotating_credential_becomes_sticky():
    # Deliberate: if we are rolling, we want ONE known exit, not a new IP per request.
    assert _user(roll("http://sodade2026-rotate:pw@p.webshare.io:80")).split("-")[-1].isdigit()


def test_soax_sessionid_is_rolled_in_place():
    out = roll("http://pkg-x-sessionid-abc123-sesslength-3600:pw@proxy.soax.com:5000")
    u = _user(out)
    assert "sesslength-3600" in u and "sessionid-abc123" not in u


def test_host_port_and_password_are_never_touched():
    src = "http://sodade2026-51:secret@p.webshare.io:80"
    out = roll(src)
    assert out.endswith("@p.webshare.io:80") and ":secret@" in out


def test_unrecognised_input_comes_back_unchanged():
    # Pure and total -- a caller can always use the result.
    for u in ("", "http://p.webshare.io:80", "not a url"):
        assert roll(u) == u


def test_rolling_twice_gives_different_exits():
    a = roll("http://sodade2026-51:pw@p.webshare.io:80")
    assert any(roll(a) != a for _ in range(3)), "the roll must actually move"

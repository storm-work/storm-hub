"""Minting a rotating credential on demand -- the replacement for leasing a pre-generated row.

The grammar assertions here deliberately duplicate tests/proxies/test_credentials.py's golden
samples rather than importing them: those pin what the GENERATOR produced, these pin what the
RUNTIME produces, and the whole point of this change is that the two must agree. If they ever
diverge, a worker would be minting a credential the provider has never seen.
"""
import pytest

from libs.proxies.lease import roll_sticky_session, session_id
from libs.proxies.rotating import RotatingPoolError, mint_rotating_url

# Credentials are ENVIRONMENT values, not settings (2026-09-16), and these are passed as `env=` so
# the suite is hermetic. That is not a style choice: apps/hub/config.py loads .env at import, so
# anything importing the Hub has the LIVE production credentials in os.environ -- an earlier version
# of these tests resolved them and printed a real password in a pytest failure message.
ENV = {
    "SOAX_ROTATING_PACKAGE": "352438", "SOAX_ROTATING_PASSWORD": "rot-pw",
    "SOAX_MOBILE_PACKAGE": "350716", "SOAX_MOBILE_PASSWORD": "mob-pw",
    "WEBSHARE_PREFIX": "sodade2026", "WEBSHARE_PASSWORD": "ws-pw",
    "CAPSOLVER_MOBILE_PACKAGE": "ACCT16HEX", "CAPSOLVER_MOBILE_PASSWORD": "cap-pw",
    "CAPSOLVER_RESIDENTIAL_PACKAGE": "ACCT16HEX", "CAPSOLVER_RESIDENTIAL_PASSWORD": "cap-pw",
}
SETTINGS: dict = {}          # dashboard knobs only; never credentials


def _user(url):
    return url.split("@")[0].split("//", 1)[1].rsplit(":", 1)[0]


# ── grammar ───────────────────────────────────────────────────────────────────────────────────
def test_soax_rotating_grammar_matches_the_generator():
    u = mint_rotating_url("soax", "rotating", env=ENV, settings=SETTINGS)
    assert u.startswith("http://package-352438-sessionid-")
    assert "-sessionlength-3600-bindttl-600-idlettl-900-opt-wb:rot-pw@proxy.soax.com:5000" in u
    assert "-country-" not in u          # no geo asked for -> segment omitted entirely (D2)


def test_soax_mobile_uses_its_own_package_AND_its_own_password():
    """scripts/gen_proxy_pool.py:44 records the trap verbatim: 'the new rotating package has its
    OWN password, different from SOAX_PASSWORD (which is still mobile's)'. Resolving them from one
    shared setting would silently authenticate mobile with the rotating package's password."""
    u = mint_rotating_url("soax", "mobile", env=ENV, settings=SETTINGS)
    assert "package-350716-" in u and ":mob-pw@" in u
    assert "352438" not in u and "rot-pw" not in u


def test_webshare_grammar_and_url_ordering():
    u = mint_rotating_url("webshare", "rotating", env=ENV, settings=SETTINGS)
    assert u.startswith("http://sodade2026-") and u.endswith(":ws-pw@p.webshare.io:80")


def test_webshare_mints_an_all_digit_session_id():
    """Webshare's gateway REJECTS letters in the session segment (live 2026-09-21: a 16-alphanumeric
    id -> 400/407). Regression guard for the refactor that switched it to the shared alphanumeric
    shape, which made every webshare mint 400 and silently drove the fleet onto soax exits."""
    from libs.proxies.rotating import rotating_session_id
    for _ in range(200):
        u = mint_rotating_url("webshare", "rotating", env=ENV, settings=SETTINGS)
        sid = rotating_session_id(u)
        assert sid.isdigit() and len(sid) == 16, f"webshare session id must be all-digits, got {sid!r}"
    # a rolled webshare url must also be all-digits (roll used the alphanumeric generator before)
    from libs.proxies.rotating import roll_rotating_url
    rolled = roll_rotating_url(mint_rotating_url("webshare", "rotating", env=ENV, settings=SETTINGS))
    assert rotating_session_id(rolled).isdigit()


def test_capsolver_grammar_matches_the_operator_sample():
    """Operator-provided 2026-10-14:
       {PKG}-{TYPE}-country_{CC}-rotate_{N}m-session_{ID}:{PW}@proxy.capsolver.com:36583"""
    u = mint_rotating_url("capsolver", "mobile", env=ENV, settings=SETTINGS, geo="NL")
    assert u.startswith("http://ACCT16HEX-mobile-country_NL-rotate_120m-session_")
    assert u.endswith(":cap-pw@proxy.capsolver.com:36583")
    # no geo -> the country_ segment is omitted entirely (D2)
    assert "country_" not in mint_rotating_url("capsolver", "mobile", env=ENV, settings=SETTINGS)
    # residential pool shares the account but carries its own TYPE segment
    r = mint_rotating_url("capsolver", "residential", env=ENV, settings=SETTINGS, geo="de")
    assert "-residential-country_DE-" in r


def test_capsolver_rolls_the_session_slot_only():
    from libs.proxies.rotating import roll_rotating_url, rotating_session_id, reseat_geo
    u = mint_rotating_url("capsolver", "mobile", env=ENV, settings=SETTINGS, geo="NL")
    sid = rotating_session_id(u)
    rolled = roll_rotating_url(u)
    assert rotating_session_id(rolled) != sid
    assert rolled.replace(rotating_session_id(rolled), "X") == u.replace(sid, "X")  # only the id moved
    # reseat uppercases and replaces the country_ segment
    assert "-country_IT-" in reseat_geo(u, "it")
    assert "country_" not in reseat_geo(u, None)
    # insert a country onto a no-geo url (before -rotate_)
    bare = mint_rotating_url("capsolver", "mobile", env=ENV, settings=SETTINGS)
    assert "-country_FR-rotate_120m-" in reseat_geo(bare, "fr")


@pytest.mark.parametrize("provider,kind,expect", [
    ("soax", "rotating", "-country-de-"),
    ("soax", "mobile", "-country-de-"),
    ("webshare", "rotating", "sodade2026-DE-"),
    ("capsolver", "mobile", "-country_DE-"),
    ("capsolver", "residential", "-country_DE-"),
])
def test_geo_is_injected_into_the_credential_per_provider(provider, kind, expect):
    """Geo has to live in the credential: it is the only place either provider accepts it, and a
    row-column filter is what this change removes."""
    assert expect in mint_rotating_url(provider, kind, env=ENV, settings=SETTINGS, geo="DE")


# ── uniqueness: the operator's stated requirement ────────────────────────────────────────────
def test_minted_session_ids_are_unique_across_a_large_batch():
    """"ensuring every rotation will gen unique session id throughout the hub" -- met without a
    registry by drawing from 62^16 rather than the old randint(100000, 999999)'s 900k."""
    ids = {session_id(mint_rotating_url("soax", "rotating", env=ENV, settings=SETTINGS)) for _ in range(5000)}
    assert len(ids) == 5000
    assert all(len(i) == 16 and i.isalnum() for i in ids)


def test_every_provider_round_trips_mint_then_identify_then_roll():
    """The bug this test exists for: webshare minted a 16-alnum id while session_id/roll only
    recognised all-digit ones, so a freshly-minted webshare URL reported NO session id and could
    not be rolled -- a worker would have been stuck on a dead exit with no way off it."""
    for provider, kind in (("soax", "rotating"), ("soax", "mobile"), ("webshare", "rotating")):
        url = mint_rotating_url(provider, kind, env=ENV, settings=SETTINGS)
        sid = session_id(url)
        assert sid, f"{provider}/{kind}: minted a credential its own identifier cannot read"
        rolled = roll_sticky_session(url)
        assert rolled != url, f"{provider}/{kind}: minted a credential that cannot be rolled"
        assert session_id(rolled) not in ("", sid)


def test_rolling_preserves_everything_except_the_session_id():
    url = mint_rotating_url("soax", "rotating", env=ENV, settings=SETTINGS, geo="IT")
    rolled = roll_sticky_session(url)
    for keep in ("package-352438", "-country-it-", "sessionlength-3600", "bindttl-600",
                 "idlettl-900", "rot-pw@proxy.soax.com:5000"):
        assert keep in rolled, keep


# ── configuration failures are loud ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("provider,kind,missing", [
    ("soax", "rotating", "SOAX_ROTATING_PASSWORD"),
    ("soax", "mobile", "SOAX_MOBILE_PACKAGE"),
    ("webshare", "rotating", "WEBSHARE_PREFIX"),
])
def test_an_unconfigured_pool_raises_and_names_the_setting(provider, kind, missing):
    """Silently minting a half-built credential would authenticate as nobody and read as a portal
    failure. The error has to name the ENV VAR an operator must go and fill in.

    The broken map is passed as `env=`, and that is the whole point of the parameter. Passing it as
    `settings=` left `env` defaulting to os.environ -- and because apps/hub/config.py loads .env at
    import, this test only passed while the real .env happened NOT to carry these keys. The moment
    credentials were added to .env (2026-09-16) all three cases flipped to failing, having been
    accidentally-correct rather than hermetic for their whole life."""
    broken = {k: v for k, v in ENV.items() if k != missing}
    with pytest.raises(RotatingPoolError) as e:
        mint_rotating_url(provider, kind, env=broken)
    assert missing in str(e.value)


def test_an_unknown_provider_raises():
    with pytest.raises(RotatingPoolError):
        mint_rotating_url("nordvpn", "rotating", env=ENV, settings=SETTINGS)


def test_soax_ttls_are_configurable_but_default_to_the_generators_values():
    assert "-sessionlength-3600-bindttl-600-idlettl-900-" in mint_rotating_url(
        "soax", "rotating", env=ENV, settings=SETTINGS)
    tuned = {**SETTINGS, "SOAX_SESSIONLENGTH": 600, "SOAX_BINDTTL": 120, "SOAX_IDLETTL": 300}
    assert "-sessionlength-600-bindttl-120-idlettl-300-" in mint_rotating_url(
        "soax", "rotating", env=ENV, settings=tuned)


# ── reseat_geo: vary the exit country on a roll, so a worker is never bound to one geo (2026-10-01) ──
from libs.proxies.rotating import reseat_geo

_SOAX = ("http://package-352438-country-de-sessionid-ABC123-sessionlength-3600-bindttl-600-"
         "idlettl-900-opt-wb:pw@proxy.soax.com:5000")


def test_reseat_geo_replaces_the_soax_country_keeping_session_and_creds():
    out = reseat_geo(_SOAX, "IT")
    assert "-country-it-" in out and "-country-de-" not in out
    assert "sessionid-ABC123-" in out              # session id preserved
    assert out.endswith("opt-wb:pw@proxy.soax.com:5000")   # creds preserved


def test_reseat_geo_inserts_a_country_when_absent():
    bare = _SOAX.replace("-country-de", "")
    assert "-country-it-" in reseat_geo(bare, "it")


def test_reseat_geo_none_drops_the_country_any_exit():
    assert "-country-" not in reseat_geo(_SOAX, None)


def test_reseat_geo_leaves_webshare_and_unknown_urls_unchanged():
    ws = "http://sodade2026-DE-17:pw@p.webshare.io:80"
    assert reseat_geo(ws, "IT") == ws               # webshare: minted geo stays, not string-surgered
    assert reseat_geo("", "IT") == ""

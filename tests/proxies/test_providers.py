"""The per-provider module contract (2026-09-16).

These test the LAYER, not any one provider: every registered provider must satisfy the same
contract, so adding a third module is checked automatically rather than needing its own suite.
Provider-specific grammar (exact strings, the golden samples) stays in test_rotating.py.
"""
import pytest

from libs.proxies import providers
from libs.proxies.providers import ProviderError, new_session_id

ENV = {
    "SOAX_ROTATING_PACKAGE": "352438", "SOAX_ROTATING_PASSWORD": "rot-pw",
    "SOAX_MOBILE_PACKAGE": "350716", "SOAX_MOBILE_PASSWORD": "mob-pw",
    "WEBSHARE_PREFIX": "sodade2026", "WEBSHARE_PASSWORD": "ws-pw",
    "CAPSOLVER_MOBILE_PACKAGE": "ACCT16HEX", "CAPSOLVER_MOBILE_PASSWORD": "cap-pw",
    "CAPSOLVER_RESIDENTIAL_PACKAGE": "ACCT16HEX", "CAPSOLVER_RESIDENTIAL_PASSWORD": "cap-pw",
}

ALL_POOLS = [(p, k) for p in providers.names() for k in providers.get(p).pools]


@pytest.mark.parametrize("provider,kind", ALL_POOLS)
def test_every_pool_mints_rolls_and_reads_back(provider, kind):
    """The three grammar methods must compose: mint -> read_id gives an id, roll changes it, and
    read_id still works on the rolled URL. A provider that minted a URL its own roll() could not
    recognise would strand every worker on it at the first dead exit."""
    prov = providers.get(provider)
    creds = providers.resolve_credentials(provider, kind, env=ENV)
    url = prov.mint(creds, session_id=new_session_id())

    first = prov.read_id(url)
    assert first, f"{provider}/{kind}: minted a URL with no readable session id"

    rolled = prov.roll(url)
    assert rolled != url, f"{provider}/{kind}: roll() did not change the URL"
    assert prov.read_id(rolled) not in ("", first), f"{provider}/{kind}: roll() did not change the id"
    # Only the session id moves -- host, password and scheme are untouched.
    assert rolled.rsplit("@", 1)[1] == url.rsplit("@", 1)[1]
    assert rolled.rsplit(":", 1)[-1].split("@")[0] == url.rsplit(":", 1)[-1].split("@")[0]


@pytest.mark.parametrize("provider,kind", ALL_POOLS)
def test_a_minted_url_is_routed_back_to_its_own_provider(provider, kind):
    """for_url() is what lets a worker roll its exit without knowing which provider it is on. If
    two providers both claimed a URL, or neither did, roll/read_id would dispatch to the wrong
    grammar -- which is the failure the old hard-coded if/else in lease.py could not even express."""
    prov = providers.get(provider)
    url = prov.mint(providers.resolve_credentials(provider, kind, env=ENV),
                    session_id=new_session_id())
    owners = [p for p in map(providers.get, providers.names()) if p.owns(url)]
    assert owners == [prov], f"{provider}/{kind}: claimed by {[p.name for p in owners]}"


@pytest.mark.parametrize("provider,kind", ALL_POOLS)
def test_an_unrecognised_credential_is_returned_unchanged(provider, kind):
    """D2 / PROXY_CONTRACTS.md:131 -- never invent a session id. Rolling a username we do not
    understand rewrites it: `u` would become `u-<id>`, which authenticates fine against a gateway
    that parses suffixes and fails outright against one that does not. Unchanged lets the caller
    fall back to something real."""
    prov = providers.get(provider)
    for junk in ("", "not-a-url", "http://u:p@example.com:1", "http://@host:1"):
        assert prov.roll(junk) == junk
        assert prov.read_id(junk) == "" or junk.startswith("http://u")


@pytest.mark.parametrize("provider,kind", ALL_POOLS)
def test_a_missing_secret_names_the_exact_env_var(provider, kind):
    """The launch endpoint surfaces this message verbatim as a 409, so it has to tell the operator
    which variable to set. 'could not build credential' would send them reading source."""
    with pytest.raises(ProviderError) as e:
        providers.resolve_credentials(provider, kind, env={})
    pool = providers.get(provider).pool(kind)
    for fieldname in pool.secrets:
        assert f"{pool.env_prefix}_{fieldname}" in str(e.value)


def test_secrets_never_come_from_settings():
    """THE security property of the 2026-09-16 split. A credential must not be suppliable by a DB
    row or a dashboard field -- only by the environment. Passing every secret via `settings` must
    still fail exactly as if nothing were configured."""
    with pytest.raises(ProviderError):
        providers.resolve_credentials("soax", "rotating", settings=dict(ENV), env={})


def test_options_come_from_settings_and_fall_back_to_defaults():
    """The other half: non-secret knobs ARE operator-tunable from the dashboard."""
    creds = providers.resolve_credentials("soax", "rotating", env=ENV)
    assert int(creds["SOAX_IDLETTL"]) == 900          # declared default
    tuned = providers.resolve_credentials("soax", "rotating", env=ENV,
                                          settings={"SOAX_IDLETTL": 300})
    assert int(tuned["SOAX_IDLETTL"]) == 300


def test_an_unknown_provider_or_pool_says_what_is_known():
    with pytest.raises(ProviderError) as e:
        providers.get("nordvpn")
    assert "soax" in str(e.value) and "webshare" in str(e.value)
    with pytest.raises(ProviderError) as e:
        providers.get("soax").pool("gold-tier")
    assert "rotating" in str(e.value) and "mobile" in str(e.value)


def test_a_blank_kind_resolves_to_the_providers_default_pool():
    """PROXY_KIND is legitimately blank ('no filter') on most bookings, so every provider needs a
    defined answer for it rather than a KeyError at launch."""
    for name in providers.names():
        assert providers.get(name).pool(None) is providers.get(name).pool("")


def test_session_ids_are_unique_across_a_large_batch():
    """The operator's stated requirement -- 'every rotation will gen unique session id throughout
    the hub' -- met without a registry. 62^16 makes coordination unnecessary, and a registry would
    reintroduce the shared-state round-trip self-rotation exists to remove."""
    ids = {new_session_id() for _ in range(20_000)}
    assert len(ids) == 20_000
    assert all(len(i) == 16 and i.isalnum() for i in ids)

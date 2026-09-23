"""Suite-wide test isolation.

THE PROBLEM THIS EXISTS FOR. `apps/hub/config.py` loads `.env` at import, so the moment anything
imports the Hub, the developer's LIVE PRODUCTION credentials are sitting in `os.environ` for the
rest of the session. Two concrete failures came from that, both real:

  * a test asserting on a minted proxy URL resolved the real webshare password and printed it
    verbatim in a pytest failure message;
  * `test_an_unconfigured_pool_raises_and_names_the_setting` passed for its whole life only because
    the real `.env` happened NOT to carry `SOAX_ROTATING_*`. On 2026-09-16 those keys were added to
    `.env` and all three parametrised cases flipped to failing -- the test had been
    accidentally-correct, never hermetic.

So the credential environment is scrubbed for every test. A test that needs credentials sets them
explicitly (`monkeypatch.setenv`, or `env=` on the provider API), which means the suite's behaviour
no longer depends on what happens to be in the .env of whoever runs it.
"""
import pytest

# Every secret any rotating-proxy provider reads. Derived from the registry rather than hardcoded,
# so a newly added provider is covered automatically instead of silently re-opening the hole.
def _credential_env_keys() -> set[str]:
    try:
        from libs.proxies import providers
    except Exception:        # pragma: no cover - providers must not be able to break collection
        return set()
    keys = set()
    for name in providers.names():
        for pool in providers.get(name).pools.values():
            keys.update(f"{pool.env_prefix}_{field}" for field in pool.secrets)
    return keys


@pytest.fixture(autouse=True)
def _scrub_ambient_credentials(monkeypatch):
    for key in _credential_env_keys():
        monkeypatch.delenv(key, raising=False)
    # Legacy name still read by scripts/gen_proxy_pool.py; same reasoning.
    monkeypatch.delenv("SOAX_PASSWORD", raising=False)

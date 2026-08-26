"""libs/proxies/credentials.py -- Phase 3 (D32) relocation of scripts/gen_proxy_pool.py's inline
credential construction. Proves grammar parity: every builder must still produce a string that
parses via apps/hub/importers.py's real parse_proxies() (not just a regex assumption), and matches
the confirmed contract in docs/engineering/PROXY_CONTRACTS.md."""
import re

import pytest

from apps.hub.importers import parse_geo, parse_proxies
from libs.proxies.credentials import (
    build_soax_credential, build_webshare_credential, soax_session_id,
)


# ── SOAX ─────────────────────────────────────────────────────────────────────────────────────
def test_soax_credential_with_country():
    cred = build_soax_credential(package="335959", password="p@ss", session_id="ABCDEF0123456789",
                                 sessionlength=3600, bindttl=600, idlettl=900, country="DE")
    assert cred == (
        "package-335959-country-de-sessionid-ABCDEF0123456789-"
        "sessionlength-3600-bindttl-600-idlettl-900-opt-wb:p@ss@proxy.soax.com:5000"
    )


def test_soax_credential_country_omitted_not_a_literal_random():
    """PROXY_CONTRACTS.md / D2: "random" means the -country-{cc} segment is physically absent, never
    the literal string "random" appearing anywhere in the credential."""
    cred = build_soax_credential(package="335959", password="p", session_id="x" * 16,
                                 sessionlength=3600, bindttl=600, idlettl=900, country=None)
    assert "-country-" not in cred
    assert "random" not in cred.lower()
    assert cred.startswith("package-335959-sessionid-")


def test_soax_credential_required_ttl_parameters_present():
    cred = build_soax_credential(package="1", password="p", session_id="s" * 16,
                                 sessionlength=1234, bindttl=56, idlettl=789)
    assert "sessionlength-1234" in cred
    assert "bindttl-56" in cred
    assert "idlettl-789" in cred


def test_soax_credential_opt_wb_present():
    """opt-wb is not currently optional in this codebase's confirmed-working grammar (every
    generated/imported soax row carries it) -- this pins that, not a hypothetical toggle."""
    cred = build_soax_credential(package="1", password="p", session_id="s" * 16,
                                 sessionlength=3600, bindttl=600, idlettl=900)
    assert "-opt-wb:" in cred


def test_soax_mobile_and_residential_share_one_grammar():
    """The distinction between soax-res and soax-mobile is external (which package number, and the
    DB row's own `kind`/`resource_type`) -- there is no separate credential FORMAT for "mobile"
    (confirmed: PROXY_CONTRACTS.md documents one soax grammar, and the residential/mobile pools in
    production both use it). This test exists so nobody "fixes" that into two builders without new
    evidence."""
    mobile = build_soax_credential(package="350716", password="p", session_id="m" * 16,
                                   sessionlength=3600, bindttl=600, idlettl=900, country="it")
    residential = build_soax_credential(package="335959", password="p", session_id="r" * 16,
                                        sessionlength=3600, bindttl=600, idlettl=900, country="it")
    # Same shape, different package -- not a different template.
    assert mobile.replace("350716", "335959").replace("m" * 16, "r" * 16) == residential


def test_soax_session_id_is_16_chars_alnum():
    sid = soax_session_id()
    assert len(sid) == 16
    assert sid.isalnum()


def test_soax_session_id_uniqueness_within_a_batch():
    seen: set[str] = set()
    ids = {soax_session_id(seen) for _ in range(500)}
    assert len(ids) == 500          # no collisions across 500 draws sharing one `seen` set
    assert ids == seen


def test_soax_session_id_never_reuses_a_pre_seeded_value(monkeypatch):
    """If the RNG were ever to repeat, the function must retry rather than hand back a duplicate --
    proves the retry-on-collision loop, not just that random() is unlikely to collide."""
    import libs.proxies.credentials as creds_mod

    calls = iter(["A" * 16, "A" * 16, "B" * 16])  # first draw collides with a pre-seeded value
    monkeypatch.setattr(creds_mod.random, "choices", lambda *a, **k: list(next(calls)))
    sid = soax_session_id(seen={"A" * 16})
    assert sid == "B" * 16


# ── Webshare ─────────────────────────────────────────────────────────────────────────────────
def test_webshare_credential_with_country():
    cred = build_webshare_credential(prefix="sodade2026", password="sempre2026", session_id=544147,
                                     country="af")
    assert cred == "p.webshare.io:80:sodade2026-AF-544147:sempre2026"


def test_webshare_credential_without_country():
    cred = build_webshare_credential(prefix="sodade2026", password="sempre2026", session_id=51)
    assert cred == "p.webshare.io:80:sodade2026-51:sempre2026"
    assert "random" not in cred.lower()


def test_webshare_session_id_accepts_the_rotate_literal_without_special_casing():
    """PROXY_CONTRACTS.md, confirmed 2026-08-25: a numeric suffix is sticky; the literal suffix
    "rotate" gives a new IP per request. The builder does not need to know this -- it is a pure
    string-formatting fact the CALLER decides by what session_id it passes."""
    cred = build_webshare_credential(prefix="sodade2026", password="p", session_id="rotate")
    assert cred == "p.webshare.io:80:sodade2026-rotate:p"


# ── Both providers must still parse through the real importer, and geo must still be derived
#    correctly -- proves this isn't just format-string parity but functional parity. ────────────
@pytest.mark.parametrize("cred", [
    build_soax_credential(package="335959", password="p", session_id="s" * 16, sessionlength=3600,
                          bindttl=600, idlettl=900, country="DE"),
    build_soax_credential(package="335959", password="p", session_id="s" * 16, sessionlength=3600,
                          bindttl=600, idlettl=900, country=None),
    build_webshare_credential(prefix="sodade2026", password="p", session_id=1, country="DE"),
    build_webshare_credential(prefix="sodade2026", password="p", session_id=1),
])
def test_builder_output_parses_via_the_real_importer(cred):
    parsed, errors = parse_proxies(cred)
    assert errors == []
    assert len(parsed) == 1
    assert parsed[0].credentials_ref  # a user:pass pair was extracted, not None


def test_builder_output_geo_detection_matches_country_argument():
    cred = build_soax_credential(package="335959", password="p", session_id="s" * 16,
                                 sessionlength=3600, bindttl=600, idlettl=900, country="DE")
    assert parse_geo(cred) == "DE"
    cred_no_geo = build_soax_credential(package="335959", password="p", session_id="s" * 16,
                                        sessionlength=3600, bindttl=600, idlettl=900, country=None)
    assert parse_geo(cred_no_geo) is None


# ── Golden sample: pins the exact pre-relocation output shape, so a future refactor of the
#    builders can't silently drift the grammar without this test noticing. ──────────────────────
def test_soax_golden_sample_matches_pre_relocation_format():
    cred = re.sub(r"sessionid-\w{16}", "sessionid-XXXXXXXXXXXXXXXX",
                  build_soax_credential(package="335959", password="ulUve74DtH3Zv2H0",
                                        session_id=soax_session_id(), sessionlength=3600,
                                        bindttl=600, idlettl=900, country="pt"))
    assert cred == (
        "package-335959-country-pt-sessionid-XXXXXXXXXXXXXXXX-"
        "sessionlength-3600-bindttl-600-idlettl-900-opt-wb:ulUve74DtH3Zv2H0@proxy.soax.com:5000"
    )


def test_webshare_golden_sample_matches_pre_relocation_format():
    cred = build_webshare_credential(prefix="sodade2026", password="sempre2026", session_id=544147,
                                     country="af")
    assert cred == "p.webshare.io:80:sodade2026-AF-544147:sempre2026"

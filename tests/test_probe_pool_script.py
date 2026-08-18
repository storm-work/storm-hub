"""probe_pool.py -- the pure parts. No network: the probe itself is live-only by nature, but the
parsing/geo/threshold logic around it is exactly where this script broke while being written (it
assumed parse_proxies returned dicts; it returns a ParsedProxy dataclass), and that is worth a
guard because the failure mode is silent -- a misparse just reports an empty pool."""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "probe_pool", Path(__file__).resolve().parent.parent / "scripts" / "probe_pool.py")
probe_pool = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe_pool)


def test_geo_is_read_from_both_provider_credential_formats():
    """Both providers encode the country, in different shapes, and the script has to read both or
    it silently buckets a whole pool under '??'."""
    soax = ("http://package-335959-country-pt-sessionid-ABC-sessionlength-3600"
            "-bindttl-600-opt-wb:pw@proxy.soax.com:5000")
    webshare = "http://sodade2026-DE-17:pw@p.webshare.io:80"
    assert probe_pool._geo_of(soax) == "PT"
    assert probe_pool._geo_of(webshare) == "DE"
    assert probe_pool._geo_of("http://user:pw@host:80") == "??"


def test_the_real_pool_line_formats_parse_through_the_hubs_own_parser():
    """A line that probes here must be a line that imports -- so this uses the Hub's parser, not a
    private regex. Guards the mistake this script shipped with: parse_proxies returns ParsedProxy
    dataclasses, and .get() on them raises rather than returning a default."""
    from apps.hub.importers import parse_proxies

    text = "\n".join([
        "package-350716-sessionid-idA87-sessionlength-3600-bindttl-600-opt-wb:pw@proxy.soax.com:5000",
        "package-335959-country-pt-sessionid-XYZ-sessionlength-3600-bindttl-600-opt-wb:pw@proxy.soax.com:5000",
        "p.webshare.io:80:sodade2026-IT-3:pw",
        "# a comment",
        "",
    ])
    parsed, errors = parse_proxies(text)
    assert len(parsed) == 3 and not errors
    assert all(hasattr(p, "endpoint") and hasattr(p, "credentials_ref") for p in parsed)


def test_a_short_200_is_not_counted_as_the_portal():
    """The threshold is the point of the script: a 200 carrying an interstitial or a provider
    notice is not the site. The real home page is ~59KB."""
    assert probe_pool.MIN_REAL_BYTES >= 40_000

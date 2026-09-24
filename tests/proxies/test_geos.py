"""libs/proxies/geos.py — per-provider exit-country tables + validation for the worker geo dropdown."""
from libs.proxies import geos


def test_normalize_is_trim_upper_and_blank_is_empty():
    assert geos.normalize_geo(" pt ") == "PT"
    assert geos.normalize_geo("De") == "DE"
    assert geos.normalize_geo("") == ""
    assert geos.normalize_geo(None) == ""


def test_blank_code_is_always_valid_it_means_unbind():
    assert geos.is_valid_geo("soax", "") is True
    assert geos.is_valid_geo("soax", None) is True


def test_validation_is_permissive_while_a_provider_list_is_empty(monkeypatch):
    # Empty table (the shipped state until the operator pastes the list) -> any non-empty code passes,
    # so the endpoint/dropdown work end to end before the data lands.
    monkeypatch.setattr(geos, "SOAX_GEOS", {})
    monkeypatch.setitem(geos._BY_PROVIDER, "soax", {})
    assert geos.is_valid_geo("soax", "PT") is True


def test_validation_is_strict_once_a_provider_list_is_populated(monkeypatch):
    monkeypatch.setitem(geos._BY_PROVIDER, "soax", {"PT": "Portugal", "DE": "Germany"})
    assert geos.is_valid_geo("soax", "pt") is True        # case-insensitive
    assert geos.is_valid_geo("soax", "DE") is True
    assert geos.is_valid_geo("soax", "US") is False        # not in the list


def test_supported_geos_unknown_provider_is_empty():
    assert geos.supported_geos("nope") == {}
    assert geos.supported_geos(None) == {}

from types import SimpleNamespace

from apps.hub import donut_profiles as dp
from libs.engine.vendor import donut


def _subs(*names):
    return [SimpleNamespace(username=n) for n in names]


def _with_profiles(monkeypatch, have):
    monkeypatch.setattr(dp, "existing_profile_names",
                        lambda g: None if have is None else frozenset(donut.profile_name(n) for n in have))


def test_keeps_only_sub_accounts_that_have_a_profile(monkeypatch):
    _with_profiles(monkeypatch, ["a", "c"])
    out = dp.keep_with_profile(_subs("a", "b", "c"), {"BROWSER_BACKEND": "donut"})
    assert [s.username for s in out] == ["a", "c"]


def test_setting_off_or_other_backend_or_unreachable_donut_does_not_filter(monkeypatch):
    subs = _subs("a", "b")
    _with_profiles(monkeypatch, ["a"])
    assert dp.keep_with_profile(subs, {"BROWSER_BACKEND": "donut", "DONUT_EXISTING_PROFILES_ONLY": False}) == subs
    assert dp.keep_with_profile(subs, {"BROWSER_BACKEND": "chromium"}) == subs
    _with_profiles(monkeypatch, None)
    assert dp.keep_with_profile(subs, {"BROWSER_BACKEND": "donut"}) == subs


def test_registrar_backend_override_only_changes_registration():
    from apps.hub.main import registrar_vendor_settings
    eff = {"BROWSER_BACKEND": "donut", "DONUT_API_TOKEN": "t", "REGISTRAR_BROWSER_BACKEND": "chromium"}
    assert registrar_vendor_settings(eff)["BROWSER_BACKEND"] == "chromium"
    assert registrar_vendor_settings({**eff, "REGISTRAR_BROWSER_BACKEND": ""})["BROWSER_BACKEND"] == "donut"
    assert registrar_vendor_settings({**eff, "REGISTRAR_BROWSER_BACKEND": "bogus"})["BROWSER_BACKEND"] == "donut"

"""retune_soax_credentials.py -- the pure rewrite. The DB half is a scoped, dry-run-by-default
script exercised by hand; this pins the string surgery, which is where a mistake would silently
corrupt 1600 live credentials."""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "retune", Path(__file__).resolve().parent.parent / "scripts" / "retune_soax_credentials.py")
retune_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(retune_mod)
retune = retune_mod.retune


def test_bindttl_is_rewritten_and_the_password_survives():
    """bindttl is how long SOAX retries the SAME dead exit before handing over a new one -- 600 was
    ten minutes of sitting on a dropped IP."""
    c = "package-350716-country-de-sessionid-ABC-sessionlength-3600-bindttl-600-opt-wb:secret"
    out = retune(c, 5)
    assert "-bindttl-5-" in out and "-bindttl-600-" not in out
    assert out.endswith(":secret")


def test_missing_opt_wb_is_added_before_the_password():
    c = "package-335959-country-pt-sessionid-XYZ-sessionlength-3600-bindttl-600:secret"
    out = retune(c, 5)
    assert out == "package-335959-country-pt-sessionid-XYZ-sessionlength-3600-bindttl-5-opt-wb:secret"


def test_an_already_correct_credential_is_left_alone():
    c = "package-350716-country-it-sessionid-Q-sessionlength-3600-bindttl-5-opt-wb:pw"
    assert retune(c, 5) == c


def test_sessionlength_is_never_touched():
    """Only bindttl changes. sessionlength is how long a session is sticky at all, a different knob
    the operator has not asked to change."""
    c = "package-350716-country-de-sessionid-A-sessionlength-3600-bindttl-600-opt-wb:pw"
    assert "sessionlength-3600" in retune(c, 5)

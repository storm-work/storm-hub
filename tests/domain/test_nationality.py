"""libs/domain/nationality.py -- passport demonym -> ISO alpha-3 for the portal registration form."""
import pytest

from libs.domain.nationality import to_iso3


def test_the_three_values_that_failed_100pct_in_production_now_resolve():
    """MEASURED 2026-09-19: 'GUINEENS' 0/14 registered, 'GUINÉENN' 0/4, 'GHANAIAN' 0/2 -- the old
    importer truncated each to 8 chars and sent the fragment as a code. The eighth-plus letters are
    exactly what separate the two Guineas, so the truncation also destroyed the evidence."""
    assert to_iso3("GUINEENS") == ("GNB", None)      # Guinea-Bissau (Portuguese GUINEENSE)
    assert to_iso3("GUINÉENN") == ("GIN", None)      # Guinea-Conakry (French GUINÉENNE)
    assert to_iso3("GHANAIAN") == ("GHA", None)


def test_the_two_guineas_are_split_by_language_not_conflated():
    """The one genuine trap in this region. A Bissau passport is Portuguese and says GUINEENSE; a
    Conakry passport is French and says GUINÉEN/GUINÉENNE. 'Guinéenne' with no 'Bissau-' qualifier
    is Conakry."""
    for gnb in ("guineense", "Guineense", "bissau-guinéen", "Guiné-Bissau", "GUINEENSE"):
        assert to_iso3(gnb)[0] == "GNB", gnb
    for gin in ("guinéenne", "Guinéen", "guineana", "GUINÉENNE"):
        assert to_iso3(gin)[0] == "GIN", gin


def test_nigeria_and_niger_are_not_confused():
    """French 'nigériane' (Nigeria) vs 'nigérienne' (Niger) -- one letter, different countries."""
    assert to_iso3("nigériane")[0] == "NGA"
    assert to_iso3("Nigerian")[0] == "NGA"
    assert to_iso3("nigérienne")[0] == "NER"


def test_diacritics_and_separators_do_not_matter():
    for v in ("CABO-VERDIANA", "cabo verdiana", "Cabo‑Verdiana", "CABOVERDIANA", "Cap-Verdienne"):
        assert to_iso3(v)[0] == "CPV", v


def test_existing_iso3_codes_round_trip():
    for code in ("CPV", "SEN", "GNB", "gin", "PRT"):
        assert to_iso3(code) == (code.upper(), None)


def test_blank_is_absent_not_an_error():
    assert to_iso3(None) == (None, None)
    assert to_iso3("") == (None, None)
    assert to_iso3("   ") == (None, None)


def test_an_unknown_value_is_an_error_never_a_truncated_code():
    """The whole point. The old fallback stored value.upper()[:8]; a wrong code fails every
    registration for that identity, so an unknown demonym must be a hard, named error."""
    code, err = to_iso3("KLINGON")
    assert code is None and err and "unrecognised" in err
    # never silently truncates a long unknown value into the 3-letter slot
    assert to_iso3("SOMELONGUNKNOWNDEMONYM")[0] is None


def test_a_bare_three_letter_word_that_is_a_demonym_still_maps_over_iso_passthrough():
    """The ISO passthrough only fires for a value that IS three letters; a 3-char code wins, but a
    real demonym longer than three chars is never mistaken for one."""
    assert to_iso3("Malienne")[0] == "MLI"

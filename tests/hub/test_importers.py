from apps.hub.importers import parse_identities, parse_identities_csv, parse_proxies


def test_parse_proxies_url_form():
    text = "http://user:pw@1.2.3.4:8080\nhttps://u2:p2@proxy.soax.com:5000\n"
    parsed, errors = parse_proxies(text)
    assert not errors
    assert parsed[0].endpoint == "1.2.3.4:8080" and parsed[0].credentials_ref == "user:pw"
    assert parsed[1].endpoint == "proxy.soax.com:5000"


def test_parse_proxies_colon_and_bare():
    parsed, errors = parse_proxies("5.6.7.8:9000:bob:secret\n9.9.9.9:1234\n# comment\nnonsense")
    assert parsed[0].endpoint == "5.6.7.8:9000" and parsed[0].credentials_ref == "bob:secret"
    assert parsed[1].endpoint == "9.9.9.9:1234" and parsed[1].credentials_ref is None
    assert errors == ["nonsense"]


def test_parse_identities_groups_subaccounts():
    csv = (
        "identity_id,first_name,last_name,traveldoc,nationality,account_type,birthdate,gender,username,password,email\n"
        "ID1,Ana,Silva,PA100,CPV,test,,,ana1,pw1,ana1@x.com\n"
        "ID1,Ana,Silva,PA100,CPV,test,,,ana2,pw2,ana2@x.com\n"
        "ID2,Bob,Jones,PA200,CPV,real,1985-03-04,M,bob1,pw3,\n"
    )
    idents, errors = parse_identities_csv(csv)
    assert not errors
    assert len(idents) == 2
    ana = idents[0]
    assert ana.full_name == "Ana Silva" and ana.passport_no == "PA100" and ana.type == "test"
    assert [s.username for s in ana.sub_accounts] == ["ana1", "ana2"]
    assert idents[1].type == "real" and idents[1].sub_accounts[0].username == "bob1"


def test_parse_identities_default_type_and_full_name_column():
    csv = ("full_name,passport_no,birthdate,gender,nationality,username,password\n"
           "Carl Doe,PA777,1979-07-07,M,CPV,carl,pw\n")
    idents, errs = parse_identities_csv(csv, default_type="real")
    assert not errs
    assert idents[0].full_name == "Carl Doe" and idents[0].type == "real"


PARIA = """=== 1 (1).jpeg ===
First Name     : ALESSANDRO
Last Name      : DO CANTO MIRANDA
Date of Birth  : 30-11-2009
Gender         : M
Nationality    : CABO-VERDIANA
Passport Number: PA443247

=== 1 (2).jpeg ===
First Name     : AILSA KIRI
Last Name      : ALFAMA SILVA VAZ DOS SANTOS
Date of Birth  : 25-08-2010
Gender         : F
Nationality    : CABO-VERDIANA
Passport Number: PA580839
"""


def test_parse_paria_labeled_via_autodetect():
    idents, errors = parse_identities(PARIA)   # auto-detects labeled format
    assert not errors
    assert len(idents) == 2
    a = idents[0]
    assert a.full_name == "ALESSANDRO DO CANTO MIRANDA"
    assert a.passport_no == "PA443247"
    assert a.birthdate == "2009-11-30"           # DD-MM-YYYY -> ISO
    assert a.nationality == "CPV"                 # CABO-VERDIANA -> CPV
    assert a.type == "real"                       # passports -> real identities
    assert a.sub_accounts == []                   # generated/registered separately


def test_labeled_records_split_on_first_name_and_varied_headers():
    """CONFIRMED 2026-10-21: a real 67-identity paria export used `== (N) ===` headers (not the
    `=== ... ===` the splitter matched) AND relied on nothing else, so EVERY record collapsed into
    one block whose field-dict kept only the LAST -- exactly one identity imported. Records must split
    on a new `First Name` AND on any `==...==` header variant. Exercises both here."""
    # record 1: the odd `== (N) ===` header shape; record 2: NO header at all (bare First Name)
    txt = ("== (1) ===\nFirst Name      : NANIA\nLast Name       : BAFILE\n"
           "Date of Birth   : 16-03-1985\nGender          : F\n"
           "Nationality     : GUINEENSE\nPassport Number : C00462666\n\n"
           "First Name      : KILLIAN KAEL\nLast Name       : XAVIER DOS SANTOS\n"
           "Date of Birth   : 24-09-2021\nGender          : M\n"
           "Nationality     : CABO-VERDIANA\nPassport Number : PA470021\n")
    idents, errors = parse_identities(txt)
    assert not errors
    assert [i.full_name for i in idents] == ["NANIA BAFILE", "KILLIAN KAEL XAVIER DOS SANTOS"]
    assert [i.passport_no for i in idents] == ["C00462666", "PA470021"]
    assert [i.nationality for i in idents] == ["GNB", "CPV"]   # GUINEENSE->GNB, CABO-VERDIANA->CPV


# ── geo must be set when a proxy row is CREATED, not only by the backfill migration ───────────
def test_parse_geo_reads_both_provider_credential_formats():
    from apps.hub.importers import parse_geo
    assert parse_geo("package-335959-country-pt-sessionid-X-sessionlength-3600-opt-wb:pw") == "PT"
    assert parse_geo("package-350716-country-de-sessionid-X-sessionlength-3600-opt-wb:pw") == "DE"
    assert parse_geo("sodade2026-IT-17:test-password") == "IT"
    assert parse_geo("package-350716-sessionid-X-sessionlength-3600-opt-wb:pw") is None
    assert parse_geo(None) is None


# ── incomplete REAL identities are rejected and reported, not admitted (2026-09-19) ──────────
def test_a_real_identity_missing_required_fields_is_skipped_with_a_named_reason():
    """Operator decision 2026-09-19: do not add a half-filled real identity to the DB. The registrar
    silently defaults missing fields (gender->'M', bday->'1985/01/01', traveldoc->''), so an
    incomplete row would produce a REAL appointment PDF with a wrong birthdate -- worse than a
    skipped row. The report must name the person and the missing field so the operator can fix it."""
    from apps.hub.importers import parse_identities

    csv = ("full_name,nationality,passport_no,birthdate,gender\n"
           "COMPLETE ONE,GHANAIAN,A0144790,1990-11-30,F\n"
           "NO PASSPORT,CPV,,1985-01-01,M\n"
           "NO DOB,CPV,PA222,,M\n"
           "NO GENDER,CPV,PA333,1980-05-05,\n")
    out, errs = parse_identities(csv, default_type="real")

    assert [i.full_name for i in out] == ["COMPLETE ONE"]
    joined = " | ".join(errs)
    assert "NO PASSPORT" in joined and "passport number" in joined
    assert "NO DOB" in joined and "date of birth" in joined
    assert "NO GENDER" in joined and "gender" in joined


def test_an_unparseable_ocr_birthdate_is_rejected_not_sent_verbatim():
    """OCR reads zeros as the letter O: '3O-11-2OO9'. _normalize_dob can't fix it, and the registrar
    would post it as bday. A non-ISO date after normalization is a hard skip."""
    from apps.hub.importers import parse_identities_labeled

    txt = ("=== x.jpg ===\nFirst Name: ANA\nLast Name: COSTA\n"
           "Date of Birth: 3O-11-2OO9\nGender: F\nNationality: CPV\nPassport Number: PA999\n")
    out, errs = parse_identities_labeled(txt)
    assert out == []
    assert "ANA COSTA" in errs[0] and "not a valid date" in errs[0]


def test_test_identities_are_exempt_from_the_completeness_check():
    """Test identities are synthetic (gen-test) and never register a real applicant, so a sparse
    test row -- name + credentials only -- must still import."""
    from apps.hub.importers import parse_identities

    out, errs = parse_identities("full_name,username,password\nTEST BOT,bot,pw\n", default_type="test")
    assert [i.full_name for i in out] == ["TEST BOT"] and errs == []


def test_an_incomplete_real_identity_does_not_orphan_its_sub_account_rows():
    """The whole group is skipped as a unit -- a sub-account row following an incomplete identity
    must not resurrect it into a fresh group."""
    from apps.hub.importers import parse_identities

    csv = ("identity_id,full_name,nationality,passport_no,birthdate,gender,username,password\n"
           "1,BAD REAL,CPV,,1985-01-01,M,,\n"
           "1,BAD REAL,,,,,user1,pw1\n")
    out, errs = parse_identities(csv, default_type="real")
    assert out == [] and len(errs) == 1 and "BAD REAL" in errs[0]

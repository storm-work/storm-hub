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
        "identity_id,first_name,last_name,traveldoc,nationality,account_type,username,password,email\n"
        "ID1,Ana,Silva,PA100,CPV,test,ana1,pw1,ana1@x.com\n"
        "ID1,Ana,Silva,PA100,CPV,test,ana2,pw2,ana2@x.com\n"
        "ID2,Bob,Jones,PA200,CPV,real,bob1,pw3,\n"
    )
    idents, errors = parse_identities_csv(csv)
    assert not errors
    assert len(idents) == 2
    ana = idents[0]
    assert ana.full_name == "Ana Silva" and ana.passport_no == "PA100" and ana.type == "test"
    assert [s.username for s in ana.sub_accounts] == ["ana1", "ana2"]
    assert idents[1].type == "real" and idents[1].sub_accounts[0].username == "bob1"


def test_parse_identities_default_type_and_full_name_column():
    csv = "full_name,username,password\nCarl Doe,carl,pw\n"
    idents, _ = parse_identities_csv(csv, default_type="real")
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


# ── geo must be set when a proxy row is CREATED, not only by the backfill migration ───────────
def test_parse_geo_reads_both_provider_credential_formats():
    from apps.hub.importers import parse_geo
    assert parse_geo("package-335959-country-pt-sessionid-X-sessionlength-3600-opt-wb:pw") == "PT"
    assert parse_geo("package-350716-country-de-sessionid-X-sessionlength-3600-opt-wb:pw") == "DE"
    assert parse_geo("sodade2026-IT-17:sempre2026") == "IT"
    assert parse_geo("package-350716-sessionid-X-sessionlength-3600-opt-wb:pw") is None
    assert parse_geo(None) is None

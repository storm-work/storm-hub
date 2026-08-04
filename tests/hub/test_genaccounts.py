import re

from apps.hub.genaccounts import (
    gen_fake_identities,
    gen_subaccounts,
    make_password,
    make_username,
    random_traveldoc,
)


def test_username_format_and_uniqueness():
    seen = set()
    for _ in range(200):
        u = make_username("João", "Silva", seen)
        assert re.fullmatch(r"joasil\d{4}", u)   # first3+last3 (accent-stripped) + 4 digits
    assert len(seen) == 200                       # all unique


def test_password_policy():
    p = make_password()
    assert p.endswith("!#") and len(p) == 14
    assert any(c.isupper() for c in p) and any(c.isdigit() for c in p) and any(c.islower() for c in p)


def test_gen_subaccounts_count():
    subs = gen_subaccounts("Ana", "Costa", 3, set())
    assert len(subs) == 3
    assert all(s.status == "new" and s.password and not s.email for s in subs)


def test_traveldoc_unique_pa_format():
    seen = set()
    for _ in range(50):
        td = random_traveldoc(seen)
        assert re.fullmatch(r"PA\d{6}", td)
    assert len(seen) == 50


def test_gen_fake_identities():
    gen = gen_fake_identities(5, 3, existing_usernames=set(), existing_passports=set())
    assert len(gen) == 5
    assert all(len(g.sub_accounts) == 3 for g in gen)
    assert all(g.passport_no.startswith("PA") and g.nationality == "CPV" for g in gen)
    # birthdate ISO
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", g.birthdate) for g in gen)

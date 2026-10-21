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
        assert re.fullmatch(r"joaosilv\d{6}", u)  # first4+last4 (accent-stripped) + 6 digits
    assert len(seen) == 200                        # all unique


def test_short_names_are_padded_not_truncated():
    seen = set()
    u = make_username("Ana", "Li", seen)
    assert re.fullmatch(r"anaxlixx\d{6}", u)


def test_the_digit_space_is_wide_enough_to_survive_the_portals_memory():
    """The digit space is the ONLY defence against re-issuing a username the portal already holds an
    appointment for. `existing` can only contain usernames in THIS database, and storm-auto -- which
    made 100+ of those appointments with the byte-identical first3+last3+4digit rule -- had its
    Account table emptied during the migration. Those usernames are unenumerable, so collisions must
    be made statistically negligible rather than detected.

    At the old 4 digits (9,000 wide) issuing 20 accounts under one name carried a ~2.1% chance of
    reusing a historical username; at 6 digits it is ~0.02%."""
    seen = set()
    for _ in range(500):
        make_username("Carlos", "Furtado", seen)
    digits = {u[len("carlfurt"):] for u in seen}
    assert len(digits) == 500
    assert all(len(d) == 6 for d in digits)
    assert max(int(d) for d in digits) > 99999   # genuinely 6-wide, not zero-padded 4


def test_password_policy():
    p = make_password()
    assert p.endswith("!#") and len(p) == 14
    assert any(c.isupper() for c in p) and any(c.isdigit() for c in p) and any(c.islower() for c in p)


def test_gen_subaccounts_count():
    subs = gen_subaccounts("Ana", "Costa", 3, set())
    assert len(subs) == 3
    assert all(s.status == "new" and s.password and not s.email for s in subs)


def test_traveldoc_is_distinct_from_real_passports():
    """TEST passports are PA + SEVEN digits -- one more than a real Cabo-Verde PA+6 passport
    (e.g. PA470021) -- so a generated test identity can never collide with / dedup against a real one
    on import (2026-10-21)."""
    seen = set()
    for _ in range(50):
        td = random_traveldoc(seen)
        assert re.fullmatch(r"PA\d{7}", td), td          # 7 digits, not 6
        assert not re.fullmatch(r"PA\d{6}", td)          # never the real PA+6 shape
    assert len(seen) == 50


def test_gen_fake_identities():
    gen = gen_fake_identities(5, 3, existing_usernames=set(), existing_passports=set())
    assert len(gen) == 5
    assert all(len(g.sub_accounts) == 3 for g in gen)
    assert all(g.passport_no.startswith("PA") and g.nationality == "CPV" for g in gen)
    # birthdate ISO
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", g.birthdate) for g in gen)

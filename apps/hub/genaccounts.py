"""Sub-account + fake-identity generators — ported from storm-auto's gen_accounts / gen_fake_accounts.

Pure (no DB, no network): usernames/passwords/fake identities. Emails are NOT generated here — they
are created at REGISTER time via mail.tm (see the register flow), so a generated sub-account starts
with just username+password and status `new`.
"""
from __future__ import annotations

import random
import re
import string
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta

from .importers import ParsedSubAccount

_MALE_FIRST = [
    "CARLOS", "JOAO", "PAULO", "ANTONIO", "JOSE", "PEDRO", "LUIS", "MANUEL", "MIGUEL", "RAFAEL",
    "NELSON", "EDSON", "MARIO", "RICARDO", "FABIO", "AMILCAR", "ELISEU", "HERNANI", "CELESTINO",
    "ADILSON", "GILSON", "LEANDRO", "RONALDO", "SERGIO", "WANDERSON", "HELDER", "EDER", "NILTON",
    "DARIO", "FELICIANO", "EUCLIDES", "ERNESTO",
]
_FEMALE_FIRST = [
    "MARIA", "ANA", "SOFIA", "LAURA", "ISABEL", "MARTA", "CATARINA", "INES", "FERNANDA", "CLAUDIA",
    "PATRICIA", "VANESSA", "ELISA", "MARLENE", "EUNICE", "SILVANA", "ROSARIA", "FILOMENA",
    "ESPERANCA", "DULCE", "GRACIETE", "LURDES", "ALDINA", "EDNA", "LETICIA",
]
_LAST = [
    "SILVA", "SANTOS", "PEREIRA", "COSTA", "FERREIRA", "LOPES", "CARVALHO", "RODRIGUES", "MONTEIRO",
    "TAVARES", "GOMES", "BORGES", "SEMEDO", "FURTADO", "ANDRADE", "BARROS", "PIRES", "VARELA",
    "MOREIRA", "CUNHA", "MATOS", "RAMOS", "CORREIA", "ALMEIDA", "MENDES", "NEVES", "TEIXEIRA",
    "LIMA", "VIEIRA", "FERNANDES",
]


def _alpha(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s or "")
    return re.sub(r"[^a-z]", "", "".join(c for c in nfkd if not unicodedata.combining(c)).lower())


# Username shape: FIRST[:n] + LAST[:n] + digits. Widened from 3-3-4 to 4-4-6 on 2026-08-23.
#
# WHY. The old rule gave 53 x 29 = 1,537 distinct name prefixes over a 9,000-wide digit space, and
# `existing` can only ever contain usernames in THIS database. storm-auto -- the predecessor that
# made 100+ of the portal's appointments -- used the byte-identical rule (its own _make_username is
# first[:3] + last[:3] + randint(1000,9999)) drawing on the same name pools, and its Account table
# is now EMPTY because the data was migrated out during the rebuild. So those usernames exist on the
# PORTAL and nowhere we can enumerate.
#
# Re-issuing one is not a harmless duplicate: the portal already holds that account's appointment,
# so the warm-up is rejected with "This applicant already has an application registered with a
# future schedule" and the identity is unusable. Three identities were retired for exactly this on
# 2026-08-23, and it is indistinguishable from our own booking having failed.
#
# We cannot enumerate what the portal knows, so the only defence is to make accidental reuse
# negligible. Per 20 accounts issued under one name prefix:
#     3-3-4     9,000 wide   ->  2.089% chance of reusing a historical username
#     4-4-6   900,000 wide   ->  0.021%                          (100x better)
# 4 characters rather than 3 also lifts the prefix count (1,537 -> 1,650), and 14 characters is
# still an ordinary-looking username.
_USERNAME_NAME_CHARS = 4
_USERNAME_DIGITS = 6


def make_username(first: str, last: str, existing: set[str]) -> str:
    """A username not present in `existing` -- which every caller populates from the WHOLE
    sub_accounts table, not just the identity being generated for."""
    n = _USERNAME_NAME_CHARS
    base = _alpha(first)[:n].ljust(n, "x") + _alpha(last)[:n].ljust(n, "x")
    lo, hi = 10 ** (_USERNAME_DIGITS - 1), 10 ** _USERNAME_DIGITS - 1
    for _ in range(300):
        cand = base + str(random.randint(lo, hi))
        if cand not in existing:
            existing.add(cand)
            return cand
    raise RuntimeError(f"cannot generate unique username for {first} {last}")


def make_password() -> str:
    pool = (random.choices(string.ascii_uppercase, k=2)
            + random.choices(string.digits, k=4)
            + random.choices(string.ascii_lowercase, k=6))
    random.shuffle(pool)
    return "".join(pool) + "!#"


def random_birthdate() -> str:
    """ISO YYYY-MM-DD, age 18-60."""
    today = date.today()
    start = today - timedelta(days=60 * 365)
    end = today - timedelta(days=18 * 365)
    d = start + timedelta(days=random.randint(0, (end - start).days))
    return d.strftime("%Y-%m-%d")


def random_traveldoc(existing: set[str]) -> str:
    for _ in range(1000):
        td = "PA" + "".join(random.choices(string.digits, k=6))
        if td not in existing:
            existing.add(td)
            return td
    raise RuntimeError("cannot generate unique traveldoc")


def random_name(gender: str) -> tuple[str, str]:
    pool = _MALE_FIRST if gender == "M" else _FEMALE_FIRST
    first = random.choice(pool)
    if random.random() < 0.2:
        first = first + " " + random.choice(pool)
    last = random.choice(_LAST)
    if random.random() < 0.3:
        last = last + " " + random.choice(_LAST)
    return first, last


def gen_subaccounts(first: str, last: str, count: int, existing_usernames: set[str]) -> list[ParsedSubAccount]:
    """Generate `count` sub-accounts (username+password) for an identity."""
    out = []
    for _ in range(max(0, count)):
        u = make_username(first, last, existing_usernames)
        out.append(ParsedSubAccount(username=u, password=make_password(), status="new"))
    return out


@dataclass
class GenIdentity:
    full_name: str
    gender: str
    birthdate: str
    passport_no: str
    nationality: str
    sub_accounts: list[ParsedSubAccount]


def gen_fake_identities(count: int, accounts_per_identity: int, *,
                        existing_usernames: set[str], existing_passports: set[str],
                        nationality: str = "CPV") -> list[GenIdentity]:
    """Generate `count` fake TEST identities, each with `accounts_per_identity` sub-accounts."""
    out: list[GenIdentity] = []
    for _ in range(max(0, count)):
        gender = random.choice(["M", "F"])
        first, last = random_name(gender)
        passport = random_traveldoc(existing_passports)
        subs = gen_subaccounts(first, last, accounts_per_identity, existing_usernames)
        out.append(GenIdentity(
            full_name=f"{first} {last}", gender=gender, birthdate=random_birthdate(),
            passport_no=passport, nationality=nationality, sub_accounts=subs,
        ))
    return out

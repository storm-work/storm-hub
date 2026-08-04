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


def make_username(first: str, last: str, existing: set[str]) -> str:
    base = _alpha(first)[:3].ljust(3, "x") + _alpha(last)[:3].ljust(3, "x")
    for _ in range(300):
        cand = base + str(random.randint(1000, 9999))
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

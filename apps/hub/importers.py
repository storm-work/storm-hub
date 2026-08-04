"""CSV / text importers for the DB-backed pools (identities+sub-accounts, proxies).

Pure parsing functions — no DB — so they unit-test against fixtures. The API layer takes the parsed
rows and inserts them. Replaces storm-auto's external-file model (proxies_*.txt / accounts.csv) with
DB tables populated via these imports.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field


# ── Proxies ───────────────────────────────────────────────────────────────────
@dataclass
class ParsedProxy:
    endpoint: str                 # host:port
    credentials_ref: str | None   # "user:pass" (kept out of the endpoint)


_URL_RE = re.compile(r"^(?:https?://)?(?:([^:@/]+):([^@/]+)@)?([^:/@]+):(\d+)/?$")


def parse_proxies(text: str) -> tuple[list[ParsedProxy], list[str]]:
    """Parse proxy lines. Accepts `http://user:pass@host:port`, `host:port:user:pass`, or `host:port`.
    Returns (parsed, errors)."""
    out: list[ParsedProxy] = []
    errors: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _URL_RE.match(line)
        if m:
            user, pw, host, port = m.groups()
            creds = f"{user}:{pw}" if user else None
            out.append(ParsedProxy(endpoint=f"{host}:{port}", credentials_ref=creds))
            continue
        # host:port:user:pass form
        parts = line.split(":")
        if len(parts) == 4 and parts[1].isdigit():
            host, port, user, pw = parts
            out.append(ParsedProxy(endpoint=f"{host}:{port}", credentials_ref=f"{user}:{pw}"))
        elif len(parts) == 2 and parts[1].isdigit():
            out.append(ParsedProxy(endpoint=line, credentials_ref=None))
        else:
            errors.append(line)
    return out, errors


# ── Identities + sub-accounts ─────────────────────────────────────────────────
@dataclass
class ParsedSubAccount:
    username: str
    password: str
    email: str | None = None
    email_pass: str | None = None
    status: str = "new"


@dataclass
class ParsedIdentity:
    full_name: str
    type: str = "test"
    birthdate: str | None = None
    passport_no: str | None = None
    gender: str | None = None
    nationality: str | None = None
    sub_accounts: list[ParsedSubAccount] = field(default_factory=list)


def _first(row: dict, *keys: str) -> str | None:
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return None


def parse_identities_csv(text: str, *, default_type: str = "test") -> tuple[list[ParsedIdentity], list[str]]:
    """Parse an identities CSV into grouped identities (each with its sub-accounts).

    Flexible column mapping (compatible with storm-auto's accounts.csv):
      identity key : identity_id  (rows sharing it group into one identity)
      name         : full_name | (first_name + last_name)
      passport     : passport_no | traveldoc
      type         : type | account_type  (test/real; else default_type)
      sub-account  : username, password, email, email_pass, status
    A row with a username yields a sub-account; identity fields come from the first row of the group.
    Returns (identities, errors).
    """
    errors: list[str] = []
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], ["empty or headerless CSV"]

    groups: dict[str, ParsedIdentity] = {}
    order: list[str] = []
    for i, row in enumerate(reader, start=2):
        row = {(k or "").strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
        name = _first(row, "full_name") or " ".join(
            x for x in (_first(row, "first_name"), _first(row, "last_name")) if x
        )
        if not name:
            errors.append(f"row {i}: no name")
            continue
        ident_key = _first(row, "identity_id") or f"__row{i}"
        typ = (_first(row, "type", "account_type") or default_type).lower()
        if typ not in ("test", "real"):
            typ = default_type
        if ident_key not in groups:
            groups[ident_key] = ParsedIdentity(
                full_name=name,
                type=typ,
                birthdate=_first(row, "birthdate", "bd"),
                passport_no=_first(row, "passport_no", "traveldoc", "passport"),
                gender=_first(row, "gender"),
                nationality=_first(row, "nationality"),
            )
            order.append(ident_key)
        username = _first(row, "username", "user")
        if username:
            groups[ident_key].sub_accounts.append(ParsedSubAccount(
                username=username,
                password=_first(row, "password", "pass") or "",
                email=_first(row, "email"),
                email_pass=_first(row, "email_pass", "emailpass"),
                status=(_first(row, "status") or "new"),
            ))
    return [groups[k] for k in order], errors

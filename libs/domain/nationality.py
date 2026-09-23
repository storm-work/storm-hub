"""Nationality: passport demonym -> the ISO 3166-1 alpha-3 code the portal's form expects.

WHY THIS EXISTS. The portal's registration form takes a nationality CODE -- every code that has ever
registered successfully here is ISO alpha-3 (CPV 483/489, SEN 12/12, PRT, BRA). Passport OCR hands
us the DEMONYM as printed on the passport, in the passport's language: Portuguese "CABO-VERDIANA",
French "GUINÉENNE", English "GHANAIAN". The old importer had a five-entry map and a fallback of
`value.upper()[:8]` -- so anything unmapped was stored as a truncated demonym and sent to the portal
as a code. Measured 2026-09-19: 'GUINEENS' 0/14 registered, 'GUINÉENN' 0/4, 'GHANAIAN' 0/2 -- 100%
failure on every unmapped value, against ~1% for the mapped ones.

THE TRUNCATION DESTROYED THE EVIDENCE. Both broken values were cut at exactly 8 characters, and the
eighth-plus letters are what separate the two Guineas:
    Portuguese GUINEENSE  (Guinea-Bissau, GNB)  ->  'GUINEENS'
    French     GUINÉENNE  (Guinea-Conakry, GIN) ->  'GUINÉENN'
The passports settled it (C00... + Balanta/Papel surnames = Bissau; 9-digit + Barry = Conakry), but
an importer must never put the operator in that position. So: an unrecognised value is an IMPORT
ERROR naming the person, never a silent best-effort code.

Matching is DIACRITIC- AND SEPARATOR-INSENSITIVE and covers masculine/feminine/plural forms, because
OCR drops accents unpredictably and a passport says "GUINÉENNE" or "GUINÉEN" depending on the
holder. Truncated legacy forms are listed explicitly so re-parsing existing data also resolves.

The two Guineas are the only genuine trap in this region and the map is deliberate about them:
    guineense / bissau-guineen / bissau-guinean          -> GNB
    guineano  / guineen(ne)  / guinean                   -> GIN
"Guinéenne" with no "Bissau-" qualifier is Guinea-Conakry -- a Bissau passport is Portuguese and
says GUINEENSE.
"""
from __future__ import annotations

import re
import unicodedata

# Normalised key (see _norm) -> ISO alpha-3. Keys are written already-normalised.
NATIONALITY_MAP: dict[str, str] = {
    # ── Cape Verde ───────────────────────────────────────────────────────────────────────────
    "CABOVERDIANA": "CPV", "CABOVERDIANO": "CPV", "CABOVERDIANOS": "CPV", "CABOVERDIANAS": "CPV",
    "CAPVERDIENNE": "CPV", "CAPVERDIEN": "CPV", "CAPEVERDEAN": "CPV", "CAPEVERDIAN": "CPV",
    # ── Guinea-Bissau (Portuguese-speaking; passport says GUINEENSE) ─────────────────────────
    "GUINEENSE": "GNB", "GUINEENSES": "GNB", "GUINEENS": "GNB",          # last = legacy truncation
    "BISSAUGUINEENNE": "GNB", "BISSAUGUINEEN": "GNB", "BISSAUGUINEAN": "GNB",
    "GUINEBISSAU": "GNB", "GUINEBISSAUENSE": "GNB",
    # ── Guinea (Conakry; French passport says GUINÉEN/GUINÉENNE) ─────────────────────────────
    "GUINEENNE": "GIN", "GUINEEN": "GIN", "GUINEENN": "GIN",              # last = legacy truncation
    "GUINEANA": "GIN", "GUINEANO": "GIN", "GUINEAN": "GIN",
    # ── Senegal ──────────────────────────────────────────────────────────────────────────────
    "SENEGALAISE": "SEN", "SENEGALAIS": "SEN", "SENEGALESA": "SEN", "SENEGALES": "SEN",
    "SENEGALESE": "SEN",
    # ── Ghana ────────────────────────────────────────────────────────────────────────────────
    "GHANAIAN": "GHA", "GANESA": "GHA", "GANES": "GHA", "GHANEENNE": "GHA", "GHANEEN": "GHA",
    # ── Gambia ───────────────────────────────────────────────────────────────────────────────
    "GAMBIAN": "GMB", "GAMBIANA": "GMB", "GAMBIANO": "GMB", "GAMBIENNE": "GMB", "GAMBIEN": "GMB",
    # ── Mali ─────────────────────────────────────────────────────────────────────────────────
    "MALIENNE": "MLI", "MALIEN": "MLI", "MALIANA": "MLI", "MALIANO": "MLI", "MALIAN": "MLI",
    # ── Mauritania ───────────────────────────────────────────────────────────────────────────
    "MAURITANIENNE": "MRT", "MAURITANIEN": "MRT", "MAURITANA": "MRT", "MAURITANO": "MRT",
    "MAURITANIAN": "MRT",
    # ── Côte d'Ivoire ────────────────────────────────────────────────────────────────────────
    "IVOIRIENNE": "CIV", "IVOIRIEN": "CIV", "IVORIAN": "CIV", "MARFINENSE": "CIV",
    "COSTAMARFINENSE": "CIV",
    # ── Sierra Leone / Liberia ───────────────────────────────────────────────────────────────
    "SIERRALEONEAN": "SLE", "SERRALEONINA": "SLE", "SERRALEONINO": "SLE",
    "LIBERIAN": "LBR", "LIBERIANA": "LBR", "LIBERIANO": "LBR", "LIBERIENNE": "LBR", "LIBERIEN": "LBR",
    # ── Nigeria vs Niger (French "nigériane" vs "nigérienne" -- different countries) ─────────
    "NIGERIAN": "NGA", "NIGERIANA": "NGA", "NIGERIANO": "NGA", "NIGERIANE": "NGA",
    "NIGERIENNE": "NER", "NIGERIEN": "NER",
    # ── Burkina Faso / Togo / Benin / Cameroon ───────────────────────────────────────────────
    "BURKINABE": "BFA", "BURKINABEE": "BFA",
    "TOGOLAISE": "TGO", "TOGOLAIS": "TGO", "TOGOLESA": "TGO", "TOGOLESE": "TGO",
    "BENINOISE": "BEN", "BENINOIS": "BEN", "BENINESE": "BEN",
    "CAMEROUNAISE": "CMR", "CAMEROUNAIS": "CMR", "CAMAROONIAN": "CMR", "CAMEROONIAN": "CMR",
    # ── Lusophone ────────────────────────────────────────────────────────────────────────────
    "PORTUGUESA": "PRT", "PORTUGUES": "PRT", "PORTUGUESE": "PRT",
    "BRASILEIRA": "BRA", "BRASILEIRO": "BRA", "BRAZILIAN": "BRA", "BRESILIENNE": "BRA", "BRESILIEN": "BRA",
    "ANGOLANA": "AGO", "ANGOLANO": "AGO", "ANGOLAN": "AGO", "ANGOLAISE": "AGO", "ANGOLAIS": "AGO",
    "MOCAMBICANA": "MOZ", "MOCAMBICANO": "MOZ", "MOZAMBICAN": "MOZ",
    "SANTOMENSE": "STP", "SAOTOMENSE": "STP",
    "TIMORENSE": "TLS",
    # ── Maghreb / Europe (occasional) ────────────────────────────────────────────────────────
    "MAROCAINE": "MAR", "MAROCAIN": "MAR", "MARROQUINA": "MAR", "MARROQUINO": "MAR", "MOROCCAN": "MAR",
    "ALGERIENNE": "DZA", "ALGERIEN": "DZA", "ARGELINA": "DZA", "ARGELINO": "DZA", "ALGERIAN": "DZA",
    "TUNISIENNE": "TUN", "TUNISIEN": "TUN", "TUNISINA": "TUN", "TUNISIAN": "TUN",
    "FRANCAISE": "FRA", "FRANCAIS": "FRA", "FRANCESA": "FRA", "FRANCES": "FRA", "FRENCH": "FRA",
    "ESPANOLA": "ESP", "ESPANOL": "ESP", "ESPAGNOLE": "ESP", "ESPAGNOL": "ESP", "SPANISH": "ESP",
    "BRITISH": "GBR", "AMERICAN": "USA",
}

_ISO3 = re.compile(r"^[A-Z]{3}$")


def _norm(v: str) -> str:
    """Uppercase, strip diacritics, drop every separator -- so 'Cabo-Verdiana', 'CABO VERDIANA' and
    'CABOVERDIANA' are one key, and OCR's accent handling stops mattering."""
    s = unicodedata.normalize("NFKD", str(v)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Z]", "", s.upper())


def to_iso3(value: str | None) -> tuple[str | None, str | None]:
    """(code, error). Exactly one is set, except that a blank value is (None, None) -- absent is not
    an error, it is just absent.

    A value that is ALREADY a three-letter code is accepted as-is (uppercased) so a CSV carrying
    codes round-trips. Anything else must be in the map. Nothing is ever truncated into the code
    slot: an unrecognised demonym is returned as an error for the caller to surface against the
    person's name, because a silently wrong code fails 100% of registrations for that identity.
    """
    if value is None or not str(value).strip():
        return None, None
    raw = str(value).strip()
    key = _norm(raw)
    if not key:
        return None, f"unrecognised nationality {raw!r}"
    if _ISO3.match(key) and len(raw.strip()) == 3:
        return key, None
    code = NATIONALITY_MAP.get(key)
    if code:
        return code, None
    return None, (f"unrecognised nationality {raw!r} -- not an ISO-3 code and not a known "
                  f"demonym; add it to libs/domain/nationality.py")

"""The exit countries each rotating provider supports -- the single source of truth for the
per-worker geo dropdown and the PATCH /workers/{id}/geo validation.

WHY PER PROVIDER. soax and webshare do not necessarily offer the same country set, and picking a
country a provider cannot serve breaks that worker's exit. So the dropdown is filtered to the
worker's resolved provider (operator decision 2026-09-24), and validation rejects a code that
provider does not list.

SOURCE (operator-supplied 2026-09-25): soax from data/geo-code list.txt (parsed, with the source's
Burundi 'di'->'bi' and Brazil 'bz'->'br' code typos corrected); webshare from the provider's own
country panel (data/geo-code webshare/*.png), which lists the same comprehensive ISO-3166-1 alpha-2
set (200+ countries, US/FR/DE/IT/ES the largest pools). Both are effectively the full country set,
so they share one canonical map here; keep them as separate names so a future divergence is a
one-line edit.

CODE FORM. Stored provider-neutrally as an UPPERCASE code (e.g. "PT"); the provider layer applies
its own case at mint time -- soax lowercases (`-country-pt-`), webshare uppercases (`-PT-`), see
libs/proxies/providers/. Labels are for the dropdown only.
"""
from __future__ import annotations

# code (UPPERCASE) -> label. Canonical ISO-3166-1 alpha-2, from the operator's soax/webshare lists.
_ALL_GEOS: dict[str, str] = {
    'AD': 'Andorra',
    'AE': 'United Arab Emirates',
    'AF': 'Afghanistan',
    'AG': 'Antigua and barbuda',
    'AI': 'Anguilla',
    'AL': 'Albania',
    'AM': 'Armenia',
    'AO': 'Angola',
    'AR': 'Argentina',
    'AS': 'American Samoa',
    'AT': 'Austria',
    'AU': 'Australia',
    'AW': 'Aruba',
    'AX': 'Åland Islands',
    'AZ': 'Azerbaijan',
    'BA': 'Bosnia and Herzegovina',
    'BB': 'Barbados',
    'BD': 'Bangladesh',
    'BE': 'Belgium',
    'BF': 'Burkina Faso',
    'BG': 'Bulgaria',
    'BH': 'Bahrain',
    'BI': 'Burundi',
    'BJ': 'Benin',
    'BL': 'Saint Barthelemy',
    'BM': 'Bermuda',
    'BN': 'Brunei Darussalam',
    'BO': 'Bolivia',
    'BQ': 'Bonaire, Sint Eustatius And saba',
    'BR': 'Brazil',
    'BT': 'Bhutan',
    'BW': 'Botswana',
    'BY': 'Belarus',
    'BZ': 'Belize',
    'CA': 'Canada',
    'CD': 'Congo, The Democratic Republic of the',
    'CF': 'Central African Republic',
    'CG': 'Congo',
    'CH': 'Switzerland',
    'CI': "Cote d'Ivoire",
    'CK': 'Cook Islands',
    'CL': 'Chile',
    'CM': 'Cameroon',
    'CN': 'China',
    'CO': 'Colombia',
    'CR': 'Costa Rica',
    'CU': 'Cuba',
    'CV': 'Cape Verde',
    'CW': 'Curacao',
    'CY': 'Cyprus',
    'CZ': 'Czechia',
    'DE': 'Germany',
    'DJ': 'Djibouti',
    'DK': 'Denmark',
    'DM': 'Dominica',
    'DO': 'Dominican Republic',
    'DZ': 'Algeria',
    'EC': 'Ecuador',
    'EE': 'Estonia',
    'EG': 'Egypt',
    'ER': 'Eritrea',
    'ES': 'Spain',
    'ET': 'Ethiopia',
    'FI': 'Finland',
    'FJ': 'Fiji',
    'FK': 'Falkand Islands',
    'FM': 'Micronesia',
    'FO': 'Faroe Islands',
    'FR': 'France',
    'GA': 'Gabon',
    'GB': 'United Kingdom',
    'GD': 'Grenada',
    'GE': 'Georgia',
    'GF': 'French Guiana',
    'GG': 'Guernsey',
    'GH': 'Ghana',
    'GI': 'Gibraltar',
    'GM': 'Gambia',
    'GN': 'Guinea',
    'GP': 'Guadeloupe',
    'GQ': 'Equatorial guinea',
    'GR': 'Greece',
    'GT': 'Guatemala',
    'GU': 'Guam',
    'GW': 'Guinea-Bissau',
    'GY': 'Guyana',
    'HK': 'Hong Kong',
    'HN': 'Honduras',
    'HR': 'Croatia',
    'HT': 'Haiti',
    'HU': 'Hungary',
    'ID': 'Indonesia',
    'IE': 'Ireland',
    'IL': 'Israel',
    'IM': 'Isle Of Man',
    'IN': 'India',
    'IQ': 'Iraq',
    'IR': 'Iran',
    'IS': 'Iceland',
    'IT': 'Italy',
    'JE': 'Jersey',
    'JM': 'Jamaica',
    'JO': 'Jordan',
    'JP': 'Japan',
    'KE': 'Kenya',
    'KG': 'Kyrgyzstan',
    'KH': 'Cambodia',
    'KN': 'Saint Kitts and Nevis',
    'KW': 'Kuwait',
    'KY': 'Cayman Islands',
    'KZ': 'Kazakhstan',
    'LB': 'Lebanon',
    'LC': 'Saint Lucia',
    'LI': 'Liechtenstein',
    'LK': 'Sri Lanka',
    'LR': 'Liberia',
    'LS': 'Lesotho',
    'LT': 'Lithuania',
    'LU': 'Luxembourg',
    'LV': 'Latvia',
    'LY': 'Libyan Arab Jamhiriya',
    'MA': 'Morocco',
    'MC': 'Monaco',
    'MD': 'Moldova',
    'ME': 'Montenegro',
    'MF': 'Saint Martin French',
    'MG': 'Madagascar',
    'MK': 'North Macedonia',
    'ML': 'Mali',
    'MM': 'Myanmar',
    'MN': 'Mongolia',
    'MO': 'Macau',
    'MQ': 'Martinique',
    'MR': 'Mauritania',
    'MS': 'Montserrat',
    'MT': 'Malta',
    'MU': 'Mauritius',
    'MV': 'Maldives',
    'MW': 'Malawi',
    'MX': 'Mexico',
    'MY': 'Malaysia',
    'MZ': 'Mozambique',
    'NA': 'Namibia',
    'NC': 'New Caledonia',
    'NE': 'Niger',
    'NG': 'Nigeria',
    'NI': 'Nicaragua',
    'NL': 'Netherlands',
    'NO': 'Norway',
    'NP': 'Nepal',
    'NU': 'Niue',
    'NZ': 'New Zealand',
    'OM': 'Oman',
    'PA': 'Panama',
    'PE': 'Peru',
    'PF': 'French Polynesia',
    'PG': 'Papua New Guinea',
    'PH': 'Philippines',
    'PK': 'Pakistan',
    'PL': 'Poland',
    'PR': 'Puerto Rico',
    'PT': 'Portugal',
    'PW': 'Palau',
    'PY': 'Paraguay',
    'QA': 'Qatar',
    'RO': 'Romania',
    'RS': 'Serbia',
    'RW': 'Rwanda',
    'SA': 'Saudi Arabia',
    'SB': 'Solomon Islands',
    'SC': 'Seychelles',
    'SD': 'Sudan',
    'SE': 'Sweden',
    'SG': 'Singapore',
    'SI': 'Slovenia',
    'SK': 'Slovakia',
    'SL': 'Sierra Leone',
    'SM': 'San Marino',
    'SN': 'Senegal',
    'SO': 'Somalia',
    'SR': 'Suriname',
    'SS': 'South Sudan',
    'ST': 'Sao Tome and Principe',
    'SV': 'El Salvador',
    'SX': 'Sint Maarten Dutch',
    'SY': 'Syrian Arab Republic',
    'SZ': 'Swaziland',
    'TC': 'Turks and Caicos Islands',
    'TD': 'Chad',
    'TG': 'Togo',
    'TH': 'Thailand',
    'TJ': 'Tajikistan',
    'TL': 'Timor-Leste',
    'TM': 'Turkmenistan',
    'TN': 'Tunisia',
    'TO': 'Tonga',
    'TR': 'Turkey',
    'TT': 'Trinidad and Tobago',
    'TW': 'Taiwan',
    'TZ': 'Tanzania',
    'UA': 'Ukraine',
    'UG': 'Uganda',
    'US': 'United States',
    'UY': 'Uruguay',
    'UZ': 'Uzbekistan',
    'VE': 'Venezuela',
    'VG': 'Virgin Islands British',
    'VI': 'Virgin Islands US',
    'VN': 'Vietnam',
    'VU': 'Vanuatu',
    'WS': 'Samoa',
    'XK': 'Kosovo',
    'YE': 'Yemen',
    'YT': 'Mayotte',
    'ZA': 'South Africa',
    'ZM': 'Zambia',
    'ZW': 'Zimbabwe',
}

# Both providers serve the full set today (see the module docstring). Separate names so a future
# provider-specific restriction is a localized change, not a refactor.
SOAX_GEOS: dict[str, str] = dict(_ALL_GEOS)
WEBSHARE_GEOS: dict[str, str] = dict(_ALL_GEOS)

_BY_PROVIDER: dict[str, dict[str, str]] = {"soax": SOAX_GEOS, "webshare": WEBSHARE_GEOS}


def normalize_geo(code: str | None) -> str:
    """Canonical stored form: trimmed, uppercase. '' for blank -- callers treat '' as 'unbound'."""
    return str(code or "").strip().upper()


def supported_geos(provider: str | None) -> dict[str, str]:
    """{code: label} the provider supports, for the dropdown. Unknown provider -> {} (no options)."""
    return _BY_PROVIDER.get(str(provider or "").strip().lower(), {})


def is_valid_geo(provider: str | None, code: str | None) -> bool:
    """Is `code` a country this provider can serve?

    A blank code is always valid: it means 'unbind' (re-randomize). Permissive when the provider's
    list is somehow empty, so the endpoint never hard-blocks on missing data.
    """
    norm = normalize_geo(code)
    if not norm:
        return True
    table = supported_geos(provider)
    if not table:
        return True
    return norm in table

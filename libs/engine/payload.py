"""Portal form-body builders, sharing one base field-merge across two distinct POSTs.

storm-auto's proven implementation (confirmed by direct read of app/core/batch_apply.py's
`_build_schedule_payload`) uses the SAME config/form_defaults.json four-section merge for two
different endpoints, each appending different trailing fields:

  ScheduleController (warmup step 5) = base merge + f0sf1=posto_id + __RequestVerificationToken=csrf
  SubmeterVistoCriaPDF (apply step 9) = base merge + f_date_c=<slot date> + cmbPeriodo=<slot period>

`_merge_base_fields` is that shared merge, exactly as `form_defaults.json`'s own `_comment` documents:

  static           -- fixed defaults, never change.
  dynamic          -- resolved via resolve_dynamic (literal date or today+-N(d|yr) expression) at
                      merge time, not import time -- the resolved value must reflect "today" when the
                      booking actually applies, not when the config was authored.
  from_account     -- read from the applicant's identity/sub-account; wins over `dynamic` for the same
                      field when the account value is non-empty (this is `form_defaults.json`'s own
                      documented precedence rule, not a rule this module invented).
  from_nationality -- `{nat}` template fields. The recovered default data templates both `nacionalidade`
                      (nationality) and `pais_residencia` (residence) to the same placeholder, but
                      those are two distinct portal fields by name and by meaning -- this builder
                      resolves each to the correct one of `nationality`/`residence` rather than
                      collapsing them, and defaults the remaining generic-named fields in this section
                      (f6sf1/f7sf1/f8/f15) to `nationality` since that's what the section is labeled
                      for. Revisit this specific split if a real submission shows it's wrong.

`booking_payload()`: the SubmeterVistoCriaPDF (apply step 9) body is deliberately the minimal 5 fields
-- {"lang": "ENG", "txtHuman": "", "back": "", "f_date_c": ..., "cmbPeriodo": ...} -- confirmed by
direct read of storm-auto's ACTUAL, currently-live submit code (batch_apply.py's SubmeterVisto
section, both the browser_form_post and primp-fallback paths: `client.post(SUBMIT_URL, ...,
data={"lang": "ENG", "txtHuman": "", "back": "", "f_date_c": slot_date_form, "cmbPeriodo":
slot_period})`), the code behind 113 confirmed real bookings. An earlier version of this function
built the FULL form_defaults merge here too, on the theory that the portal re-validates the whole
schedule form on submit and silently rejects a minimal body -- that theory is directly contradicted
by storm-auto's own proven code, which has never sent this endpoint anything but these 5 fields. The
full merge genuinely is needed, but only for the OTHER endpoint this module builds a payload for:
ScheduleController (warmup step 5, `schedule_controller_payload()` below) -- a different POST to a
different URL earlier in the flow, not the final submit.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any


class PayloadError(ValueError):
    pass


# Ported from storm-auto batch_apply._resolve_dynamic. Lives here (not in apps/hub/config_store.py,
# which re-exports it) because libs/ must not depend on apps/ -- apps/ builds on libs/, never the
# reverse.
def resolve_dynamic(expr: str, *, today: date | None = None) -> str:
    """Resolve a form_defaults dynamic value.
    - literal `YYYY/MM/DD` or `YYYY-MM-DD` -> normalized `YYYY/MM/DD` (pins a fixed travel date)
    - `today±Nd` / `today±Nyr` -> rolling offset in the portal's `YYYY/MM/DD` format
    - anything else -> "" """
    expr = (expr or "").strip()
    m_lit = re.fullmatch(r"(\d{4})[/-](\d{2})[/-](\d{2})", expr)
    if m_lit:
        return f"{m_lit.group(1)}/{m_lit.group(2)}/{m_lit.group(3)}"
    today = today or date.today()
    m = re.fullmatch(r"today([+-])(\d+)(d|yr)", expr)
    if not m:
        return ""
    sign, n, unit = m.group(1), int(m.group(2)), m.group(3)
    delta = timedelta(days=n) if unit == "d" else timedelta(days=n * 365)
    result = today + delta if sign == "+" else today - delta
    return result.strftime("%Y/%m/%d")


def _merge_base_fields(
    account: dict[str, Any], form_defaults: dict[str, Any], *, nationality: str, residence: str
) -> dict[str, Any]:
    payload: dict[str, Any] = dict(form_defaults.get("static", {}))

    for field, expr in form_defaults.get("dynamic", {}).items():
        payload[field] = resolve_dynamic(expr)

    for field, account_key in form_defaults.get("from_account", {}).items():
        value = account.get(account_key)
        if value:   # from_account wins over dynamic only when non-empty (form_defaults.json's rule)
            payload[field] = value

    for field, template in form_defaults.get("from_nationality", {}).items():
        if field == "pais_residencia":
            payload[field] = template.replace("{nat}", residence)
        else:
            payload[field] = template.replace("{nat}", nationality)

    return payload


def schedule_controller_payload(
    account: dict[str, Any],
    form_defaults: dict[str, Any],
    *,
    posto_id: str,
    csrf: str,
    nationality: str,
    residence: str,
) -> dict[str, Any]:
    """The warmup-time ScheduleController POST body (step 5) -- base fields + posto + the CSRF token
    extracted from the preceding Formulario response. No slot is chosen yet at this stage."""
    payload = _merge_base_fields(account, form_defaults, nationality=nationality, residence=residence)
    payload["f0sf1"] = posto_id
    payload["__RequestVerificationToken"] = csrf
    return payload


def booking_payload(quest_steps: dict[str, list[dict]], slot: dict[str, Any], *, residence: str) -> dict[str, Any]:
    """The apply-time SubmeterVistoCriaPDF POST body (step 9) -- exactly the 5 fields storm-auto's
    proven submit code sends (see the module docstring). `posto_id` travels via the URL query string
    at this step, not the body -- that's built at the call site, matching the confirmed pipeline.
    `quest_steps`/`residence` are used only to fail loudly if this is somehow called for an applicant
    with no valid questionnaire path; the body itself no longer needs the questionnaire data."""
    if residence not in quest_steps:
        raise PayloadError(
            f"no quest_steps entry for residence {residence!r} -- applying without a valid "
            "questionnaire path is a real bug, not something to silently paper over"
        )
    return {
        # PT, matching the confirmed manual booking HAR (data/new_booking.har): the successful
        # SubmeterVistoCriaPDF sends lang=PT, and the portal then renders responses in Portuguese --
        # which is what our detection markers (_HOME_MARKERS etc.) are written for. (ENG version is
        # on branch apply-revert-proven.)
        "lang": "PT",
        "txtHuman": "",
        "back": "",
        "f_date_c": str(slot["date"]).replace("-", "/"),
        "cmbPeriodo": str(slot["period"]),
    }

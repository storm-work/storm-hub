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
-- {"lang": <PT on this branch>, "txtHuman": "", "back": "", "f_date_c": ..., "cmbPeriodo": ...}.
The FIELD SET was confirmed by direct read of storm-auto's ACTUAL, currently-live submit code
(batch_apply.py's SubmeterVisto section, which sent lang=ENG); the PT-version branch keeps the same
five fields but sends lang=PT to match the confirmed completed-booking HAR (post id 3059 france),
which is PT end to end. See booking_payload()'s own body comment. Original ENG citation
(batch_apply.py's SubmeterVisto
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


# ── Questionnaire / Formulario, as the portal's own page sends them ──────────────────────────────
# Reference: reference/EVISA.zip (2026-11-04, posto 5084, nationality GNB / residence CPV) and
# data/new_booking.har. Questionario's goNext() builds every QuestNextQuestion GET from the same five
# form values in this order, empty ones included: question 1 sets nacionalidade, 21 sets
# pais_residencia, 2 sets tipo_passaporte -- each BEFORE its own request goes out.
QUEST_STEP_KEYS = ("id_pergunta", "valor", "lang", "nacionalidade", "pais_residencia", "tipo_passaporte")

# The questionnaire <select> each answered question lives in; questForm submits them to Formulario.
# Question 1 (cb_paises) is absent from the real Formulario body, so it is not listed.
QUEST_SELECT_NAMES = {
    "21": "cb_pais_residencia", "2": "cb_tipo_passaporte", "3": "cb_qt_dias",
    "5": "cb_trab_sazonal", "6": "cb_motivo_estada_sch", "16": "cb_viaja_reune_turismo",
}


def quest_step_params(steps: list[dict], *, nationality: str, residence: str) -> list[dict[str, str]]:
    """One ordered QuestNextQuestion query per step, exactly as goNext() sends it.

    quest_steps is keyed by RESIDENCE and its question-1 answer is a fixed country (CPV for the
    IRL/FRA lists), so the applicant's own nationality and residence override questions 1 and 21:
    a GNB national resident in CPV must answer 1=GNB, not the list's CPV."""
    state = {"nacionalidade": nationality or "", "pais_residencia": "", "tipo_passaporte": ""}
    out = []
    for step in steps:
        qid, valor = str(step.get("id_pergunta", "")), str(step.get("valor", ""))
        if qid == "1":
            valor = nationality or valor
            state["nacionalidade"] = valor
        elif qid == "21":
            valor = residence or valor
            state["pais_residencia"] = valor
        elif qid == "2":
            state["tipo_passaporte"] = valor
        out.append({"id_pergunta": qid, "valor": valor, "lang": "PT", **state})
    return out


def formulario_body(steps: list[dict], last_fields: dict[str, Any], *, nationality: str,
                    residence: str) -> dict[str, Any]:
    """The Formulario?copy=true POST body questForm submits: its hidden fields, the answered selects,
    then the visa-type inputs the last step returned. We used to send only the last-step inputs."""
    params = quest_step_params(steps, nationality=nationality, residence=residence)
    final = params[-1] if params else {"nacionalidade": nationality, "pais_residencia": residence,
                                       "tipo_passaporte": ""}
    body: dict[str, Any] = {"lang": "PT", "nacionalidade": final["nacionalidade"],
                            "pais_residencia": final["pais_residencia"],
                            "tipo_passaporte": final["tipo_passaporte"], "copia_pedido": ""}
    for p in params:
        name = QUEST_SELECT_NAMES.get(p["id_pergunta"])
        if name:
            body[name] = p["valor"]
    for k, v in (last_fields or {}).items():
        body.setdefault(k, v)
    return body


# ScheduleController multipart field order, verbatim from the real vistoForm submit (83 parts).
SC_FIELD_ORDER = (
    "lang txtHuman __RequestVerificationToken RGPDAccepted posto_representacao f0sf1 f1 f2 f3 f4 f6 "
    "f6sf1 f7sf1 f8 f9 f10 txtApelidoPaternal txtNomePaternal txtEnderecoPaternal txtTelefonePaternal "
    "txtEmailPaternal cmbNacionalidadePaternal f5 f13 f14 f16 f17 f15 f43 f43sf2 f43sf3 f43sf4 f43sf5 "
    "f43sf6 f0 f45 f46 f18sf1 f18sf2 f18sf3 f19 f20sf1 f20sf2 f29 f29sf2 txtInfoMotEstada em_destino_1 "
    "em_destino_2 f32 f24 f25 f30 f31 cmbImpressoesDigitais dataImpressoesDigitais "
    "numVinImpressoesDigitais f27 f27sf2 cmbReferencia f34 f34sf3 f34sf2 f34sf4 f34sf5 "
    "cmbDespesasRequerente_1 cmbDespesasRequerente_2 cmbDespesasPatrocinador_1 "
    "cmbDespesasPatrocinador_2 tipo_visto tipo_visto_desc class_visto cod_estada id_visto_doc "
    "tipo_passaporte nacionalidade pais_residencia foto file1 file2 file3 file4 f_date_c cmbPeriodo"
).split()
# Sent as empty FILE parts (filename=""), in place -- never also as text fields.
SC_FILE_FIELDS = ("foto", "file1", "file2", "file3", "file4")
# Values the Formulario page itself carries as hidden inputs: the server's echo of what the
# questionnaire declared, so they win over form_defaults (whose tipo_visto_desc is the ENG label).
FORM_ECHO_KEYS = ("tipo_visto", "tipo_visto_desc", "class_visto", "cod_estada", "id_visto_doc",
                  "tipo_passaporte", "nacionalidade", "pais_residencia")


def _parse_ymd(s: Any) -> date | None:
    m = re.fullmatch(r"(\d{4})[/-](\d{2})[/-](\d{2})", str(s or "").strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def stay_dates_error(fields: dict[str, Any], *, today: date | None = None) -> str | None:
    """Why the arrival (f30) / departure (f31) pair is one the Formulario page would refuse, or None.
    The page blanks f31 and f25 when departure is not after arrival."""
    d30, d31 = _parse_ymd(fields.get("f30")), _parse_ymd(fields.get("f31"))
    if d30 is None or d31 is None:
        return f"arrival/departure not a YYYY/MM/DD date (f30={fields.get('f30')!r} f31={fields.get('f31')!r})"
    if d31 <= d30:
        return f"departure f31={fields.get('f31')} is not after arrival f30={fields.get('f30')}"
    if d30 < (today or date.today()):
        return f"arrival f30={fields.get('f30')} is in the past"
    return None


def schedule_controller_payload(
    account: dict[str, Any],
    form_defaults: dict[str, Any],
    *,
    posto_id: str,
    csrf: str,
    nationality: str,
    residence: str,
    form_echo: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The warmup-time ScheduleController POST body (step 5) -- base fields + posto + the CSRF token
    extracted from the preceding Formulario response. No slot is chosen yet at this stage.
    `form_echo` is the Formulario page's own inputs (FORM_ECHO_KEYS are taken from it). The result is
    in the real form's field order; SC_FILE_FIELDS are the caller's to send as empty file parts."""
    payload = _merge_base_fields(account, form_defaults, nationality=nationality, residence=residence)
    payload["f0sf1"] = posto_id
    payload["__RequestVerificationToken"] = csrf
    for k in FORM_ECHO_KEYS:
        if (form_echo or {}).get(k):
            payload[k] = form_echo[k]
    # f25 is the stay length the page computes from the dates (calcularDuracaoEValidar: days from
    # f30 to f31), not a constant -- form_defaults' static "1" disagreed with every date pair.
    d30, d31 = _parse_ymd(payload.get("f30")), _parse_ymd(payload.get("f31"))
    if d30 and d31 and d31 > d30:
        payload["f25"] = str((d31 - d30).days)
    # PT-version: the confirmed completed-booking HAR (data/ref/...COMPLETO BOOK...post id 3059
    # france.har) POSTs ScheduleController with lang=PT -- the whole flow (home-lang POST ->
    # Formulario -> ScheduleController -> SubmeterVistoCriaPDF) is PT end to end. form_defaults.json
    # still carries the ENG default (it is the canonical config, unchanged on this branch), so we
    # override lang here, exactly as booking_payload() hardcodes PT for the submit. Keeping the flow
    # single-language also makes the portal render Portuguese, which is what our bounce/home markers
    # (_HOME_MARKERS, _PORTAL_BUSY_MARKERS ...) are written for -- see ops_live's lang comment.
    payload["lang"] = "PT"
    rank = {k: i for i, k in enumerate(SC_FIELD_ORDER)}
    return {k: payload[k] for k in sorted(payload, key=lambda k: rank.get(k, len(rank)))}


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

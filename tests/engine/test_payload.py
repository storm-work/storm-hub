import json
from pathlib import Path

import pytest

from libs.engine.payload import PayloadError, booking_payload, schedule_controller_payload

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


@pytest.fixture()
def form_defaults():
    return json.loads((_CONFIG_DIR / "form_defaults.json").read_text())


@pytest.fixture()
def quest_steps():
    return json.loads((_CONFIG_DIR / "quest_steps.json").read_text())


@pytest.fixture()
def account():
    return {
        "email": "test@example.com",
        "last_name": "SILVA",
        "first_name": "JOAO",
        "birthdate": "1990-01-01",
        "gender_expanded": "M",
        "traveldoc": "P1234567",
        "passport_issued": "2020-01-01",
        "passport_expiry": "2030-01-01",
    }


# ── booking_payload (SubmeterVistoCriaPDF, apply step 9) -- confirmed against storm-auto's actual
# proven submit code to be exactly these 5 fields, NOT a form_defaults merge. See
# libs/engine/payload.py's module docstring for the direct-read citation. ──────────────────────
def test_booking_payload_is_exactly_the_five_confirmed_fields(quest_steps):
    # PT-version: the confirmed manual booking HAR submits lang=PT (ENG version is on
    # apply-revert-proven). Five fields otherwise identical.
    payload = booking_payload(quest_steps, {"date": "2026-08-18", "period": 4}, residence="IRL")
    assert payload == {
        "lang": "PT",
        "txtHuman": "",
        "back": "",
        "f_date_c": "2026/08/18",
        "cmbPeriodo": "4",
    }


def test_slot_date_and_period_set_with_slash_format(quest_steps):
    payload = booking_payload(quest_steps, {"date": "2026-08-18", "period": 4}, residence="IRL")
    assert payload["f_date_c"] == "2026/08/18"
    assert payload["cmbPeriodo"] == "4"


def test_unknown_residence_raises_instead_of_silently_applying(quest_steps):
    with pytest.raises(PayloadError):
        booking_payload(quest_steps, {"date": "2026-08-18", "period": 4}, residence="NOWHERE")


@pytest.mark.parametrize("residence", ["IRL", "CPV", "CIV", "GNB", "SEN", "FRA"])
def test_every_known_residence_produces_the_minimal_payload(quest_steps, residence):
    payload = booking_payload(quest_steps, {"date": "2026-08-18", "period": 4}, residence=residence)
    assert payload["cmbPeriodo"] == "4"
    assert set(payload) == {"lang", "txtHuman", "back", "f_date_c", "cmbPeriodo"}


# ── schedule_controller_payload (ScheduleController, warmup step 5) -- the FULL form_defaults
# merge belongs here, confirmed against storm-auto's _build_schedule_payload. This is the only
# place _merge_base_fields' precedence rules are user-visible now that booking_payload doesn't
# merge at all. ──────────────────────────────────────────────────────────────────────────────
def test_schedule_controller_static_fields_copied_verbatim(form_defaults, account):
    payload = schedule_controller_payload(
        account, form_defaults, posto_id="5084", csrf="tok123", nationality="CPV", residence="IRL",
    )
    # PT-version: the whole flow is lang=PT (confirmed completed-booking HAR, post id 3059 france).
    # schedule_controller_payload overrides form_defaults.json's ENG default, same as booking_payload.
    assert payload["lang"] == "PT"
    assert payload["tipo_visto"] == "C"
    assert payload["cmbReferencia"] == "individual"


def test_schedule_controller_from_account_fields_win_over_dynamic(form_defaults, account):
    payload = schedule_controller_payload(
        account, form_defaults, posto_id="5084", csrf="tok123", nationality="CPV", residence="IRL",
    )
    assert payload["f0"] == "test@example.com"
    assert payload["f1"] == "SILVA"
    assert payload["f14"] == "P1234567"
    # f16/f17 exist in BOTH dynamic and from_account -- account value must win (form_defaults.json's
    # own documented precedence rule)
    assert payload["f16"] == "2020-01-01"
    assert payload["f17"] == "2030-01-01"


def test_schedule_controller_dynamic_fields_resolved_when_account_value_absent(form_defaults):
    account_no_dates = {"email": "x@example.com"}   # no passport_issued/expiry -> dynamic wins
    payload = schedule_controller_payload(
        account_no_dates, form_defaults, posto_id="5084", csrf="tok123", nationality="CPV", residence="IRL",
    )
    assert payload["f16"] != ""   # resolved from the "today-3yr" dynamic expression
    assert payload["f30"] == "2026/11/01"   # literal dynamic date, normalized


def test_schedule_controller_nationality_and_residence_kept_distinct(form_defaults, account):
    payload = schedule_controller_payload(
        account, form_defaults, posto_id="5084", csrf="tok123", nationality="CPV", residence="IRL",
    )
    assert payload["nacionalidade"] == "CPV"
    assert payload["pais_residencia"] == "IRL"
    # the remaining generic from_nationality fields default to nationality
    assert payload["f8"] == "CPV"
    assert payload["f15"] == "CPV"


def test_schedule_controller_payload_fields(form_defaults, account):
    # Confirmed against storm-auto's proven _build_schedule_payload.
    payload = schedule_controller_payload(
        account, form_defaults, posto_id="5084", csrf="tok123", nationality="CPV", residence="IRL",
    )
    assert payload["f0sf1"] == "5084"
    assert payload["__RequestVerificationToken"] == "tok123"
    # f_date_c/cmbPeriodo are static placeholder keys (empty string) at this stage -- no slot has
    # been chosen yet, unlike booking_payload() which is built once a slot IS chosen.
    assert payload["f_date_c"] == ""
    assert payload["cmbPeriodo"] == ""


# ── reference/EVISA.zip (2026-11-04, posto 5084, GNB national resident in CPV) ──────────────────
from datetime import date  # noqa: E402

from libs.engine.payload import (SC_FIELD_ORDER, formulario_body, quest_step_params,  # noqa: E402
                                 stay_dates_error)


def test_quest_steps_are_shaped_like_gonext(quest_steps):
    """goNext() sends the same six params in a fixed order, empties included, and question 1/21
    carry the applicant's own nationality/residence even when the residence list says CPV."""
    params = quest_step_params(quest_steps["CPV"], nationality="GNB", residence="CPV")
    assert list(params[0]) == ["id_pergunta", "valor", "lang", "nacionalidade", "pais_residencia",
                               "tipo_passaporte"]
    assert params[0] == {"id_pergunta": "1", "valor": "GNB", "lang": "PT", "nacionalidade": "GNB",
                         "pais_residencia": "", "tipo_passaporte": ""}
    assert params[1]["valor"] == "CPV" and params[1]["pais_residencia"] == "CPV"
    assert params[1]["tipo_passaporte"] == ""
    assert params[2]["tipo_passaporte"] == "01" and params[-1]["tipo_passaporte"] == "01"


def test_formulario_body_matches_the_real_questform(quest_steps):
    last = {"tipo_visto": "C", "tipo_visto_desc": "VISTO DE CURTA DURAÇÃO", "class_visto": "SCH",
            "cod_estada": "10", "id_visto_doc": "36"}
    body = formulario_body(quest_steps["CPV"], last, nationality="GNB", residence="CPV")
    # Field-for-field the HAR's Formulario?copy=true body (quest_steps CPV answers 6=10 TURISMO).
    assert list(body.items()) == [
        ("lang", "PT"), ("nacionalidade", "GNB"), ("pais_residencia", "CPV"),
        ("tipo_passaporte", "01"), ("copia_pedido", ""), ("cb_pais_residencia", "CPV"),
        ("cb_tipo_passaporte", "01"), ("cb_qt_dias", "SCH"), ("cb_trab_sazonal", "O"),
        ("cb_motivo_estada_sch", "10"), ("cb_viaja_reune_turismo", "FAM_N"),
        *last.items()]


def test_schedule_controller_order_echo_and_stay_length(form_defaults, account):
    fd = {**form_defaults, "dynamic": {**form_defaults["dynamic"], "f30": "2027/02/20",
                                       "f31": "2027/02/27"}}
    p = schedule_controller_payload(
        account, fd, posto_id="5084", csrf="t", nationality="GNB", residence="CPV",
        form_echo={"tipo_visto_desc": "VISTO DE CURTA DURAÇÃO", "nacionalidade": "GNB", "lang": "ENG"})
    assert list(p) == list(SC_FIELD_ORDER)               # same 83 fields, the real form's order
    assert p["f25"] == "7"                                # days from f30 to f31, as the page computes
    assert p["tipo_visto_desc"] == "VISTO DE CURTA DURAÇÃO"   # server's PT label, not ENG default
    assert p["lang"] == "PT"                              # echo only covers FORM_ECHO_KEYS


def test_inverted_dates_keep_f25_and_are_reported(form_defaults, account):
    fd = {**form_defaults, "dynamic": {**form_defaults["dynamic"], "f30": "2027/01/03",
                                       "f31": "2026/01/10"}}
    p = schedule_controller_payload(account, fd, posto_id="5084", csrf="t", nationality="CPV",
                                    residence="IRL")
    assert p["f25"] == form_defaults["static"]["f25"]
    assert "not after arrival" in stay_dates_error(p, today=date(2026, 11, 5))
    assert stay_dates_error({"f30": "2027/02/20", "f31": "2027/02/27"}, today=date(2026, 11, 5)) is None
    assert "past" in stay_dates_error({"f30": "2026/02/20", "f31": "2026/02/27"},
                                      today=date(2026, 11, 5))

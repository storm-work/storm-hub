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
    payload = booking_payload(quest_steps, {"date": "2026-08-18", "period": 4}, residence="IRL")
    assert payload == {
        "lang": "ENG",
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
    assert payload["lang"] == "ENG"
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

"""ScheduleController answers 200 whether it accepted the form or bounced it.

Measured across 17 live warm-ups on 2026-08-20, the response size split perfectly bimodally:

    warmed (10):  54,053 - 54,171 bytes
    failed  (7): 1,272,769 - 1,272,787 bytes     <- the form page re-rendered with its errors

and the SAME identities failed every time, byte-for-byte. The body was fetched and then discarded,
so a deterministic, per-identity data rejection surfaced only as an unexplained "ScheduleController
was rejected". _form_error_text pulls the validation messages out of that body.
"""
from __future__ import annotations

from apps.worker.ops_live import _form_error_text


def test_extracts_error_classed_elements():
    html = """<html><body>
      <span class="msgErro">O campo Data de Nascimento e obrigatorio</span>
      <div id="erroPassaporte">Numero de passaporte invalido</div>
    </body></html>"""
    assert _form_error_text(html) == [
        "O campo Data de Nascimento e obrigatorio",
        "Numero de passaporte invalido",
    ]


def test_deduplicates_repeated_messages():
    """The portal repeats the same message next to every offending field."""
    html = "".join(
        '<span class="error">Campo obrigatorio</span>' for _ in range(12)
    )
    assert _form_error_text(html) == ["Campo obrigatorio"]


def test_falls_back_to_validation_vocabulary_when_no_error_class():
    html = "<html><body><td>Preencha o campo obrigatorio Foto</td></body></html>"
    assert _form_error_text(html) == ["Preencha o campo obrigatorio Foto"]


def test_ignores_scripts_and_styles():
    """A 1.27MB body is mostly inline script -- matching inside it would produce noise, not errors."""
    html = """<html><body>
      <script>var msg = "erro inesperado no javascript";</script>
      <style>.erro { color: red; }</style>
      <span class="msgErro">Campo invalido</span>
    </body></html>"""
    assert _form_error_text(html) == ["Campo invalido"]


def test_is_capped_so_it_cannot_flood_the_capped_event_stream():
    """This text goes to the shared Redis evt stream -- an unbounded dump ages out real history."""
    html = "".join(
        f'<span class="error">Erro numero {i}</span>' for i in range(200)
    )
    out = _form_error_text(html)
    assert len(out) == 5


def test_long_and_empty_chunks_are_skipped():
    html = ('<span class="error"></span>'
            '<span class="error">' + "x" * 500 + "</span>"
            '<span class="error">Data invalida</span>')
    assert _form_error_text(html) == ["Data invalida"]


def test_empty_body_is_not_an_error():
    assert _form_error_text("") == []
    assert _form_error_text(None) == []


def test_an_accepted_page_yields_nothing():
    """No false positives on a normal page -- otherwise the log lies about why a warm-up failed."""
    html = "<html><body><div id='cmbPeriodo'>ok</div><a href='getSlots'>x</a></body></html>"
    assert _form_error_text(html) == []

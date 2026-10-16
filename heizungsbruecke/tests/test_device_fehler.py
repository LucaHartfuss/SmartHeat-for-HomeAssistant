"""Spec Stufe 3 S5 6.1: details der Fehler an den Server, nur in der Form des Vertrags."""
import math

import pytest

from smartheat_device import fehler, wire


def test_details_in_der_form_des_vertrags():
    assert fehler.details("raumfuehler", "anzahl", min=1, max=8, anzahl=9) == {
        "feld": "raumfuehler", "regel": "anzahl", "min": 1, "max": 8, "anzahl": 9}
    assert fehler.details() == {} and fehler.gueltig(None)


@pytest.mark.parametrize("kwargs", [{"feld": "entity_id"}, {"regel": "erfunden"}, {"feld": "seconds", "min": True},
                                    {"wert": math.nan}, {"anzahl": "3"}])
def test_verstoesse_sind_programmierfehler(kwargs):
    with pytest.raises(ValueError):
        fehler.details(**kwargs)


def test_body_ohne_text():
    assert fehler.body("intern", None) == {"grund": "intern"}
    assert fehler.body("ungueltig", {"feld": "tagestick", "regel": "format"}) == {
        "grund": "ungueltig", "details": {"feld": "tagestick", "regel": "format"}}
    assert set(fehler.body("x", {"regel": "fehlt"})) <= set(wire.ERROR_FIELDS)


def test_gueltig_lehnt_fremde_schluessel_und_texte_ab():
    assert not fehler.gueltig({"text": "frei"}) and not fehler.gueltig("frei")
    assert not fehler.gueltig({"feld": "<script>"}) and fehler.gueltig({"feld": "pruefung_s", "max": 3600})

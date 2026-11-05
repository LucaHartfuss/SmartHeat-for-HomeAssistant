"""Zustand je Kreis (`kreise[n].zustand`): die Pruefung nimmt die drei Werte des Vertrags an und lehnt jeden anderen laut
ab; ein fehlendes Feld liest die Sicht als `in_betrieb`; der Wert steht je Kreis bereit, hat aber noch keine Wirkung
(ein Wechsel startet die Laufzeit nicht neu)."""
import copy

import pytest
from geraet_helpers import gateway_konfiguration
from test_core_config_check_kreise import LOCK, _geprueft, _gw, _gw_kreis

from smartheat_core import config_check as cc
from smartheat_core import kreisansicht
from smartheat_device import wire
from smartheat_runtime import dokument_config as dc

ZUSTAENDE = ("in_betrieb", "ausser_betrieb", "testphase_ohne")


def _mit_zustand(kreis: dict, zustand) -> dict:
    return {**kreis, "zustand": zustand}


def test_der_vertrag_und_die_pruefung_kennen_dieselben_zustaende():
    assert wire.KREIS_ZUSTAENDE == ZUSTAENDE == cc.KREIS_ZUSTAENDE
    assert "zustand" in wire.KONFIGURATION_KREIS_OPTIONAL and "zustand" in cc.KONFIG_KREIS_OPTIONAL
    assert {"testphase", "kaelteschutz"} <= set(wire.CAPABILITY_OPTIONAL)


@pytest.mark.parametrize("zustand", ZUSTAENDE)
def test_die_pruefung_nimmt_jeden_der_drei_zustaende_an(zustand):
    assert _geprueft(_gw(_mit_zustand(_gw_kreis(1), zustand)), LOCK) is None


def test_ein_kreis_ohne_zustandsfeld_ist_gueltig():
    assert "zustand" not in _gw_kreis(1)
    assert _geprueft(_gw(_gw_kreis(1)), LOCK) is None


@pytest.mark.parametrize("fremd", ["ausser-betrieb", "IN_BETRIEB", "", "aus", None, 1, True, ["in_betrieb"], {}])
def test_ein_fremder_zustand_wird_laut_abgelehnt(fremd):
    ablehnung = _geprueft(_gw(_mit_zustand(_gw_kreis(1), fremd)), LOCK)
    assert ablehnung == cc.Ablehnung(cc.UNGUELTIG, cc.befund("zustand", "unbekannt"))


def test_ein_fremder_zustand_im_zweiten_kreis_lehnt_das_ganze_dokument_ab():
    neu = _gw(_gw_kreis(1), _mit_zustand(_gw_kreis(2), "pause"))
    assert _geprueft(neu, LOCK) == cc.Ablehnung(cc.UNGUELTIG, cc.befund("zustand", "unbekannt"))


def test_ein_anderes_fremdes_kreisfeld_bleibt_abgelehnt():
    neu = _gw(_mit_zustand(_gw_kreis(1), "in_betrieb") | {"modus": "x"})
    assert _geprueft(neu, LOCK) == cc.Ablehnung(cc.UNGUELTIG, cc.befund("kreise", "unbekannt"))


def _dok(*zustaende):
    kreise = [_gw_kreis(n) if z is None else _mit_zustand(_gw_kreis(n), z) for n, z in enumerate(zustaende, start=1)]
    return _gw(*kreise)


@pytest.mark.parametrize("zustand", ZUSTAENDE)
def test_der_zustand_steht_je_kreis_bereit(zustand):
    assert kreisansicht.zustand(_dok(zustand), 1) == zustand


def test_ein_fehlendes_feld_liest_sich_als_in_betrieb():
    assert kreisansicht.zustand(_dok(None), 1) == "in_betrieb"
    assert kreisansicht.zustand(_dok(None, "ausser_betrieb")) == "in_betrieb"  # ohne Nummer: der erste Kreis


def test_zwei_kreise_haben_je_ihren_zustand():
    dokument = _dok("ausser_betrieb", "testphase_ohne", None)
    assert [kreisansicht.zustand(dokument, n) for n in (1, 2, 3)] == ["ausser_betrieb", "testphase_ohne", "in_betrieb"]


def test_ein_unbekannter_kreis_und_fremdinhalt_sind_in_betrieb():
    assert kreisansicht.zustand(_dok("ausser_betrieb"), 7) == "in_betrieb"
    assert kreisansicht.zustand({"kreise": [{"kreis": 1, "zustand": "fremd"}]}, 1) == "in_betrieb"
    assert kreisansicht.zustand("kein Dokument", 1) == "in_betrieb"


def test_die_leere_konfiguration_ist_in_betrieb():
    assert kreisansicht.zustand({"anlage": None, "kreise": []}, 1) == "in_betrieb"


def test_ein_wechsel_des_zustands_startet_die_laufzeit_nicht_neu():
    alt, neu = _dok("in_betrieb", "in_betrieb"), _dok("ausser_betrieb", "testphase_ohne")
    assert dc.laufzeit_relevant_kreis(alt, neu, 1) is False
    assert dc.laufzeit_relevant_kreis(alt, neu, 2) is False
    assert dc.laufzeit_relevant(alt, neu) is False


def test_eine_andere_aenderung_im_kreis_startet_weiter_neu():
    alt = _dok("in_betrieb")
    neu = copy.deepcopy(alt)
    neu["kreise"][0]["zustand"] = "ausser_betrieb"
    neu["kreise"][0]["verteilsystem"] = "Fussbodenheizung"
    assert dc.laufzeit_relevant_kreis(alt, neu, 1) is True


def test_gateway_konfiguration_ohne_zustandsfeld_bleibt_unveraendert():
    assert all("zustand" not in kreis for kreis in gateway_konfiguration()["kreise"])

"""Das Abo gilt je Kreis: `kreise[n].abo` bestimmt den Status des Kreises n. Der Host startet nur bei einer
laufzeitrelevanten Aenderung neu; das Wiederaufnehmen eines Kreises (Abo wieder aktiv) ist keine."""
import copy

import pytest

from smartheat_core import kreisansicht
from smartheat_runtime import dokument_config as dc
from smartheat_runtime import entitlement


def _dok(*abos, **anlage):
    kreise = [{"kreis": n, "name": f"K{n}", "bindung": {"x": n}, "hebelsatz": "vaillant_vrc720",
               "verteilsystem": "Heizkoerper", "abo": abo, "raeume": []} for n, abo in enumerate(abos, start=1)]
    return {"anlage": "kunde3", "weg": None, "tagestick": "12:00", "bindungsparameter": {"p": 1}, "kreise": kreise,
            **anlage}


@pytest.mark.parametrize("abo, status", [
    ("aktiv", entitlement.ACTIVE), ("inaktiv", entitlement.INACTIVE), (None, entitlement.UNKNOWN),
    ("vielleicht", entitlement.UNKNOWN),
])
def test_der_abo_status_kommt_vom_gefragten_kreis(abo, status):
    assert dc.abo_status(_dok("aktiv", abo), 2) == status
    assert dc.abo_status(_dok(abo, "aktiv"), 1) == status


def test_ein_unbekannter_kreis_hat_kein_abo():
    assert dc.abo_status(_dok("aktiv"), 7) == entitlement.UNKNOWN


def test_ohne_kreisnummer_gilt_der_erste_kreis():
    assert dc.abo_status(_dok("inaktiv", "aktiv")) == entitlement.INACTIVE


def test_die_sicht_eines_kreises_nach_nummer():
    dokument = _dok("aktiv", "inaktiv")
    assert kreisansicht.fuer(dokument, 2)["name"] == "K2"
    assert kreisansicht.fuer(dokument, 3)["kreis"] is None  # ohne Kreisdaten


def test_wiederaufnahme_ist_keine_laufzeitrelevante_aenderung_des_kreises():
    alt, neu = _dok("inaktiv", "aktiv"), _dok("aktiv", "aktiv")
    assert dc.laufzeit_relevant(alt, neu)  # das Dokument als Ganzes hat sich geaendert
    assert not dc.laufzeit_relevant_kreis(alt, neu, 1)
    assert dc.abo_wiederaufnahme(alt, neu, 1)
    assert not dc.abo_wiederaufnahme(alt, neu, 2)


def test_ein_abo_wechsel_auf_inaktiv_bleibt_relevant():
    assert dc.laufzeit_relevant_kreis(_dok("aktiv"), _dok("inaktiv"), 1)
    assert not dc.abo_wiederaufnahme(_dok("aktiv"), _dok("inaktiv"), 1)


def test_eine_aenderung_eines_anderen_kreises_beruehrt_diesen_kreis_nicht():
    alt = _dok("aktiv", "aktiv")
    neu = copy.deepcopy(alt)
    neu["kreise"][1]["hebelsatz"] = "weishaupt_wwp"
    assert not dc.laufzeit_relevant_kreis(alt, neu, 1)
    assert dc.laufzeit_relevant_kreis(alt, neu, 2)


@pytest.mark.parametrize("feld, wert", [("anlage", "kunde4"), ("tagestick", "13:00"), ("weg", "x"),
                                        ("bindungsparameter", {"p": 2})])
def test_anlagenweite_felder_betreffen_jeden_kreis(feld, wert):
    alt = _dok("aktiv", "aktiv")
    assert dc.laufzeit_relevant_kreis(alt, {**alt, feld: wert}, 1)
    assert dc.laufzeit_relevant_kreis(alt, {**alt, feld: wert}, 2)


def test_ein_neuer_oder_entfernter_kreis_ist_relevant():
    assert dc.laufzeit_relevant_kreis(_dok("aktiv"), _dok("aktiv", "aktiv"), 2)
    assert dc.laufzeit_relevant_kreis(_dok("aktiv", "aktiv"), _dok("aktiv"), 2)


def test_ursprungswerte_starten_nichts_neu():
    alt = _dok("aktiv")
    neu = copy.deepcopy(alt)
    neu["kreise"][0]["ursprungswerte"] = {"curve": 1.0}
    assert not dc.laufzeit_relevant_kreis(alt, neu, 1)

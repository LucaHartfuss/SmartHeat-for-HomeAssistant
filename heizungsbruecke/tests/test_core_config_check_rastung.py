"""Lokale Pruefung gegen die Rastung je Kreis: Schreibziele, Soll-Entity, Anzahl der Kreise."""
import copy

import konfig_form
import pytest
from geraet_helpers import gateway_konfiguration, ha_konfiguration
from rastung_beispiele import rastung_aus

from smartheat_core import config_check as cc
from smartheat_core import rastung
from smartheat_device import fehler

EINGERASTET = cc.Ablehnung(cc.HEBELSATZ_EINGERASTET)


def _kreis(nummer: int, *, heizkreis=None, thermostat=None, **flach) -> dict:
    bindung = {"treiber": {"id": "simulation", "parameter": {"heizkreis": nummer if heizkreis is None else heizkreis}},
               "thermostat": f"0x{100 + nummer:016x}" if thermostat is None else thermostat}
    quelle = {"bindung": bindung, "raumfuehler": [f"zigbee:0x{nummer:016x}:temperature"],
              "hebelsatz": "viessmann_vicare", "verteilsystem": "Heizkoerper", "abo": "aktiv", **flach}
    return konfig_form.kreis(quelle, nummer=nummer)


def _gw(*nummern, **kreis) -> dict:
    return gateway_konfiguration(kreise=[_kreis(n, **kreis) for n in nummern])


def _geprueft(neu, lock, host=cc.HOST_GATEWAY):
    ablehnung = cc.pruefen(neu, lock, host=host)
    if ablehnung is not None:
        assert not hasattr(ablehnung, "text") and fehler.gueltig(ablehnung.details), ablehnung
    return ablehnung


def test_the_same_pair_with_other_write_targets_is_rejected():
    lock = rastung_aus(_gw(1))
    assert _geprueft(_gw(1), lock) is None
    assert _geprueft(_gw(1, heizkreis=3), lock) == EINGERASTET
    assert _geprueft(_kreis_mit_treiber_parameter(), lock) == EINGERASTET


def _kreis_mit_treiber_parameter():
    neu = _gw(1)
    neu["kreise"][0]["bindung"]["treiber"]["parameter"]["schreibschutz"] = False
    return neu


def test_a_new_circle_number_with_the_same_targets_is_accepted():
    lock = rastung_aus(_gw(1))
    neu = gateway_konfiguration(kreise=[_kreis(5, heizkreis=1, thermostat=f"0x{101:016x}")])
    assert [k["kreis"] for k in neu["kreise"]] == [5]
    assert _geprueft(neu, lock) is None


def test_more_circles_than_the_lock_knows_are_rejected():
    lock = rastung_aus(_gw(1))
    assert _geprueft(_gw(1, 2), lock) == EINGERASTET
    assert _geprueft(_gw(1, 2, 3, 4), lock) == EINGERASTET


def test_a_circle_without_an_entry_is_rejected_but_fewer_circles_are_fine():
    lock = rastung_aus(_gw(1, 2))
    assert _geprueft(_gw(1, 2), lock) is None
    assert _geprueft(_gw(1), lock) is None
    assert _geprueft(_gw(2), lock) is None
    assert _geprueft(_gw(1, 3), lock) == EINGERASTET


def test_an_empty_document_is_valid_and_loosens_nothing():
    lock = rastung_aus(_gw(1))
    assert _geprueft({"anlage": None, "raumfuehler": []}, lock) is None
    assert _geprueft(_gw(1, heizkreis=9), lock) == EINGERASTET   # danach gilt die Rastung unverandert weiter
    ohne_kreise = copy.deepcopy(_gw(1))
    ohne_kreise["kreise"] = []
    assert _geprueft(ohne_kreise, lock) is not None


def test_a_lock_in_the_old_format_rejects_every_document(tmp_path):
    pfad = tmp_path / rastung.DATEI
    pfad.write_text('{"hebelsatz": "viessmann_vicare", "verteilsystem": "Heizkoerper"}')
    assert _geprueft(_gw(1), rastung.lesen(pfad)) == EINGERASTET


def test_the_pair_is_checked_before_the_bindings_and_the_targets_after_them():
    lock = rastung_aus(_gw(1))
    fremd = _gw(1, heizkreis=3)
    fremd["kreise"][0]["hebelsatz"] = "weishaupt_wwp"
    assert _geprueft(fremd, lock) == EINGERASTET
    kaputt = _gw(1, heizkreis=3)
    kaputt["kreise"][0]["bindung"]["thermostat"] = "kein-thermostat"
    ablehnung = _geprueft(kaputt, lock)
    assert ablehnung is not None and ablehnung.grund != cc.HEBELSATZ_EINGERASTET


# --- Home Assistant -------------------------------------------------------------------------------------------------

def _ha(**rollen) -> dict:
    return ha_konfiguration(bindung={"rollen": {**ha_konfiguration()["kreise"][0]["bindung"]["rollen"], **rollen}})


def _ha_pruefen(neu, lock):
    return _geprueft(neu, lock, host=cc.HOST_HA)


def test_home_assistant_write_entities_and_the_soll_entity_are_locked():
    lock = rastung_aus(_ha())
    assert _ha_pruefen(_ha(), lock) is None
    assert _ha_pruefen(_ha(curve="number.zuhause_circuit_1_heating_curve"), lock) == EINGERASTET
    assert _ha_pruefen(_ha(room_setpoint="climate.zuhause_zone_1_circuit_1_climate"), lock) == EINGERASTET
    assert _ha_pruefen(_ha(room_target="climate.anderes_thermostat::temperature"), lock) == EINGERASTET


def test_a_home_assistant_config_entry_or_circle_change_is_locked():
    lock = rastung_aus(_ha())
    neu = _ha()
    neu["kreise"][0]["bindung"]["kreis"]["config_entry"] = "e2"
    assert _ha_pruefen(neu, lock) == EINGERASTET
    neu = _ha()
    neu["kreise"][0]["bindung"]["kreis"]["nummer"] = "1"
    assert _ha_pruefen(neu, lock) == EINGERASTET


@pytest.mark.parametrize("rolle", ["outdoor_temp", "flow_temperature", "system_water_pressure"])
def test_a_home_assistant_read_role_can_change_without_a_new_setup(rolle):
    lock = rastung_aus(_ha())
    assert _ha_pruefen(_ha(**{rolle: "sensor.ein_neuer_fuehler"}), lock) is None

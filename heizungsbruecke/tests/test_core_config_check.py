"""Spec 5b 3.1: lokale Pruefung der Konfiguration, jede Regel mit Positiv- und Negativfall (Spec 5b 9)."""
import pytest

from smartheat_core import config_check as cc
from smartheat_device import wire
from smartheat_runtime import options

SENSOR = "zigbee:0x00124b0000000001:temperature"
THERMOSTAT = "0x00124b0000000002"


def _konfiguration(**changes):
    value = {
        "anlage": "kunde3", "profil": "viessmann_gastherme_heizkoerper", "hebelsatz": "viessmann_vicare",
        "verteilsystem": "Heizkoerper",
        "bindung": {"treiber": {"id": "simulation", "parameter": {"heizkreis": 0}}, "thermostat": THERMOSTAT},
        "raumfuehler": [SENSOR], "tagestick": "12:12", "abo": "aktiv", "mindest_software": "0.6.0", "software": None,
    }
    value.update(changes)
    return value


def _pruefen(neu, vorher=None, **kwargs):
    return cc.pruefen(neu, vorher, host=cc.HOST_GATEWAY, **kwargs)


def test_a_valid_gateway_configuration_passes():
    assert _pruefen(_konfiguration()) is None


def test_an_empty_configuration_always_passes():
    assert _pruefen({"anlage": None, "raumfuehler": []}, _konfiguration()) is None
    assert cc.ist_leer({"anlage": None}) and not cc.ist_leer(_konfiguration())


def test_no_object_is_invalid():
    assert _pruefen(["kein", "objekt"]).grund == cc.UNGUELTIG


@pytest.mark.parametrize("change", [
    {"anlage": ""}, {"anlage": 5}, {"anlage": "x" * 65}, {"profil": None}, {"hebelsatz": None},
    {"verteilsystem": 3}, {"bindung": []}, {"raumfuehler": []}, {"raumfuehler": "x"}, {"raumfuehler": [5]},
    {"raumfuehler": [SENSOR] * 9}, {"tagestick": "25:00"}, {"tagestick": None}, {"abo": "vielleicht"},
    {"abo": None}, {"intervalle": {"pruefung_s": 0}}, {"intervalle": {"telemetrie_s": 601}},
    {"intervalle": {"telemetrie_s": True}}, {"intervalle": {"anders": 5}}, {"intervalle": [5]},
])
def test_structure_and_ranges(change):
    assert _pruefen(_konfiguration(**change)).grund == cc.UNGUELTIG


def test_test_intervals_pass():
    assert _pruefen(_konfiguration(intervalle={"pruefung_s": 5, "telemetrie_s": 10})) is None


def test_interval_bounds_are_the_bounds_of_the_options():
    assert cc.TELEMETRIE_S == (10, options.MAX_TELEMETRY_INTERVAL_SECONDS)
    assert cc.PRUEFUNG_S == (1, 3600)


@pytest.mark.parametrize("hebelsatz, verteilsystem", [("unbekannt", "Heizkoerper"), ("viessmann_vicare", "Kachelofen")])
def test_missing_local_safety_values(hebelsatz, verteilsystem):
    assert _pruefen(_konfiguration(hebelsatz=hebelsatz, verteilsystem=verteilsystem)).grund == cc.SICHERHEITSWERTE_FEHLEN


def test_floor_heating_has_safety_values():
    assert _pruefen(_konfiguration(verteilsystem="Fussbodenheizung")) is None


@pytest.mark.parametrize("change", [{"verteilsystem": "Fussbodenheizung"}, {"hebelsatz": "weishaupt_wwp"}])
def test_lever_set_and_distribution_are_locked_while_set_up(change):
    assert _pruefen(_konfiguration(**change), _konfiguration()).grund == cc.HEBELSATZ_EINGERASTET


def test_a_change_through_not_set_up_is_allowed():
    assert _pruefen(_konfiguration(verteilsystem="Fussbodenheizung"), {"anlage": None}) is None


def test_reconfiguring_with_the_same_lever_set_is_allowed():
    assert _pruefen(_konfiguration(raumfuehler=["treiber:room_temperature"]), _konfiguration()) is None


@pytest.mark.parametrize("ref", [
    "sensor.wohnzimmer", "zigbee:0x00124b0000000001:humidity", "zigbee:0x1:temperature", "treiber:outdoor_temp",
])
def test_room_sensors_must_be_gateway_references(ref):
    assert _pruefen(_konfiguration(raumfuehler=[ref])).grund == cc.ROLLE_UNZULAESSIG


@pytest.mark.parametrize("ref", [SENSOR, "zigbee:0x00124b0000000002:local_temperature", "treiber:room_temperature"])
def test_allowed_room_sensors(ref):
    assert _pruefen(_konfiguration(raumfuehler=[ref])) is None


@pytest.mark.parametrize("bindung", [
    {"treiber": {"id": "simulation", "parameter": {}}, "thermostat": "wohnzimmer"},
    {"treiber": {"id": "", "parameter": {}}, "thermostat": None},
    {"treiber": {"id": "simulation"}, "thermostat": None},
    {"thermostat": None},
])
def test_binding_roles(bindung):
    assert _pruefen(_konfiguration(bindung=bindung)).grund == cc.ROLLE_UNZULAESSIG


def test_without_thermostat():
    assert _pruefen(_konfiguration(bindung={"treiber": {"id": "simulation", "parameter": {}}, "thermostat": None})) is None


def test_a_room_sensor_twice_is_a_double_assignment():
    ablehnung = _pruefen(_konfiguration(raumfuehler=[SENSOR, SENSOR]))
    assert ablehnung.grund == cc.DOPPELT_BELEGT and SENSOR in ablehnung.text


def test_driver_parameters_are_checked_by_the_host():
    seen = []

    def treiber(bindung):
        seen.append(bindung["treiber"]["id"])
        return "Heizkreis 7 gibt es an dieser Anlage nicht."

    ablehnung = _pruefen(_konfiguration(), treiber_pruefen=treiber)
    assert (ablehnung.grund, ablehnung.text, seen) == (
        cc.TREIBER_PARAMETER, "Heizkreis 7 gibt es an dieser Anlage nicht.", ["simulation"])
    assert _pruefen(_konfiguration(), treiber_pruefen=lambda bindung: None) is None


def test_the_driver_is_asked_only_for_an_otherwise_valid_configuration():
    seen = []
    _pruefen(_konfiguration(raumfuehler=[SENSOR, SENSOR]), treiber_pruefen=lambda bindung: seen.append(1))
    assert seen == []


def test_home_assistant_bindings_follow_with_5c():
    assert cc.pruefen(_konfiguration(), None, host=cc.HOST_HA).grund == cc.ROLLE_UNZULAESSIG


def test_reasons_are_contract_reasons():
    assert set(cc.GRUENDE) <= set(wire.DOKUMENT_ERRORS)

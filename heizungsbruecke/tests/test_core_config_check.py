"""Spec 5b 3.1: lokale Pruefung der Konfiguration, jede Regel mit Positiv- und Negativfall (Spec 5b 9)."""
import pytest
from bindung_beispiele import parameter

from smartheat_core import config_check as cc
from smartheat_core import rastung
from smartheat_core.rastung import Rastung
from smartheat_device import fehler, wire

SENSOR = "zigbee:0x00124b0000000001:temperature"
THERMOSTAT = "0x00124b0000000002"


def _konfiguration(**changes):
    value = {
        "anlage": "kunde3", "profil": "viessmann_gastherme_heizkoerper", "hebelsatz": "viessmann_vicare",
        "verteilsystem": "Heizkoerper",
        "bindung": {"treiber": {"id": "simulation", "parameter": {"heizkreis": 0}}, "thermostat": THERMOSTAT},
        "raumfuehler": [SENSOR], "tagestick": "12:12", "abo": "aktiv", "mindest_software": "0.6.0", "software": None,
        "bindungsparameter": parameter("viessmann_vicare"),
    }
    value.update(changes)
    return value


LOCK = Rastung("viessmann_vicare", "Heizkoerper")


def _form(ablehnung):
    if ablehnung is not None:
        assert not hasattr(ablehnung, "text") and fehler.gueltig(ablehnung.details), ablehnung
    return ablehnung


def _pruefen(neu, rastung=None, **kwargs):
    return _form(cc.pruefen(neu, rastung, host=cc.HOST_GATEWAY, **kwargs))


def test_a_valid_gateway_configuration_passes():
    assert _pruefen(_konfiguration()) is None


def test_an_empty_configuration_always_passes():
    assert _pruefen({"anlage": None, "raumfuehler": []}, LOCK) is None
    assert _pruefen({"anlage": None, "raumfuehler": [], "bindungsparameter": None}) is None
    assert cc.ist_leer({"anlage": None}) and not cc.ist_leer(_konfiguration())


def test_no_object_is_invalid():
    assert _pruefen(["kein", "objekt"]).grund == cc.UNGUELTIG


@pytest.mark.parametrize("change", [
    {"anlage": ""}, {"anlage": 5}, {"anlage": "x" * 65}, {"profil": None}, {"hebelsatz": None},
    {"verteilsystem": 3}, {"bindung": []}, {"raumfuehler": []}, {"raumfuehler": "x"}, {"raumfuehler": [5]},
    {"raumfuehler": [SENSOR] * 9}, {"tagestick": "25:00"}, {"tagestick": None}, {"abo": "vielleicht"},
    {"abo": None},
])
def test_structure_and_ranges(change):
    assert _pruefen(_konfiguration(**change)).grund == cc.UNGUELTIG


def test_interval_bounds():
    assert cc.TELEMETRIE_S == (10, 600)
    assert cc.ABFRAGE_S == (10, 3600)
    assert cc.PRUEFUNG_S == (1, 3600)


@pytest.mark.parametrize("hebelsatz, verteilsystem", [("unbekannt", "Heizkoerper"), ("viessmann_vicare", "Kachelofen")])
def test_missing_local_safety_values(hebelsatz, verteilsystem):
    assert _pruefen(_konfiguration(hebelsatz=hebelsatz, verteilsystem=verteilsystem)).grund == cc.SICHERHEITSWERTE_FEHLEN


def test_floor_heating_has_safety_values():
    assert _pruefen(_konfiguration(verteilsystem="Fussbodenheizung")) is None


@pytest.mark.parametrize("change", [{"verteilsystem": "Fussbodenheizung"}, {"hebelsatz": "weishaupt_wwp"}])
def test_lever_set_and_distribution_are_locked(change):
    assert _pruefen(_konfiguration(**change), LOCK) == cc.Ablehnung(cc.HEBELSATZ_EINGERASTET)


def test_without_lock_the_first_choice_is_free():
    assert _pruefen(_konfiguration(verteilsystem="Fussbodenheizung"), None) is None


def test_a_broken_lock_rejects_even_the_same_pair():
    assert _pruefen(_konfiguration(), rastung.UNBEKANNT).grund == cc.HEBELSATZ_EINGERASTET


def test_reconfiguring_with_the_same_lever_set_is_allowed():
    assert _pruefen(_konfiguration(raumfuehler=["treiber:room_temperature"]), LOCK) is None


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


@pytest.mark.parametrize(("bindung", "details"), [
    ({"treiber": {"id": "simulation", "parameter": {}}, "thermostat": None, "abfrage_s": 60},
     {"feld": "bindung", "regel": "unbekannt"}),
    ({"treiber": {"id": "simulation", "parameter": {}, "extra": 1}, "thermostat": None},
     {"feld": "treiber", "regel": "unbekannt"}),
    ({"treiber": {"id": "simulation", "parameter": {}}}, {"feld": "thermostat", "regel": "fehlt"}),
])
def test_a_foreign_or_missing_gateway_binding_field_is_a_role_rejection(bindung, details):
    """V1 ist auf beiden Seiten strikt (Nutzer-Entscheidung 2026-10-16): kein fremdes Feld, der fremde Name geht nicht
    hoch."""
    assert _pruefen(_konfiguration(bindung=bindung)) == cc.Ablehnung(cc.ROLLE_UNZULAESSIG, details)


def test_without_thermostat():
    assert _pruefen(_konfiguration(bindung={"treiber": {"id": "simulation", "parameter": {}}, "thermostat": None})) is None


def test_a_room_sensor_twice_is_a_double_assignment():
    ablehnung = _pruefen(_konfiguration(raumfuehler=[SENSOR, SENSOR]))
    assert ablehnung.grund == cc.DOPPELT_BELEGT
    assert ablehnung.details == {"feld": "raumfuehler", "regel": "doppelt"}


def test_driver_parameters_are_checked_by_the_host():
    seen = []

    def treiber(bindung):
        seen.append(bindung["treiber"]["id"])
        return {"feld": "parameter", "regel": "ungueltig"}

    ablehnung = _pruefen(_konfiguration(), treiber_pruefen=treiber)
    assert (ablehnung.grund, ablehnung.details, seen) == (
        cc.TREIBER_PARAMETER, {"feld": "parameter", "regel": "ungueltig"}, ["simulation"])
    assert _pruefen(_konfiguration(), treiber_pruefen=lambda bindung: None) is None


def test_the_driver_is_asked_only_for_an_otherwise_valid_configuration():
    seen = []
    _pruefen(_konfiguration(raumfuehler=[SENSOR, SENSOR]), treiber_pruefen=lambda bindung: seen.append(1))
    assert seen == []


def test_a_gateway_shaped_configuration_is_not_a_home_assistant_binding():
    assert cc.pruefen(_konfiguration(), None, host=cc.HOST_HA).grund == cc.ROLLE_UNZULAESSIG


@pytest.mark.parametrize(("change", "details"), [
    ({"tagestick": "25:00"}, {"feld": "tagestick", "regel": "format"}),
    ({"raumfuehler": [SENSOR] * 9}, {"feld": "raumfuehler", "regel": "anzahl", "min": 1, "max": 8, "anzahl": 9}),
    ({"abo": "vielleicht"}, {"feld": "abo", "regel": "unbekannt"}),
])
def test_aufbau_nennt_feld_und_regel(change, details):
    assert _pruefen(_konfiguration(**change)) == cc.Ablehnung(cc.UNGUELTIG, details)


@pytest.mark.parametrize(("hebelsatz", "verteilsystem", "feld"), [
    ("unbekannt", "Heizkoerper", "hebelsatz"), ("viessmann_vicare", "Kachelofen", "verteilsystem")])
def test_fehlende_sicherheitswerte_nennen_das_feld(hebelsatz, verteilsystem, feld):
    assert _pruefen(_konfiguration(hebelsatz=hebelsatz, verteilsystem=verteilsystem)).details == {
        "feld": feld, "regel": "unbekannt"}


def test_reasons_are_contract_reasons():
    assert set(cc.GRUENDE) <= set(wire.DOKUMENT_ERRORS)


def test_eingerichtet_ohne_bindungsparameter_wird_abgelehnt():
    neu = _konfiguration()
    neu.pop("bindungsparameter")
    ablehnung = _pruefen(neu)
    assert ablehnung is not None and ablehnung.grund == cc.UNGUELTIG
    assert ablehnung.details["feld"] == "bindungsparameter"


def test_lockerer_schreibschutz_wird_abgelehnt():
    neu = _konfiguration(bindungsparameter=parameter("viessmann_vicare", schreibschutz={
        "tag": None, "lebensdauer_hinweis": None, "durchsetzen_tag": 5}))
    ablehnung = _pruefen(neu)
    assert ablehnung.grund == cc.UNGUELTIG
    assert ablehnung.details == {"feld": "durchsetzen_tag", "regel": "bereich", "min": 1, "max": 4}


def test_leere_konfiguration_braucht_keine_bindungsparameter():
    assert _pruefen({"anlage": None, "raumfuehler": [], "bindungsparameter": None}) is None


def test_rastung_wird_vor_den_bindungsparametern_geprueft():
    """Ein fremder Hebelsatz bleibt hebelsatz_eingerastet, auch ohne Parameter (Rastung zuerst, Spec 4.2)."""
    neu = _konfiguration(hebelsatz="weishaupt_wwp", bindungsparameter=None)
    assert _pruefen(neu, LOCK) == cc.Ablehnung(cc.HEBELSATZ_EINGERASTET)


def test_bindungsparameter_werden_vor_den_rollen_geprueft():
    neu = _konfiguration(bindungsparameter=None, bindung={"thermostat": None})
    assert _pruefen(neu).grund == cc.UNGUELTIG


def test_bindungsparameter_eines_fremden_hebelsatzes_werden_abgelehnt():
    neu = _konfiguration(bindungsparameter=parameter("vaillant_vrc720"))
    assert _pruefen(neu).grund == cc.UNGUELTIG

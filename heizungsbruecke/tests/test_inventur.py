"""Spec 5c 1.3 (G-9): Inventur nur nach Filter, Felder nach dem Vertrag; der Server liest sie (ha_inventur.lesen)."""
import logging
import math
import re
from collections.abc import Mapping

from fakes import FakeHa

from heizungsbruecke import inventur
from smartheat_device import wire

FILTER = {"plattformen": ["mypyllant"], "domaenen": ["climate", "weather"],
          "geraeteklassen": [["sensor", "temperature"], ["sensor", "battery"]], "dienste": ["notify.mobile_app_"]}


def _ha():
    ha = FakeHa()
    ha.states = {"number.curve": 1.2, "climate.wz": "heat", "climate.wz::temperature": 21.0,
                 "climate.wz::hvac_modes": ["heat", "off"], "climate.wz::friendly_name": "Wohnzimmer",
                 "sensor.t": 20.5, "sensor.t::device_class": "temperature", "sensor.t::unit_of_measurement": "°C",
                 "sensor.t::state_class": "measurement",
                 "light.flur": "on", "sensor.strom": 3.0, "sensor.strom::device_class": "power"}
    ha.registry = [
        {"entity_id": "number.curve", "platform": "mypyllant", "unique_id": "mypyllant SYS_circuit_0_heating_curve",
         "original_name": "Heating Curve", "name": "Kurve (vom Nutzer)", "config_entry_id": "e1", "device_id": "d1"},
        {"entity_id": "climate.wz", "platform": "zha", "config_entry_id": "e2", "device_id": "d2"},
        {"entity_id": "sensor.t", "platform": "zha", "config_entry_id": "e2", "device_id": "d2",
         "original_device_class": "temperature"},
        {"entity_id": "light.flur", "platform": "hue"},
        {"entity_id": "sensor.strom", "platform": "shelly"},
        {"entity_id": "sensor.aus", "platform": "mypyllant", "disabled_by": "user"},
    ]
    ha.devices = [{"id": "d1", "name": "Zuhause", "model": "ecoTEC plus", "config_entries": ["e1"]},
                  {"id": "d2", "name": "Wohnzimmer", "name_by_user": "WZ", "model": "eTRV", "config_entries": ["e2"]},
                  {"id": "d9", "name": "Fremd", "model": "x", "config_entries": ["e9"]}]
    ha.services = [{"domain": "notify", "services": {"mobile_app_handy": {}, "familie": {}}},
                   {"domain": "light", "services": {"turn_on": {}}}]
    return ha


def test_only_filtered_candidates_with_contract_fields():
    daten = inventur.inventur(_ha(), FILTER)
    assert set(daten) == set(wire.INVENTUR_HA_FIELDS)
    ids = {e["entity_id"] for e in daten["entities"]}
    assert ids == {"number.curve", "climate.wz", "sensor.t"}
    for entity in daten["entities"]:
        assert set(entity) == set(wire.INVENTUR_ENTITY_FIELDS)
        assert set(entity["attribute"]) <= set(wire.INVENTUR_ATTRIBUTE)
    assert {g["id"] for g in daten["geraete"]} == {"d1", "d2"}
    for geraet in daten["geraete"]:
        assert set(geraet) == set(wire.INVENTUR_GERAET_FIELDS)
    assert daten["dienste"] == ["notify.mobile_app_handy"]
    assert (daten["einheit_temperatur"], daten["zeitzone"]) == ("°C", "Europe/Berlin")


def test_entity_fields_come_from_registry_and_state():
    daten = inventur.inventur(_ha(), FILTER)
    by_id = {e["entity_id"]: e for e in daten["entities"]}
    curve = by_id["number.curve"]
    # Der Server erkennt die Kreise am original_name (Name der Integration), nicht am Nutzernamen.
    assert (curve["plattform"], curve["unique_id"], curve["name"], curve["config_entry"], curve["geraet"]) == (
        "mypyllant", "mypyllant SYS_circuit_0_heating_curve", "Heating Curve", "e1", "d1")
    sensor = by_id["sensor.t"]
    assert (sensor["einheit"], sensor["device_class"], sensor["state_class"], sensor["zustand"]) == (
        "°C", "temperature", "measurement", "20.5")
    climate = by_id["climate.wz"]
    assert climate["attribute"] == {"temperature": 21.0, "hvac_modes": ["heat", "off"]}  # friendly_name nicht
    assert climate["aktualisiert"] == "2026-10-11T10:00:00+00:00"
    assert {g["id"]: g["name"] for g in daten["geraete"]}["d2"] == "WZ"


def test_device_class_from_the_state_when_the_registry_has_none():
    """config/entity_registry/list liefert keine device_class; dann zaehlt das Attribut des Zustands."""
    ha = _ha()
    ha.registry = [e for e in ha.registry if e["entity_id"] != "sensor.t"] + [
        {"entity_id": "sensor.t", "platform": "zha", "config_entry_id": "e2", "device_id": "d2"}]
    daten = inventur.inventur(ha, FILTER)
    assert "sensor.t" in {e["entity_id"] for e in daten["entities"]}


def test_entity_without_registry_entry_still_counts_by_domain():
    ha = _ha()
    ha.states["weather.forecast_home"] = "cloudy"
    daten = inventur.inventur(ha, FILTER)
    weather = next(e for e in daten["entities"] if e["entity_id"] == "weather.forecast_home")
    assert weather["plattform"] == inventur.OHNE_REGISTRY and weather["config_entry"] is None


def test_filter_check():
    assert inventur.filter_pruefen(FILTER) is None and inventur.filter_pruefen(SERVER_FILTER) is None
    assert inventur.filter_pruefen({}) is None and inventur.filter_pruefen({"unbekannt": 1, "dienste": None}) is None
    for kaputt in (None, [], {"plattformen": [["x"]]}, {"domaenen": "climate"}, {"geraeteklassen": [["sensor"]]},
                   {"geraeteklassen": [["sensor", 1]]}, {"dienste": [None]}):
        assert inventur.filter_pruefen(kaputt) == inventur.FILTER_TEXT


def test_empty_filter_selects_nothing():
    daten = inventur.inventur(_ha(), {})
    assert daten["entities"] == [] and daten["geraete"] == [] and daten["dienste"] == []


def test_the_inventory_contents_never_reach_the_log(caplog):
    caplog.set_level(logging.DEBUG)
    inventur.inventur(_ha(), FILTER)
    assert "Heating Curve" not in caplog.text and "mobile_app_handy" not in caplog.text


# --- Gegenprobe gegen den Leser des Servers -------------------------------------------------------------------------
# Kopie (nicht Import) der Regeln aus HeizungssteuerungServer/src/heizungsserver/einrichtung/ha_inventur.py (lesen,
# _eintrag, _geraet, _attribute, _wert, _zustand, _text, _zahl; Stand Plan S, Server 0b5d7b1) und des Filters
# filter_ha(). Der Server verwirft still, was nicht passt; hier muss alles ankommen.

SERVER_ENTITY_ID = re.compile(r"[a-z_]+\.[a-z0-9_]+")
SERVER_DIENST = re.compile(r"notify\.[a-z0-9_]+")
SERVER_DIENST_PRAEFIX = "notify.mobile_app_"
SERVER_MAX_TEXT = 255
SERVER_MAX_ZAHL = 1e12
SERVER_FILTER = {"plattformen": ["mypyllant", "vicare", "weishaupt_modbus"], "domaenen": ["climate", "weather"],
                 "geraeteklassen": [["sensor", "temperature"], ["sensor", "battery"], ["binary_sensor", "battery"]],
                 "dienste": [SERVER_DIENST_PRAEFIX]}
SERVER_BEFEHL = {"stunden": 0, "filter": SERVER_FILTER}


def _s_text(value):
    if not isinstance(value, str) or not value or len(value) > SERVER_MAX_TEXT:
        return None
    return value if value.isprintable() else None


def _s_zahl(value):
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return abs(value) < SERVER_MAX_ZAHL


def _s_wert(value):
    if _s_zahl(value):
        return value
    if isinstance(value, str):
        return _s_text(value)
    if isinstance(value, list) and len(value) <= 64:
        return [t for t in (_s_text(item) for item in value) if t is not None]
    return None


def _s_attribute(raw):
    if not isinstance(raw, Mapping):
        return {}
    return {key: value for key in wire.INVENTUR_ATTRIBUTE if key in raw and (value := _s_wert(raw[key])) is not None}


def _s_eintrag(raw):
    if not isinstance(raw, Mapping):
        return None
    entity_id, plattform = _s_text(raw.get("entity_id")), _s_text(raw.get("plattform"))
    if entity_id is None or not SERVER_ENTITY_ID.fullmatch(entity_id) or plattform is None:
        return None
    zustand = str(raw.get("zustand")) if _s_zahl(raw.get("zustand")) else _s_text(raw.get("zustand"))
    return {"entity_id": entity_id, "plattform": plattform, "unique_id": _s_text(raw.get("unique_id")),
            "name": _s_text(raw.get("name")), "config_entry": _s_text(raw.get("config_entry")),
            "geraet": _s_text(raw.get("geraet")), "einheit": _s_text(raw.get("einheit")),
            "device_class": _s_text(raw.get("device_class")), "state_class": _s_text(raw.get("state_class")),
            "zustand": zustand, "attribute": _s_attribute(raw.get("attribute")),
            "aktualisiert": _s_text(raw.get("aktualisiert"))}


def _s_geraet(raw):
    if not isinstance(raw, Mapping) or (geraet_id := _s_text(raw.get("id"))) is None:
        return None
    entries = raw.get("config_entries")
    entries = [e for e in map(_s_text, entries[:32]) if e] if isinstance(entries, list) else []
    return {"id": geraet_id, "name": _s_text(raw.get("name")), "modell": _s_text(raw.get("modell")),
            "config_entries": entries}


def _s_lesen(daten):
    def liste(key):
        value = daten.get(key)
        return value if isinstance(value, list) else []

    return {"entities": [e for e in map(_s_eintrag, liste("entities")) if e is not None],
            "geraete": [g for g in map(_s_geraet, liste("geraete")) if g is not None],
            "dienste": [d for d in map(_s_text, liste("dienste"))
                        if d is not None and SERVER_DIENST.fullmatch(d) and d.startswith(SERVER_DIENST_PRAEFIX)],
            "einheit_temperatur": _s_text(daten.get("einheit_temperatur")),
            "zeitzone": _s_text(daten.get("zeitzone"))}


def test_the_server_reader_keeps_everything_the_addon_sends():
    """Die Felder der Inventur sind genau die, die ha_inventur.lesen liest, mit denselben Typen: nichts faellt weg."""
    ha = _ha()
    ha.states.update({"binary_sensor.batt": "off", "binary_sensor.batt::device_class": "battery",
                      "climate.vicare": "heat", "climate.vicare::temperature": 20})
    ha.registry.append({"entity_id": "climate.vicare", "platform": "vicare", "unique_id": "v-1",
                        "original_name": "Heating", "config_entry_id": "e3", "device_id": "d1"})
    daten = inventur.inventur(ha, SERVER_BEFEHL["filter"])
    gelesen = _s_lesen(daten)
    assert gelesen == daten
    assert {e["entity_id"] for e in gelesen["entities"]} == {
        "number.curve", "climate.wz", "sensor.t", "binary_sensor.batt", "climate.vicare"}
    assert gelesen["dienste"] == ["notify.mobile_app_handy"]


def test_the_server_command_payload_is_accepted_by_the_contract():
    assert set(SERVER_BEFEHL["filter"]) == set(wire.INVENTUR_FILTER_FIELDS)
    command = wire.COMMANDS["inventory"]
    assert set(SERVER_BEFEHL) <= set(command.payload) | set(command.payload_optional)

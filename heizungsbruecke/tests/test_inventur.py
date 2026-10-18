"""Spec 5c 1.3 (G-9): Inventur nur nach Filter, Felder nach dem Vertrag; der Server liest sie (ha_inventur.lesen)."""
import logging
import math
import re
from collections.abc import Mapping

import pytest
from fakes import FakeHa, nachricht_bytes

from heizungsbruecke import inventur
from smartheat_device import inventory, nachrichten, wire

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


@pytest.mark.parametrize("liste, nachricht", [("INVENTUR_HA_FIELDS", "inventur"),
                                              ("INVENTUR_ENTITY_FIELDS", "inventur.entity"),
                                              ("INVENTUR_GERAET_FIELDS", "inventur.geraet")])
def test_die_inventur_wird_gegen_den_vertrag_gebaut_und_nennt_nur_feldnamen(monkeypatch, liste, nachricht):
    """Die Erzeuger lesen die Feldlisten des Vertrags: eine Liste ohne ein Feld, das der Erzeuger setzt, ist ein
    Vertragsfehler mit Feldnamen, nie mit Werten aus HA (Regel 6)."""
    felder = getattr(wire, liste)
    monkeypatch.setattr(wire, liste, felder[:-1])
    with pytest.raises(nachrichten.Vertragsfehler) as fehler:
        inventur.inventur(_ha(), FILTER)
    assert str(fehler.value) == f"{nachricht}: fehlt [], fremd [{felder[-1]!r}]"
    assert "Heating Curve" not in str(fehler.value)
    assert "mobile_app_handy" not in str(fehler.value) and "ecoTEC" not in str(fehler.value)


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
    assert inventur.filter_pruefen({}) is None and inventur.filter_pruefen({"dienste": None}) is None
    for kaputt in (None, [], {"plattformen": [["x"]]}, {"domaenen": "climate"}, {"geraeteklassen": [["sensor"]]},
                   {"geraeteklassen": [["sensor", 1]]}, {"dienste": [None]}):
        assert inventur.filter_pruefen(kaputt) == {"feld": "filter", "regel": "ungueltig"}


def test_ein_fremder_filterschluessel_ist_unbekannt_und_traegt_nur_vertragsnamen():
    """V1 ist beidseitig strikt (Nutzer 2026-10-16): ein Schluessel ausserhalb von INVENTUR_FILTER_FIELDS lehnt den
    Filter ab; sein Name steht nur in details, wenn er ein Feld des Vertrags ist (G-6)."""
    assert inventur.filter_pruefen({**FILTER, "geheim": 1}) == {"regel": "unbekannt"}
    assert inventur.filter_pruefen({**FILTER, "treiber": "simulation"}) == {"feld": "treiber", "regel": "unbekannt"}
    assert inventur.filter_pruefen({"treiber": "simulation"}) == {"feld": "treiber", "regel": "unbekannt"}


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


WEIT = {"plattformen": ["mypyllant", "mobile_app", "nuki", "zha"], "domaenen": ["lock", "person", "device_tracker"],
        "geraeteklassen": [["binary_sensor", "door"], ["binary_sensor", "battery"]],
        "dienste": ["notify."]}


def _ha_weit():
    """Ein Server-Filter, der mehr verlangt als die Positivliste erlaubt (A5-30)."""
    ha = _ha()
    ha.states.update({
        "sensor.mypyllant_betriebsart": "heating",  # ohne Klasse, Plattform liefert number.curve
        "sensor.handy_ort": "Hauptstr. 1",  # mobile_app, ohne Klasse
        "sensor.handy_akku": 80, "sensor.handy_akku::device_class": "battery",
        "lock.haustuer": "locked", "person.luca": "home", "device_tracker.handy": "home",
        "binary_sensor.tuer": "off", "binary_sensor.tuer::device_class": "door",
        "binary_sensor.wz_batt": "off", "binary_sensor.wz_batt::device_class": "battery",
        "sensor.zha_lqi": 200,
    })
    ha.registry += [
        {"entity_id": "sensor.mypyllant_betriebsart", "platform": "mypyllant", "config_entry_id": "e1"},
        {"entity_id": "sensor.handy_ort", "platform": "mobile_app"},
        {"entity_id": "sensor.handy_akku", "platform": "mobile_app"},
        {"entity_id": "lock.haustuer", "platform": "nuki"},
        {"entity_id": "person.luca", "platform": "person"},
        {"entity_id": "device_tracker.handy", "platform": "mobile_app"},
        {"entity_id": "binary_sensor.tuer", "platform": "zha"},
        {"entity_id": "binary_sensor.wz_batt", "platform": "zha"},
        {"entity_id": "sensor.zha_lqi", "platform": "zha"},
    ]
    return ha


def test_positivliste_laesst_nur_heizungsdaten_durch():
    ids = {e["entity_id"] for e in inventur.inventur(_ha_weit(), WEIT)["entities"]}
    assert "sensor.mypyllant_betriebsart" in ids  # Plattform mit einstellbarer Entity (number.curve)
    assert "sensor.handy_akku" in ids  # Klasse battery
    assert "binary_sensor.wz_batt" in ids
    assert "sensor.zha_lqi" in ids  # zha liefert climate.wz: einstellbare Plattform
    for verboten in ("sensor.handy_ort", "lock.haustuer", "person.luca", "device_tracker.handy", "binary_sensor.tuer"):
        assert verboten not in ids


def test_positivliste_begrenzt_die_dienste():
    assert inventur.inventur(_ha_weit(), WEIT)["dienste"] == ["notify.mobile_app_handy"]


def test_verworfenes_steht_nur_als_anzahl_im_log(caplog):
    caplog.set_level(logging.INFO)
    inventur.inventur(_ha_weit(), WEIT)
    text = caplog.text
    assert "lock=1" in text and "person=1" in text and "device_tracker=1" in text
    for name in ("haustuer", "handy_ort", "Hauptstr", "luca"):
        assert name not in text


def test_der_normale_filter_verliert_nichts():
    """Der Filter des Servers (filter_ha) verlangt nichts ausserhalb der Liste: dieselben Entities wie ohne Liste."""
    ids = {e["entity_id"] for e in inventur.inventur(_ha(), FILTER)["entities"]}
    assert ids == {"number.curve", "climate.wz", "sensor.t"}


# --- Randfaelle der Positivliste und der Eingaben (Abschlussreview Stufe 3 S5a, Mutationsprobe) ---


class _RohHa(FakeHa):
    """FakeHa, dessen Registry- und Zustandslisten roh bleiben (auch Eintraege, die keine Objekte sind)."""

    def __init__(self, registry, states, devices=(), services=()):
        super().__init__()
        self._roh_registry, self._roh_states = registry, states
        self.devices, self.services = list(devices), list(services)

    def entity_registry(self):
        return self._roh_registry

    def get_states(self):
        return self._roh_states


def _zustand(entity_id, wert="1", **attribute):
    return {"entity_id": entity_id, "state": wert, "attributes": attribute, "last_updated": "t"}


def test_entities_ohne_registry_schalten_sich_nicht_gegenseitig_frei():
    """Eine climate ohne Registry-Eintrag macht ihre "Plattform" (ohne_registry) nicht einstellbar: ein klassenloser
    Sensor ohne Registry fliegt trotz Filter raus (sonst oeffnete ein Template-Sensor die Positivliste)."""
    ha = FakeHa()
    ha.states = {"climate.x": "heat", "sensor.y": 5}
    filter_ = {"plattformen": [inventur.OHNE_REGISTRY], "domaenen": ["climate"]}
    ids = {e["entity_id"] for e in inventur.inventur(ha, filter_)["entities"]}
    assert ids == {"climate.x"}


@pytest.mark.parametrize("domaene, klasse", [("weather", None), ("binary_sensor", "battery")])
def test_plattform_nur_mit_wetter_oder_binaersensor_ist_nicht_einstellbar(domaene, klasse):
    """Einstellbar machen nur climate, number und select; weather und binary_sensor schalten klassenlose Sensoren
    ihrer Plattform nicht frei."""
    ha = FakeHa()
    ha.states = {f"{domaene}.a": "x", "sensor.b": 1}
    if klasse:
        ha.states[f"{domaene}.a::device_class"] = klasse
    ha.registry = [{"entity_id": f"{domaene}.a", "platform": "wx"}, {"entity_id": "sensor.b", "platform": "wx"}]
    ids = {e["entity_id"] for e in inventur.inventur(ha, {"plattformen": ["wx"]})["entities"]}
    assert ids == {f"{domaene}.a"}


def test_registry_und_zustandseintraege_ohne_entity_id_werden_uebersprungen():
    registry = ["kein objekt", {"platform": "mypyllant"}, None, {"entity_id": "number.curve", "platform": "mypyllant"}]
    states = [42, {"state": "1"}, ["number.curve"], _zustand("number.curve")]
    daten = inventur.inventur(_RohHa(registry, states), FILTER)
    assert [e["entity_id"] for e in daten["entities"]] == ["number.curve"]


def test_deaktivierte_entity_mit_zustand_faellt_raus():
    ha = _ha()
    ha.states["number.aus"] = 3
    ha.registry.append({"entity_id": "number.aus", "platform": "mypyllant", "disabled_by": "user"})
    ids = {e["entity_id"] for e in inventur.inventur(ha, FILTER)["entities"]}
    assert "number.aus" not in ids and "number.curve" in ids


def test_config_entries_des_geraets_sind_eine_liste():
    ha = _ha()
    ha.devices[0] = {**ha.devices[0], "config_entries": None}
    ha.devices[1] = {k: v for k, v in ha.devices[1].items() if k != "config_entries"}
    geraete = {g["id"]: g for g in inventur.inventur(ha, FILTER)["geraete"]}
    assert geraete["d1"]["config_entries"] == [] and geraete["d2"]["config_entries"] == []
    assert {g["id"]: g["config_entries"] for g in inventur.inventur(_ha(), FILTER)["geraete"]} == {
        "d1": ["e1"], "d2": ["e2"]}


def test_nur_notify_dienste_erscheinen_in_der_ausgabe():
    ha = _ha()
    ha.services = [{"domain": "light", "services": {"mobile_app_x": {}}},
                   {"domain": "notify", "services": {"mobile_app_handy": {}}}]
    assert inventur.inventur(ha, FILTER)["dienste"] == ["notify.mobile_app_handy"]


class _GrosseHa(FakeHa):
    """Fake-HA mit 5000 Entities: passend viele Raumfuehler (zha, Klasse temperature, je ein Geraet), der Rest Lampen
    (hue). get_states liefert die vorgebauten Zustaende (die Attribut-Suche von FakeHa waere hier quadratisch)."""

    def __init__(self, passend: int, gesamt: int = 5000):
        super().__init__()
        self._zustaende, self.registry, self.devices = [], [], []
        for n in range(gesamt):
            if n < passend:
                entity_id = f"sensor.raum_{n:04d}_temperatur"
                self._zustaende.append({"entity_id": entity_id, "state": "20.5", "last_updated": "2026-10-11T10:00:00",
                                        "attributes": {"device_class": "temperature", "unit_of_measurement": "°C",
                                                       "state_class": "measurement"}})
                self.registry.append({"entity_id": entity_id, "platform": "zha", "unique_id": f"zha-{n:08d}",
                                      "original_name": "Temperatur", "config_entry_id": "e2", "device_id": f"d{n}"})
                self.devices.append({"id": f"d{n}", "name": f"Thermostat {n}", "model": "eTRV",
                                     "config_entries": ["e2"]})
            else:
                entity_id = f"light.lampe_{n:04d}"
                self._zustaende.append({"entity_id": entity_id, "state": "on", "attributes": {}})
                self.registry.append({"entity_id": entity_id, "platform": "hue"})

    def get_states(self):
        return [dict(zustand) for zustand in self._zustaende]


def test_a_large_installation_stays_under_the_message_limit():
    """5000 Entities, davon 50 nach dem Filter: die Inventur ist eine Nachricht unter der Grenze des Links (120 KB =
    122 880 Bytes)."""
    daten = inventur.inventur(_GrosseHa(passend=50), FILTER)
    assert len(daten["entities"]) == 50 and len(daten["geraete"]) == 50
    teile = inventory.nachrichten("c1", daten)
    assert len(teile) == 1 and nachricht_bytes(teile[0]) <= 122_880


def test_an_inventory_too_large_for_eight_messages_is_refused_instead_of_crossing_the_limit():
    """Waehlt der Filter alle 5000 Entities, passt die Inventur nicht in 8 Nachrichten unter der Grenze: sie wird
    abgelehnt (InventurZuGross, der Befehl endet mit intern, client-kern.md), nie eine Nachricht ueber der Grenze.
    Gegenprobe: 1000 passende Entities gehen in mehreren Teilen, jeder unter der Grenze."""
    with pytest.raises(inventory.InventurZuGross):
        inventory.nachrichten("c1", inventur.inventur(_GrosseHa(passend=5000), FILTER))
    teile = inventory.nachrichten("c1", inventur.inventur(_GrosseHa(passend=1000), FILTER))
    assert 1 < len(teile) <= 8 and all(nachricht_bytes(teil) <= 122_880 for teil in teile)

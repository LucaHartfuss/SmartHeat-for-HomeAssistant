"""Spec 5c 1.3, Stufe 3 S5 3.2: Konfigurationsdokument -> HaBindung; Rollentabellen; HA-Teil der lokalen Pruefung."""
import json
from unittest.mock import patch

import bindung_beispiele
import pytest
import requests
from bindung_beispiele import parameter
from fakes import FakeHa, HaWebSocket, ha_antwort

from heizungsbruecke import dokument
from heizungsbruecke.ha_api import HomeAssistantApi
from smartheat_core import config_check
from smartheat_runtime import dokument_config
from smartheat_runtime.dokument_config import ConfigError
from smartheat_runtime.roles import LEVER_ROLES

KONFIGURATION = {
    "anlage": "zuhause2", "profil": "vaillant_gastherme_heizkoerper", "hebelsatz": "vaillant_vrc720",
    "verteilsystem": "Heizkoerper", "tagestick": "12:07", "abo": "aktiv", "mindest_software": "1.0.0",
    "software": None, "raumfuehler": ["sensor.room_actual"],
    "bindung": {
        "kreis": {"plattform": "mypyllant", "config_entry": "e1", "anlage": "S", "nummer": "0"},
        "rollen": {"room_target": "sensor.room_target", "curve": "number.curve_current",
                   "room_setpoint": "climate.zone", "min_flow": "number.min_flow", "heat_limit": "number.heat_limit",
                   "outdoor_temp": "sensor.outdoor_temp"},
        "batterien": ["sensor.thermostat_battery"], "benachrichtigung": ["notify.mobile_app_handy"], "hinweise_aus": [],
        "abfrage_s": 1800,
    },
    "bindungsparameter": bindung_beispiele.parameter("vaillant_vrc720"),
}


def _ha():
    ha = FakeHa()
    ha.states["climate.zone"] = "auto"
    registry = {"number.curve_current": ("mypyllant", "e1", "d-therme"), "climate.zone": ("mypyllant", "e1", "d-therme"),
                "number.min_flow": ("mypyllant", "e1", "d-therme"), "number.heat_limit": ("mypyllant", "e1", "d-therme"),
                "sensor.outdoor_temp": ("mypyllant", "e1", "d-therme"), "sensor.room_target": ("zha", "e2", "d-tst"),
                "sensor.room_actual": ("zha", "e2", "d-tst"), "sensor.thermostat_battery": ("zha", "e2", "d-tst")}
    ha.registry = [{"entity_id": e, "platform": p, "config_entry_id": c, "device_id": d}
                   for e, (p, c, d) in registry.items()]
    # echtes HA: config/entity_registry/list liefert keine device_class, sie steht im Zustand (A6-02)
    ha.states.update({"sensor.thermostat_battery": 80.0, "sensor.thermostat_battery::device_class": "battery",
                      "sensor.room_target::device_class": "temperature",
                      "sensor.room_actual::device_class": "temperature"})
    return ha


def _dazu(ha, entity_id, platform=None, klasse=None, wert=1.0):
    """Entity mit Zustand (und Klasse nur im Zustand) in die Attrappe; ohne platform kein Registry-Eintrag."""
    if platform is not None:
        ha.registry.append({"entity_id": entity_id, "platform": platform, "config_entry_id": "e9", "device_id": None})
    ha.states[entity_id] = wert
    if klasse is not None:
        ha.states[f"{entity_id}::device_class"] = klasse


def _mit_rolle(rolle, ref):
    rollen = {**KONFIGURATION["bindung"]["rollen"], rolle: ref}
    return {**KONFIGURATION, "bindung": {**KONFIGURATION["bindung"], "rollen": rollen}}


def _mit_rolle_in(rolle, ref, konfiguration):
    rollen = {**konfiguration["bindung"]["rollen"], rolle: ref}
    return {**konfiguration, "bindung": {**konfiguration["bindung"], "rollen": rollen}}


def _fremd(ha, entity_id, platform="template"):
    ha.registry = [{**e, "platform": platform} if e["entity_id"] == entity_id else e for e in ha.registry]


@pytest.fixture
def konfiguration():
    return KONFIGURATION


def test_bindung_bildet_rollen_auf_laufzeitrollen_ab(konfiguration):
    b = dokument.bindung(konfiguration)
    assert "room_target" not in b.rollen and b.soll_entity == konfiguration["bindung"]["rollen"]["room_target"]
    assert b.rollen["curve_current"] == konfiguration["bindung"]["rollen"]["curve"]
    assert b.rollen["shift_current"] == konfiguration["bindung"]["rollen"]["room_setpoint"]
    assert b.raumfuehler == tuple(konfiguration["raumfuehler"])


def test_bindung_traegt_die_ha_teile_des_dokuments(konfiguration):
    b = dokument.bindung(konfiguration)
    assert b.hebelsatz == "vaillant_vrc720"
    assert b.rollen == {"curve_current": "number.curve_current", "shift_current": "climate.zone",
                        "min_flow": "number.min_flow", "heat_limit": "number.heat_limit",
                        "outdoor_temp": "sensor.outdoor_temp"}
    assert b.raumfuehler == ("sensor.room_actual",) and b.batterien == ("sensor.thermostat_battery",)
    assert b.benachrichtigung == ("notify.mobile_app_handy",) and b.hinweise_aus == ()
    assert b.kreis == konfiguration["bindung"]["kreis"] and b.kreis is not konfiguration["bindung"]["kreis"]
    assert b.soll_entity == "sensor.room_target"


def test_weitere_hebel_und_kennzahl_rollen_kommen_in_der_bindung_an():
    konfiguration = _mit_rolle("level", "number.level")
    konfiguration = _mit_rolle_in("flow_temperature", "sensor.vl", konfiguration)
    b = dokument.bindung(konfiguration)
    assert b.rollen["level_current"] == "number.level" and b.rollen["flow_temperature"] == "sensor.vl"
    assert dokument.laufzeit_rolle("min_flow") == "min_flow" and dokument.laufzeit_rolle("curve") == "curve_current"
    assert dokument.laufzeit_rolle("outdoor_temp") == "outdoor_temp"


def test_leere_und_falsche_teile_des_dokuments_sind_leer_statt_ein_fehler():
    b = dokument.bindung({"hebelsatz": "vaillant_vrc720", "bindung": {"rollen": {"curve": "", "min_flow": 5},
                                                                      "batterien": "x", "kreis": None},
                          "raumfuehler": ["sensor.a", 3]})
    assert b.rollen == {} and b.soll_entity is None and b.batterien == () and b.kreis == {}
    assert b.raumfuehler == ("sensor.a",)


def test_tabellen_decken_dieselben_hebelsaetze():
    assert set(dokument.PFLICHT_ROLLEN) == set(dokument.SCHREIBREGELN)
    for hebelsatz, rollen in dokument.PFLICHT_ROLLEN.items():
        assert "room_target" not in rollen
        assert set(dokument.SCHREIBREGELN[hebelsatz]) <= set(rollen) | set(dokument.WEITERE_SCHREIBZIELE)


@pytest.mark.parametrize("hebelsatz", [None, "", "gibt_es_nicht", 3, ["weishaupt_wwp"]])
def test_unbekannter_hebelsatz_ist_ein_konfigurationsfehler(konfiguration, hebelsatz):
    with pytest.raises(ConfigError, match="Hebelsatz"):
        dokument.bindung({**konfiguration, "hebelsatz": hebelsatz})
    with pytest.raises(ConfigError):
        dokument.lokal_pruefen({**konfiguration, "hebelsatz": hebelsatz})
    assert dokument.zuordnung_pruefen({**konfiguration, "hebelsatz": hebelsatz}, _ha()) == {
        "feld": "hebelsatz", "regel": "unbekannt"}


# --- Pflicht- und Schreibregeln des Hebelsatzes (frueher test_config.py) ---

def _konf(hebelsatz, **rollen):
    """Dokument des Hebelsatzes mit genau diesen Rollen (Laufzeitrollen; room_target ist immer da). Ein leerer Wert
    laesst die Rolle weg."""
    bindung_rollen = {"room_target": "sensor.t"}
    umkehr = {laufzeit: rolle for rolle, laufzeit in LEVER_ROLES.items()}
    for laufzeit, ref in rollen.items():
        if ref:
            bindung_rollen[umkehr.get(laufzeit, laufzeit)] = ref
    return {**KONFIGURATION, "hebelsatz": hebelsatz, "bindung": {**KONFIGURATION["bindung"], "rollen": bindung_rollen}}


VAILLANT = {"curve_current": "number.c", "shift_current": "climate.zone", "min_flow": "number.mf",
            "outdoor_temp": "sensor.o", "heat_limit": "number.hl"}
WEISHAUPT = {"curve_current": "number.hk", "shift_current": "number.normal", "heat_limit": "number.swu",
             "outdoor_temp": "sensor.o", "mode_select": "select.betriebsart", "setpoint_comfort": "number.komfort",
             "setpoint_setback": "number.absenk"}
VIESSMANN = {"curve_current": "number.slope", "level_current": "number.shift", "shift_current": "number.normal_temperature",
             "outdoor_temp": "sensor.o", "mode_select": "climate.heizkreis"}


def _lokal(hebelsatz, rollen, **aenderung):
    return dokument.lokal_pruefen(_konf(hebelsatz, **{**rollen, **aenderung}))


def test_the_valid_vaillant_document_passes():
    assert _lokal("vaillant_vrc720", VAILLANT) is None


@pytest.mark.parametrize("rolle, value", [
    ("shift_current", "sensor.zone_temperature"),
    ("shift_current", "input_number.shift"),
    ("shift_current", "climate.zone::current_temperature"),
    ("min_flow", "climate.zone"),
    ("min_flow", "input_number.min_flow"),
    ("min_flow", "sensor.min_flow"),
    ("heat_limit", "sensor.heizgrenze"),
    ("heat_limit", "input_number.heizgrenze"),
])
def test_unwritable_entity_domain_is_refused(rolle, value):
    # Spec 5.6: Zonen-Entity muss schreibbar sein; HaPlantBinding.write kennt climate.set_temperature und
    # number.set_value.
    assert f"{value} muss" in _lokal("vaillant_vrc720", VAILLANT, **{rolle: value})


@pytest.mark.parametrize("shift", ["climate.zone", "climate.zone::temperature", "number.shift"])
def test_writable_shift_domains_are_accepted(shift):
    assert _lokal("vaillant_vrc720", VAILLANT, shift_current=shift) is None


@pytest.mark.parametrize("missing", dokument.PFLICHT_ROLLEN["vaillant_vrc720"])
def test_every_vaillant_role_is_required(missing):
    assert f"Für die Ansteuerung vaillant_vrc720 fehlt die Zuordnung {missing}." == _lokal(
        "vaillant_vrc720", VAILLANT, **{missing: ""})


def test_the_target_entity_is_required():
    konfiguration = _konf("vaillant_vrc720", **VAILLANT)
    del konfiguration["bindung"]["rollen"]["room_target"]
    assert dokument.lokal_pruefen(konfiguration) == "Die Wunschtemperatur-Entity fehlt."


@pytest.mark.parametrize("missing", dokument.PFLICHT_ROLLEN["weishaupt_wwp"])
def test_every_weishaupt_role_is_required(missing):
    assert f"weishaupt_wwp fehlt die Zuordnung {missing}" in _lokal("weishaupt_wwp", WEISHAUPT, **{missing: ""})


def test_the_valid_weishaupt_document_passes():
    assert _lokal("weishaupt_wwp", WEISHAUPT) is None


def test_basis_does_not_need_curve_or_heat_limit():
    assert _lokal("weishaupt_wwp_basis", WEISHAUPT, curve_current="", heat_limit="") is None


@pytest.mark.parametrize(("rolle", "value"), [
    ("mode_select", "input_select.betriebsart"), ("setpoint_comfort", "sensor.komfort"),
    ("shift_current", "climate.zone"),
])
def test_weishaupt_entities_must_be_writable(rolle, value):
    assert f"{value} muss" in _lokal("weishaupt_wwp", WEISHAUPT, **{rolle: value})


def test_viessmann_program_must_be_a_climate_entity():
    assert _lokal("viessmann_vicare", VIESSMANN) is None
    assert "select.programm muss" in _lokal("viessmann_vicare", VIESSMANN, mode_select="select.programm")


@pytest.mark.parametrize("hebelsatz, rollen", [("weishaupt_wwp", WEISHAUPT), ("viessmann_vicare", VIESSMANN)])
def test_mode_select_is_required_for_weishaupt_and_viessmann(hebelsatz, rollen):
    # Ohne Betriebsart/Heizprogramm wirft read_aux einen KeyError (Review Task 8/9): Ablehnung vor dem Start.
    assert "mode_select" in dokument.PFLICHT_ROLLEN[hebelsatz]
    assert "fehlt die Zuordnung mode_select" in _lokal(hebelsatz, rollen, mode_select="")


def test_the_client1_like_mapping_passes():
    assert dokument.zuordnung_pruefen(KONFIGURATION, _ha()) is None


def test_missing_required_role():
    rollen = dict(KONFIGURATION["bindung"]["rollen"])
    del rollen["heat_limit"]
    konfiguration = {**KONFIGURATION, "bindung": {**KONFIGURATION["bindung"], "rollen": rollen}}
    assert dokument.zuordnung_pruefen(konfiguration, _ha()) == {"feld": "heat_limit", "regel": "fehlt"}


def test_unknown_entity_is_refused():
    ha = _ha()
    ha.missing.add("number.heat_limit")
    assert dokument.zuordnung_pruefen(KONFIGURATION, ha) == {"feld": "heat_limit", "regel": "unbekannt"}


def test_write_target_of_another_platform_is_refused():
    ha = _ha()
    _fremd(ha, "number.heat_limit")
    assert dokument.zuordnung_pruefen(KONFIGURATION, ha) == {"feld": "heat_limit", "regel": "nicht_erlaubt"}


def test_the_curve_is_a_write_target_too():
    """Die Heizkurve ist bei vaillant_vrc720 kein Teil von SCHREIBREGELN, wird aber jeden Takt geschrieben
    (Spec 1.3, A4-11): ein number.* der Plattform template darf sie nicht sein."""
    ha = _ha()
    _fremd(ha, "number.curve_current")
    assert dokument.zuordnung_pruefen(KONFIGURATION, ha) == {"feld": "curve", "regel": "nicht_erlaubt"}
    ha = _ha()
    ha.registry = [{**e, "config_entry_id": "fremd"} if e["entity_id"] == "number.curve_current" else e
                   for e in ha.registry]
    assert dokument.zuordnung_pruefen(KONFIGURATION, ha) == {"feld": "curve", "regel": "nicht_erlaubt"}


def test_every_lever_of_the_binding_is_checked():
    konfiguration = _mit_rolle("level", "number.level")
    ha = _ha()
    _dazu(ha, "number.level")
    ha.registry.append({"entity_id": "number.level", "platform": "template", "config_entry_id": None,
                        "device_id": None})
    assert dokument.zuordnung_pruefen(konfiguration, ha) == {"feld": "level", "regel": "nicht_erlaubt"}


def test_a_circuit_without_config_entry_checks_only_the_platform():
    kreis = {k: v for k, v in KONFIGURATION["bindung"]["kreis"].items() if k != "config_entry"}
    konfiguration = {**KONFIGURATION, "bindung": {**KONFIGURATION["bindung"], "kreis": kreis}}
    assert dokument.zuordnung_pruefen(konfiguration, _ha()) is None
    ha = _ha()
    _fremd(ha, "number.heat_limit")
    assert dokument.zuordnung_pruefen(konfiguration, ha) == {"feld": "heat_limit", "regel": "nicht_erlaubt"}


def test_the_platform_of_the_write_targets_comes_from_the_server_document():
    """Heutiges Verhalten, Restpunkt A5-70 (Regel 4: eine lokale Positivliste der Plattformen nur mit Go des Nutzers):
    nennt ein kompromittierter Server als Kreis die Plattform einer beliebigen anderen Integration (hier shelly, ein
    fremdes Geraet), nimmt die Pruefung die number-Entities dieser Plattform als Schreibziele an. Gegenprobe: dieselbe
    Bindung mit dem echten Kreis (mypyllant) wird abgelehnt."""
    fremd = {"number.curve_current", "climate.zone", "number.min_flow", "number.heat_limit", "sensor.outdoor_temp"}
    ha = _ha()
    ha.registry = [{**e, "platform": "shelly", "config_entry_id": "e-shelly", "device_id": "d-shelly"}
                   if e["entity_id"] in fremd else e for e in ha.registry]
    assert dokument.zuordnung_pruefen(KONFIGURATION, ha) == {"feld": "room_setpoint", "regel": "nicht_erlaubt"}
    kreis = {**KONFIGURATION["bindung"]["kreis"], "plattform": "shelly", "config_entry": "e-shelly"}
    vom_server = {**KONFIGURATION, "bindung": {**KONFIGURATION["bindung"], "kreis": kreis}}
    assert dokument.zuordnung_pruefen(vom_server, ha) is None


def test_soll_entity_on_the_device_of_a_write_target_is_refused():
    ha = _ha()
    ha.registry = [{**e, "device_id": "d-therme"} if e["entity_id"] == "sensor.room_target" else e for e in ha.registry]
    assert dokument.zuordnung_pruefen(KONFIGURATION, ha) == {"feld": "room_target", "regel": "nicht_erlaubt"}


@pytest.mark.parametrize("room_target, shift", [
    ("climate.zone::temperature", "climate.zone"),
    ("climate.zone", "climate.zone::temperature"),
    ("climate.zone::temperature", "climate.zone::temperature"),
])
def test_the_zone_as_room_target_is_refused(room_target, shift):
    # Final-Review I1 (bis 0.36 Startpruefung der Optionen): das Add-on schriebe die Parallelverschiebung in die Quelle
    # des Kundenwunsches (Rueckkopplung bis shift_max). Die Pruefung lehnt das Dokument vor dem Uebernehmen ab.
    konfiguration = _mit_rolle("room_setpoint", shift)
    konfiguration = {**konfiguration, "bindung": {**konfiguration["bindung"], "rollen": {
        **konfiguration["bindung"]["rollen"], "room_target": room_target}}}
    assert dokument.zuordnung_pruefen(konfiguration, _ha()) == {"feld": "room_target", "regel": "nicht_erlaubt"}


def test_the_current_temperature_of_the_zone_is_a_valid_room_sensor():
    konfiguration = {**KONFIGURATION, "raumfuehler": ["sensor.room_actual", "climate.zone::current_temperature"]}
    assert dokument.zuordnung_pruefen(konfiguration, _ha()) is None


def test_ha_unreachable_is_not_a_verdict():
    """Eine kurze HA-Stoerung (Neustart des Hosts) darf eine Version nie dauerhaft ablehnen (der Server stellt eine
    abgelehnte Version nicht erneut zu): kein Text, sondern die vorlaeufige Ausnahme."""
    ha = _ha()
    ha.offline = True
    with pytest.raises(config_check.NichtPruefbar):
        dokument.zuordnung_pruefen(KONFIGURATION, ha)


def test_ha_still_starting_is_not_a_verdict_either():
    """Waehrend HA hochfaehrt fehlen die Entities der Hersteller-Integration noch (404): kein Ablehnungsgrund."""
    ha = _ha()
    ha.config = {"state": "NOT_RUNNING"}
    ha.is_reachable = lambda: False
    with pytest.raises(config_check.NichtPruefbar):
        dokument.zuordnung_pruefen(KONFIGURATION, ha)


def test_a_hanging_ha_ends_the_check_at_the_first_timeout_without_a_verdict():
    """Heutiges Verhalten, Restpunkt 5c-D-8 (d): haengt HA statt zu verweigern, wartet die Pruefung im Geraete-Thread
    auf das Zeitlimit des ersten haengenden Aufrufs (10 s, Argument an requests) und endet dann mit NichtPruefbar, ohne
    weitere Aufrufe. Echte HomeAssistantApi, das Haengen als requests.Timeout am requests-Rand."""
    aufrufe = []

    def haengt(url, **kw):
        aufrufe.append((url.removeprefix("http://supervisor/core/api"), kw["timeout"]))
        raise requests.Timeout("haengt")

    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    with patch("heizungsbruecke.ha_api.requests.get", autospec=True, side_effect=haengt), \
            HaWebSocket().angeschlossen() as verbinden, pytest.raises(config_check.NichtPruefbar):
        dokument.zuordnung_pruefen(KONFIGURATION, api)
    assert aufrufe == [("/config", 10)] and verbinden.call_count == 0

    aufrufe.clear()

    def config_antwortet(url, **kw):
        if url.endswith("/config"):
            return ha_antwort({"state": "RUNNING"})
        return haengt(url, **kw)

    with patch("heizungsbruecke.ha_api.requests.get", autospec=True, side_effect=config_antwortet), \
            HaWebSocket(_ha().registry).angeschlossen(), pytest.raises(config_check.NichtPruefbar):
        dokument.zuordnung_pruefen(KONFIGURATION, api)
    assert aufrufe == [("/states/sensor.room_target", 10)]


def test_an_entity_whose_integration_loads_after_running_is_refused_at_the_first_setup():
    """Heutiges Verhalten, Restpunkt 5c-D-8 (e): HA laeuft (RUNNING), die Entity steht in der Registry, ihre
    Integration hat aber noch keinen Zustand geliefert (/api/states 404): die erste Einrichtung wird abgelehnt
    (unbekannt), nicht als NichtPruefbar zurueckgehalten."""
    ha = _ha()
    assert any(e["entity_id"] == "number.heat_limit" for e in ha.registry)
    ha.missing.add("number.heat_limit")
    assert dokument.zuordnung_pruefen(KONFIGURATION, ha) == {"feld": "heat_limit", "regel": "unbekannt"}


def test_a_contract_violation_in_the_details_is_a_programming_error_not_a_missing_ha(monkeypatch):
    """fehler.details wirft bei einem Verstoss ValueError; das darf nicht als NichtPruefbar (HA nicht erreichbar, still
    wiederholen) enden, sondern muss als Fehler sichtbar werden."""
    def verstoss(*args, **kwargs):
        raise ValueError("details passen nicht zum Vertrag")

    monkeypatch.setattr(dokument.fehler, "details", verstoss)
    ha = _ha()
    ha.missing.add("number.heat_limit")
    with pytest.raises(ValueError, match="Vertrag"):
        dokument.zuordnung_pruefen(KONFIGURATION, ha)


def test_a_broken_ha_answer_is_not_a_verdict():
    class Broken(FakeHa):
        def entity_registry(self):
            raise RuntimeError("WS-Kommando fehlgeschlagen")

    with pytest.raises(config_check.NichtPruefbar):
        dokument.zuordnung_pruefen(KONFIGURATION, Broken())


def test_unchanged_binding_skips_the_ha_part():
    ha = _ha()
    ha.offline = True
    neu = {**KONFIGURATION, "tagestick": "13:00", "abo": "inaktiv"}
    assert dokument.zuordnung_pruefen(neu, ha, KONFIGURATION) is None


@pytest.mark.parametrize("aenderung", [
    {"raumfuehler": ["sensor.other"]}, {"bindung": {**KONFIGURATION["bindung"], "abfrage_s": 600}},
])
def test_a_changed_binding_raumfuehler_or_lever_set_is_checked_against_ha(aenderung):
    ha = _ha()
    ha.offline = True
    with pytest.raises(config_check.NichtPruefbar):
        dokument.zuordnung_pruefen({**KONFIGURATION, **aenderung}, ha, KONFIGURATION)


def test_a_changed_lever_set_is_checked_against_ha():
    """Bindung gleich zur uebernommenen, nur der Hebelsatz anders: trotzdem gegen HA (Pflichtrollen siehe Weishaupt)."""
    weishaupt = {**KONFIGURATION, "hebelsatz": "weishaupt_wwp", "bindung": {**KONFIGURATION["bindung"], "rollen": {
        **KONFIGURATION["bindung"]["rollen"], "room_setpoint": "number.zone", "mode_select": "select.mode", "setpoint_comfort": "number.komfort",
        "setpoint_setback": "number.absenk"}}}
    ha = _ha()
    ha.offline = True
    with pytest.raises(config_check.NichtPruefbar):
        dokument.zuordnung_pruefen(weishaupt, ha, {**weishaupt, "hebelsatz": "vaillant_vrc720"})
    assert dokument.zuordnung_pruefen(weishaupt, ha, weishaupt) is None


def test_the_local_part_runs_even_for_an_unchanged_binding():
    rollen = dict(KONFIGURATION["bindung"]["rollen"])
    del rollen["heat_limit"]
    ohne = {**KONFIGURATION, "bindung": {**KONFIGURATION["bindung"], "rollen": rollen}}
    ha = _ha()
    ha.offline = True
    assert dokument.zuordnung_pruefen(ohne, ha, ohne) == {"feld": "heat_limit", "regel": "fehlt"}


def test_the_check_runs_through_config_check_as_role_not_allowed():
    ha = _ha()
    _fremd(ha, "number.heat_limit")
    ablehnung = config_check.pruefen(KONFIGURATION, None, host=config_check.HOST_HA,
                                     treiber_pruefen=lambda k: dokument.zuordnung_pruefen(k, ha))
    assert ablehnung == config_check.Ablehnung(config_check.ROLLE_UNZULAESSIG,
                                               {"feld": "heat_limit", "regel": "nicht_erlaubt"})


def test_die_ablehnung_nennt_die_rolle_nie_die_entity():
    ha = _ha()
    ha.missing.add("sensor.room_actual")  # Raumfuehler aus KONFIGURATION fehlt in HA
    details = dokument.zuordnung_pruefen(KONFIGURATION, ha)
    assert details == {"feld": "raumfuehler", "regel": "unbekannt"} and "room_actual" not in json.dumps(details)


def test_beschreibung_kommt_aus_der_gespeicherten_konfiguration():
    """Spec Stufe 3 S4b 4.3: Start und Abmelden (HaHost.sign_off_parts) bauen die Beschreibung ueber
    dokument_config.beschreibung aus der gespeicherten Konfiguration -- nicht aus einer Tabelle."""
    hebelsatz = KONFIGURATION["hebelsatz"]
    konfiguration = {**KONFIGURATION, "bindungsparameter": parameter(hebelsatz, zurueck=[], zurueck_optional=[])}
    assert dokument_config.beschreibung(konfiguration).restore_originals == ()
    ohne = {**KONFIGURATION, "bindungsparameter": None}
    with pytest.raises(ConfigError):
        dokument_config.beschreibung(ohne)


# --- A6-02: jede Rolle nur auf Entities der lokalen Positivliste (Probe FH-1) ---

def _rollen(konfiguration, ha):
    return dokument.rollen_pruefen(dokument.bindung(konfiguration), ha)


def _batterie(ref):
    return {**KONFIGURATION, "bindung": {**KONFIGURATION["bindung"], "batterien": [ref]}}


def _fremde_domaenen(ha):
    _dazu(ha, "person.luca", "person", wert="not_home")
    _dazu(ha, "lock.haustuer", "kwikset", wert="unlocked")
    _dazu(ha, "zone.home", wert="1")  # YAML-Zone ohne Registry-Eintrag
    ha.states["zone.home::longitude"] = 9.18


@pytest.mark.parametrize("rolle", ["outdoor_temp", "operating_mode", "generator_state"])
@pytest.mark.parametrize("ref", ["zone.home::longitude", "person.luca", "lock.haustuer"])
def test_fh1_lese_rollen_auf_fremde_entities_abgelehnt(rolle, ref):
    """Probe FH-1: ein kompromittierter Server bindet Lese-Rollen an Standort, Person oder Schloss; die Telemetrie
    schickte deren Zustand an den Server. Jetzt nicht_erlaubt, mit der Rolle (nie der Entity-ID) als Feld."""
    ha = _ha()
    _fremde_domaenen(ha)
    konfiguration = _mit_rolle(rolle, ref)
    assert dokument.zuordnung_pruefen(konfiguration, ha) == {"feld": rolle, "regel": "nicht_erlaubt"}
    assert _rollen(konfiguration, ha) == (rolle, "nicht_erlaubt")


def test_fh1_gegenprobe_dieselben_rollen_auf_erlaubten_entities():
    ha = _ha()
    _dazu(ha, "weather.haus")
    _dazu(ha, "select.betriebsart", "mypyllant")
    _dazu(ha, "climate.programm", "mypyllant")
    konfiguration = _mit_rolle("outdoor_temp", "weather.haus::temperature")
    for rolle, ref in (("mode_select", "select.betriebsart"), ("operating_mode", "climate.programm")):
        konfiguration = _mit_rolle_in(rolle, ref, konfiguration)
    assert _rollen(konfiguration, ha) is None


def test_gegenprobe_climate_ist_auch_als_mode_select_erlaubt():
    """Gegenprobe zur Domaenen-Positivliste: dieselbe climate-Entity ist als mode_select zulaessig wie als
    operating_mode (die Positivliste kennt die Domaene, nicht die Rolle)."""
    ha = _ha()
    _dazu(ha, "climate.programm", "mypyllant")
    assert _rollen(_mit_rolle("mode_select", "climate.programm"), ha) is None


def test_ohne_soll_entity_entfaellt_room_target_statt_einer_assertion():
    ha = _ha()
    rollen = {k: v for k, v in KONFIGURATION["bindung"]["rollen"].items() if k != "room_target"}
    konfiguration = {**KONFIGURATION, "bindung": {**KONFIGURATION["bindung"], "rollen": rollen}}
    assert _rollen(konfiguration, ha) is None


def test_eine_kennzahl_ohne_klasse_braucht_die_plattform_mit_einstellbarem():
    ha = _ha()
    _dazu(ha, "sensor.vorlauf", "zha")  # zha liefert hier nur Sensoren: nichts Einstellbares
    konfiguration = _mit_rolle("flow_temperature", "sensor.vorlauf")
    assert dokument.zuordnung_pruefen(konfiguration, ha) == {"feld": "flow_temperature", "regel": "nicht_erlaubt"}
    # derselbe Sensor auf der Kreis-Plattform (mypyllant hat number/climate): erlaubt
    ha = _ha()
    _dazu(ha, "sensor.vorlauf", "mypyllant")
    assert dokument.zuordnung_pruefen(konfiguration, ha) is None


def test_der_sensor_mit_klasse_nur_im_zustand_ist_erlaubt():
    ha = _ha()
    _dazu(ha, "sensor.vorlauf", "zha", klasse="temperature")
    assert dokument.zuordnung_pruefen(_mit_rolle("flow_temperature", "sensor.vorlauf"), ha) is None
    _dazu(ha, "sensor.zaehler", "zha", klasse="energy")
    assert _rollen(_mit_rolle("energy_primary_heating", "sensor.zaehler"), ha) is None
    assert all(entry.get("device_class") is None for entry in ha.registry)  # wie das echte HA


def test_eine_sensor_klasse_ausserhalb_der_liste_zaehlt_nicht():
    ha = _ha()
    _dazu(ha, "sensor.licht", "zha", klasse="illuminance")
    assert _rollen(_mit_rolle("flow_temperature", "sensor.licht"), ha) == ("flow_temperature", "nicht_erlaubt")


@pytest.mark.parametrize(("klasse", "urteil"), [("door", ("batterien", "nicht_erlaubt")), ("battery", None)])
def test_eine_batterie_als_binary_sensor_braucht_die_klasse_battery(klasse, urteil):
    ha = _ha()
    _dazu(ha, "binary_sensor.fenster", "zha", klasse=klasse)
    assert _rollen(_batterie("binary_sensor.fenster"), ha) == urteil


def test_der_raumfuehler_als_climate_attribut_ist_erlaubt_ein_schloss_nicht():
    ha = _ha()
    _fremde_domaenen(ha)
    ok = {**KONFIGURATION, "raumfuehler": ["climate.zone::current_temperature"]}
    assert _rollen(ok, ha) is None
    schloss = {**KONFIGURATION, "raumfuehler": ["lock.haustuer"]}
    assert _rollen(schloss, ha) == ("raumfuehler", "nicht_erlaubt")
    assert dokument.zuordnung_pruefen(schloss, ha) == {"feld": "raumfuehler", "regel": "nicht_erlaubt"}


@pytest.mark.parametrize("ref", ["input_number.wunsch", "lock.haustuer"])
def test_die_soll_entity_nur_aus_der_positivliste(ref):
    ha = _ha()
    _dazu(ha, "input_number.wunsch", wert=21.0)
    _fremde_domaenen(ha)
    assert dokument.zuordnung_pruefen(_mit_rolle("room_target", ref), ha) == {
        "feld": "room_target", "regel": "nicht_erlaubt"}


def test_ein_gebundenes_climate_als_soll_entity_bleibt_erlaubt():
    """Bewusst hingenommen (Spec 2): die Soll-Entity darf ein anderes climate im Haus sein."""
    ha = _ha()
    _dazu(ha, "climate.kinderzimmer", "tado")
    assert _rollen(_mit_rolle("room_target", "climate.kinderzimmer::temperature"), ha) is None


def test_ein_sensor_ohne_klasse_einer_fremd_plattform_mit_number_ist_als_batterie_erlaubt():
    """Restrisiko (Spec 4, wie die Inventur): zha liefert hier irgendein number, damit gilt jeder zha-Sensor ohne
    Klasse als einstellbar-Plattform und damit auch als Batterie."""
    ha = _ha()
    _dazu(ha, "sensor.irgendwas", "zha")
    _dazu(ha, "number.zigbee_dimmer", "zha")
    assert _rollen(_batterie("sensor.irgendwas"), ha) is None


def test_die_einstellbare_plattform_zaehlt_nur_aus_aktiven_entities_mit_zustand():
    ha = _ha()
    _dazu(ha, "sensor.irgendwas", "zha")
    ha.registry.append({"entity_id": "number.aus", "platform": "zha", "disabled_by": "user"})
    ha.states["number.aus"] = 1.0
    assert _rollen(_batterie("sensor.irgendwas"), ha) == ("batterien", "nicht_erlaubt")
    ha = _ha()
    _dazu(ha, "sensor.irgendwas", "zha")
    ha.registry.append({"entity_id": "number.ohne_zustand", "platform": "zha"})  # kein Zustand: zaehlt nicht
    assert _rollen(_batterie("sensor.irgendwas"), ha) == ("batterien", "nicht_erlaubt")


def test_eine_gebundene_entity_ohne_zustand_ist_nicht_pruefbar():
    ha = _ha()
    # die Batterie aus KONFIGURATION hat keinen Zustand
    del ha.states["sensor.thermostat_battery"], ha.states["sensor.thermostat_battery::device_class"]
    with pytest.raises(config_check.NichtPruefbar):
        _rollen(KONFIGURATION, ha)
    with pytest.raises(config_check.NichtPruefbar):  # entity_exists sagt ja, der Zustand fehlt trotzdem
        dokument.zuordnung_pruefen(KONFIGURATION, ha)


def test_das_feld_ist_die_v1_rolle_in_der_reihenfolge_von_mit_ha():
    ha = _ha()
    _fremde_domaenen(ha)
    konfiguration = _mit_rolle("curve", "lock.haustuer")
    konfiguration = {**konfiguration, "raumfuehler": ["lock.haustuer"]}
    assert _rollen(konfiguration, ha) == ("curve", "nicht_erlaubt")  # Rollen vor Raumfuehler
    assert _rollen(_mit_rolle("room_target", "lock.haustuer"), ha) == ("room_target", "nicht_erlaubt")  # zuerst
    assert dokument.zuordnung_pruefen(konfiguration, ha) == {"feld": "curve", "regel": "nicht_erlaubt"}


def test_unbekannte_entity_bleibt_vor_der_positivliste_unbekannt():
    ha = _ha()
    _fremde_domaenen(ha)
    ha.missing.add("number.heat_limit")
    konfiguration = _mit_rolle("outdoor_temp", "lock.haustuer")
    assert dokument.zuordnung_pruefen(konfiguration, ha) == {"feld": "heat_limit", "regel": "unbekannt"}


def test_die_positivliste_greift_vor_den_schreibziel_regeln():
    ha = _ha()
    _fremd(ha, "number.heat_limit")  # Schreibziel einer Fremd-Plattform (nicht_erlaubt) ...
    _fremde_domaenen(ha)
    konfiguration = _mit_rolle("operating_mode", "lock.haustuer")  # ... und eine verbotene Lese-Rolle
    assert dokument.zuordnung_pruefen(konfiguration, ha) == {"feld": "operating_mode", "regel": "nicht_erlaubt"}


def _client1():
    """Die echte Zuordnung von client1 (Pruefung 2026-10-21, a602_pruefen.py): Entity, Plattform, Klasse."""
    def ent(entity_id, plattform, klasse=None, device="d-vaillant"):
        return entity_id, plattform, klasse, device

    rollen = {
        "curve": ent("number.zuhause_circuit_0_heating_curve", "mypyllant"),
        "energy_primary_heating": ent("sensor.zuhause_circuit_0_energy_primary_heating", "mypyllant", "energy"),
        "energy_thermal_heating": ent("sensor.zuhause_circuit_0_energy_thermal_heating", "mypyllant", "energy"),
        "flow_setpoint": ent("sensor.heizraum_zuhause_circuit_0_flow_temperature_setpoint", "mypyllant", "temperature"),
        "flow_temperature": ent("sensor.zuhause_circuit_0_current_flow_temperature", "mypyllant", "temperature"),
        "heat_limit": ent("number.zuhause_circuit_0_heat_demand_limited_by_outside_temperature", "mypyllant"),
        "min_flow": ent("number.zuhause_circuit_0_min_flow_temperature_setpoint", "mypyllant"),
        "outdoor_temp": ent("sensor.zuhause_outdoor_temperature", "mypyllant", "temperature"),
        "room_setpoint": ent("climate.zuhause_zone_1_circuit_0_climate", "mypyllant"),
        "system_water_pressure": ent("sensor.zuhause_system_water_pressure", "mypyllant", "pressure"),
    }
    soll = ent("climate.wohnzimmer_thermostat", "homematicip_local", device="d-wz")
    fuehler = ent("sensor.wohnzimmer_thermostat_temperatur", "homematicip_local", "temperature", "d-wz")
    batterie = ent("binary_sensor.wohnzimmer_thermostat_batterie", "homematicip_local", "battery", "d-wz")
    ha = FakeHa()
    ha.states.clear()
    for entity_id, plattform, klasse, device in [*rollen.values(), soll, fuehler, batterie]:
        ha.registry.append({"entity_id": entity_id, "platform": plattform, "config_entry_id": "e1" if
                            plattform == "mypyllant" else "e2", "device_id": device})
        ha.states[entity_id] = 21.0
        if klasse:
            ha.states[f"{entity_id}::device_class"] = klasse
    ha.states[f"{soll[0]}::temperature"] = 21.0
    konfiguration = {**KONFIGURATION, "raumfuehler": [fuehler[0]], "bindung": {
        **KONFIGURATION["bindung"], "batterien": [batterie[0]],
        "benachrichtigung": ["notify.mobile_app_lucas_iphone"],
        "rollen": {"room_target": f"{soll[0]}::temperature", **{rolle: e[0] for rolle, e in rollen.items()}}}}
    return konfiguration, ha


def test_die_echte_zuordnung_von_client1_besteht():
    konfiguration, ha = _client1()
    assert _rollen(konfiguration, ha) is None
    assert dokument.zuordnung_pruefen(konfiguration, ha) is None
    assert config_check.ha_bindung_pruefen(konfiguration) is None


def test_client1_mit_einer_verbotenen_rolle_wird_abgelehnt():
    konfiguration, ha = _client1()
    _fremde_domaenen(ha)
    verboten = _mit_rolle_in("system_water_pressure", "person.luca", konfiguration)
    assert dokument.zuordnung_pruefen(verboten, ha) == {"feld": "system_water_pressure", "regel": "nicht_erlaubt"}

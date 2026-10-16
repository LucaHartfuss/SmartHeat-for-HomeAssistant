"""Spec 5c 1.3, Stufe 3 S5 3.2: Konfigurationsdokument -> HaBindung; Rollentabellen; HA-Teil der lokalen Pruefung."""
import bindung_beispiele
import pytest
from bindung_beispiele import parameter
from fakes import FakeHa

from heizungsbruecke import dokument
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
        "batterien": ["sensor.thermostat_battery"], "benachrichtigung": ["notify.handy"], "hinweise_aus": [],
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
    return ha


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
    assert b.benachrichtigung == ("notify.handy",) and b.hinweise_aus == ()
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
    assert "Hebelsatz" in dokument.zuordnung_pruefen({**konfiguration, "hebelsatz": hebelsatz}, _ha())


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
    assert "heat_limit" in dokument.zuordnung_pruefen(konfiguration, _ha())


def test_unknown_entity_is_refused():
    ha = _ha()
    ha.missing.add("number.heat_limit")
    assert "number.heat_limit" in dokument.zuordnung_pruefen(KONFIGURATION, ha)


def test_write_target_of_another_platform_is_refused():
    ha = _ha()
    _fremd(ha, "number.heat_limit")
    assert "number.heat_limit" in dokument.zuordnung_pruefen(KONFIGURATION, ha)


def test_the_curve_is_a_write_target_too():
    """Die Heizkurve ist bei vaillant_vrc720 kein Teil von SCHREIBREGELN, wird aber jeden Takt geschrieben
    (Spec 1.3, A4-11): ein number.* der Plattform template darf sie nicht sein."""
    ha = _ha()
    _fremd(ha, "number.curve_current")
    assert "number.curve_current" in dokument.zuordnung_pruefen(KONFIGURATION, ha)
    ha = _ha()
    ha.registry = [{**e, "config_entry_id": "fremd"} if e["entity_id"] == "number.curve_current" else e
                   for e in ha.registry]
    assert "number.curve_current" in dokument.zuordnung_pruefen(KONFIGURATION, ha)


def test_every_lever_of_the_binding_is_checked():
    konfiguration = _mit_rolle("level", "number.level")
    ha = _ha()
    ha.registry.append({"entity_id": "number.level", "platform": "template", "config_entry_id": None,
                        "device_id": None})
    assert "number.level" in dokument.zuordnung_pruefen(konfiguration, ha)


def test_a_circuit_without_config_entry_checks_only_the_platform():
    kreis = {**KONFIGURATION["bindung"]["kreis"], "config_entry": None}
    konfiguration = {**KONFIGURATION, "bindung": {**KONFIGURATION["bindung"], "kreis": kreis}}
    assert dokument.zuordnung_pruefen(konfiguration, _ha()) is None
    ha = _ha()
    _fremd(ha, "number.heat_limit")
    assert "number.heat_limit" in dokument.zuordnung_pruefen(konfiguration, ha)


def test_soll_entity_on_the_device_of_a_write_target_is_refused():
    ha = _ha()
    ha.registry = [{**e, "device_id": "d-therme"} if e["entity_id"] == "sensor.room_target" else e for e in ha.registry]
    assert "Wunschtemperatur" in dokument.zuordnung_pruefen(KONFIGURATION, ha)


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
    assert "Wunschtemperatur" in dokument.zuordnung_pruefen(konfiguration, _ha())


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
    assert "heat_limit" in dokument.zuordnung_pruefen(ohne, ha, ohne)


def test_the_check_runs_through_config_check_as_role_not_allowed():
    ha = _ha()
    _fremd(ha, "number.heat_limit")
    ablehnung = config_check.pruefen(KONFIGURATION, None, host=config_check.HOST_HA,
                                     treiber_pruefen=lambda k: dokument.zuordnung_pruefen(k, ha))
    assert ablehnung.grund == config_check.ROLLE_UNZULAESSIG and "number.heat_limit" in ablehnung.text


def test_beschreibung_kommt_aus_der_gespeicherten_konfiguration():
    """Spec Stufe 3 S4b 4.3: Start und Abmelden (HaHost.sign_off_parts) bauen die Beschreibung ueber
    dokument_config.beschreibung aus der gespeicherten Konfiguration -- nicht aus einer Tabelle."""
    hebelsatz = KONFIGURATION["hebelsatz"]
    konfiguration = {**KONFIGURATION, "bindungsparameter": parameter(hebelsatz, zurueck=[], zurueck_optional=[])}
    assert dokument_config.beschreibung(konfiguration).restore_originals == ()
    ohne = {**KONFIGURATION, "bindungsparameter": None}
    with pytest.raises(ConfigError):
        dokument_config.beschreibung(ohne)

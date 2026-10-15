"""Spec 5c 1.3: Konfigurationsdokument -> bisherige Optionen; HA-Teil der lokalen Pruefung."""
import bindung_beispiele
import pytest
from fakes import FakeHa

from heizungsbruecke import dokument
from smartheat_core import config_check

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


def _fremd(ha, entity_id, platform="template"):
    ha.registry = [{**e, "platform": platform} if e["entity_id"] == entity_id else e for e in ha.registry]


def test_options_carry_the_old_keys_and_the_target_store():
    options = dokument.optionen(KONFIGURATION)
    assert options["tenant_id"] == options["setup_id"] == "zuhause2"
    assert options["entity_curve_current"] == "number.curve_current"
    assert options["entity_shift_current"] == "climate.zone"
    assert options["entity_room_target"] == dokument.SOLL_REF
    assert options["room_sensors"] == ["sensor.room_actual"]
    assert options["notify_services"] == ["notify.handy"]
    assert options["bindungsparameter"] is KONFIGURATION["bindungsparameter"]
    assert options["daily_trigger_time"] == "12:07" and options["lever_set"] == "vaillant_vrc720"
    assert dokument.soll_entity(KONFIGURATION) == "sensor.room_target"


def test_intervals_and_other_levers_reach_the_options():
    konfiguration = {**KONFIGURATION, "intervalle": {"pruefung_s": 5, "telemetrie_s": 30}}
    konfiguration = {**konfiguration, "bindung": {**konfiguration["bindung"], "rollen": {
        **konfiguration["bindung"]["rollen"], "level": "number.level", "flow_temperature": "sensor.vl"}}}
    options = dokument.optionen(konfiguration)
    assert options["local_check_interval_seconds"] == 5 and options["telemetry_interval_seconds"] == 30
    assert options["entity_level_current"] == "number.level" and options["entity_flow_temperature"] == "sensor.vl"
    assert dokument.option_fuer("min_flow") == "entity_min_flow" and dokument.option_fuer("outdoor_temp") == "entity_outdoor_temp"


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
    """Die Heizkurve ist bei vaillant_vrc720 kein Teil von writable_entity_rules, wird aber jeden Takt geschrieben
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

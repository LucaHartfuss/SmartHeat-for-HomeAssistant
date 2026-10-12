"""Add-on-Konfiguration ab 1.0.0 (Spec 5c 1.3): Pflicht- und Schreibregeln je Hebelsatz, die das Konfigurationsdokument
vor dem Start erfuellen muss (dokument.lokal_pruefen), und die gemeinsamen Pruefungen aus smartheat_runtime.options
(Intervalle, Hebelsatz, lokale Sicherheitswerte), die HaHost.load auf die Optionen des Dokuments anwendet. Die
Optionspruefung von options.json (resolve_effective_options, Zugang, Transport) entfiel mit dem Optionsmodus; Muster
der Raumfuehler, Batterien, Handys und Hinweise prueft smartheat_core.config_check (test_core_config_check_ha.py)."""
import dataclasses

import pytest
from geraet_helpers import konfiguration_aus_optionen

from heizungsbruecke import config, dokument
from smartheat_core.safety import LOCAL_SAFETY
from smartheat_runtime.options import (
    DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS,
    ConfigError,
    local_check_interval,
    telemetry_interval,
    validate,
    validate_local_check_interval,
    validate_telemetry_interval,
)

VALID = {
    "tenant_id": "t", "verteilsystem": "Heizkoerper", "daily_trigger_time": "12:00", "room_sensors": ["sensor.r"],
    "entity_room_target": "sensor.t", "entity_curve_current": "number.c", "entity_shift_current": "climate.zone",
    "entity_min_flow": "number.mf", "entity_outdoor_temp": "sensor.o", "entity_heat_limit": "number.hl",
}


def _lokal(options: dict) -> str | None:
    """Der Teil der lokalen Pruefung ohne HA fuer das Dokument aus den Test-Optionen (leere Optionen = Rolle fehlt)."""
    return dokument.lokal_pruefen(konfiguration_aus_optionen(options))


def test_boost_values_outside_the_ranges_are_a_configuration_error(monkeypatch):
    """Ersetzt die validate_boost_config-Tests bis 0.29.0: Boost-Werte ausserhalb der Bereiche sind ein Startfehler
    statt still geclampt (der Boost ist der einzige Schreibpfad ohne Server-Aufsicht). Geprueft wird jetzt in
    smartheat_core.safety.check_invariants (test_core_safety::test_invariants); hier der Weg bis zum Konfigurationsfehler."""
    safety = dict(LOCAL_SAFETY)
    heizkoerper = safety[("vaillant_vrc720", "Heizkoerper")]
    for comfort_boost in ({**heizkoerper.comfort_boost, "curve": 99.0}, {**heizkoerper.comfort_boost, "curve": -1.0},
                          {**heizkoerper.comfort_boost, "room_setpoint": 999.0},
                          {**heizkoerper.comfort_boost, "room_setpoint": 26.0}):
        safety[("vaillant_vrc720", "Heizkoerper")] = dataclasses.replace(heizkoerper, comfort_boost=comfort_boost)
        monkeypatch.setattr("smartheat_core.safety.LOCAL_SAFETY", safety)
        with pytest.raises(ConfigError, match="Comfort-Boost"):
            config.local_safety({"verteilsystem": "Heizkoerper"})


def test_boost_values_on_the_range_boundaries_are_accepted(monkeypatch):
    safety = dict(LOCAL_SAFETY)
    heizkoerper = safety[("vaillant_vrc720", "Heizkoerper")]
    low, high = heizkoerper.ranges["curve"]
    for curve in (low, high):
        boosted = dataclasses.replace(heizkoerper, comfort_boost={**heizkoerper.comfort_boost, "curve": curve})
        safety[("vaillant_vrc720", "Heizkoerper")] = boosted
        monkeypatch.setattr("smartheat_core.safety.LOCAL_SAFETY", safety)
        assert config.local_safety({"verteilsystem": "Heizkoerper"}).comfort_boost["curve"] == curve


def test_the_options_of_the_document_carry_the_local_safety_values():
    safety = config.local_safety(dokument.optionen(konfiguration_aus_optionen(VALID)))
    assert safety.ranges["curve"] == (0.4, 1.5)
    assert safety.ranges["room_setpoint"] == (15.0, 25.0)
    assert safety.ranges["min_flow"] == (20.0, 30.0)
    assert safety.ranges["heat_limit"] == (5.0, 23.0)
    assert safety.arrival_threshold_k == 0.5
    assert (safety.comfort_boost["curve"], safety.comfort_boost["room_setpoint"]) == (1.5, 25.0)
    assert safety.comfort_boost["heat_limit"] == 23.0  # bis 0.29.0: Boost-Heizgrenze = heat_limit_max


@pytest.mark.parametrize("verteilsystem", [None, "", "Deckenheizung", "Unbekannt"])
def test_an_unknown_verteilsystem_has_no_safety_values(verteilsystem):
    with pytest.raises(ConfigError, match="verteilsystem"):
        config.local_safety({"verteilsystem": verteilsystem})


def test_validate_local_check_interval_accepts_absent_and_valid_values():
    assert validate_local_check_interval({}) is None
    assert validate_local_check_interval({"local_check_interval_seconds": 30}) is None
    assert validate_local_check_interval({"local_check_interval_seconds": 60}) is None
    assert validate_local_check_interval({"local_check_interval_seconds": 3600}) is None


@pytest.mark.parametrize("value", [3601, float("nan"), float("inf"), "not-a-number", 0, -5, 0.5, True])
def test_validate_local_check_interval_flags_invalid_values_by_name(value):
    # NaN/inf und Text: bis 2026-09 rutschten sie durch bzw. warfen TypeError statt des Fehlertexts.
    error = validate_local_check_interval({"local_check_interval_seconds": value})
    assert error is not None and "local_check_interval_seconds" in error


@pytest.mark.parametrize("value, accepted", [(9, False), (10, True), (300, True), (600, True), (601, False), (900, False)])
def test_telemetry_interval_bounds(value, accepted):
    error = validate_telemetry_interval({"telemetry_interval_seconds": value})
    assert (error is None) == accepted


@pytest.mark.parametrize("value", [0, 601, float("nan"), float("inf"), "300s", True])
def test_validate_telemetry_interval_flags_invalid_values_by_name(value):
    error = validate_telemetry_interval({"telemetry_interval_seconds": value})
    assert error is not None and "telemetry_interval_seconds" in error


def test_telemetry_interval_error_explains_the_reserve():
    error = validate_telemetry_interval({"telemetry_interval_seconds": 900})
    assert "600" in error and "15 min" in error


def test_validate_returns_first_error_or_none():
    valid = {"entity_outdoor_temp": "sensor.outdoor"}

    assert validate(valid) is None
    error = validate({**valid, "local_check_interval_seconds": 0})
    assert error is not None and "local_check_interval_seconds" in error
    error = validate({**valid, "telemetry_interval_seconds": 5})
    assert error is not None and "telemetry_interval_seconds" in error


def test_intervals_fall_back_to_300_seconds():
    assert DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS == 300
    assert local_check_interval({}) == 300
    assert telemetry_interval({}) == 300
    assert local_check_interval({"local_check_interval_seconds": 60}) == 60
    assert telemetry_interval({"telemetry_interval_seconds": 30}) == 30


# --- Pflicht- und Schreibregeln des Hebelsatzes (dokument.lokal_pruefen) ---

def test_the_valid_vaillant_document_passes():
    assert _lokal(VALID) is None


@pytest.mark.parametrize("key, value", [
    ("entity_shift_current", "sensor.zone_temperature"),
    ("entity_shift_current", "input_number.shift"),
    ("entity_shift_current", "climate.zone::current_temperature"),
    ("entity_min_flow", "climate.zone"),
    ("entity_min_flow", "input_number.min_flow"),
    ("entity_min_flow", "sensor.min_flow"),
    ("entity_heat_limit", "sensor.heizgrenze"),
    ("entity_heat_limit", "input_number.heizgrenze"),
])
def test_unwritable_entity_domain_is_refused(key, value):
    # Spec 5.6: Zonen-Entity muss schreibbar sein; HaPlantBinding.write kennt climate.set_temperature und
    # number.set_value.
    assert f"{value} muss" in _lokal({**VALID, key: value})


@pytest.mark.parametrize("shift", ["climate.zone", "climate.zone::temperature", "number.shift"])
def test_writable_shift_domains_are_accepted(shift):
    assert _lokal({**VALID, "entity_shift_current": shift}) is None


@pytest.mark.parametrize("missing", [k for k in config.REQUIRED_ENTITY_OPTIONS["vaillant_vrc720"]
                                     if k != "entity_room_target"])
def test_every_vaillant_role_is_required(missing):
    assert "fehlt die Zuordnung" in _lokal({**VALID, missing: ""})


# --- Plan 3b: Hebelsatz ---

WEISHAUPT_OPTIONS = {
    **{key: value for key, value in VALID.items() if key not in ("entity_min_flow",)},
    "lever_set": "weishaupt_wwp", "entity_curve_current": "number.hk", "entity_shift_current": "number.normal",
    "entity_heat_limit": "number.swu", "entity_mode_select": "select.betriebsart",
    "entity_setpoint_comfort": "number.komfort", "entity_setpoint_setback": "number.absenk",
}
VIESSMANN_OPTIONS = {
    **{key: value for key, value in VALID.items() if key not in ("entity_min_flow", "entity_heat_limit")},
    "lever_set": "viessmann_vicare", "entity_curve_current": "number.slope", "entity_level_current": "number.shift",
    "entity_shift_current": "number.normal_temperature", "entity_mode_select": "climate.heizkreis",
}


@pytest.mark.parametrize("options", [{}, {"lever_set": None}, {"lever_set": ""}, {"lever_set": "vaillant_vrc720"}])
def test_missing_lever_set_means_vaillant(options):
    assert config.lever_set_id(options) == "vaillant_vrc720"
    assert config.binding_description(options).lever_set.id == "vaillant_vrc720"


@pytest.mark.parametrize("value", ["buderus", 3, ["weishaupt_wwp"], True])
def test_unknown_lever_set_is_a_configuration_error(value):
    with pytest.raises(ConfigError, match="lever_set"):
        config.lever_set_id({"lever_set": value})


def test_weishaupt_has_its_own_safety_values():
    options = dokument.optionen(konfiguration_aus_optionen(WEISHAUPT_OPTIONS))
    assert _lokal(WEISHAUPT_OPTIONS) is None
    assert config.local_safety(options).ranges == {
        "curve": (0.30, 1.00), "room_setpoint": (16.0, 25.0), "heat_limit": (5.0, 23.0),
    }
    floor = config.local_safety({**options, "verteilsystem": "Fussbodenheizung"})
    assert floor.comfort_boost == {}


@pytest.mark.parametrize("missing", [k for k in config.REQUIRED_ENTITY_OPTIONS["weishaupt_wwp"]
                                     if k not in ("entity_room_target", "entity_outdoor_temp")])
def test_every_weishaupt_role_is_required(missing):
    assert f"weishaupt_wwp fehlt die Zuordnung {missing.removeprefix('entity_')}" in _lokal(
        {**WEISHAUPT_OPTIONS, missing: ""})


def test_basis_does_not_need_curve_or_heat_limit():
    options = {**WEISHAUPT_OPTIONS, "lever_set": "weishaupt_wwp_basis", "entity_curve_current": "",
               "entity_heat_limit": ""}
    assert _lokal(options) is None


@pytest.mark.parametrize(("key", "value"), [
    ("entity_mode_select", "input_select.betriebsart"), ("entity_setpoint_comfort", "sensor.komfort"),
    ("entity_shift_current", "climate.zone"),
])
def test_weishaupt_entities_must_be_writable(key, value):
    assert f"{value} muss" in _lokal({**WEISHAUPT_OPTIONS, key: value})


def test_viessmann_program_must_be_a_climate_entity():
    assert _lokal(VIESSMANN_OPTIONS) is None
    assert "select.programm muss" in _lokal({**VIESSMANN_OPTIONS, "entity_mode_select": "select.programm"})


@pytest.mark.parametrize("lever_set", ["weishaupt_wwp", "viessmann_vicare"])
def test_mode_select_is_required_for_weishaupt_and_viessmann(lever_set):
    # Ohne Betriebsart/Heizprogramm wirft read_aux einen KeyError (Review Task 8/9): Konfigurationsfehler beim Start.
    assert "entity_mode_select" in config.required_entity_options(lever_set)
    options = WEISHAUPT_OPTIONS if lever_set == "weishaupt_wwp" else VIESSMANN_OPTIONS
    assert "fehlt die Zuordnung mode_select" in _lokal({**options, "entity_mode_select": ""})


@pytest.mark.parametrize(("value", "error"), [
    (None, False), (30, False), (10, False), (3600, False), (9, True), (3601, True), ("30", True), (True, True),
    (float("nan"), True), (float("inf"), True), (float("-inf"), True),
])
def test_poll_interval_is_validated(value, error):
    options = {} if value is None else {"poll_interval_seconds": value}
    assert (config.validate(options) is not None) is error


def test_binding_description_takes_the_poll_interval():
    assert config.binding_description({**WEISHAUPT_OPTIONS, "poll_interval_seconds": 90}).settle_seconds == 240
    assert config.binding_description(WEISHAUPT_OPTIONS).settle_seconds == 120
    assert config.binding_description({**VALID, "poll_interval_seconds": 90}).settle_seconds == 2100


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 5, 99999, "90", True])
def test_binding_description_ignores_an_invalid_poll_interval(value):
    # Das Abmelden laeuft ohne config.validate: ein ungueltiges Intervall gilt dort als fehlend (Standardwert).
    assert config.binding_description({**WEISHAUPT_OPTIONS, "poll_interval_seconds": value}).settle_seconds == 120

import dataclasses
import json

import pytest

from heizungsbruecke import config
from heizungsbruecke.config import (
    DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS,
    NEW_ENTITY_OPTIONS,
    REQUIRED_OPTIONS,
    ConfigError,
    is_configured,
    load_options_safe,
    local_check_interval,
    resolve_effective_options,
    telemetry_interval,
    validate,
    validate_local_check_interval,
    validate_telemetry_interval,
)
from smartheat_core.safety import LOCAL_SAFETY

PROFILE_PARAMS = {
    "verteilsystem": "Heizkoerper",
    "daily_trigger_time": "12:00",
}

REQUIRED = {
    "tenant_id": "wohnung1",
    "mqtt_username": "wohnung1_a1b2c3d4", "mqtt_password": "geheim",
    "room_sensors": ["sensor.rt"], "entity_room_target": "sensor.target_rt",
    "entity_curve_current": "number.curve", "entity_shift_current": "climate.zone",
    "entity_min_flow": "number.min_flow",
    "entity_outdoor_temp": "sensor.outdoor", "entity_heat_limit": "number.heat_limit",
}

BASE_URL = {"accounts_api_base_url": "https://accounts.example.test"}


def test_is_configured_true_when_all_required_fields_present():
    assert is_configured(dict(REQUIRED)) is True


def test_is_configured_false_when_a_required_field_is_missing():
    assert is_configured({"tenant_id": "wohnung1"}) is False


def test_is_configured_false_for_empty_options():
    assert is_configured({}) is False


def test_load_options_safe_defaults_when_no_file(tmp_path):
    assert load_options_safe(tmp_path / "does_not_exist.json") == {}


def test_load_options_safe_falls_back_on_corrupt_file(tmp_path):
    # Simulates power loss on the Pi's disk (Datentraeger) mid-write: a truncated/corrupt options
    # file must not crash the whole add-on before it can even report its state.
    path = tmp_path / "options.json"
    path.write_bytes(b"{not valid json..")

    assert load_options_safe(path) == {}


def test_load_options_safe_passes_through_valid_file(tmp_path):
    path = tmp_path / "options.json"
    path.write_text(json.dumps({"tenant_id": "wohnung1"}))

    assert load_options_safe(path) == {"tenant_id": "wohnung1"}


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


def test_is_configured_true_for_0_17_0_options_without_new_values():
    # Alte Konfiguration: "profile" gesetzt, neue Werte fehlen. Muss als "eingerichtet"
    # gelten, damit der Start laut abbricht statt still zu warten (Spec TP3, 2.1).
    assert is_configured({**REQUIRED, "profile": "vaillant_gastherme_heizkoerper"}) is True


def test_required_options_do_not_contain_new_values():
    assert "profile" not in REQUIRED_OPTIONS
    assert not set(PROFILE_PARAMS) & set(REQUIRED_OPTIONS)
    assert "accounts_api_base_url" not in REQUIRED_OPTIONS


def test_required_options_no_longer_contain_entity_offset_current():
    assert "entity_offset_current" not in REQUIRED_OPTIONS
    assert not set(NEW_ENTITY_OPTIONS) & set(REQUIRED_OPTIONS)


def test_new_entity_options_constant():
    assert NEW_ENTITY_OPTIONS == ("entity_shift_current", "entity_min_flow")


def test_resolve_effective_options_uses_local_safety_and_daily_trigger_time():
    effective = resolve_effective_options({**REQUIRED, **PROFILE_PARAMS, **BASE_URL})
    safety = config.local_safety(effective)

    assert safety.ranges["curve"] == (0.4, 1.5)
    assert safety.ranges["room_setpoint"] == (15.0, 25.0)
    assert safety.ranges["min_flow"] == (20.0, 30.0)
    assert safety.arrival_threshold_k == 0.5
    assert safety.comfort_boost["curve"] == 1.5
    assert safety.comfort_boost["room_setpoint"] == 25.0
    assert "curve_min" not in effective and "boost_curve_value" not in effective  # keine flachen Optionen mehr
    assert effective["daily_trigger_time"] == "12:00"
    assert effective["tenant_id"] == "wohnung1"


def test_resolve_effective_options_ignores_safety_values_in_options():
    effective = resolve_effective_options(
        {**REQUIRED, **PROFILE_PARAMS, **BASE_URL, "shift_max": 28.0, "boost_curve_value": 0.1}
    )

    safety = config.local_safety(effective)
    assert safety.ranges["room_setpoint"][1] == 25.0  # lokale Sicherheitswerte gewinnen
    assert safety.comfort_boost["curve"] == 1.5


@pytest.mark.parametrize("verteilsystem", [None, "", "Fussbodenheizung", "Unbekannt"])
def test_resolve_effective_options_rejects_verteilsystem(verteilsystem):
    options = {**REQUIRED, **PROFILE_PARAMS, **BASE_URL, "verteilsystem": verteilsystem}
    if verteilsystem is None:
        del options["verteilsystem"]

    with pytest.raises(ConfigError, match="verteilsystem"):
        resolve_effective_options(options)


def test_resolve_effective_options_0_17_0_config_names_daily_trigger_time():
    # Eine 0.17.0-Konfiguration hat weder verteilsystem noch daily_trigger_time; die
    # Pruefung auf daily_trigger_time greift zuerst.
    with pytest.raises(ConfigError, match="daily_trigger_time"):
        resolve_effective_options({**REQUIRED, "profile": "vaillant_gastherme_heizkoerper"})


def test_resolve_effective_options_passes_base_url():
    effective = resolve_effective_options({**REQUIRED, **PROFILE_PARAMS, **BASE_URL})

    assert effective["accounts_api_base_url"] == "https://accounts.example.test"


def test_accounts_api_base_url_strips_trailing_slash():
    from heizungsbruecke.config import resolve_accounts_api_base_url
    assert resolve_accounts_api_base_url("https://accounts.example.test/") == "https://accounts.example.test"


def test_accounts_api_base_url_keeps_path_and_strips_trailing_slash():
    from heizungsbruecke.config import resolve_accounts_api_base_url
    assert resolve_accounts_api_base_url("https://example.test/api/") == "https://example.test/api"


@pytest.mark.parametrize("value", [None, "", "http://accounts.example.test", "https://", "accounts.example.test", 42])
def test_accounts_api_base_url_rejects(value):
    from heizungsbruecke.config import resolve_accounts_api_base_url
    with pytest.raises(ConfigError, match="accounts_api_base_url"):
        resolve_accounts_api_base_url(value)


def test_resolve_effective_options_requires_base_url():
    with pytest.raises(ConfigError, match="accounts_api_base_url"):
        resolve_effective_options({**REQUIRED, **PROFILE_PARAMS})


def test_accounts_api_base_url_constant_is_gone():
    import heizungsbruecke.config as config_module
    assert not hasattr(config_module, "ACCOUNTS_API_BASE_URL")


def test_validate_local_check_interval_accepts_absent_and_valid_values():
    assert validate_local_check_interval({}) is None
    assert validate_local_check_interval({"local_check_interval_seconds": 30}) is None
    assert validate_local_check_interval({"local_check_interval_seconds": 60}) is None


def test_validate_local_check_interval_flags_value_above_thirty_six_hundred():
    error = validate_local_check_interval({"local_check_interval_seconds": 3601})
    assert error is not None
    assert "local_check_interval_seconds" in error


def test_validate_local_check_interval_accepts_thirty_six_hundred():
    assert validate_local_check_interval({"local_check_interval_seconds": 3600}) is None


def test_validate_telemetry_interval_accepts_absent_and_valid_values():
    assert validate_telemetry_interval({}) is None
    assert validate_telemetry_interval({"telemetry_interval_seconds": 10}) is None
    assert validate_telemetry_interval({"telemetry_interval_seconds": 300}) is None


def test_validate_telemetry_interval_flags_value_below_ten():
    error = validate_telemetry_interval({"telemetry_interval_seconds": 0})
    assert error is not None
    assert "telemetry_interval_seconds" in error


@pytest.mark.parametrize("value, accepted", [(9, False), (10, True), (300, True), (600, True), (601, False), (900, False)])
def test_telemetry_interval_bounds(value, accepted):
    error = validate_telemetry_interval({"telemetry_interval_seconds": value})
    assert (error is None) == accepted


def test_telemetry_interval_error_explains_the_reserve():
    error = validate_telemetry_interval({"telemetry_interval_seconds": 900})
    assert "600" in error and "15 min" in error


def test_validate_telemetry_interval_flags_value_above_the_maximum_by_name():
    error = validate_telemetry_interval({"telemetry_interval_seconds": 601})
    assert error is not None
    assert "telemetry_interval_seconds" in error


def test_validate_local_check_interval_flags_nan():
    # Task 3a (final-review-fixes-plan): value < 10/> 60 is False for NaN, so a hand-
    # edited options.json with NaN used to sail through validation unnoticed.
    error = validate_local_check_interval({"local_check_interval_seconds": float("nan")})
    assert error is not None
    assert "local_check_interval_seconds" in error


def test_validate_local_check_interval_flags_infinity():
    error = validate_local_check_interval({"local_check_interval_seconds": float("inf")})
    assert error is not None
    assert "local_check_interval_seconds" in error


def test_validate_telemetry_interval_flags_nan():
    error = validate_telemetry_interval({"telemetry_interval_seconds": float("nan")})
    assert error is not None
    assert "telemetry_interval_seconds" in error


def test_validate_telemetry_interval_flags_infinity():
    error = validate_telemetry_interval({"telemetry_interval_seconds": float("inf")})
    assert error is not None
    assert "telemetry_interval_seconds" in error


def test_validate_local_check_interval_flags_non_numeric_string_without_raising():
    # M1 (final whole-branch review, 2026-09-20): math.isnan()/math.isinf() raise
    # TypeError on a non-numeric value (e.g. a hand-edited
    # "local_check_interval_seconds": "30s" in options.json) instead of returning the
    # clean German validation error this function exists to produce.
    error = validate_local_check_interval({"local_check_interval_seconds": "not-a-number"})
    assert error is not None
    assert "local_check_interval_seconds" in error


def test_validate_telemetry_interval_flags_non_numeric_string_without_raising():
    error = validate_telemetry_interval({"telemetry_interval_seconds": "300s"})
    assert error is not None
    assert "telemetry_interval_seconds" in error


def test_default_local_check_interval_seconds_is_300():
    assert DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS == 300


def test_validate_returns_first_error_or_none():
    valid = {"entity_outdoor_temp": "sensor.outdoor"}

    assert validate(valid) is None
    error = validate({**valid, "local_check_interval_seconds": 0})
    assert error is not None and "local_check_interval_seconds" in error
    error = validate({**valid, "telemetry_interval_seconds": 5})
    assert error is not None and "telemetry_interval_seconds" in error


def test_intervals_fall_back_to_300_seconds():
    assert local_check_interval({}) == 300
    assert telemetry_interval({}) == 300
    assert local_check_interval({"local_check_interval_seconds": 60}) == 60
    assert telemetry_interval({"telemetry_interval_seconds": 30}) == 30


@pytest.mark.parametrize("raw,expected", [
    (None, []), ("notify.x", []), (["notify.mobile_app_a", "notify.b"], ["notify.mobile_app_a", "notify.b"]),
    (["notify.a", "", 3, "light.x"], ["notify.a"]),
])
def test_notify_services_is_tolerant(raw, expected):
    options = {} if raw is None else {"notify_services": raw}
    assert config.notify_services(options) == expected


def _resolvable(**overrides):
    return {**REQUIRED, **PROFILE_PARAMS, **BASE_URL, **overrides}


def test_required_options_no_longer_contain_entity_room_actual():
    assert "entity_room_actual" not in REQUIRED_OPTIONS


@pytest.mark.parametrize("room_sensors", [None, [], "sensor.rt"])
def test_missing_or_empty_room_sensors_is_an_outdated_configuration(room_sensors):
    options = _resolvable()
    if room_sensors is None:
        del options["room_sensors"]
    else:
        options["room_sensors"] = room_sensors

    with pytest.raises(ConfigError, match="Konfiguration veraltet – bitte SmartHeat-Einrichtung erneut durchführen"):
        resolve_effective_options(options)


def test_0_18_0_options_are_reported_as_outdated_not_as_unconfigured():
    old = {k: v for k, v in _resolvable().items() if k != "room_sensors"}
    old["entity_room_actual"] = "sensor.rt"

    assert is_configured(old) is True
    with pytest.raises(ConfigError, match="Konfiguration veraltet"):
        resolve_effective_options(old)


@pytest.mark.parametrize("bad", [["sensor.RT"], ["climate.wz"], ["climate.wz::temperature"], ["light.x"], [3]])
def test_room_sensors_must_be_sensor_or_climate_current_temperature(bad):
    with pytest.raises(ConfigError, match="room_sensors"):
        resolve_effective_options(_resolvable(room_sensors=bad))


def test_room_sensors_accept_sensors_and_climate_current_temperature():
    resolved = resolve_effective_options(_resolvable(room_sensors=["sensor.a", "climate.wz::current_temperature"]))

    assert resolved["room_sensors"] == ["sensor.a", "climate.wz::current_temperature"]


@pytest.mark.parametrize("outdoor,ok", [
    ("sensor.aussen", True), ("weather.forecast_home", True), ("climate.x", False), ("", False),
])
def test_outdoor_source_is_sensor_or_weather(outdoor, ok):
    options = _resolvable(entity_outdoor_temp=outdoor)
    if ok:
        assert resolve_effective_options(options)["entity_outdoor_temp"] == outdoor
    else:
        with pytest.raises(ConfigError, match="entity_outdoor_temp"):
            resolve_effective_options(options)


def test_list_options_default_to_empty_lists():
    resolved = resolve_effective_options(_resolvable())

    assert resolved["battery_entities"] == []
    assert resolved["notify_services"] == []


@pytest.mark.parametrize("key,value", [
    ("battery_entities", ["light.x"]), ("battery_entities", "sensor.x"),
    ("notify_services", ["mobile_app_x"]), ("notify_services", "notify.x"),
])
def test_invalid_list_options_are_config_errors(key, value):
    with pytest.raises(ConfigError, match=key):
        resolve_effective_options(_resolvable(**{key: value}))


def test_valid_list_options_are_passed_through():
    resolved = resolve_effective_options(_resolvable(
        battery_entities=["sensor.wz_battery", "binary_sensor.kz_battery_low"],
        notify_services=["notify.mobile_app_a"],
    ))

    assert resolved["battery_entities"] == ["sensor.wz_battery", "binary_sensor.kz_battery_low"]
    assert resolved["notify_services"] == ["notify.mobile_app_a"]


@pytest.mark.parametrize("raw,expected", [
    (None, []), ("batterie", []), (["batterie", "notbetrieb", 3], ["batterie"]),
    (["raumfuehler", "quellwechsel"], ["raumfuehler", "quellwechsel"]),
])
def test_notify_hints_off_is_tolerant(raw, expected):
    options = {} if raw is None else {"notify_hints_off": raw}
    assert config.notify_hints_off(options) == expected


def test_notify_hints_off_defaults_to_empty_and_is_passed_through():
    assert resolve_effective_options(_resolvable())["notify_hints_off"] == []
    resolved = resolve_effective_options(_resolvable(notify_hints_off=["batterie", "manueller_eingriff"]))
    assert resolved["notify_hints_off"] == ["batterie", "manueller_eingriff"]


@pytest.mark.parametrize("value", [["notbetrieb"], ["konfiguration"], ["zugang"], "batterie", [None]])
def test_critical_or_unknown_hint_categories_are_config_errors(value):
    with pytest.raises(ConfigError, match="notify_hints_off"):
        resolve_effective_options(_resolvable(notify_hints_off=value))


@pytest.mark.parametrize("value,expected", [(None, False), (False, False), (True, True), ("true", False)])
def test_is_signed_off_only_for_true(value, expected):
    options = {} if value is None else {"abgemeldet": value}
    assert config.is_signed_off(options) is expected


VALID = {
    "tenant_id": "t", "mqtt_username": "u", "mqtt_password": "p", "verteilsystem": "Heizkoerper",
    "daily_trigger_time": "12:00", "room_sensors": ["sensor.r"], "entity_room_target": "sensor.t",
    "entity_curve_current": "number.c", "entity_shift_current": "climate.zone", "entity_min_flow": "number.mf",
    "entity_outdoor_temp": "sensor.o", "entity_heat_limit": "number.hl",
    "accounts_api_base_url": "https://accounts.example.test",
}


def test_effective_options_carry_new_safety_values():
    effective = config.resolve_effective_options(VALID)
    safety = config.local_safety(effective)
    assert safety.ranges["room_setpoint"] == (15.0, 25.0)
    assert safety.ranges["min_flow"] == (20.0, 30.0)
    assert (safety.comfort_boost["curve"], safety.comfort_boost["room_setpoint"]) == (1.5, 25.0)
    assert effective["daily_trigger_time"] == "12:00"
    assert "day_avg_window_start" not in effective and "avg_window_hours" not in effective


def test_effective_options_carry_the_heat_limit_clamps():
    safety = config.local_safety(config.resolve_effective_options(VALID))
    assert safety.ranges["heat_limit"] == (5.0, 23.0)
    assert safety.comfort_boost["heat_limit"] == 23.0  # bis 0.29.0: Boost-Heizgrenze = heat_limit_max


def test_outdated_options_without_shift_role():
    old = {key: value for key, value in VALID.items() if key not in ("entity_shift_current", "entity_min_flow")}
    old["entity_offset_current"] = "number.min_flow"
    assert config.is_configured(old)
    with pytest.raises(config.ConfigError, match="Konfiguration veraltet.*entity_shift_current"):
        config.resolve_effective_options(old)


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
def test_unwritable_entity_domain_is_a_configuration_error(key, value):
    # Spec 5.6: Zonen-Entity muss schreibbar sein; HaPlantBinding.write kennt climate.set_temperature und
    # number.set_value.
    with pytest.raises(config.ConfigError, match=f"Option '{key}'.*bitte SmartHeat neu konfigurieren"):
        config.resolve_effective_options({**VALID, key: value})


@pytest.mark.parametrize("shift", ["climate.zone", "climate.zone::temperature", "number.shift"])
def test_writable_shift_domains_are_accepted(shift):
    effective = config.resolve_effective_options({**VALID, "entity_shift_current": shift})
    assert effective["entity_shift_current"] == shift


@pytest.mark.parametrize("room_target, shift", [
    ("climate.zone::temperature", "climate.zone"),
    ("climate.zone", "climate.zone::temperature"),
    ("climate.zone::temperature", "climate.zone::temperature"),
])
def test_zone_as_room_target_is_a_configuration_error(room_target, shift):
    # Final-Review I1: Das Add-on schriebe die Parallelverschiebung in die Quelle des Kundenwunsches
    # (Rueckkopplung bis shift_max).
    options = {**VALID, "entity_room_target": room_target, "entity_shift_current": shift}
    with pytest.raises(config.ConfigError, match="entity_shift_current.*entity_room_target.*neu konfigurieren"):
        config.resolve_effective_options(options)


def test_zone_current_temperature_as_room_sensor_is_accepted():
    options = {**VALID, "room_sensors": ["sensor.r", "climate.zone::current_temperature"]}
    assert config.resolve_effective_options(options)["room_sensors"] == ["sensor.r", "climate.zone::current_temperature"]


@pytest.mark.parametrize("value", [None, "", "25:00", "12"])
def test_daily_trigger_time_validated(value):
    with pytest.raises(config.ConfigError, match="daily_trigger_time"):
        config.resolve_effective_options({**VALID, "daily_trigger_time": value})


@pytest.mark.parametrize("value", [0, -5, 0.5, True])
def test_validate_local_check_interval_flags_values_below_one_and_bools(value):
    assert validate_local_check_interval({"local_check_interval_seconds": value}) is not None


def test_validate_telemetry_interval_flags_bools():
    assert validate_telemetry_interval({"telemetry_interval_seconds": True}) is not None

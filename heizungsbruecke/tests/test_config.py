import json

import pytest

from heizungsbruecke.config import (
    ConfigError,
    DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS,
    is_configured,
    load_options_safe,
    local_check_interval,
    resolve_effective_options,
    telemetry_interval,
    validate,
    validate_boost_config,
    validate_derived_sensor_prerequisites,
    validate_local_check_interval,
    validate_telemetry_interval,
)


def _base_options(**overrides):
    options = {
        "curve_min": 0.2,
        "curve_max": 0.8,
        "offset_min": 0.0,
        "offset_max": 5.0,
        "boost_curve_value": 0.5,
        "boost_offset_value": 2.0,
    }
    options.update(overrides)
    return options


PROFILE_PARAMS = {
    "verteilsystem": "Heizkoerper",
    "daily_trigger_time": "12:00",
    "day_avg_window_start": "14:00", "day_avg_window_end": "17:00",
    "night_avg_window_start": "04:00", "night_avg_window_end": "07:00",
}

REQUIRED = {
    "tenant_id": "wohnung1",
    "mqtt_username": "wohnung1_a1b2c3d4", "mqtt_password": "geheim",
    "entity_room_actual": "sensor.rt", "entity_room_target": "sensor.target_rt",
    "entity_curve_current": "number.curve", "entity_offset_current": "number.offset",
    "entity_outdoor_temp": "sensor.outdoor", "entity_heat_limit": "number.heat_limit",
}


def test_is_configured_true_when_all_required_fields_present():
    assert is_configured(dict(REQUIRED)) is True


def test_is_configured_false_when_a_required_field_is_missing():
    assert is_configured({"tenant_id": "wohnung1"}) is False


def test_is_configured_false_for_empty_options():
    assert is_configured({}) is False


def test_load_options_safe_defaults_when_no_file(tmp_path):
    assert load_options_safe(tmp_path / "does_not_exist.json") == {}


def test_load_options_safe_falls_back_on_corrupt_file(tmp_path):
    # Simulates power loss on the Pi's SD card mid-write: a truncated/corrupt options
    # file must not crash the whole add-on before it can even report its state.
    path = tmp_path / "options.json"
    path.write_bytes(b"{not valid json..")

    assert load_options_safe(path) == {}


def test_load_options_safe_passes_through_valid_file(tmp_path):
    path = tmp_path / "options.json"
    path.write_text(json.dumps({"tenant_id": "wohnung1"}))

    assert load_options_safe(path) == {"tenant_id": "wohnung1"}


def test_validate_boost_config_returns_none_when_within_range():
    assert validate_boost_config(_base_options()) is None


def test_validate_boost_config_flags_curve_value_above_max():
    error = validate_boost_config(_base_options(boost_curve_value=99.0))
    assert error is not None
    assert "boost_curve_value" in error


def test_validate_boost_config_flags_curve_value_below_min():
    error = validate_boost_config(_base_options(boost_curve_value=-1.0))
    assert error is not None
    assert "boost_curve_value" in error


def test_validate_boost_config_flags_offset_value_out_of_range():
    error = validate_boost_config(_base_options(boost_offset_value=999.0))
    assert error is not None
    assert "boost_offset_value" in error


def test_validate_boost_config_accepts_boundary_values():
    assert validate_boost_config(_base_options(boost_curve_value=0.2)) is None
    assert validate_boost_config(_base_options(boost_curve_value=0.8)) is None
    assert validate_boost_config(_base_options(boost_offset_value=0.0)) is None
    assert validate_boost_config(_base_options(boost_offset_value=5.0)) is None


def test_is_configured_true_for_0_17_0_options_without_new_values():
    # Alte Konfiguration: "profile" gesetzt, neue Werte fehlen. Muss als "eingerichtet"
    # gelten, damit der Start laut abbricht statt still zu warten (Spec TP3, 2.1).
    assert is_configured({**REQUIRED, "profile": "vaillant_gastherme_heizkoerper"}) is True


def test_required_options_do_not_contain_new_values():
    from heizungsbruecke.config import REQUIRED_OPTIONS
    assert "profile" not in REQUIRED_OPTIONS
    assert not set(PROFILE_PARAMS) & set(REQUIRED_OPTIONS)
    assert "accounts_api_base_url" not in REQUIRED_OPTIONS


def test_resolve_effective_options_uses_local_safety_and_option_windows():
    effective = resolve_effective_options({**REQUIRED, **PROFILE_PARAMS})

    assert effective["curve_min"] == 0.4
    assert effective["curve_max"] == 1.5
    assert effective["offset_min"] == 20.0
    assert effective["offset_max"] == 30.0
    assert effective["boost_threshold_k"] == 0.5
    assert effective["boost_curve_value"] == 1.5
    assert effective["boost_offset_value"] == 30.0
    assert effective["daily_trigger_time"] == "12:00"
    assert effective["day_avg_window_start"] == "14:00"
    assert effective["day_avg_window_end"] == "17:00"
    assert effective["night_avg_window_start"] == "04:00"
    assert effective["night_avg_window_end"] == "07:00"
    assert effective["avg_window_hours"] == 3.0
    assert effective["tenant_id"] == "wohnung1"


def test_resolve_effective_options_ignores_safety_values_in_options():
    effective = resolve_effective_options(
        {**REQUIRED, **PROFILE_PARAMS, "offset_max": 28.0, "boost_curve_value": 0.1}
    )

    assert effective["offset_max"] == 30.0  # lokale Sicherheitswerte gewinnen
    assert effective["boost_curve_value"] == 1.5


def test_resolve_effective_options_takes_windows_from_options():
    effective = resolve_effective_options({
        **REQUIRED, **PROFILE_PARAMS,
        "daily_trigger_time": "11:30",
        "day_avg_window_start": "13:00", "day_avg_window_end": "17:00",
        "night_avg_window_start": "03:00", "night_avg_window_end": "07:00",
    })

    assert effective["daily_trigger_time"] == "11:30"
    assert effective["avg_window_hours"] == 4.0


@pytest.mark.parametrize("verteilsystem", [None, "", "Fussbodenheizung", "Unbekannt"])
def test_resolve_effective_options_rejects_verteilsystem(verteilsystem):
    options = {**REQUIRED, **PROFILE_PARAMS, "verteilsystem": verteilsystem}
    if verteilsystem is None:
        del options["verteilsystem"]

    with pytest.raises(ConfigError, match="verteilsystem"):
        resolve_effective_options(options)


def test_resolve_effective_options_names_missing_window_option():
    options = {**REQUIRED, **PROFILE_PARAMS}
    del options["night_avg_window_end"]

    with pytest.raises(ConfigError, match="night_avg_window_end"):
        resolve_effective_options(options)


def test_resolve_effective_options_rejects_unequal_windows():
    with pytest.raises(ConfigError, match="gleich gross"):
        resolve_effective_options({**REQUIRED, **PROFILE_PARAMS, "day_avg_window_end": "18:00"})


def test_resolve_effective_options_0_17_0_config_names_verteilsystem():
    with pytest.raises(ConfigError, match="verteilsystem"):
        resolve_effective_options({**REQUIRED, "profile": "vaillant_gastherme_heizkoerper"})


def test_validate_derived_sensor_prerequisites_returns_none_when_present():
    options = {"entity_room_actual": "sensor.rt", "entity_outdoor_temp": "sensor.outdoor"}

    assert validate_derived_sensor_prerequisites(options) is None


def test_validate_derived_sensor_prerequisites_flags_missing_outdoor_temp():
    options = {"entity_room_actual": "sensor.rt"}

    error = validate_derived_sensor_prerequisites(options)

    assert error is not None
    assert "entity_outdoor_temp" in error


def test_validate_derived_sensor_prerequisites_flags_missing_room_actual():
    options = {"entity_outdoor_temp": "sensor.outdoor"}

    error = validate_derived_sensor_prerequisites(options)

    assert error is not None
    assert "entity_room_actual" in error


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
    valid = _base_options(entity_room_actual="sensor.rt", entity_outdoor_temp="sensor.outdoor")

    assert validate(valid) is None
    assert "boost_curve_value" in validate({**valid, "boost_curve_value": 99.0})
    assert "telemetry_interval_seconds" in validate({**valid, "telemetry_interval_seconds": 5})
    assert "entity_outdoor_temp" in validate({**valid, "entity_outdoor_temp": ""})


def test_intervals_fall_back_to_300_seconds():
    assert local_check_interval({}) == 300
    assert telemetry_interval({}) == 300
    assert local_check_interval({"local_check_interval_seconds": 60}) == 60
    assert telemetry_interval({"telemetry_interval_seconds": 30}) == 30

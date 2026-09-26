import pytest

from heizungsbruecke.manifest import build_manifest, ManifestError


def test_build_manifest_with_all_required_roles_succeeds():
    options = {
        "entity_room_actual": "climate.wohnzimmer_thermostat",
        "entity_room_target": "climate.wohnzimmer_thermostat",
        "entity_curve_current": "number.weishaupt_heizkurve_steigung",
        "entity_offset_current": "number.weishaupt_heizkurve_niveau",
        "entity_room_day_avg": "sensor.day_avg",
        "entity_room_night_avg": "sensor.night_avg",
        "entity_heat_limit": "sensor.heat_limit",
        "entity_dat": "sensor.dat",
        "entity_dart": "sensor.dart",
    }
    manifest = build_manifest(options)
    assert manifest.entity_ids["room_actual"] == "climate.wohnzimmer_thermostat"
    assert "outdoor_temp" not in manifest.entity_ids


def test_build_manifest_missing_required_role_raises():
    options = {
        "entity_room_actual": "climate.wohnzimmer_thermostat",
    }
    with pytest.raises(ManifestError):
        build_manifest(options)


def test_build_manifest_includes_optional_role_when_present():
    options = {
        "entity_room_actual": "climate.wohnzimmer_thermostat",
        "entity_room_target": "climate.wohnzimmer_thermostat",
        "entity_curve_current": "number.steigung",
        "entity_offset_current": "number.niveau",
        "entity_room_day_avg": "sensor.day_avg",
        "entity_room_night_avg": "sensor.night_avg",
        "entity_heat_limit": "sensor.heat_limit",
        "entity_dat": "sensor.dat",
        "entity_dart": "sensor.dart",
        "entity_outdoor_temp": "sensor.aussentemperatur",
    }
    manifest = build_manifest(options)
    assert manifest.entity_ids["outdoor_temp"] == "sensor.aussentemperatur"


def test_build_manifest_missing_profile_required_role_raises():
    options = {
        "entity_room_actual": "climate.wz",
        "entity_room_target": "climate.wz",
        "entity_curve_current": "number.curve",
        "entity_offset_current": "number.offset",
        # entity_dat fehlt -- ist Pflicht
        "entity_room_day_avg": "sensor.day_avg",
        "entity_room_night_avg": "sensor.night_avg",
        "entity_heat_limit": "sensor.heat_limit",
        "entity_dart": "sensor.dart",
    }

    with pytest.raises(ManifestError, match="dat"):
        build_manifest(options)


def test_build_manifest_succeeds_with_all_profile_roles():
    options = {
        "entity_room_actual": "climate.wz",
        "entity_room_target": "climate.wz",
        "entity_curve_current": "number.curve",
        "entity_offset_current": "number.offset",
        "entity_room_day_avg": "sensor.day_avg",
        "entity_room_night_avg": "sensor.night_avg",
        "entity_heat_limit": "sensor.heat_limit",
        "entity_dat": "sensor.dat",
        "entity_dart": "sensor.dart",
    }

    manifest = build_manifest(options)

    assert manifest.entity_ids["dat"] == "sensor.dat"


def test_build_manifest_prefers_derived_entity_ids_over_options():
    options = {
        "entity_room_actual": "climate.wz",
        "entity_room_target": "climate.wz",
        "entity_curve_current": "number.curve",
        "entity_offset_current": "number.offset",
        "entity_heat_limit": "sensor.heat_limit",
        "entity_dat": "sensor.dat_from_options_should_be_ignored",
    }
    derived_entity_ids = {
        "dat": "sensor.smartheat_client1_dat",
        "dart": "sensor.smartheat_client1_dart",
        "room_day_avg": "input_number.smartheat_client1_room_day_avg",
        "room_night_avg": "input_number.smartheat_client1_room_night_avg",
    }

    manifest = build_manifest(options, derived_entity_ids)

    assert manifest.entity_ids["dat"] == "sensor.smartheat_client1_dat"


def test_build_manifest_picks_up_optional_kpi_entity_when_configured():
    options = {
        "entity_room_actual": "climate.wz",
        "entity_room_target": "climate.wz",
        "entity_curve_current": "number.curve",
        "entity_offset_current": "number.offset",
        "entity_room_day_avg": "sensor.day_avg",
        "entity_room_night_avg": "sensor.night_avg",
        "entity_heat_limit": "sensor.heat_limit",
        "entity_dat": "sensor.dat",
        "entity_dart": "sensor.dart",
        "entity_flow_temperature": "sensor.flow",
    }

    manifest = build_manifest(options)

    assert manifest.entity_ids["flow_temperature"] == "sensor.flow"


def test_build_manifest_omits_unconfigured_kpi_entity():
    options = {
        "entity_room_actual": "climate.wz",
        "entity_room_target": "climate.wz",
        "entity_curve_current": "number.curve",
        "entity_offset_current": "number.offset",
        "entity_room_day_avg": "sensor.day_avg",
        "entity_room_night_avg": "sensor.night_avg",
        "entity_heat_limit": "sensor.heat_limit",
        "entity_dat": "sensor.dat",
        "entity_dart": "sensor.dart",
    }

    manifest = build_manifest(options)

    assert "flow_temperature" not in manifest.entity_ids


def test_build_manifest_treats_empty_string_kpi_entity_as_unconfigured():
    options = {
        "entity_room_actual": "climate.wz",
        "entity_room_target": "climate.wz",
        "entity_curve_current": "number.curve",
        "entity_offset_current": "number.offset",
        "entity_room_day_avg": "sensor.day_avg",
        "entity_room_night_avg": "sensor.night_avg",
        "entity_heat_limit": "sensor.heat_limit",
        "entity_dat": "sensor.dat",
        "entity_dart": "sensor.dart",
        "entity_flow_temperature": "",
    }

    manifest = build_manifest(options)

    assert "flow_temperature" not in manifest.entity_ids


def test_outdoor_min_is_taken_from_derived_entities():
    from heizungsbruecke.manifest import OPTIONAL_SNAPSHOT_ROLES, build_manifest
    assert OPTIONAL_SNAPSHOT_ROLES == ("outdoor_min_24h", "room_target_avg_24h")
    options = {
        "entity_room_actual": "sensor.rt", "entity_room_target": "climate.x::temperature",
        "entity_curve_current": "number.c", "entity_offset_current": "number.o", "entity_heat_limit": "number.h",
    }
    derived = {"dat": "sensor.dat", "dart": "sensor.dart", "room_day_avg": "input_number.d",
               "room_night_avg": "input_number.n", "outdoor_min_24h": "sensor.omin"}
    assert build_manifest(options, derived).entity_ids["outdoor_min_24h"] == "sensor.omin"


def test_required_roles_are_known_manifest_roles():
    from heizungsbruecke.manifest import ALL_ROLES, REQUIRED_ROLES

    assert REQUIRED_ROLES == (
        "room_actual", "room_target", "curve_current", "offset_current",
        "room_day_avg", "room_night_avg", "heat_limit", "dat", "dart",
    )
    assert set(REQUIRED_ROLES) <= set(ALL_ROLES)


def test_build_manifest_ignores_profile_option():
    options = {
        "entity_room_actual": "sensor.rt", "entity_room_target": "sensor.target",
        "entity_curve_current": "number.curve", "entity_offset_current": "number.offset",
        "entity_room_day_avg": "sensor.day", "entity_room_night_avg": "sensor.night",
        "entity_heat_limit": "number.limit", "entity_dat": "sensor.dat", "entity_dart": "sensor.dart",
    }

    assert build_manifest(options) == build_manifest({**options, "profile": "does_not_exist"})

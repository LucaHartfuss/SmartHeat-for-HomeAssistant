import pytest

from heizungsbruecke.manifest import build_manifest, ManifestError


def test_build_manifest_with_all_required_roles_succeeds():
    options = {
        "profile": "weishaupt_waermepumpe_fussbodenheizung",
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
        "profile": "weishaupt_waermepumpe_fussbodenheizung",
        "entity_room_actual": "climate.wohnzimmer_thermostat",
    }
    with pytest.raises(ManifestError):
        build_manifest(options)


def test_build_manifest_includes_optional_role_when_present():
    options = {
        "profile": "weishaupt_waermepumpe_fussbodenheizung",
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


def test_build_manifest_missing_profile_raises():
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

    with pytest.raises(ManifestError):
        build_manifest(options)


def test_build_manifest_unknown_profile_raises_manifest_error():
    options = {
        "profile": "does_not_exist",
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

    with pytest.raises(ManifestError):
        build_manifest(options)


def test_build_manifest_missing_profile_required_role_raises():
    options = {
        "profile": "weishaupt_waermepumpe_fussbodenheizung",
        "entity_room_actual": "climate.wz",
        "entity_room_target": "climate.wz",
        "entity_curve_current": "number.curve",
        "entity_offset_current": "number.offset",
        # entity_dat fehlt -- ist Pflicht fuer dieses Profil
        "entity_room_day_avg": "sensor.day_avg",
        "entity_room_night_avg": "sensor.night_avg",
        "entity_heat_limit": "sensor.heat_limit",
        "entity_dart": "sensor.dart",
    }

    with pytest.raises(ManifestError, match="dat"):
        build_manifest(options)


def test_build_manifest_succeeds_with_all_profile_roles():
    options = {
        "profile": "vaillant_gastherme_heizkoerper",
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
        "profile": "vaillant_gastherme_heizkoerper",
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

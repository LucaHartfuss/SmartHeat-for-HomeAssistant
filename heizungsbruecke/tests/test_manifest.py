import pytest

from heizungsbruecke.manifest import build_manifest, ManifestError


def test_build_manifest_with_all_required_roles_succeeds():
    options = {
        "entity_room_actual": "climate.wohnzimmer_thermostat",
        "entity_room_target": "climate.wohnzimmer_thermostat",
        "entity_curve_current": "number.weishaupt_heizkurve_steigung",
        "entity_offset_current": "number.weishaupt_heizkurve_niveau",
    }
    manifest = build_manifest(options)
    assert manifest.entity_ids["room_actual"] == "climate.wohnzimmer_thermostat"
    assert "outdoor_temp" not in manifest.entity_ids


def test_build_manifest_missing_required_role_raises():
    options = {"entity_room_actual": "climate.wohnzimmer_thermostat"}
    with pytest.raises(ManifestError):
        build_manifest(options)


def test_build_manifest_includes_optional_role_when_present():
    options = {
        "entity_room_actual": "climate.wohnzimmer_thermostat",
        "entity_room_target": "climate.wohnzimmer_thermostat",
        "entity_curve_current": "number.steigung",
        "entity_offset_current": "number.niveau",
        "entity_outdoor_temp": "sensor.aussentemperatur",
    }
    manifest = build_manifest(options)
    assert manifest.entity_ids["outdoor_temp"] == "sensor.aussentemperatur"

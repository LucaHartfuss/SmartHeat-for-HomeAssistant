import pytest

from heizungsbruecke.manifest import build_manifest, entity_ref
from smartheat_runtime.roles import ManifestError

# room_actual kommt immer aus derived_sensors (Raumtemperatur-Template, Spec TP6 3.2).
ROOM_ACTUAL = {"room_actual": "sensor.smartheat_t1_raumtemperatur"}

BASE = {
    "entity_room_target": "sensor.t", "entity_curve_current": "number.c", "entity_shift_current": "climate.zone",
    "entity_min_flow": "number.mf", "entity_heat_limit": "number.hl", "entity_outdoor_temp": "sensor.o",
}
DERIVED = {"room_actual": "sensor.room"}


def test_build_manifest_with_all_required_roles_succeeds():
    options = {
        "entity_room_target": "climate.wohnzimmer_thermostat",
        "entity_curve_current": "number.weishaupt_heizkurve_steigung",
        "entity_shift_current": "number.weishaupt_heizkurve_niveau",
        "entity_min_flow": "number.mindestvorlauf",
        "entity_heat_limit": "sensor.heat_limit",
        "entity_outdoor_temp": "sensor.aussentemperatur",
    }
    manifest = build_manifest(options, ROOM_ACTUAL)
    assert manifest.entity_ids["room_actual"] == "sensor.smartheat_t1_raumtemperatur"
    assert "flow_setpoint" not in manifest.entity_ids


def test_build_manifest_missing_required_role_raises():
    with pytest.raises(ManifestError):
        build_manifest({}, ROOM_ACTUAL)


def test_build_manifest_includes_optional_role_when_present():
    options = {**BASE, "entity_flow_setpoint": "sensor.vl_soll"}
    manifest = build_manifest(options, ROOM_ACTUAL)
    assert manifest.entity_ids["flow_setpoint"] == "sensor.vl_soll"


def test_build_manifest_missing_profile_required_role_raises():
    # entity_min_flow fehlt -- ist Pflicht (TP11).
    options = {key: value for key, value in BASE.items() if key != "entity_min_flow"}

    with pytest.raises(ManifestError, match="min_flow"):
        build_manifest(options, ROOM_ACTUAL)


def test_build_manifest_succeeds_with_all_profile_roles():
    manifest = build_manifest(BASE, ROOM_ACTUAL)

    assert manifest.entity_ids["min_flow"] == "number.mf"


def test_build_manifest_prefers_derived_entity_ids_over_options():
    options = {**BASE, "entity_room_target": "sensor.target_from_options_should_be_ignored"}
    derived_entity_ids = {**ROOM_ACTUAL, "room_target": "sensor.smartheat_client1_room_target"}

    manifest = build_manifest(options, derived_entity_ids)

    assert manifest.entity_ids["room_target"] == "sensor.smartheat_client1_room_target"


def test_build_manifest_picks_up_optional_kpi_entity_when_configured():
    options = {**BASE, "entity_flow_temperature": "sensor.flow"}

    manifest = build_manifest(options, ROOM_ACTUAL)

    assert manifest.entity_ids["flow_temperature"] == "sensor.flow"


def test_build_manifest_omits_unconfigured_kpi_entity():
    manifest = build_manifest(BASE, ROOM_ACTUAL)

    assert "flow_temperature" not in manifest.entity_ids


def test_build_manifest_treats_empty_string_kpi_entity_as_unconfigured():
    options = {**BASE, "entity_flow_temperature": ""}

    manifest = build_manifest(options, ROOM_ACTUAL)

    assert "flow_temperature" not in manifest.entity_ids


def test_climate_shift_reads_target_temperature_attribute():
    assert build_manifest(BASE, DERIVED).entity_ids["shift_current"] == "climate.zone::temperature"


def test_number_shift_is_kept():
    manifest = build_manifest({**BASE, "entity_shift_current": "number.shift"}, DERIVED)
    assert manifest.entity_ids["shift_current"] == "number.shift"


@pytest.mark.parametrize("role, value, expected", [
    ("shift_current", "climate.zone", "climate.zone::temperature"),
    ("shift_current", "climate.zone::temperature", "climate.zone::temperature"),
    ("shift_current", "number.shift", "number.shift"),
    ("room_target", "climate.wz", "climate.wz"),
])
def test_entity_ref(role, value, expected):
    assert entity_ref(role, value) == expected


def test_every_snapshot_lever_is_mapped_by_the_required_roles():
    # Ersetzt test_snapshot_roles (Schema 4: der Snapshot meldet room_target und die Hebel des Hebelsatzes statt
    # SNAPSHOT_ROLES). Die Pflichtrollen decken jeden Hebel ab; der Payload selbst: test_runtime.py
    # test_snapshot_payload_is_schema_4.
    from heizungsbruecke.ha_binding import HaPlantBinding

    binding = HaPlantBinding(None, build_manifest(BASE, DERIVED))
    assert [lever for lever in binding.description.lever_set.levers if binding.has(lever)] == [
        "curve", "room_setpoint", "heat_limit",
    ]


def test_flow_setpoint_is_optional_role():
    manifest = build_manifest({**BASE, "entity_flow_setpoint": "sensor.vl_soll"}, DERIVED)
    assert manifest.entity_ids["flow_setpoint"] == "sensor.vl_soll"


def test_required_roles_are_known_manifest_roles():
    from smartheat_runtime.roles import ALL_ROLES, REQUIRED_ROLES

    assert REQUIRED_ROLES == (
        "room_actual", "room_target", "curve_current", "shift_current", "min_flow", "heat_limit", "outdoor_temp",
    )
    assert set(REQUIRED_ROLES) <= set(ALL_ROLES)


def test_build_manifest_ignores_profile_option():
    assert build_manifest(BASE, ROOM_ACTUAL) == build_manifest({**BASE, "profile": "does_not_exist"}, ROOM_ACTUAL)


def test_required_roles_per_lever_set():
    from smartheat_core.levers import LEVER_SETS
    from smartheat_runtime.roles import ALL_ROLES, REQUIRED_ROLES, REQUIRED_ROLES_BY_LEVER_SET

    assert set(REQUIRED_ROLES_BY_LEVER_SET) == set(LEVER_SETS)
    assert REQUIRED_ROLES_BY_LEVER_SET["vaillant_vrc720"] == REQUIRED_ROLES
    assert all(set(roles) <= set(ALL_ROLES) for roles in REQUIRED_ROLES_BY_LEVER_SET.values())
    assert {"level_current", "mode_select", "setpoint_comfort", "setpoint_setback"} <= set(ALL_ROLES)


def test_build_manifest_checks_the_roles_of_the_lever_set():
    options = {
        "entity_room_target": "sensor.t", "entity_outdoor_temp": "sensor.o", "entity_shift_current": "number.normal",
        "entity_mode_select": "select.betriebsart", "entity_setpoint_comfort": "number.komfort",
    }
    with pytest.raises(ManifestError, match="setpoint_setback"):
        build_manifest(options, {"room_actual": "sensor.r"}, "weishaupt_wwp_basis")
    manifest = build_manifest({**options, "entity_setpoint_setback": "number.absenk"}, {"room_actual": "sensor.r"},
                              "weishaupt_wwp_basis")
    assert manifest.entity_ids["mode_select"] == "select.betriebsart"

from pathlib import Path

import yaml

from heizungsbruecke.config import REQUIRED_OPTIONS as _REQUIRED_OPTIONS

CONFIG_YAML_PATH = Path(__file__).resolve().parents[1] / "config.yaml"


def _load_config_yaml() -> dict:
    return yaml.safe_load(CONFIG_YAML_PATH.read_text())


def _is_optional(type_spec) -> bool:
    """Supervisor: bei Listen entscheidet das Listenelement (`- str?`)."""
    if isinstance(type_spec, list):
        return bool(type_spec) and str(type_spec[0]).endswith("?")
    return str(type_spec).endswith("?")


def test_schema_declares_every_option_the_integration_writes():
    """The smartheat HA integration configures this add-on as an external caller
    via Supervisor's AddonManager (POST /addons/{slug}/options), not through this
    add-on's own /addons/self/options. Supervisor validates that write against
    config.yaml's schema and silently drops any key missing from it -- `schema:
    false` looks harmless (no error surfaces anywhere) but means every field the
    integration sends is discarded, so the add-on stays permanently unconfigured.
    Regression test for exactly that: verified end-to-end against a real
    Supervisor on 2026-09-15 (client1 rollout test), see
    docs/superpowers/plans/2026-09-14-smartheat-config-integration.md.
    """
    config = _load_config_yaml()
    schema = config.get("schema")

    assert isinstance(schema, dict), (
        "config.yaml's schema must be a field dict, not `false` -- Supervisor "
        "rejects every option an external caller (the smartheat integration) "
        "sends when there is no schema to validate against."
    )
    missing = [field for field in _REQUIRED_OPTIONS if field not in schema]
    assert not missing, f"schema is missing required option(s): {missing}"


def test_schema_fields_the_integration_never_sends_are_optional():
    """The integration's config_flow.py only ever submits the fields in
    _REQUIRED_OPTIONS (tenant_id, profile, entity_*) -- Supervisor validates a
    set-options call against the *whole* schema, so any additional field left
    non-optional (no trailing `?`) makes every push fail the same way a missing
    field does: silently, straight into AddonError, with no log line to explain
    why (this bit us right after fixing the sibling test above, in the same
    debugging session -- verified end-to-end against a real Supervisor on
    2026-09-15, client1 rollout test).
    """
    config = _load_config_yaml()
    schema = config["schema"]

    non_optional_extra = [
        field
        for field, type_spec in schema.items()
        if field not in _REQUIRED_OPTIONS and not _is_optional(type_spec)
    ]
    assert not non_optional_extra, (
        f"schema field(s) the integration never sends must be optional (`?`): "
        f"{non_optional_extra}"
    )


def test_config_yaml_has_new_optional_kpi_entity_options():
    config = _load_config_yaml()

    for key in (
        "entity_flow_temperature", "entity_return_temperature", "entity_operating_mode",
        "entity_system_water_pressure", "entity_efficiency_ratio",
        "entity_energy_electrical_heating", "entity_energy_electrical_dhw",
        "entity_energy_primary_heating", "entity_energy_primary_dhw",
        "entity_energy_thermal_heating", "entity_energy_thermal_dhw",
    ):
        assert config["options"][key] == ""
        assert config["schema"][key] == "str?"


def test_every_optional_kpi_role_has_matching_config_option_and_schema():
    """Drift guard: the KPI roles registered in manifest.ALL_ROLES must each have an
    `entity_<role>` option (default "") and an optional `str?` schema entry."""
    from heizungsbruecke.manifest import ALL_ROLES

    kpi_roles = ALL_ROLES[ALL_ROLES.index("flow_temperature"):]
    assert len(kpi_roles) == 11
    config = _load_config_yaml()
    for role in kpi_roles:
        key = f"entity_{role}"
        assert config["options"].get(key) == "", f"options[{key}] must default to empty string"
        assert config["schema"].get(key) == "str?", f"schema[{key}] must be `str?`"


NEW_OPTIONAL_SCHEMA = {
    "verteilsystem": "list(Heizkoerper|Fussbodenheizung)?",
    "daily_trigger_time": "str?",
    "accounts_api_base_url": "url?",
}


def test_profile_option_is_gone():
    config = _load_config_yaml()
    assert "profile" not in config["options"]
    assert "profile" not in config["schema"]


def test_profile_params_options_are_optional_schema_entries_without_defaults():
    # Leere Defaults waeren fuer list(...) ungueltig; die Werte kommen von der Integration.
    config = _load_config_yaml()
    for key, spec in NEW_OPTIONAL_SCHEMA.items():
        assert config["schema"].get(key) == spec, key
        assert key not in config["options"], key


def test_notify_services_is_an_optional_string_list_and_notify_service_is_gone():
    schema = _load_config_yaml()["schema"]

    assert schema["notify_services"] == ["str?"]
    assert "notify_service" not in schema
    assert "notify_service" not in _load_config_yaml()["options"]


def test_tp6_list_options_and_setup_id_are_optional_and_room_actual_is_gone():
    config = _load_config_yaml()
    schema = config["schema"]

    assert schema["room_sensors"] == ["str?"]
    assert schema["battery_entities"] == ["str?"]
    assert schema["setup_id"] == "str?"
    assert "entity_room_actual" not in schema
    assert "entity_room_actual" not in config["options"]
    for key in ("room_sensors", "notify_services", "battery_entities", "setup_id"):
        assert key not in config["options"]


def test_addon_version_constant_matches_config_yaml():
    from heizungsbruecke.status import ADDON_VERSION

    assert _load_config_yaml()["version"] == ADDON_VERSION


def test_tp7_options_are_optional_and_have_no_default():
    config = _load_config_yaml()
    schema = config["schema"]

    assert schema["abgemeldet"] == "bool?"
    assert schema["notify_hints_off"] == ["list(raumfuehler|batterie|manueller_eingriff|quellwechsel)?"]
    assert "abgemeldet" not in config["options"] and "notify_hints_off" not in config["options"]
    assert "abgemeldet" not in _REQUIRED_OPTIONS and "notify_hints_off" not in _REQUIRED_OPTIONS


def test_new_entity_options_are_optional_and_offset_is_gone():
    config = _load_config_yaml()
    schema, options = config["schema"], config["options"]
    for key in ("entity_shift_current", "entity_min_flow", "entity_flow_setpoint"):
        assert schema[key] == "str?" and options[key] == ""
    for gone in ("entity_offset_current", "day_avg_window_start", "day_avg_window_end",
                 "night_avg_window_start", "night_avg_window_end"):
        assert gone not in schema and gone not in options
    assert schema["daily_trigger_time"] == "str?"

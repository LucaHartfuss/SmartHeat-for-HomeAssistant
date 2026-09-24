from pathlib import Path

import yaml

from heizungsbruecke.__main__ import _REQUIRED_OPTIONS

CONFIG_YAML_PATH = Path(__file__).resolve().parents[1] / "config.yaml"


def _load_config_yaml() -> dict:
    return yaml.safe_load(CONFIG_YAML_PATH.read_text())


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
        if field not in _REQUIRED_OPTIONS and not str(type_spec).endswith("?")
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

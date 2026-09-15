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

"""Gemeinsame Optionspruefung (Plan G2a, Praezisierung 1): dieselben Objekte im Add-on und im Gateway."""
import pytest

from heizungsbruecke import config
from smartheat_runtime import options


def test_addon_config_reexports_the_shared_functions():
    for name in (
        "ConfigError", "resolve_transport", "local_safety", "lever_set_id", "binding_description",
        "resolve_accounts_api_base_url", "validate", "validate_telemetry_interval", "local_check_interval",
        "telemetry_interval", "notify_hints_off", "is_signed_off", "TOKEN_OPTION", "SECRET_OPTIONS",
        "MAX_TELEMETRY_INTERVAL_SECONDS", "DEFAULT_LEVER_SET",
    ):
        assert getattr(config, name) is getattr(options, name), name


def test_shared_checks_raise_the_shared_error():
    with pytest.raises(options.ConfigError):
        options.resolve_accounts_api_base_url("http://unsicher.example.test")
    with pytest.raises(config.ConfigError):
        options.lever_set_id({"lever_set": "unbekannt"})
    assert options.hints_off_strict({"notify_hints_off": ["batterie"]}) == ["batterie"]

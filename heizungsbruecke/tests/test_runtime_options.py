"""Gemeinsame Optionspruefung (Plan G2a, Praezisierung 1): dieselben Objekte im Add-on und im Gateway."""
import pytest

from heizungsbruecke import config
from smartheat_runtime import options


def test_addon_config_reexports_the_shared_functions():
    # Seit 1.0.0 (Spec 5c) nur noch, was das Add-on auf die Optionen des Konfigurationsdokuments anwendet; Transport,
    # Zugang, Accounts-API und die Abmelde-Option entfielen mit dem Optionsmodus (Phase 1 S1b: auch ihre Pruefungen).
    for name in ("ConfigError", "local_safety", "lever_set_id", "binding_description", "validate", "DEFAULT_LEVER_SET"):
        assert getattr(config, name) is getattr(options, name), name


def test_shared_checks_raise_the_shared_error():
    with pytest.raises(config.ConfigError):
        options.lever_set_id({"lever_set": "unbekannt"})
    assert options.hints_off_strict({"notify_hints_off": ["batterie"]}) == ["batterie"]

"""config.yaml ab 1.0.0 (Spec 5c 1.1/1.2): nur Optionen zum Starten, Supervisor-Discovery, kein Optionsmodus mehr. Die
Pruefungen der frueheren Rollen-, Zugangs- und Intervall-Optionen entfallen mit dem Optionsmodus."""
from pathlib import Path

import pytest
import yaml

from heizungsbruecke.version import ADDON_VERSION

CONFIG_YAML_PATH = Path(__file__).resolve().parents[1] / "config.yaml"


def _load_config_yaml() -> dict:
    return yaml.safe_load(CONFIG_YAML_PATH.read_text())


def test_addon_version_constant_matches_config_yaml():
    assert _load_config_yaml()["version"] == ADDON_VERSION


def test_slug_and_architectures():
    config = _load_config_yaml()
    assert config["slug"] == "heizungsbruecke"
    assert config["arch"] == ["aarch64", "amd64"]


def test_config_yaml_has_only_start_options():
    data = _load_config_yaml()
    assert "options" not in data  # Standard {} (Add-on-Linter: Schluessel mit Standardwert entfernen)
    assert data["schema"] == {"bootstrap_url": "match(^https://.+)?", "log_level": "list(debug|info|warning|error)?"}
    with pytest.raises(KeyError):
        data["schema"]["tenant_id"]


def test_discovery_and_supervisor_api_without_host_network():
    """Discovery (POST /discovery, Dienst smartheat) und Uhr (GET /host/info) brauchen die Supervisor-API; ausgehend
    MQTT zu IoT Core und die Supervisor-API brauchen das Host-Netz nicht (Nutzer-Entscheidung 2026-10-11)."""
    data = _load_config_yaml()
    assert data["discovery"] == ["smartheat"]
    assert data["hassio_api"] is True and data["homeassistant_api"] is True
    assert "host_network" not in data
    assert data["startup"] == "services"


def test_log_levels_match_the_entry_point():
    from heizungsbruecke.__main__ import LOG_LEVELS
    values = _load_config_yaml()["schema"]["log_level"].removeprefix("list(").removesuffix(")?").split("|")
    assert set(values) == set(LOG_LEVELS)

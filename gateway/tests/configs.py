"""Gueltige apply_config-Konfiguration fuer Tests (Testwerte, keine Zugangsdaten)."""
from smartheat_gateway.config import split_secrets
from smartheat_gateway.files import write_json

SENSOR = "0x00124b0000000001"
THERMOSTAT = "0x00124b0000000002"


def apply_config(**overrides) -> dict:
    config = {
        "config_version": 1, "tenant_id": "test-tenant", "setup_id": "setup-1", "lever_set": "viessmann_vicare",
        "verteilsystem": "Heizkoerper",
        "transport": {"kind": "mosquitto_cloudflared", "host": "127.0.0.1", "port": 18830},
        "mqtt_username": "test-user", "mqtt_password": "test-password", "installation_token": "test-token",
        "accounts_api_base_url": "https://accounts.example.test", "daily_trigger_time": "12:00",
        "notify_hints_off": [], "driver": {"id": "simulation", "parameter": {"lever_set": "viessmann_vicare"}},
        "room_sensors": [f"zigbee:{SENSOR}:temperature"], "thermostat": THERMOSTAT, "room_target_start": 20.0,
        "cloudflared": {
            "hostname": "mqtt.example.test", "service_token_id": "test-id", "service_token_secret": "test-secret",
        },
        "abgemeldet": False,
    }
    config.update(overrides)
    return config


def write_runtime_files(paths, config: dict) -> None:
    public, secrets = split_secrets(config)
    write_json(paths.runtime_secrets, secrets, private=True)
    write_json(paths.runtime_config, public)

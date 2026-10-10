"""Gueltige apply_config-Konfiguration fuer Tests (Testwerte, keine Zugangsdaten)."""
from smartheat_device import documents as device_documents
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


def konfiguration(**changes) -> dict:
    """Konfigurationsdokument (Inhalt ohne schema/version) fuer den Simulations-Treiber; Testwerte."""
    value = {
        "anlage": "test-tenant", "profil": "viessmann_gastherme_heizkoerper", "hebelsatz": "viessmann_vicare",
        "verteilsystem": "Heizkoerper",
        "bindung": {"treiber": {"id": "simulation", "parameter": {"lever_set": "viessmann_vicare", "heizkreis": 0}},
                    "thermostat": THERMOSTAT},
        "raumfuehler": [f"zigbee:{SENSOR}:temperature"], "tagestick": "12:00", "abo": "aktiv",
        "mindest_software": "0.6.0", "software": None,
    }
    value.update(changes)
    return value


def leer() -> dict:
    """Leere Konfiguration (Spec 5b 3.1, wie device_config.leer() des Servers): kein Geraet eingerichtet."""
    return {**konfiguration(), "anlage": None, "profil": None, "hebelsatz": None, "verteilsystem": None,
            "bindung": None, "tagestick": None, "abo": None, "raumfuehler": []}


def bedienung(raum_soll: float = 20.0) -> dict:
    return {"modus": "manuell", "raum_soll": raum_soll, "quelle": "portal", "ts": "2026-01-15T07:00:00+00:00"}


def dokumente_schreiben(paths, konfiguration=None, bedienung=None, *, version: int = 1) -> None:
    """Dokumente direkt im Format des Dokumentspeichers ablegen (wie nach einer Uebernahme, D1 Task 3)."""
    for name, inhalt in (("konfiguration.json", konfiguration), ("bedienung.json", bedienung)):
        if inhalt is not None:
            device_documents.write_json(paths.device_dir / name, {"version": version, "inhalt": inhalt,
                                                                  "dirty": False})

import pytest
from configs import THERMOSTAT, apply_config, write_runtime_files

from smartheat_core.binding import BINDINGS
from smartheat_gateway import config
from smartheat_gateway.paths import Paths
from smartheat_runtime import options
from smartheat_runtime.options import ConfigError
from smartheat_runtime.runtime_config import RuntimeConfig


@pytest.fixture
def paths(data_dir):
    return Paths(data_dir)


def test_missing_or_broken_file_means_not_configured(paths):
    assert config.load_raw(paths) == {}
    paths.runtime_config.write_text("{kaputt")
    raw = config.load_raw(paths)
    assert not config.is_configured(raw)
    boot = config.boot_info(raw, paths)
    assert (boot.configured, boot.signed_off) == (False, False)


def test_unreadable_file_means_not_configured(paths):
    paths.runtime_config.mkdir()  # ein Ordner statt der Datei: OSError beim Lesen
    raw = config.load_raw(paths)
    assert raw == {} and not config.is_configured(raw)


def test_round_trip_through_the_files(paths):
    write_runtime_files(paths, apply_config())
    raw = config.load_raw(paths)
    assert raw["mqtt_password"] == "test-password"
    assert raw["cloudflared"]["service_token_secret"] == "test-secret"
    runtime, gateway = config.parse(raw, paths)
    assert isinstance(runtime, RuntimeConfig)
    assert runtime.tenant_id == "test-tenant" and runtime.room_sensor_refs == ("zigbee:0x00124b0000000001:temperature",)
    assert runtime.entitlement_path == paths.entitlement
    assert gateway.driver_id == "simulation" and gateway.thermostat == THERMOSTAT
    public, secrets = paths.runtime_config.read_text(), paths.runtime_secrets.read_text()
    for secret in ("test-password", "test-token", "test-secret"):  # Passwort, Installations-Token, Tunnel-Secret
        assert secret not in public and secret in secrets
    assert raw["installation_token"] == "test-token"


def test_unknown_lever_set_falls_back_in_boot_info_but_parse_rejects_it(paths):
    write_runtime_files(paths, apply_config(lever_set="gibtsnicht"))
    raw = config.load_raw(paths)
    assert config.boot_info(raw, paths).lever_set == BINDINGS[options.DEFAULT_LEVER_SET].lever_set
    with pytest.raises(ConfigError):
        config.parse(raw, paths)


def test_poll_interval_becomes_the_driver_parameter(paths):
    write_runtime_files(paths, apply_config(poll_interval_seconds=120))
    _, gateway = config.parse(config.load_raw(paths), paths)
    assert gateway.driver_spec()["parameter"]["poll_seconds"] == 120


def test_split_drops_missing_secrets():
    public, secrets = config.split_secrets(apply_config(setup_id="setup-2", mqtt_password=None))
    assert "mqtt_password" not in secrets and "mqtt_password" not in public
    assert public["setup_id"] == "setup-2"


@pytest.mark.parametrize(("overrides", "fragment"), [
    ({"driver": {"id": "gibtsnicht", "parameter": {}}}, "Treiber"),
    ({"room_sensors": []}, "room_sensors"),
    ({"room_sensors": ["sensor.wohnzimmer"]}, "room_sensors"),
    ({"thermostat": "kein-ieee"}, "thermostat"),
    ({"room_target_start": 26.0}, "room_target_start"),
    ({"room_target_start": 20.3}, "room_target_start"),
    ({"verteilsystem": "Unbekannt"}, "verteilsystem"),
    ({"accounts_api_base_url": "http://x.example.test"}, "accounts_api_base_url"),
    ({"telemetry_interval_seconds": 900}, "telemetry_interval_seconds"),
])
def test_invalid_values_raise_the_shared_config_error(paths, overrides, fragment):
    write_runtime_files(paths, apply_config(**overrides))
    with pytest.raises(ConfigError, match=fragment):
        config.parse(config.load_raw(paths), paths)


def test_schema_check_of_apply_config():
    assert config.check_apply_config(apply_config()) is None
    assert "unbekannt" in config.check_apply_config({**apply_config(), "extra": 1})
    missing = apply_config()
    del missing["driver"]
    assert "driver" in config.check_apply_config(missing)
    assert "config_version" in config.check_apply_config(apply_config(config_version=2))


def test_daily_trigger_time_is_required():
    missing = apply_config()
    del missing["daily_trigger_time"]
    assert "daily_trigger_time" in config.check_apply_config(missing)


def test_signed_off_boot_needs_only_the_tenant(paths):
    write_runtime_files(paths, {"config_version": 1, "tenant_id": "test-tenant", "setup_id": "s-2", "abgemeldet": True,
                                "lever_set": "viessmann_vicare", "verteilsystem": "Heizkoerper",
                                "driver": {"id": "simulation", "parameter": {}}})
    boot = config.boot_info(config.load_raw(paths), paths)
    assert boot.signed_off and not boot.configured and boot.setup_id == "s-2"


def test_secret_values_lists_every_secret(paths):
    write_runtime_files(paths, apply_config())
    values = config.secret_values(config.load_raw(paths))
    assert {"test-password", "test-token", "test-secret"} <= set(values)


def test_transport_key_is_only_read_for_iot_core(paths):
    paths.transport_key.parent.mkdir(parents=True, exist_ok=True)
    paths.transport_key.write_text("test-key")  # liegt nach create_csr und abgebrochenem Einrichten noch da
    write_runtime_files(paths, apply_config())
    assert "tls_private_key" not in config.load_raw(paths)
    iot = {"kind": "iot_core", "host": "x.example.test", "port": 8883, "alpn": None, "ca_pem": "x", "client_id": "t"}
    write_runtime_files(paths, apply_config(transport=iot, mqtt_username=None, mqtt_password=None))
    assert config.load_raw(paths)["tls_private_key"] == "test-key"


def test_redact_replaces_secrets_in_plain_and_repr_form(paths):
    write_runtime_files(paths, apply_config(mqtt_password="test-pa'ss\\wort"))
    raw = config.load_raw(paths)
    text = f"Fehler mit {raw['mqtt_password']} und {raw['mqtt_password']!r} und {raw['installation_token']}"
    redacted = config.redact(text, raw)
    assert "test-pa" not in redacted and "test-token" not in redacted
    assert redacted.count(config.REDACTED) == 3


def test_missing_accounts_url_names_the_portal_not_the_integration(paths):
    # Der Schluessel ist Pflicht im Schema (sonst meldet schon check_apply_config); ein leerer Wert erreicht
    # resolve_accounts_api_base_url.
    write_runtime_files(paths, apply_config(accounts_api_base_url=""))
    with pytest.raises(ConfigError) as error:
        config.parse(config.load_raw(paths), paths)
    assert "SmartHeat-Portal" in str(error.value) and "Integration" not in str(error.value)

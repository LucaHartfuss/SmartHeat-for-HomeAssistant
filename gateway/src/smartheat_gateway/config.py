"""Laufzeit-Konfiguration des Gateways (Spec SHG G2 3.1, G3 2.5, Plan G2a Praezisierung 2). Der Agent schreibt
runtime_config.json (ohne Geheimnisse) und secrets/runtime.json; die Laufzeit fuehrt beide (plus den Transport-Schluessel
aus create_csr, nur bei iot_core) zu einem Options-Dict mit den Namen des Add-ons zusammen und prueft mit
smartheat_runtime.options."""
import contextlib
import re
from dataclasses import dataclass

from smartheat_core.binding import BINDINGS
from smartheat_gateway.agent import wire
from smartheat_gateway.drivers.registry import driver_ids
from smartheat_gateway.files import read_json
from smartheat_gateway.paths import Paths
from smartheat_gateway.target_store import is_valid_portal_target
from smartheat_gateway.texts import SHG_TEXTS
from smartheat_gateway.version import GATEWAY_VERSION
from smartheat_runtime import options
from smartheat_runtime.options import ConfigError
from smartheat_runtime.runtime_config import BootInfo, RuntimeConfig
from smartheat_runtime.windows import validate_daily_trigger_time
from smartheat_transport.descriptor import KIND_IOT_CORE

IEEE = re.compile(r"0x[0-9a-f]{16}")
ROOM_SENSOR_REF = re.compile(r"zigbee:0x[0-9a-f]{16}:(temperature|local_temperature)|treiber:room_temperature")
# Top-level-Geheimnisse; das Geheimnis von cloudflared steckt im Unterobjekt.
_SECRET_KEYS = ("mqtt_password", "installation_token")
_CLOUDFLARED_SECRET = "cloudflared_service_token_secret"
REDACTED = "***"


@dataclass(frozen=True)
class GatewayConfig:
    driver_id: str
    driver_parameter: dict
    room_sensors: tuple[str, ...]
    thermostat: str | None
    room_target_start: float
    cloudflared: dict | None
    # Plan G2a Praezisierung 14: poll_interval_seconds -> Abfrageintervall des Treibers (None = Standard des Treibers).
    poll_seconds: float | None = None

    def driver_spec(self) -> dict:
        parameter = dict(self.driver_parameter)
        if self.poll_seconds is not None:
            parameter["poll_seconds"] = self.poll_seconds
        return {"id": self.driver_id, "parameter": parameter}


def split_secrets(config: dict) -> tuple[dict, dict]:
    """(oeffentlich, geheim) fuer die zwei Dateien; None-Werte und fehlende Geheimnisse fallen weg."""
    public = {key: value for key, value in config.items() if key not in _SECRET_KEYS and value is not None}
    secrets = {key: config[key] for key in _SECRET_KEYS if config.get(key)}
    cloudflared = config.get("cloudflared")
    if isinstance(cloudflared, dict):
        public["cloudflared"] = {k: v for k, v in cloudflared.items() if k != "service_token_secret"}
        if cloudflared.get("service_token_secret"):
            secrets[_CLOUDFLARED_SECRET] = cloudflared["service_token_secret"]
    return public, secrets


def load_raw(paths: Paths) -> dict:
    """Options-Dict aus den Dateien; fehlende, kaputte oder unlesbare runtime_config.json = {} (nicht
    konfiguriert, nie ein Absturz)."""
    public = read_json(paths.runtime_config)
    if not isinstance(public, dict):
        return {}
    secrets = read_json(paths.runtime_secrets)
    secrets = secrets if isinstance(secrets, dict) else {}
    raw = {**public, **{key: secrets[key] for key in _SECRET_KEYS if key in secrets}}
    if isinstance(raw.get("cloudflared"), dict) and _CLOUDFLARED_SECRET in secrets:
        raw["cloudflared"] = {**raw["cloudflared"], "service_token_secret": secrets[_CLOUDFLARED_SECRET]}
    add_transport_key(paths, raw)
    return raw


def add_transport_key(paths: Paths, raw: dict) -> None:
    """Schluessel aus create_csr als tls_private_key, nur fuer Transport iot_core: bei mosquitto_cloudflared muss das
    Feld leer sein (credential_for); ein liegengebliebener Schluessel (create_csr, Einrichten abgebrochen) stoert
    dann nicht."""
    transport = raw.get("transport")
    if not isinstance(transport, dict) or transport.get("kind") != KIND_IOT_CORE:
        return
    with contextlib.suppress(OSError, ValueError):
        raw["tls_private_key"] = paths.transport_key.read_text()


def secret_values(raw: dict) -> list[str]:
    values = [raw.get(key) for key in (*_SECRET_KEYS, "tls_private_key")]
    cloudflared = raw.get("cloudflared")
    if isinstance(cloudflared, dict):
        values.append(cloudflared.get("service_token_secret"))
    return [value for value in values if isinstance(value, str) and value]


def redact(text: str, raw: dict) -> str:
    """Ersetzt jedes Geheimnis aus raw in text durch *** (auch in der repr-Form mit Escapes); die einzige
    Schwaerzungsfunktion des Gateways (Host und Lebenszyklus nutzen sie)."""
    for secret in secret_values(raw):
        text = text.replace(secret, REDACTED).replace(repr(secret)[1:-1], REDACTED)
    return text


def is_configured(raw: dict) -> bool:
    return (
        raw.get("config_version") == wire.APPLY_CONFIG_VERSION and bool(raw.get("tenant_id"))
        and bool(raw.get("setup_id")) and isinstance(raw.get("driver"), dict) and raw.get("abgemeldet") is not True
    )


def boot_info(raw: dict, paths: Paths) -> BootInfo:
    """Wirft nie: ein unbekannter Hebelsatz faellt fuers Statusereignis auf den Standard zurueck (wie der Host des
    Add-ons); parse() meldet ihn danach als Konfigurationsfehler."""
    try:
        lever_set = options.binding_description(raw).lever_set
    except ConfigError:
        lever_set = BINDINGS[options.DEFAULT_LEVER_SET].lever_set
    return BootInfo(
        configured=is_configured(raw),
        signed_off=options.is_signed_off(raw) and bool(raw.get("tenant_id")),
        tenant_id=raw.get("tenant_id"), setup_id=raw.get("setup_id"),
        lever_set=lever_set, client_version=GATEWAY_VERSION,
        notify_hints_off=tuple(options.notify_hints_off(raw)),
        local_check_interval=options.local_check_interval(raw),
        backup_path=paths.backup, failsafe_path=paths.failsafe,
    )


def check_apply_config(config: dict) -> str | None:
    """Schema (G3 2.5, Fixture): bekannte Schluessel, Pflichtschluessel, Version. Werte prueft parse()."""
    if not isinstance(config, dict):
        return "config ist kein Objekt"
    unknown = sorted(set(config) - set(wire.APPLY_CONFIG_REQUIRED) - set(wire.APPLY_CONFIG_OPTIONAL))
    if unknown:
        return f"unbekannte Schluessel: {', '.join(unknown)}"
    missing = [key for key in wire.APPLY_CONFIG_REQUIRED if key not in config]
    if missing:
        return f"Pflichtschluessel fehlen: {', '.join(missing)}"
    if config["config_version"] != wire.APPLY_CONFIG_VERSION:
        return f"config_version {config['config_version']!r} nicht unterstuetzt"
    return None


def parse(raw: dict, paths: Paths) -> tuple[RuntimeConfig, GatewayConfig]:
    error = check_apply_config({key: value for key, value in raw.items() if key != "tls_private_key"})
    if error:
        raise ConfigError(f"Konfiguration: {error} – {options.RECONFIGURE_HINT}")
    lever_set = options.lever_set_id(raw)
    safety = options.local_safety(raw)
    descriptor, credential = options.resolve_transport(raw)  # Option "transport" ist hier ein Objekt
    base_url = options.resolve_accounts_api_base_url(
        raw.get("accounts_api_base_url"), SHG_TEXTS.accounts_url_missing_hint,
    )
    try:
        daily = validate_daily_trigger_time(raw.get("daily_trigger_time"))
    except ValueError as error_text:
        raise ConfigError(str(error_text)) from None
    problem = options.validate(raw)
    if problem:
        raise ConfigError(problem)
    hints_off = options.hints_off_strict(raw)
    driver = raw["driver"]
    if not isinstance(driver, dict) or driver.get("id") not in driver_ids() or not isinstance(
        driver.get("parameter"), dict
    ):
        raise ConfigError(f"Treiber {driver!r} unbekannt oder ohne Parameter – {options.RECONFIGURE_HINT}")
    sensors = raw["room_sensors"]
    if not isinstance(sensors, list) or not sensors or any(
        not isinstance(ref, str) or not ROOM_SENSOR_REF.fullmatch(ref) for ref in sensors
    ):
        raise ConfigError(f"Option 'room_sensors' ({sensors!r}) ungueltig – {options.RECONFIGURE_HINT}")
    thermostat = raw.get("thermostat")
    if thermostat is not None and (not isinstance(thermostat, str) or not IEEE.fullmatch(thermostat)):
        raise ConfigError(f"Option 'thermostat' ({thermostat!r}) ist keine IEEE-Adresse – {options.RECONFIGURE_HINT}")
    start = raw["room_target_start"]
    if not is_valid_portal_target(start):
        raise ConfigError(f"Option 'room_target_start' ({start!r}) ausserhalb 15–25 °C in 0,5-K-Schritten")
    cloudflared = raw.get("cloudflared")
    if cloudflared is not None and (
        not isinstance(cloudflared, dict) or set(cloudflared) != set(wire.APPLY_CONFIG_CLOUDFLARED_FIELDS)
        or not all(isinstance(value, str) and value for value in cloudflared.values())
    ):
        raise ConfigError(f"Option 'cloudflared' unvollstaendig – {options.RECONFIGURE_HINT}")
    runtime = RuntimeConfig(
        tenant_id=raw["tenant_id"], setup_id=raw["setup_id"], lever_set_id=lever_set, local_safety=safety,
        descriptor=descriptor, credential=credential, installation_token=raw[options.TOKEN_OPTION],
        accounts_api_base_url=base_url, daily_trigger_time=daily,
        local_check_interval=options.local_check_interval(raw), telemetry_interval=options.telemetry_interval(raw),
        notify_hints_off=tuple(hints_off), room_sensor_refs=tuple(sensors), battery_refs=(),
        entitlement_path=paths.entitlement,
    )
    gateway = GatewayConfig(
        driver_id=driver["id"], driver_parameter=dict(driver["parameter"]), room_sensors=tuple(sensors),
        thermostat=thermostat, room_target_start=float(start), cloudflared=cloudflared,
        poll_seconds=raw.get(options.POLL_INTERVAL_OPTION),
    )
    return runtime, gateway

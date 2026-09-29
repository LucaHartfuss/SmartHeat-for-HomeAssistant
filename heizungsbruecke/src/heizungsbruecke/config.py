"""Add-on-Optionen: Pflichtfelder, aufgeloeste Sicherheitswerte und Fenster, Startpruefungen und feste Adressen."""
import json
import logging
import math
import re
import urllib.parse
from pathlib import Path

from heizungsbruecke.notifier import HINT_CATEGORIES
from heizungsbruecke.safety import resolve_local_safety
from heizungsbruecke.windows import validate_daily_trigger_time

MQTT_HOST = "127.0.0.1"
# Muss zum `local_port`-Default von cloudflared_access_mqtt passen: Konvention, kein
# geteilter Konfigurationswert zwischen den beiden Add-ons.
MQTT_PORT = 18830

# Dateien im Add-on-Datenverzeichnis. Nutzer lesen sie zur Laufzeit als `config.<NAME>`, damit
# Tests sie umbiegen koennen.
DATA_DIR = Path("/data")
OPTIONS_PATH = DATA_DIR / "options.json"
BACKUP_PATH = DATA_DIR / "backup.json"
FAILSAFE_PATH = DATA_DIR / "failsafe_state.json"
DERIVED_SENSORS_PATH = DATA_DIR / "derived_sensors.json"
ENTITLEMENT_PATH = DATA_DIR / "entitlement_state.json"

# Lokale Checks laufen eventgetrieben; dieser Takt gilt nur noch fuer den Watchdog-Fallback
# bei getrennter WS-Verbindung (und fuer daynight/grace_check).
DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS = 300
DEFAULT_TELEMETRY_INTERVAL_SECONDS = 300


class ConfigError(ValueError):
    """Konfiguriert, aber ungueltig: Ruhezustand `konfigurationsfehler` statt Regelung (kein
    Exit, Neupruefung nach __main__.CONFIG_RECHECK_SECONDS). Die Meldung nennt die fehlende oder
    ungueltige Option."""


# Bewusst ohne verteilsystem/accounts_api_base_url/room_sensors: eine alte Konfiguration
# (0.17.0/0.18.0) soll als "eingerichtet" gelten und laut als Konfigurationsfehler melden,
# statt still auf die Integration zu warten. entity_shift_current/entity_min_flow bewusst
# ebenfalls nicht hier: eine 0.23.0-Konfiguration gilt so ebenfalls als "eingerichtet" und
# meldet "Konfiguration veraltet" (resolve_effective_options), statt still auf die
# Integration zu warten.
REQUIRED_OPTIONS = (
    "tenant_id", "mqtt_username", "mqtt_password",
    "entity_room_target", "entity_curve_current", "entity_outdoor_temp", "entity_heat_limit",
)

# TP11: Parallelverschiebung (Zonen-Wunschtemperatur) und Mindestvorlauf sind die neuen
# Rollen des generischen Pfads. Nicht in REQUIRED_OPTIONS (siehe Kommentar oben).
NEW_ENTITY_OPTIONS = ("entity_shift_current", "entity_min_flow")

OUTDATED_CONFIGURATION = "Konfiguration veraltet – bitte SmartHeat-Einrichtung erneut durchführen"
_ROOM_SENSOR = re.compile(r"sensor\.[a-z0-9_]+|climate\.[a-z0-9_]+::current_temperature")
_OUTDOOR_SOURCE = re.compile(r"(sensor|weather)\.[a-z0-9_]+")
_BATTERY_ENTITY = re.compile(r"(sensor|binary_sensor)\.[a-z0-9_]+")
_NOTIFY_SERVICE = re.compile(r"notify\.[a-z0-9_]+")

logger = logging.getLogger(__name__)


def is_configured(options: dict) -> bool:
    """config.yaml hat Pflichtfelder, aber mit leeren Defaults: bis die SmartHeat-Integration
    options.json per Supervisor-API fuellt, startet das Add-on mit leeren Werten. Das ist ein
    normaler Zustand, kein Fehler."""
    return all(options.get(field) for field in REQUIRED_OPTIONS)


def _resolve_sources(options: dict) -> dict:
    """Quellen aus der Integration (Spec TP6 3.1). room_sensors zuerst: fehlt es, ist die
    Konfiguration aelter als TP6 und muss neu eingerichtet werden."""
    room_sensors = options.get("room_sensors")
    if not isinstance(room_sensors, list) or not room_sensors:
        raise ConfigError(f"{OUTDATED_CONFIGURATION} (Option 'room_sensors' fehlt)")
    invalid = [ref for ref in room_sensors if not isinstance(ref, str) or not _ROOM_SENSOR.fullmatch(ref)]
    if invalid:
        raise ConfigError(f"Option 'room_sensors': ungueltige Eintraege {invalid!r}")
    outdoor = options.get("entity_outdoor_temp")
    if not isinstance(outdoor, str) or not _OUTDOOR_SOURCE.fullmatch(outdoor):
        raise ConfigError(f"Option 'entity_outdoor_temp' ({outdoor!r}) muss sensor.* oder weather.* sein")
    return {
        "room_sensors": list(room_sensors),
        "battery_entities": _string_list(options, "battery_entities", _BATTERY_ENTITY),
        "notify_services": _string_list(options, "notify_services", _NOTIFY_SERVICE),
        "notify_hints_off": _hints_off(options),
    }


def _hints_off(options: dict) -> list[str]:
    """Streng: nur die abschaltbaren Hinweis-Kategorien; kritische Meldungen sind nie abschaltbar."""
    value = options.get("notify_hints_off", [])
    if not isinstance(value, list) or any(item not in HINT_CATEGORIES for item in value):
        raise ConfigError(
            f"Option 'notify_hints_off' ({value!r}) enthaelt unbekannte oder nicht abschaltbare Kategorien"
        )
    return list(value)


def notify_hints_off(options: dict) -> list[str]:
    """Tolerant wie notify_services: der notifier entsteht vor der Optionspruefung."""
    raw = options.get("notify_hints_off")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if item in HINT_CATEGORIES]


def is_signed_off(options: dict) -> bool:
    """Option abgemeldet (Spec TP7 3.3), setzt die Integration beim Entfernen; fehlend = False."""
    return options.get("abgemeldet") is True


def _string_list(options: dict, key: str, pattern: re.Pattern) -> list[str]:
    value = options.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not pattern.fullmatch(item) for item in value):
        raise ConfigError(f"Option '{key}' ({value!r}) ist keine gueltige Liste")
    return list(value)


def resolve_effective_options(options: dict) -> dict:
    sources = _resolve_sources(options)
    missing = [key for key in NEW_ENTITY_OPTIONS if not options.get(key)]
    if missing:
        raise ConfigError(f"{OUTDATED_CONFIGURATION} (Option '{missing[0]}' fehlt)")
    try:
        daily_trigger_time = validate_daily_trigger_time(options.get("daily_trigger_time"))
    except ValueError as error:
        raise ConfigError(str(error)) from None
    verteilsystem = options.get("verteilsystem")
    if not verteilsystem:
        raise ConfigError(
            "Option 'verteilsystem' fehlt - bitte die SmartHeat-Integration neu einrichten"
        )
    try:
        safety = resolve_local_safety(verteilsystem)
    except ValueError as error:
        raise ConfigError(f"Option 'verteilsystem': {error}") from None
    base_url = resolve_accounts_api_base_url(options.get("accounts_api_base_url"))
    return {
        **options,
        **sources,
        "curve_min": safety.curve_min,
        "curve_max": safety.curve_max,
        "shift_min": safety.shift_min,
        "shift_max": safety.shift_max,
        "min_flow_min": safety.min_flow_min,
        "min_flow_max": safety.min_flow_max,
        "boost_threshold_k": safety.boost_threshold_k,
        "boost_curve_value": safety.boost_curve_value,
        "boost_shift_value": safety.boost_shift_value,
        "daily_trigger_time": daily_trigger_time,
        "accounts_api_base_url": base_url,
    }


def resolve_accounts_api_base_url(value) -> str:
    """Basis-URL der accounts-api (Abo-Status). Setzt die Integration; https-Pflicht, weil
    der Status ueber das Internet abgefragt wird."""
    if not isinstance(value, str) or not value:
        raise ConfigError(
            "Option 'accounts_api_base_url' fehlt - bitte die SmartHeat-Integration neu einrichten"
        )
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ConfigError(
            f"Option 'accounts_api_base_url' ({value!r}) muss mit https:// beginnen und einen Host haben"
        )
    return value.rstrip("/")


def validate_boost_config(options: dict) -> str | None:
    """Boost-Werte ausserhalb der Clamps sind ein Startfehler statt still geclampt: der Boost
    ist der einzige Schreibpfad ohne Server-Aufsicht."""
    curve_min, curve_max = options["curve_min"], options["curve_max"]
    shift_min, shift_max = options["shift_min"], options["shift_max"]
    boost_curve_value = options["boost_curve_value"]
    boost_shift_value = options["boost_shift_value"]

    if not (curve_min <= boost_curve_value <= curve_max):
        return (
            f"boost_curve_value ({boost_curve_value}) liegt ausserhalb des konfigurierten "
            f"Bereichs [curve_min={curve_min}, curve_max={curve_max}]"
        )
    if not (shift_min <= boost_shift_value <= shift_max):
        return (
            f"boost_shift_value ({boost_shift_value}) liegt ausserhalb des konfigurierten "
            f"Bereichs [shift_min={shift_min}, shift_max={shift_max}]"
        )
    return None


def _is_finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not math.isnan(value) and not math.isinf(value)


def validate_local_check_interval(options: dict) -> str | None:
    """Die Obergrenze 3600 s begrenzt, wie veraltet der Watchdog-Fallback bei getrennter
    WS-Verbindung werden kann. Prueft auch ein von Hand editiertes options.json."""
    value = options.get("local_check_interval_seconds")
    if value is not None and not _is_finite_number(value):
        return f"local_check_interval_seconds ({value!r}) ist kein gueltiger endlicher Zahlenwert"
    if value is not None and value > 3600:
        return (
            f"local_check_interval_seconds ({value}) liegt ueber dem zulaessigen Maximum "
            f"von 3600 Sekunden (1h) - seit der Umstellung auf eventgetriebene Trigger "
            f"steuert dieser Wert nur noch den Watchdog-/Fallback-Takt, nicht mehr "
            f"routinemaessiges Polling"
        )
    return None


def validate_telemetry_interval(options: dict) -> str | None:
    """Untergrenze 10 s wie im config.yaml-Schema, auch fuer ein von Hand editiertes
    options.json: sonst fluten Telemetrie-Publishes den Server. Obergrenze 900 s (TP11-Review):
    darueber lernt der Regelkern serverseitig praktisch nie (Abdeckungsregel > 15 min Luecke =
    Pause)."""
    value = options.get("telemetry_interval_seconds")
    if value is not None and not _is_finite_number(value):
        return f"telemetry_interval_seconds ({value!r}) ist kein gueltiger endlicher Zahlenwert"
    if value is not None and value < 10:
        return (
            f"telemetry_interval_seconds ({value}) liegt unter dem zulaessigen Minimum "
            f"von 10 Sekunden"
        )
    if value is not None and value > 900:
        return (
            f"telemetry_interval_seconds ({value}) liegt ueber dem zulaessigen Maximum "
            f"von 900 Sekunden (15 min) - der Regelkern wertet nur Telemetrie-Luecken bis "
            f"15 min als zusammenhaengend, darueber lernt er praktisch nie"
        )
    return None


def validate(options: dict) -> str | None:
    """Erste Fehlermeldung der Startpruefungen, sonst None."""
    for check in (
        validate_boost_config, validate_local_check_interval,
        validate_telemetry_interval,
    ):
        error = check(options)
        if error:
            return error
    return None


def local_check_interval(options: dict) -> float:
    return options.get("local_check_interval_seconds", DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS)


def telemetry_interval(options: dict) -> float:
    return options.get("telemetry_interval_seconds", DEFAULT_TELEMETRY_INTERVAL_SECONDS)


def notify_services(options: dict) -> list[str]:
    """Notify-Dienste fuer Push-Meldungen (Spec TP6 3.1). Tolerant, weil auch Startfehler vor
    der Optionspruefung noch gemeldet werden sollen: ungueltige Eintraege fallen weg."""
    raw = options.get("notify_services")
    if not isinstance(raw, list):
        return []
    return [service for service in raw if isinstance(service, str) and _NOTIFY_SERVICE.fullmatch(service)]


def load_options_safe(path: Path) -> dict:
    """{} sowohl ohne Datei (noch nicht eingerichtet) als auch bei kaputter Datei (Stromausfall
    beim Schreiben): der Start soll sauber melden koennen statt abzustuerzen."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as error:
        logger.warning(
            "options.json konnte nicht gelesen werden (%s), starte mit leerer Konfiguration: %s",
            path, error,
        )
        return {}

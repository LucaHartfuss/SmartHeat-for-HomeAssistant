"""Add-on-Optionen: Pflichtfelder, Profilwerte, Startpruefungen und feste Adressen."""
import json
import logging
import math
from pathlib import Path

from heizungsbruecke.profiles import (
    resolve_boost_defaults,
    resolve_local_clamps,
    resolve_window_defaults,
    window_size_hours,
)

MQTT_HOST = "127.0.0.1"
# Muss zum `local_port`-Default von cloudflared_access_mqtt passen: Konvention, kein
# geteilter Konfigurationswert zwischen den beiden Add-ons.
MQTT_PORT = 18830
# Gleicher Host fuer jeden Tenant (siehe DEFAULT_HEIZUNGSSERVER_BASE_URL in der Integration).
ACCOUNTS_API_BASE_URL = "https://accounts.hartfussha.org"

# Dateien im Add-on-Datenverzeichnis. Nutzer lesen sie zur Laufzeit als `config.<NAME>`, damit
# Tests sie umbiegen koennen.
DATA_DIR = Path("/data")
OPTIONS_PATH = DATA_DIR / "options.json"
BACKUP_PATH = DATA_DIR / "backup.json"
FAILSAFE_PATH = DATA_DIR / "failsafe_state.json"
DERIVED_SENSORS_PATH = DATA_DIR / "derived_sensors.json"
DAYNIGHT_SNAPSHOT_PATH = DATA_DIR / "daynight_snapshot_state.json"
ENTITLEMENT_PATH = DATA_DIR / "entitlement_state.json"

# Lokale Checks laufen eventgetrieben; dieser Takt gilt nur noch fuer den Watchdog-Fallback
# bei getrennter WS-Verbindung (und fuer daynight/grace_check).
DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS = 300
DEFAULT_TELEMETRY_INTERVAL_SECONDS = 300

REQUIRED_OPTIONS = (
    "tenant_id", "profile", "mqtt_username", "mqtt_password",
    "entity_room_actual", "entity_room_target",
    "entity_curve_current", "entity_offset_current", "entity_outdoor_temp", "entity_heat_limit",
)

logger = logging.getLogger(__name__)


def is_configured(options: dict) -> bool:
    """config.yaml hat Pflichtfelder, aber mit leeren Defaults: bis die SmartHeat-Integration
    options.json per Supervisor-API fuellt, startet das Add-on mit leeren Werten. Das ist ein
    normaler Zustand, kein Fehler."""
    return all(options.get(field) for field in REQUIRED_OPTIONS)


def resolve_effective_options(options: dict) -> dict:
    clamps = resolve_local_clamps(options["profile"])
    boost = resolve_boost_defaults(options["profile"])
    windows = resolve_window_defaults(options["profile"])
    return {
        **options,
        "curve_min": clamps.curve_min,
        "curve_max": clamps.curve_max,
        "offset_min": clamps.offset_min,
        "offset_max": clamps.offset_max,
        "boost_threshold_k": boost.threshold_k,
        "boost_curve_value": boost.curve_value,
        "boost_offset_value": boost.offset_value,
        "daily_trigger_time": windows.daily_trigger_time,
        "day_avg_window_start": windows.day_avg_window_start,
        "day_avg_window_end": windows.day_avg_window_end,
        "night_avg_window_start": windows.night_avg_window_start,
        "night_avg_window_end": windows.night_avg_window_end,
        "avg_window_hours": window_size_hours(windows.day_avg_window_start, windows.day_avg_window_end),
    }


def validate_boost_config(options: dict) -> str | None:
    """Boost-Werte ausserhalb der Clamps sind ein Startfehler statt still geclampt: der Boost
    ist der einzige Schreibpfad ohne Server-Aufsicht."""
    curve_min, curve_max = options["curve_min"], options["curve_max"]
    offset_min, offset_max = options["offset_min"], options["offset_max"]
    boost_curve_value = options["boost_curve_value"]
    boost_offset_value = options["boost_offset_value"]

    if not (curve_min <= boost_curve_value <= curve_max):
        return (
            f"boost_curve_value ({boost_curve_value}) liegt ausserhalb des konfigurierten "
            f"Bereichs [curve_min={curve_min}, curve_max={curve_max}]"
        )
    if not (offset_min <= boost_offset_value <= offset_max):
        return (
            f"boost_offset_value ({boost_offset_value}) liegt ausserhalb des konfigurierten "
            f"Bereichs [offset_min={offset_min}, offset_max={offset_max}]"
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
    options.json: sonst fluten Telemetrie-Publishes den Server."""
    value = options.get("telemetry_interval_seconds")
    if value is not None and not _is_finite_number(value):
        return f"telemetry_interval_seconds ({value!r}) ist kein gueltiger endlicher Zahlenwert"
    if value is not None and value < 10:
        return (
            f"telemetry_interval_seconds ({value}) liegt unter dem zulaessigen Minimum "
            f"von 10 Sekunden"
        )
    return None


def validate_derived_sensor_prerequisites(options: dict) -> str | None:
    """Vorab pruefen, damit ein fehlendes Feld eine klare Startmeldung ergibt statt eines
    KeyError in derived_sensors.ensure_all."""
    missing = [field for field in ("entity_room_actual", "entity_outdoor_temp") if not options.get(field)]
    if missing:
        return (
            "Folgende Pflichtfelder fehlen in der Add-on-Konfiguration (werden fuer "
            f"automatisch berechnete Sensoren gebraucht): {', '.join(missing)}"
        )
    return None


def validate(options: dict) -> str | None:
    """Erste Fehlermeldung der Startpruefungen, sonst None."""
    for check in (
        validate_boost_config, validate_local_check_interval,
        validate_telemetry_interval, validate_derived_sensor_prerequisites,
    ):
        error = check(options)
        if error:
            return error
    return None


def local_check_interval(options: dict) -> float:
    return options.get("local_check_interval_seconds", DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS)


def telemetry_interval(options: dict) -> float:
    return options.get("telemetry_interval_seconds", DEFAULT_TELEMETRY_INTERVAL_SECONDS)


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

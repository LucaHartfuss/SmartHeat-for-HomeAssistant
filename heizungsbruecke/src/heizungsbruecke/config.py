"""Add-on-Optionen: Pflichtfelder, aufgeloeste Sicherheitswerte und Tagestick-Zeit, Startpruefungen und feste Adressen."""
import json
import logging
import re
from pathlib import Path

from smartheat_runtime.options import (  # noqa: F401  (Rueck-Import: config.X bleibt Vertrag fuer Tests/Contract-Check)
    CERTIFICATE_CREDENTIAL_OPTIONS,
    DEFAULT_LEVER_SET,
    DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS,
    DEFAULT_TELEMETRY_INTERVAL_SECONDS,
    LEVER_SET_OPTION,
    MAX_TELEMETRY_INTERVAL_SECONDS,
    OUTDATED_CONFIGURATION,
    PASSWORD_CREDENTIAL_OPTIONS,
    POLL_INTERVAL_OPTION,
    POLL_INTERVAL_RANGE,
    RECONFIGURE_HINT,
    SECRET_OPTIONS,
    TOKEN_OPTION,
    TRANSPORT_OPTION,
    ConfigError,
    binding_description,
    is_signed_off,
    lever_set_id,
    local_check_interval,
    local_safety,
    notify_hints_off,
    resolve_accounts_api_base_url,
    resolve_transport,
    telemetry_interval,
    validate,
    validate_local_check_interval,
    validate_poll_interval,
    validate_telemetry_interval,
)
from smartheat_runtime.options import hints_off_strict as _hints_off
from smartheat_runtime.runtime_config import BATTERY_LOW_FLAG, BATTERY_PERCENT, BatteryRef, RuntimeConfig
from smartheat_runtime.windows import validate_daily_trigger_time

# Dateien im Add-on-Datenverzeichnis. Nutzer lesen sie zur Laufzeit als `config.<NAME>`, damit
# Tests sie umbiegen koennen.
DATA_DIR = Path("/data")
OPTIONS_PATH = DATA_DIR / "options.json"
BACKUP_PATH = DATA_DIR / "backup.json"
FAILSAFE_PATH = DATA_DIR / "failsafe_state.json"
DERIVED_SENSORS_PATH = DATA_DIR / "derived_sensors.json"
ENTITLEMENT_PATH = DATA_DIR / "entitlement_state.json"


# Bewusst ohne verteilsystem/accounts_api_base_url/room_sensors: eine alte Konfiguration
# (0.17.0/0.18.0) soll als "eingerichtet" gelten und laut als Konfigurationsfehler melden,
# statt still auf die Integration zu warten. entity_shift_current/entity_min_flow bewusst
# ebenfalls nicht hier: eine 0.23.0-Konfiguration gilt so ebenfalls als "eingerichtet" und
# meldet "Konfiguration veraltet" (resolve_effective_options), statt still auf die
# Integration zu warten. Zugangsdaten bewusst nicht hier (seit AWS-2): eine alte Konfiguration gilt als
# eingerichtet und meldet "Konfiguration veraltet" (resolve_transport).
REQUIRED_OPTIONS = (
    "tenant_id",
    "entity_room_target", "entity_curve_current", "entity_outdoor_temp", "entity_heat_limit",
)

# TP11: Parallelverschiebung (Zonen-Wunschtemperatur) und Mindestvorlauf sind die neuen
# Rollen des generischen Pfads. Nicht in REQUIRED_OPTIONS (siehe Kommentar oben).
NEW_ENTITY_OPTIONS = ("entity_shift_current", "entity_min_flow")

_ROOM_SENSOR = re.compile(r"sensor\.[a-z0-9_]+|climate\.[a-z0-9_]+::current_temperature")
_OUTDOOR_SOURCE = re.compile(r"(sensor|weather)\.[a-z0-9_]+")
_BATTERY_ENTITY = re.compile(r"(sensor|binary_sensor)\.[a-z0-9_]+")
_NOTIFY_SERVICE = re.compile(r"notify\.[a-z0-9_]+")
# Spec 5.6 "Zonen-Entity schreibbar": HaPlantBinding.write kennt nur climate.set_temperature und
# number.set_value (ein input_number- oder sensor-Wert waere beim Start gueltig, liesse sich aber
# nie schreiben).
_WRITABLE_ENTITY = {
    "entity_shift_current": (
        re.compile(r"climate\.[a-z0-9_]+(::temperature)?|number\.[a-z0-9_]+"), "eine climate.*- oder number.*-Entity",
    ),
    "entity_min_flow": (re.compile(r"number\.[a-z0-9_]+"), "eine number.*-Entity"),
    "entity_heat_limit": (re.compile(r"number\.[a-z0-9_]+"), "eine number.*-Entity"),
}
# Minimal-Pflichtfelder fuer "eingerichtet" bei einem anderen Hebelsatz: der Rest wird in resolve_effective_options als
# Konfigurationsfehler gemeldet statt still als "nicht eingerichtet" zu warten.
_CORE_REQUIRED_OPTIONS = ("tenant_id", "entity_room_target", "entity_outdoor_temp")
# Pflicht-Entity-Optionen je Hebelsatz (Startpruefung "Entity existiert", Konfigurationsfehler bei fehlender Option).
# Vaillant: wie bis 0.30.0 (__main__.REQUIRED_ENTITY_OPTIONS).
REQUIRED_ENTITY_OPTIONS: dict[str, tuple[str, ...]] = {
    "vaillant_vrc720": (
        "entity_room_target", "entity_curve_current", "entity_shift_current", "entity_min_flow", "entity_heat_limit",
        "entity_outdoor_temp",
    ),
    "weishaupt_wwp": (
        "entity_room_target", "entity_curve_current", "entity_shift_current", "entity_heat_limit", "entity_outdoor_temp",
        "entity_mode_select", "entity_setpoint_comfort", "entity_setpoint_setback",
    ),
    "weishaupt_wwp_basis": (
        "entity_room_target", "entity_shift_current", "entity_outdoor_temp", "entity_mode_select",
        "entity_setpoint_comfort", "entity_setpoint_setback",
    ),
    "viessmann_vicare": (
        "entity_room_target", "entity_curve_current", "entity_level_current", "entity_shift_current",
        "entity_outdoor_temp", "entity_mode_select",
    ),
}
_NUMBER_ENTITY = (re.compile(r"number\.[a-z0-9_]+"), "eine number.*-Entity")
# Schreibbarkeit je Hebelsatz (Spec 5.6 "schreibbar"): Vaillant = _WRITABLE_ENTITY (Contract-Check 25 vergleicht nur
# diese mit der Integration); Weishaupt-Betriebsart ist ein select, das Viessmann-Heizprogramm eine climate-Entity.
_WRITABLE_BY_LEVER_SET: dict[str, dict[str, tuple[re.Pattern, str]]] = {
    "weishaupt_wwp": {
        "entity_curve_current": _NUMBER_ENTITY, "entity_shift_current": _NUMBER_ENTITY,
        "entity_heat_limit": _NUMBER_ENTITY, "entity_setpoint_comfort": _NUMBER_ENTITY,
        "entity_setpoint_setback": _NUMBER_ENTITY,
        "entity_mode_select": (re.compile(r"select\.[a-z0-9_]+"), "eine select.*-Entity"),
    },
    "weishaupt_wwp_basis": {
        "entity_shift_current": _NUMBER_ENTITY, "entity_setpoint_comfort": _NUMBER_ENTITY,
        "entity_setpoint_setback": _NUMBER_ENTITY,
        "entity_mode_select": (re.compile(r"select\.[a-z0-9_]+"), "eine select.*-Entity"),
    },
    "viessmann_vicare": {
        "entity_curve_current": _NUMBER_ENTITY, "entity_level_current": _NUMBER_ENTITY,
        "entity_shift_current": _NUMBER_ENTITY,
        "entity_mode_select": (re.compile(r"climate\.[a-z0-9_]+"), "eine climate.*-Entity"),
    },
}

logger = logging.getLogger(__name__)


def is_configured(options: dict) -> bool:
    """config.yaml hat Pflichtfelder, aber mit leeren Defaults: bis die SmartHeat-Integration
    options.json per Supervisor-API fuellt, startet das Add-on mit leeren Werten. Das ist ein
    normaler Zustand, kein Fehler. Plan 3b: mit einem anderen Hebelsatz als Vaillant genuegen die
    Minimal-Pflichtfelder; was fehlt, meldet resolve_effective_options als Konfigurationsfehler."""
    required = REQUIRED_OPTIONS if options.get(LEVER_SET_OPTION) in (None, "", DEFAULT_LEVER_SET) else _CORE_REQUIRED_OPTIONS
    return all(options.get(field) for field in required)


def required_entity_options(lever_set: str) -> tuple[str, ...]:
    return REQUIRED_ENTITY_OPTIONS[lever_set]


def writable_entity_rules(lever_set: str) -> dict[str, tuple[re.Pattern, str]]:
    return _WRITABLE_ENTITY if lever_set == DEFAULT_LEVER_SET else _WRITABLE_BY_LEVER_SET[lever_set]


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


def _string_list(options: dict, key: str, pattern: re.Pattern) -> list[str]:
    value = options.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not pattern.fullmatch(item) for item in value):
        raise ConfigError(f"Option '{key}' ({value!r}) ist keine gueltige Liste")
    return list(value)


def resolve_effective_options(options: dict) -> dict:
    lever_set = lever_set_id(options)
    sources = _resolve_sources(options)
    if lever_set == DEFAULT_LEVER_SET:
        missing = [key for key in NEW_ENTITY_OPTIONS if not options.get(key)]
        if missing:
            raise ConfigError(f"{OUTDATED_CONFIGURATION} (Option '{missing[0]}' fehlt)")
    missing = [key for key in required_entity_options(lever_set) if not options.get(key)]
    if missing:
        raise ConfigError(f"Option '{missing[0]}' fehlt für den Hebelsatz '{lever_set}' – {RECONFIGURE_HINT}")
    for key, (pattern, expected) in writable_entity_rules(lever_set).items():
        value = options[key]
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise ConfigError(f"Option '{key}' ({value!r}) muss {expected} sein – {RECONFIGURE_HINT}")
    # Final-Review I1: Zone (wird geschrieben) und Raum-Soll (Kundenwunsch, wird gelesen) auf derselben
    # Entity waeren eine Rueckkopplung bis shift_max. Vergleich ohne "::attribut".
    shift = options["entity_shift_current"]
    if shift.partition("::")[0] == str(options.get("entity_room_target", "")).partition("::")[0]:
        raise ConfigError(
            f"Option 'entity_shift_current' ({shift!r}) ist dieselbe Entity wie 'entity_room_target' – "
            f"die Heizzone kann nicht zugleich Raum-Soll sein, {RECONFIGURE_HINT}"
        )
    try:
        daily_trigger_time = validate_daily_trigger_time(options.get("daily_trigger_time"))
    except ValueError as error:
        raise ConfigError(str(error)) from None
    verteilsystem = options.get("verteilsystem")
    if not verteilsystem:
        raise ConfigError(
            "Option 'verteilsystem' fehlt - bitte die SmartHeat-Integration neu einrichten"
        )
    local_safety(options)
    base_url = resolve_accounts_api_base_url(options.get("accounts_api_base_url"))
    return {
        **options,
        **sources,
        "daily_trigger_time": daily_trigger_time,
        "accounts_api_base_url": base_url,
    }


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


def runtime_config(options: dict) -> RuntimeConfig:
    """Hostneutrale Laufzeit-Konfiguration aus den wirksamen, geprueften Optionen (nach resolve_effective_options und
    validate; resolve_transport prueft hier noch einmal)."""
    descriptor, credential = resolve_transport(options)
    return RuntimeConfig(
        tenant_id=options["tenant_id"],
        setup_id=options.get("setup_id"),
        lever_set_id=lever_set_id(options),
        local_safety=local_safety(options),
        descriptor=descriptor,
        credential=credential,
        installation_token=options[TOKEN_OPTION],
        accounts_api_base_url=options["accounts_api_base_url"],
        daily_trigger_time=options.get("daily_trigger_time"),
        local_check_interval=local_check_interval(options),
        telemetry_interval=telemetry_interval(options),
        notify_hints_off=tuple(notify_hints_off(options)),
        room_sensor_refs=tuple(options["room_sensors"]),
        battery_refs=tuple(
            BatteryRef(entity_id, BATTERY_LOW_FLAG if entity_id.startswith("binary_sensor.") else BATTERY_PERCENT)
            for entity_id in options.get("battery_entities", [])
        ),
        entitlement_path=ENTITLEMENT_PATH,
    )

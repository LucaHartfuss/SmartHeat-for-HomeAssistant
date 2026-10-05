"""Add-on-Optionen: Pflichtfelder, aufgeloeste Sicherheitswerte und Tagestick-Zeit, Startpruefungen und feste Adressen."""
import json
import logging
import math
import re
import urllib.parse
from pathlib import Path

from smartheat_core.binding import BINDINGS, BindingDescription, with_poll_interval
from smartheat_core.safety import LocalSafety, resolve_local_safety
from smartheat_runtime.notifier import HINT_CATEGORIES
from smartheat_runtime.runtime_config import RuntimeConfig
from smartheat_runtime.windows import validate_daily_trigger_time
from smartheat_transport.connect import connect_options
from smartheat_transport.descriptor import (
    Credential,
    Descriptor,
    TransportConfigError,
    credential_for,
    parse_descriptor,
)

# Dateien im Add-on-Datenverzeichnis. Nutzer lesen sie zur Laufzeit als `config.<NAME>`, damit
# Tests sie umbiegen koennen.
DATA_DIR = Path("/data")
OPTIONS_PATH = DATA_DIR / "options.json"
BACKUP_PATH = DATA_DIR / "backup.json"
FAILSAFE_PATH = DATA_DIR / "failsafe_state.json"
DERIVED_SENSORS_PATH = DATA_DIR / "derived_sensors.json"
ENTITLEMENT_PATH = DATA_DIR / "entitlement_state.json"

# Lokale Checks laufen eventgetrieben; dieser Takt gilt nur noch fuer den Watchdog-Fallback
# bei getrennter WS-Verbindung (und fuer grace_check/health).
DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS = 300
DEFAULT_TELEMETRY_INTERVAL_SECONDS = 300
# Obergrenze des Telemetrie-Intervalls (Regel 4, TP12e, AU-022): der Server wertet Luecken ab
# samples.MAX_GAP (15 min) als Pause. Bei 900 s laege jede Nachricht mit positiver
# Laufzeitabweichung darueber (dauerhaft Aufwaermphase/keine_heizstunden ohne Alarm); 600 s lassen
# Reserve (Contract-Check 33: <= 0,8 x MAX_GAP). Muss zu config.yaml (schema) passen.
MAX_TELEMETRY_INTERVAL_SECONDS = 600


class ConfigError(ValueError):
    """Konfiguriert, aber ungueltig: Ruhezustand `konfigurationsfehler` statt Regelung (kein
    Exit, Neupruefung nach __main__.CONFIG_RECHECK_SECONDS). Die Meldung nennt die fehlende oder
    ungueltige Option."""


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

TRANSPORT_OPTION = "transport"
PASSWORD_CREDENTIAL_OPTIONS = ("mqtt_username", "mqtt_password")
CERTIFICATE_CREDENTIAL_OPTIONS = ("tls_certificate", "tls_private_key")
TOKEN_OPTION = "installation_token"
# Werte, die nie in einem Status, einer Meldung oder einem Log erscheinen duerfen (Regel 6).
SECRET_OPTIONS = ("mqtt_password", "tls_private_key", TOKEN_OPTION)

OUTDATED_CONFIGURATION = "Konfiguration veraltet – bitte SmartHeat-Einrichtung erneut durchführen"
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
RECONFIGURE_HINT = "bitte SmartHeat neu konfigurieren"
# Plan 3b: Hebelsatz des Bindings (Option lever_set, schreibt die Integration ab Plan 3c). Fehlt sie, gilt Vaillant
# (client1 unveraendert, kein Verhaltenswechsel).
LEVER_SET_OPTION = "lever_set"
DEFAULT_LEVER_SET = "vaillant_vrc720"
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
# Abfrageintervall der Hersteller-Integration (Plan 3b, Wartezeit nach eigenem Schreiben); Grenzen sind eine Annahme.
POLL_INTERVAL_OPTION = "poll_interval_seconds"
POLL_INTERVAL_RANGE = (10, 3600)

logger = logging.getLogger(__name__)


def is_configured(options: dict) -> bool:
    """config.yaml hat Pflichtfelder, aber mit leeren Defaults: bis die SmartHeat-Integration
    options.json per Supervisor-API fuellt, startet das Add-on mit leeren Werten. Das ist ein
    normaler Zustand, kein Fehler. Plan 3b: mit einem anderen Hebelsatz als Vaillant genuegen die
    Minimal-Pflichtfelder; was fehlt, meldet resolve_effective_options als Konfigurationsfehler."""
    required = REQUIRED_OPTIONS if options.get(LEVER_SET_OPTION) in (None, "", DEFAULT_LEVER_SET) else _CORE_REQUIRED_OPTIONS
    return all(options.get(field) for field in required)


def lever_set_id(options: dict) -> str:
    """Hebelsatz aus der Option lever_set; fehlend oder leer = Vaillant. Ein unbekannter Wert ist ein
    Konfigurationsfehler (kein stiller Rueckfall auf Vaillant)."""
    value = options.get(LEVER_SET_OPTION)
    if value is None or value == "":
        return DEFAULT_LEVER_SET
    if not isinstance(value, str) or value not in BINDINGS:
        raise ConfigError(f"Option '{LEVER_SET_OPTION}' ({value!r}) ist kein bekannter Hebelsatz – {RECONFIGURE_HINT}")
    return value


def required_entity_options(lever_set: str) -> tuple[str, ...]:
    return REQUIRED_ENTITY_OPTIONS[lever_set]


def writable_entity_rules(lever_set: str) -> dict[str, tuple[re.Pattern, str]]:
    return _WRITABLE_ENTITY if lever_set == DEFAULT_LEVER_SET else _WRITABLE_BY_LEVER_SET[lever_set]


def binding_description(options: dict) -> BindingDescription:
    """Wirksame Binding-Beschreibung: Standard-Beschreibung des Hebelsatzes mit der Wartezeit zum Abfrageintervall
    (Option poll_interval_seconds; Vaillant bleibt bei 2100 s). Ein ungueltiges Intervall gilt als fehlend: der Start
    lehnt es per validate ab, das Abmelden (ohne validate) soll daran nicht scheitern."""
    interval = options.get(POLL_INTERVAL_OPTION)
    valid = interval is not None and validate_poll_interval(options) is None
    return with_poll_interval(BINDINGS[lever_set_id(options)], interval if valid else None)


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


def resolve_transport(options: dict) -> tuple[Descriptor, Credential]:
    """Spec AWS-IoT 5.1: Deskriptor und Zugangsdaten aus den Optionen; Zertifikat, Schluessel und CA werden
    hier schon geladen, damit ein Widerspruch beim Start als Konfigurationsfehler auffaellt statt als
    endlose Verbindungsfehler. Fehlt transport oder installation_token, stammt die Konfiguration von vor
    AWS-2 (keine Kompatibilitaet: neu einrichten)."""
    for key in (TRANSPORT_OPTION, TOKEN_OPTION):
        if not options.get(key):
            raise ConfigError(f"{OUTDATED_CONFIGURATION} (Option '{key}' fehlt)")
    try:
        descriptor = parse_descriptor(options[TRANSPORT_OPTION])
        credential = credential_for(
            descriptor,
            username=options.get("mqtt_username") or None, password=options.get("mqtt_password") or None,
            certificate_pem=options.get("tls_certificate") or None,
            private_key_pem=options.get("tls_private_key") or None,
        )
        connect_options(descriptor, credential)
    except TransportConfigError as error:
        raise ConfigError(f"Transport bzw. Zugangsdaten: {error} – {RECONFIGURE_HINT}") from None
    return descriptor, credential


def local_safety(options: dict) -> LocalSafety:
    """Lokale Sicherheitswerte fuer den Hebelsatz des HA-Bindings (Option lever_set) und das Verteilsystem des Profils
    (Regel 4). Die Boost-Werte liegen per check_invariants in den Bereichen (bis 0.29.0 validate_boost_config)."""
    lever_set = lever_set_id(options)
    try:
        return resolve_local_safety(lever_set, options.get("verteilsystem"))
    except ValueError as error:
        raise ConfigError(f"Option 'verteilsystem': {error}") from None


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


def _is_finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not math.isnan(value) and not math.isinf(value)


def _is_interval_number(value) -> bool:
    """Endliche Zahl, aber kein bool (True waere sonst 1 Sekunde)."""
    return not isinstance(value, bool) and _is_finite_number(value)


def validate_local_check_interval(options: dict) -> str | None:
    """Die Obergrenze 3600 s begrenzt, wie veraltet der Watchdog-Fallback bei getrennter
    WS-Verbindung werden kann. Prueft auch ein von Hand editiertes options.json."""
    value = options.get("local_check_interval_seconds")
    if value is not None and not _is_interval_number(value):
        return f"local_check_interval_seconds ({value!r}) ist kein gueltiger endlicher Zahlenwert"
    if value is not None and value < 1:
        return f"local_check_interval_seconds ({value}) liegt unter dem zulaessigen Minimum von 1 Sekunde"
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
    options.json: sonst fluten Telemetrie-Publishes den Server. Obergrenze 600 s (TP12e): der
    Regelkern wertet Luecken ab 15 min als Pause, 900 s liessen keine Reserve fuer
    Laufzeitschwankungen."""
    value = options.get("telemetry_interval_seconds")
    if value is not None and not _is_interval_number(value):
        return f"telemetry_interval_seconds ({value!r}) ist kein gueltiger endlicher Zahlenwert"
    if value is not None and value < 10:
        return (
            f"telemetry_interval_seconds ({value}) liegt unter dem zulaessigen Minimum "
            f"von 10 Sekunden"
        )
    if value is not None and value > MAX_TELEMETRY_INTERVAL_SECONDS:
        return (
            f"telemetry_interval_seconds ({value}) liegt ueber dem zulaessigen Maximum "
            f"von {MAX_TELEMETRY_INTERVAL_SECONDS} Sekunden ({MAX_TELEMETRY_INTERVAL_SECONDS // 60} min) - der Server wertet Telemetrie-Luecken "
            f"ab 15 min als Pause; darueber bleibt keine Reserve fuer Laufzeitschwankungen"
        )
    return None


def validate_poll_interval(options: dict) -> str | None:
    """Plan 3b: optionales Abfrageintervall der Hersteller-Integration (Sekunden), Grenzen POLL_INTERVAL_RANGE."""
    value = options.get(POLL_INTERVAL_OPTION)
    if value is None:
        return None
    low, high = POLL_INTERVAL_RANGE
    if not _is_interval_number(value) or not low <= value <= high:
        return f"{POLL_INTERVAL_OPTION} ({value!r}) muss eine Zahl von {low} bis {high} Sekunden sein"
    return None


def validate(options: dict) -> str | None:
    """Erste Fehlermeldung der Startpruefungen, sonst None."""
    for check in (
        validate_local_check_interval, validate_telemetry_interval, validate_poll_interval,
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


def runtime_config(options: dict) -> RuntimeConfig:
    """Hostneutrale Laufzeit-Konfiguration aus den wirksamen, gepruefte Optionen (nach resolve_effective_options und
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
        battery_refs=tuple(options.get("battery_entities", [])),
        entitlement_path=ENTITLEMENT_PATH,
    )

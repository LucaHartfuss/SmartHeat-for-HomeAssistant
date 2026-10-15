"""Hostneutrale Pruefung der Laufzeit-Optionen (Plan SHG G2a, Praezisierung 1): Hebelsatz und lokale
Sicherheitswerte, Intervalle, Hinweis-Schalter, Abmelden-Flag. Das HA-Add-on (heizungsbruecke.config) und das Gateway
(smartheat_gateway.config) pruefen mit denselben Funktionen; Optionsnamen = Optionen des Add-ons bzw. Schluessel von
apply_config (Spec SHG G3 2.5)."""
import math

from smartheat_core.binding import BindingDescription
from smartheat_core.bindungsparameter import beschreibung
from smartheat_core.levers import LEVER_SETS
from smartheat_core.safety import LocalSafety, resolve_local_safety
from smartheat_runtime.notifier import HINT_CATEGORIES

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
    Exit, Neupruefung nach app.CONFIG_RECHECK_SECONDS). Die Meldung nennt die fehlende oder
    ungueltige Option."""


RECONFIGURE_HINT = "bitte SmartHeat neu konfigurieren"
# Plan 3b: Hebelsatz des Bindings (Option lever_set, schreibt die Integration ab Plan 3c). Fehlt sie, gilt Vaillant
# (Bestandsanlage unveraendert, kein Verhaltenswechsel).
LEVER_SET_OPTION = "lever_set"
DEFAULT_LEVER_SET = "vaillant_vrc720"
# Spec Stufe 3 S4b: Bindungsparameter aus der Konfiguration (dokument.optionen bzw. Gateway-Host); ohne sie kein Start.
BINDUNGSPARAMETER_OPTION = "bindungsparameter"


def lever_set_id(options: dict) -> str:
    """Hebelsatz aus der Option lever_set; fehlend oder leer = Vaillant. Ein unbekannter Wert ist ein
    Konfigurationsfehler (kein stiller Rueckfall auf Vaillant)."""
    value = options.get(LEVER_SET_OPTION)
    if value is None or value == "":
        return DEFAULT_LEVER_SET
    if not isinstance(value, str) or value not in LEVER_SETS:
        raise ConfigError(f"Option '{LEVER_SET_OPTION}' ({value!r}) ist kein bekannter Hebelsatz – {RECONFIGURE_HINT}")
    return value


def binding_description(options: dict) -> BindingDescription:
    """Wirksame Binding-Beschreibung aus den Bindungsparametern der Konfiguration (Spec Stufe 3 S4b); ConfigError,
    wenn sie fehlen oder eine lokale Pruefung verletzen. Keine Ersatztabelle."""
    hebelsatz = lever_set_id(options)  # wirft selbst ConfigError mit eigenem Text
    try:
        return beschreibung(hebelsatz, options.get(BINDUNGSPARAMETER_OPTION))
    except ValueError as error:
        raise ConfigError(f"Konfiguration: {error}") from None


def hints_off_strict(options: dict) -> list[str]:
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


def local_safety(options: dict) -> LocalSafety:
    """Lokale Sicherheitswerte fuer den Hebelsatz des HA-Bindings (Option lever_set) und das Verteilsystem des Profils
    (Regel 4). Die Boost-Werte liegen per check_invariants in den Bereichen (bis 0.29.0 validate_boost_config)."""
    lever_set = lever_set_id(options)
    try:
        return resolve_local_safety(lever_set, options.get("verteilsystem"))
    except ValueError as error:
        raise ConfigError(f"Option 'verteilsystem': {error}") from None


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


def validate(options: dict) -> str | None:
    """Erste Fehlermeldung der Startpruefungen, sonst None."""
    for check in (
        validate_local_check_interval, validate_telemetry_interval,
    ):
        error = check(options)
        if error:
            return error
    return None


def local_check_interval(options: dict) -> float:
    return options.get("local_check_interval_seconds", DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS)


def telemetry_interval(options: dict) -> float:
    return options.get("telemetry_interval_seconds", DEFAULT_TELEMETRY_INTERVAL_SECONDS)

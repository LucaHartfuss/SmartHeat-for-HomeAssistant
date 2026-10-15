"""Add-on-Konfiguration (Spec 5c 1.3): die Einstellungen kommen aus dem Konfigurationsdokument des Geraetekerns
(dokument.optionen), nicht mehr aus options.json. Hier stehen die festen Pfade, die Pflicht- und Schreibregeln je
Hebelsatz und die Pruefungen der Sicherheitswerte; options.json bleibt nur fuer bootstrap_url und log_level (Task 9)."""
import re
from pathlib import Path

from smartheat_runtime.options import (  # noqa: F401  (Rueck-Import: der Host und dokument lesen diese Namen als config.X)
    DEFAULT_LEVER_SET,
    ConfigError,
    binding_description,
    lever_set_id,
    local_safety,
    validate,
)

# Dateien im Add-on-Datenverzeichnis. Nutzer lesen sie zur Laufzeit als `config.<NAME>`, damit
# Tests sie umbiegen koennen.
DATA_DIR = Path("/data")
OPTIONS_PATH = DATA_DIR / "options.json"
BACKUP_PATH = DATA_DIR / "backup.json"
FAILSAFE_PATH = DATA_DIR / "failsafe_state.json"
DERIVED_SENSORS_PATH = DATA_DIR / "derived_sensors.json"
ENTITLEMENT_PATH = DATA_DIR / "entitlement_state.json"
# Laufzeit-Zustand (Soll-Speicher, Abmelde-Markierung) wie im Gateway unter <daten>/runtime/; den Geraetekern legt
# selbst device/ unter DEVICE_DATA_DIR an.
RUNTIME_DIR = DATA_DIR / "runtime"
ROOM_TARGET_PATH = RUNTIME_DIR / "room_target.json"
ABGEMELDET_PATH = RUNTIME_DIR / "abgemeldet.json"
AUFRAEUMEN_PATH = DATA_DIR / "aufraeumen.json"
DEVICE_DATA_DIR = DATA_DIR
# Rastung des Sicherheitssatzes (Spec Stufe 3 S3 3.3): gleich smartheat_device.device.DEVICE_DIR / smartheat_core.rastung.DATEI
# (ein Test haelt das gleich); die Laufzeit liest sie, der Dokumentspeicher des Geraetekerns schreibt sie.
RASTUNG_PATH = DEVICE_DATA_DIR / "device" / "sicherheitssatz.json"

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

def required_entity_options(lever_set: str) -> tuple[str, ...]:
    return REQUIRED_ENTITY_OPTIONS[lever_set]


def writable_entity_rules(lever_set: str) -> dict[str, tuple[re.Pattern, str]]:
    return _WRITABLE_ENTITY if lever_set == DEFAULT_LEVER_SET else _WRITABLE_BY_LEVER_SET[lever_set]

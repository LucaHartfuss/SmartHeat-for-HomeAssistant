"""Hilfs-Entities der Bruecke. Der Raumtemperatur-Template-Sensor ist die Rolle room_actual (Mittel
aller gueltigen Raumfuehler), bei einer weather-Quelle ein Aussentemperatur-Template die Rolle
outdoor_temp. Seit TP11 bildet der Server alle Mittelwerte aus der Telemetrie; die frueheren
Statistik- und Tag-/Nacht-Helfer raeumt ensure_all beim Start weg.

Jeder Helfer merkt sich die Quelle, auf der er angelegt wurde (derived_sensors.json). Weicht sie
ab oder ist sie unbekannt (Bestand vor TP6), wird er geloescht und neu angelegt; die Entity-ID
bleibt dabei gleich (slugify(name), gegen echtes HA geprueft)."""
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from heizungsbruecke.backup_store import load_backup, save_backup
from heizungsbruecke.helper_templates import outdoor_temperature_template, room_temperature_template

logger = logging.getLogger(__name__)

# TP11: Tracking-Schluessel der frueheren Helfer (DAT/DART, Raum-Mittel, 24-h-Minimum als
# Statistik-Helfer, Tag-/Nachtmittel als input_number).
OBSOLETE_STATISTICS = ("room_12h_avg", "dart", "dat", "outdoor_min_24h")
OBSOLETE_INPUT_NUMBERS = ("room_day_avg", "room_night_avg")


@dataclass(frozen=True)
class DerivedSensors:
    entity_ids: dict[str, str]
    # Tracking-Schluessel der Helfer, die wegen einer geaenderten/unbekannten Quelle neu entstanden.
    replaced: tuple[str, ...]
    # Fingerabdruck aller Quellen, Zustand der Meldung "quellwechsel" (notifier).
    sources_fingerprint: str


def ensure_all(ha_api, tenant_id: str, room_sensors: list[str], outdoor_source: str, state_path: Path) -> DerivedSensors:
    tracking = load_backup(state_path)
    replaced: list[str] = []
    sources: dict[str, str] = {}

    def ensure(key: str, source: str | None, create_fn) -> str:
        entity_id, was_replaced = _ensure_entity(ha_api, tracking, key, source, state_path, create_fn)
        if was_replaced:
            replaced.append(key)
        if source is not None:
            sources[key] = source
        return entity_id

    room_template = room_temperature_template(room_sensors)
    room_actual = ensure(
        "room_temperature", room_template,
        lambda: ha_api.create_template_sensor(name=f"SmartHeat {tenant_id} Raumtemperatur", template=room_template),
    )
    result = {"room_actual": room_actual}

    if outdoor_source.startswith("weather."):
        outdoor_template = outdoor_temperature_template(outdoor_source)
        result["outdoor_temp"] = ensure(
            "outdoor_temperature", outdoor_template,
            lambda: ha_api.create_template_sensor(
                name=f"SmartHeat {tenant_id} Außentemperatur", template=outdoor_template,
            ),
        )
    else:
        _drop_unused(ha_api, tracking, "outdoor_temperature", state_path)
    _remove_obsolete(ha_api, tracking, state_path)

    fingerprint = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()[:12]
    return DerivedSensors(entity_ids=result, replaced=tuple(replaced), sources_fingerprint=fingerprint)


def _ensure_entity(ha_api, tracking: dict, key: str, source: str | None, state_path: Path, create_fn) -> tuple[str, bool]:
    """(Entity-ID, neu angelegt wegen Quellwechsel?). Erst loeschen, dann anlegen: nur so
    bekommt der neue Helfer wieder dieselbe Entity-ID.

    Quellwechsel heisst: eine Quelle ist verlangt und weicht von der gemerkten ab, auch wenn der
    alte Helfer schon fehlt (Loeschen gelang, Anlegen scheiterte im vorigen Versuch -- das
    Tracking wird erst nach dem Anlegen geschrieben)."""
    existing = tracking.get(key, {})
    existing_id = existing.get("entity_id")
    replaced = bool(existing_id) and source is not None and existing.get("source") != source
    if existing_id and ha_api.entity_exists(existing_id):
        if not replaced:
            return existing_id, False
        logger.info("Hilfs-Entity %s (%s) wird wegen geaenderter Quelle neu angelegt", existing_id, key)
        ha_api.delete_helper(existing_id)
    entity_id = create_fn()
    tracking[key] = {"entity_id": entity_id} if source is None else {"entity_id": entity_id, "source": source}
    save_backup(state_path, tracking)
    return entity_id, replaced


def _drop_unused(ha_api, tracking: dict, key: str, state_path: Path, delete=None) -> str | None:
    """Ein nicht mehr gebrauchter Helfer (Template-Helfer bei weather -> sensor, TP11-Altbestand)
    wird entfernt, erst in HA, dann aus dem Tracking. `delete` loescht in HA (Standard:
    ha_api.delete_helper). Gibt die entfernte Entity-ID zurueck, None ohne Tracking-Eintrag.
    Wirft, wenn das Loeschen scheitert; der Eintrag bleibt dann stehen."""
    existing_id = tracking.get(key, {}).get("entity_id")
    if existing_id is None:
        return None
    if ha_api.entity_exists(existing_id):
        (delete or ha_api.delete_helper)(existing_id)
    del tracking[key]
    save_backup(state_path, tracking)
    return existing_id


def _remove_obsolete(ha_api, tracking: dict, state_path: Path) -> None:
    """TP11: DAT/DART, Raum-Mittel, 24-h-Minimum und Tag-/Nachtmittel werden nicht mehr gebraucht.
    Ein Fehler beim Loeschen ist kein Startfehler; der Eintrag bleibt dann fuer den naechsten Start."""
    for key in OBSOLETE_STATISTICS + OBSOLETE_INPUT_NUMBERS:
        delete = ha_api.delete_input_number if key in OBSOLETE_INPUT_NUMBERS else ha_api.delete_helper
        try:
            removed = _drop_unused(ha_api, tracking, key, state_path, delete=delete)
        except Exception as error:
            entity_id = tracking.get(key, {}).get("entity_id")
            logger.warning("Alter Hilfssensor %s (%s) konnte nicht entfernt werden: %s", entity_id, key, error)
            continue
        if removed is not None:
            logger.info("Alter Hilfssensor %s (%s) entfernt", removed, key)

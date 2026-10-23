"""Hilfs-Entities der Bruecke. Der Raumtemperatur-Template-Sensor ist die Rolle room_actual (Mittel
aller gueltigen Raumfuehler), bei einer weather-Quelle ein Aussentemperatur-Template die Rolle
outdoor_temp. Der Server bildet alle Mittelwerte aus der Telemetrie.

Jeder Helfer merkt sich die Quelle, auf der er angelegt wurde (derived_sensors.json). Fehlt sie oder
weicht sie ab, wird er geloescht und neu angelegt; die Entity-ID bleibt dabei gleich (slugify(name),
gegen echtes HA geprueft)."""
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from heizungsbruecke.helper_templates import outdoor_temperature_template, room_temperature_template
from smartheat_core.logtext import lesbar
from smartheat_runtime.backup_store import load_backup, save_backup

logger = logging.getLogger(__name__)

# Tracking-Schluessel der Helfer, die dieses Add-on anlegt; jeder andere Eintrag ist ein Altrest.
TRACKING_KEYS = ("room_temperature", "outdoor_temperature")


@dataclass(frozen=True)
class DerivedSensors:
    entity_ids: dict[str, str]
    # Tracking-Schluessel der Helfer, die wegen einer geaenderten/unbekannten Quelle neu entstanden.
    replaced: tuple[str, ...]
    # Fingerabdruck aller Quellen, Zustand der Meldung "quellwechsel" (notifier).
    sources_fingerprint: str


def ensure_all(
    ha_api, tenant_id: str, room_sensors: list[str], outdoor_source: str, state_path: Path,
) -> DerivedSensors:
    tracking = load_backup(state_path)
    replaced: list[str] = []
    sources: dict[str, str] = {}

    def ensure(key: str, source: str | None, create_fn) -> str:
        entity_id, was_replaced = _ensure_entity(
            ha_api, tracking, key, source, state_path, create_fn, tenant_id,
        )
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
        _drop_unused(ha_api, tracking, "outdoor_temperature", state_path, tenant_id)

    fingerprint = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()[:12]
    return DerivedSensors(entity_ids=result, replaced=tuple(replaced), sources_fingerprint=fingerprint)


def _ensure_entity(
    ha_api, tracking: dict, key: str, source: str | None, state_path: Path, create_fn, tenant_id: str,
) -> tuple[str, bool]:
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
        logger.info("Hilfs-Entity %s (%s) wird wegen geaenderter Quelle neu angelegt", lesbar(str(existing_id)), key)
        ha_api.delete_helper(existing_id, tenant_id=tenant_id)
    entity_id = create_fn()
    tracking[key] = {"entity_id": entity_id} if source is None else {"entity_id": entity_id, "source": source}
    save_backup(state_path, tracking)
    return entity_id, replaced


def _drop_unused(ha_api, tracking: dict, key: str, state_path: Path, tenant_id: str) -> str | None:
    """Ein nicht mehr gebrauchter Helfer (Template-Helfer bei weather -> sensor) wird entfernt, erst in HA
    (ha_api.delete_helper mit dem Tenant fuer die Titelpruefung), dann aus dem Tracking. Gibt die entfernte
    Entity-ID zurueck, None ohne Tracking-Eintrag. Wirft, wenn das Loeschen scheitert; der Eintrag bleibt dann
    stehen."""
    existing_id = tracking.get(key, {}).get("entity_id")
    if existing_id is None:
        return None
    if ha_api.entity_exists(existing_id):
        ha_api.delete_helper(existing_id, tenant_id=tenant_id)
    del tracking[key]
    save_backup(state_path, tracking)
    return existing_id


def entfernen(ha_api, tenant_id: str, state_path: Path) -> bool:
    """Alle vom Add-on angelegten Hilfssensoren loeschen (Entfernen der Anlage): je Helfer erst in HA,
    dann aus dem Tracking (wie _drop_unused). Wirft nie. Ein nicht loeschbarer Helfer (HA antwortet nicht, Fehler)
    bleibt im Tracking fuer den naechsten Versuch, ins Log kommt nur die Fehlerart. Kaputte Eintraege ohne Entity-ID
    und Eintraege fremder Schluessel (Altreste frueherer Versionen) fallen ohne Loeschversuch weg. True, wenn kein
    Helfer mehr uebrig ist; dann ist auch die Datei weg."""
    try:
        tracking = load_backup(state_path)
    except (OSError, ValueError) as error:
        logger.warning("Liste der Hilfssensoren nicht lesbar (%s), verworfen", type(error).__name__)
        tracking = {}
    if not isinstance(tracking, dict):
        tracking = {}
    for key in list(tracking):
        entry = tracking[key]
        if key not in TRACKING_KEYS or not isinstance(entry, dict) or not isinstance(entry.get("entity_id"), str):
            del tracking[key]
            continue
        try:
            removed = _drop_unused(ha_api, tracking, key, state_path, tenant_id)
        except Exception as error:
            logger.warning("Hilfssensor %s (%s) nicht geloescht (%s), neuer Versuch beim naechsten Start",
                           lesbar(str(entry["entity_id"])), key, type(error).__name__)
            continue
        logger.info("Hilfssensor %s (%s) geloescht", lesbar(str(removed)), key)
    try:
        if tracking:
            save_backup(state_path, tracking)
            return False
        state_path.unlink(missing_ok=True)
    except OSError as error:
        logger.warning("Liste der Hilfssensoren nicht gespeichert (%s)", type(error).__name__)
        return False
    return True

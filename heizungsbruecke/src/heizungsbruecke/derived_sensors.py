"""Hilfs-Entities der Bruecke (Spec TP6 3.2/3.3). Der Raumtemperatur-Template-Sensor ist die
Rolle room_actual (Mittel aller gueltigen Raumfuehler), bei einer weather-Quelle ein
Aussentemperatur-Template die Rolle outdoor_temp. DAT/DART, das Raum-Mittel des Tagfensters und
das 24-h-Minimum sind Statistik-Helfer darauf, Tag-/Nachtmittel input_number-Helfer.

Jeder Helfer merkt sich die Quelle, auf der er angelegt wurde (derived_sensors.json). Weicht sie
ab oder ist sie unbekannt (Bestand vor TP6), wird er geloescht und neu angelegt; die Entity-ID
bleibt dabei gleich (slugify(name), gegen echtes HA geprueft). Statistik-Helfer laden ihr Fenster
aus dem Recorder: bleibt die Quell-Entity gleich, geht nichts verloren. input_number-Helfer haben
keine Quelle und werden nie neu angelegt.

Die Tracking-Schluessel 'room_12h_avg' und '_room_12h_avg' bleiben fuer Kontinuitaet mit
bestehenden Installationen, auch wenn das Fenster aus den Optionen kommt (windows.py)."""
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from heizungsbruecke.backup_store import load_backup, save_backup
from heizungsbruecke.helper_templates import outdoor_temperature_template, room_temperature_template

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DerivedSensors:
    entity_ids: dict[str, str]
    # Tracking-Schluessel der Helfer, die wegen einer geaenderten/unbekannten Quelle neu entstanden.
    replaced: tuple[str, ...]
    # Fingerabdruck aller Quellen, Zustand der Meldung "quellwechsel" (notifier).
    sources_fingerprint: str


def ensure_all(
    ha_api, tenant_id: str, room_sensors: list[str], outdoor_source: str,
    avg_window_hours: float, state_path: Path,
) -> DerivedSensors:
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
        outdoor_entity = ensure(
            "outdoor_temperature", outdoor_template,
            lambda: ha_api.create_template_sensor(
                name=f"SmartHeat {tenant_id} Außentemperatur", template=outdoor_template,
            ),
        )
        result["outdoor_temp"] = outdoor_entity
    else:
        outdoor_entity = outdoor_source
        _drop_unused(ha_api, tracking, "outdoor_temperature", state_path)

    result["_room_12h_avg"] = ensure(
        "room_12h_avg", room_actual,
        lambda: ha_api.create_statistics_sensor(
            name=f"SmartHeat {tenant_id} Raumtemp. {avg_window_hours:g}h-Mittel",
            source_entity_id=room_actual, max_age_hours=avg_window_hours,
        ),
    )
    result["dart"] = ensure(
        "dart", room_actual,
        lambda: ha_api.create_statistics_sensor(
            name=f"SmartHeat {tenant_id} DART", source_entity_id=room_actual, max_age_hours=24,
        ),
    )
    result["dat"] = ensure(
        "dat", outdoor_entity,
        lambda: ha_api.create_statistics_sensor(
            name=f"SmartHeat {tenant_id} DAT", source_entity_id=outdoor_entity, max_age_hours=24,
        ),
    )
    result["room_day_avg"] = ensure(
        "room_day_avg", None,
        lambda: ha_api.create_input_number(
            object_id=f"smartheat_{tenant_id}_room_day_avg", name=f"SmartHeat {tenant_id} Raumtemp. Tagesmittel",
            minimum=0.0, maximum=35.0, step=0.01, initial=20.0,
        ),
    )
    result["room_night_avg"] = ensure(
        "room_night_avg", None,
        lambda: ha_api.create_input_number(
            object_id=f"smartheat_{tenant_id}_room_night_avg", name=f"SmartHeat {tenant_id} Raumtemp. Nachtmittel",
            minimum=0.0, maximum=35.0, step=0.01, initial=20.0,
        ),
    )
    try:
        result["outdoor_min_24h"] = ensure(
            "outdoor_min_24h", outdoor_entity,
            lambda: ha_api.create_statistics_sensor(
                name=f"SmartHeat {tenant_id} Aussentemp. 24h-Minimum", source_entity_id=outdoor_entity,
                max_age_hours=24, state_characteristic="value_min",
            ),
        )
    except Exception as exc:
        logger.warning(
            "Optionaler Hilfssensor outdoor_min_24h konnte nicht angelegt werden, "
            "Sommersperre bleibt inaktiv: %s", exc,
        )

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


def _drop_unused(ha_api, tracking: dict, key: str, state_path: Path) -> None:
    """Ein nicht mehr gebrauchter Template-Helfer (weather -> sensor) wird entfernt."""
    existing_id = tracking.get(key, {}).get("entity_id")
    if existing_id is None:
        return
    if ha_api.entity_exists(existing_id):
        ha_api.delete_helper(existing_id)
    del tracking[key]
    save_backup(state_path, tracking)

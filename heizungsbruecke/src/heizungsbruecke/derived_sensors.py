from pathlib import Path

from heizungsbruecke.backup_store import load_backup, save_backup


def ensure_all(
    ha_api, tenant_id: str, room_actual_entity_id: str, outdoor_temp_entity_id: str,
    avg_window_hours: float, state_path: Path,
) -> dict[str, str]:
    """Legt (idempotent) die HA-Helfer an, die heizungsbruecke fuer DAT/DART/
    Tag-/Nachtmittel braucht, und liefert deren Entity-IDs als Rollen-Dict fuer
    manifest.build_manifest()'s derived_entity_ids-Parameter. '_room_12h_avg' ist
    keine Manifest-Rolle, sondern die Quelle, die daynight_snapshot.maybe_snapshot()
    fuer die taeglichen Snapshots braucht.

    `avg_window_hours` kommt seit Design-Spec 2026-09-16 (Abschnitt B) aus dem
    gewaehlten Profil (siehe profiles.py::resolve_window_defaults) statt eines
    hartcodierten 12h-Werts. Der Tracking-/Rueckgabeschluessel bleibt bewusst
    'room_12h_avg'/'_room_12h_avg' fuer Kontinuitaet mit bereits provisionierten
    Installationen (client1): eine Umbenennung wuerde _ensure_entity den
    bestehenden Eintrag nicht mehr finden lassen und einen doppelten HA-Helfer
    anlegen, statt den vorhandenen wiederzuverwenden. Nur max_age_hours und der
    Anzeigename der zugrunde liegenden Sensor-Erzeugung aendern sich.
    """
    tracking = load_backup(state_path)

    room_12h_avg = _ensure_entity(
        ha_api, tracking, "room_12h_avg", state_path,
        lambda: ha_api.create_statistics_sensor(
            name=f"SmartHeat {tenant_id} Raumtemp. {avg_window_hours:g}h-Mittel",
            source_entity_id=room_actual_entity_id, max_age_hours=avg_window_hours,
        ),
    )
    dart = _ensure_entity(
        ha_api, tracking, "dart", state_path,
        lambda: ha_api.create_statistics_sensor(
            name=f"SmartHeat {tenant_id} DART", source_entity_id=room_actual_entity_id, max_age_hours=24,
        ),
    )
    dat = _ensure_entity(
        ha_api, tracking, "dat", state_path,
        lambda: ha_api.create_statistics_sensor(
            name=f"SmartHeat {tenant_id} DAT", source_entity_id=outdoor_temp_entity_id, max_age_hours=24,
        ),
    )
    room_day_avg = _ensure_entity(
        ha_api, tracking, "room_day_avg", state_path,
        lambda: ha_api.create_input_number(
            object_id=f"smartheat_{tenant_id}_room_day_avg",
            name=f"SmartHeat {tenant_id} Raumtemp. Tagesmittel",
            minimum=0.0, maximum=35.0, step=0.01, initial=20.0,
        ),
    )
    room_night_avg = _ensure_entity(
        ha_api, tracking, "room_night_avg", state_path,
        lambda: ha_api.create_input_number(
            object_id=f"smartheat_{tenant_id}_room_night_avg",
            name=f"SmartHeat {tenant_id} Raumtemp. Nachtmittel",
            minimum=0.0, maximum=35.0, step=0.01, initial=20.0,
        ),
    )

    return {
        "dat": dat,
        "dart": dart,
        "room_day_avg": room_day_avg,
        "room_night_avg": room_night_avg,
        "_room_12h_avg": room_12h_avg,
    }


def _ensure_entity(ha_api, tracking: dict, key: str, state_path: Path, create_fn) -> str:
    existing = tracking.get(key, {}).get("entity_id")
    if existing and ha_api.entity_exists(existing):
        return existing
    entity_id = create_fn()
    tracking[key] = {"entity_id": entity_id}
    save_backup(state_path, tracking)
    return entity_id

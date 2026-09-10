from pathlib import Path

from heizungsbruecke.backup_store import load_backup, save_backup
from heizungsbruecke.clamping import clamp
from heizungsbruecke.manifest import ChannelManifest

_CLAMPED_ROLES = ("curve_current", "offset_current")


def publish_snapshot(manifest: ChannelManifest, ha_api, mqtt_client, seq: str) -> None:
    for role, entity_id in manifest.entity_ids.items():
        value = ha_api.get_state(entity_id)
        mqtt_client.publish_value(role=role, value=value, seq=seq)


def handle_down_message(
    role: str,
    value: float,
    manifest: ChannelManifest,
    ha_api,
    curve_min: float,
    curve_max: float,
    offset_min: float,
    offset_max: float,
    backup_path: Path,
) -> None:
    entity_id = manifest.entity_ids[role]

    if role == "curve_current":
        clamped = clamp(value, curve_min, curve_max)
    elif role == "offset_current":
        clamped = clamp(value, offset_min, offset_max)
    else:
        clamped = value

    ha_api.set_number_value(entity_id, clamped)

    if role in _CLAMPED_ROLES:
        backup = load_backup(backup_path)
        backup[role] = clamped
        save_backup(backup_path, backup)

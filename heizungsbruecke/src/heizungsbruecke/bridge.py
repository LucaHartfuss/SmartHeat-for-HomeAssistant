import logging
from pathlib import Path

from heizungsbruecke.backup_store import load_backup, save_backup
from heizungsbruecke.boost import BoostDecision
from heizungsbruecke.clamping import clamp
from heizungsbruecke.manifest import ChannelManifest

_CLAMPED_ROLES = ("curve_current", "offset_current")

logger = logging.getLogger(__name__)


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
    clamp_ranges = {
        "curve_current": (curve_min, curve_max),
        "offset_current": (offset_min, offset_max),
    }

    if role not in clamp_ranges:
        logger.warning(
            "Down-Nachricht fuer nicht erkannte/nicht schreibbare Rolle '%s' verworfen (kein Clamp-Bereich definiert)",
            role,
        )
        return

    entity_id = manifest.entity_ids[role]
    minimum, maximum = clamp_ranges[role]
    clamped = clamp(value, minimum, maximum)

    if clamped != value:
        logger.warning(
            "Wert fuer Rolle '%s' geclampt: empfangen=%s, geschrieben=%s (Bereich [%s, %s])",
            role, value, clamped, minimum, maximum,
        )

    ha_api.set_number_value(entity_id, clamped)

    if role in _CLAMPED_ROLES:
        backup = load_backup(backup_path)
        backup[role] = clamped
        save_backup(backup_path, backup)


def apply_boost_decision(
    decision: BoostDecision,
    boost_was_active: bool,
    manifest: ChannelManifest,
    ha_api,
    curve_min: float,
    curve_max: float,
    offset_min: float,
    offset_max: float,
    backup_path: Path,
) -> bool:
    """Writes the (clamped) boost values while boost is active, and restores the
    last known-good (backed-up) values, also clamped, on the active -> inactive
    transition. Returns the boost-active state to carry into the next tick.
    """
    if decision.active:
        if "curve_current" in manifest.entity_ids:
            ha_api.set_number_value(
                manifest.entity_ids["curve_current"], clamp(decision.curve_value, curve_min, curve_max)
            )
        if "offset_current" in manifest.entity_ids:
            ha_api.set_number_value(
                manifest.entity_ids["offset_current"], clamp(decision.offset_value, offset_min, offset_max)
            )
        logger.warning("Boost aktiv: Sollwerte auf Boost-Werte gesetzt")
        return True

    if boost_was_active:
        backup = load_backup(backup_path)
        if "curve_current" in backup and "curve_current" in manifest.entity_ids:
            ha_api.set_number_value(
                manifest.entity_ids["curve_current"], clamp(backup["curve_current"], curve_min, curve_max)
            )
        if "offset_current" in backup and "offset_current" in manifest.entity_ids:
            ha_api.set_number_value(
                manifest.entity_ids["offset_current"], clamp(backup["offset_current"], offset_min, offset_max)
            )
        logger.warning("Boost beendet: Werte aus Backup wiederhergestellt (sofern vorhanden)")
        return False

    return False

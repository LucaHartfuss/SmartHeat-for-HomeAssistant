"""Snapshot fuer den Server: Pflichtrollen lesen/pruefen und als eine Nachricht publizieren."""
import logging
import math
from dataclasses import dataclass
from datetime import datetime

from heizungsbruecke.manifest import OPTIONAL_SNAPSHOT_ROLES, SNAPSHOT_ROLES, ChannelManifest

logger = logging.getLogger(__name__)

SNAPSHOT_SCHEMA_VERSION = 2

# Nur auf Gueltigkeit geprueft, nicht gesendet: ohne gueltiges room_actual scheitern
# Comfort- und Notfall-Boost still.
VALIDITY_ONLY_ROLES = ("room_actual",)


def _is_finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(frozen=True)
class SnapshotRead:
    roles: dict[str, float]
    invalid_roles: tuple[str, ...]


def read_snapshot_roles(
    manifest: ChannelManifest, ha_api, computed_values: dict[str, float | None] | None = None,
) -> SnapshotRead:
    """Liest alle gemappten Server-Pflichtrollen plus room_actual. Ungueltig heisst: get_state
    wirft (unavailable/unknown, nicht numerisch, HTTP-Fehler) oder der Wert ist nicht endlich.
    Optionale Rollen fehlen still, wenn sie nicht lesbar sind. Meldet selbst nichts: das
    macht die Zustellung einmal pro Fehlerbeginn."""
    computed_values = computed_values or {}
    roles: dict[str, float] = {}

    for role in OPTIONAL_SNAPSHOT_ROLES:
        if role in computed_values:
            value = computed_values[role]
        elif role in manifest.entity_ids:
            try:
                value = ha_api.get_state(manifest.entity_ids[role])
            except Exception:
                logger.warning("Optionale Rolle '%s' nicht lesbar, wird weggelassen", role)
                continue
        else:
            continue
        if _is_finite_number(value):
            roles[role] = value

    invalid: list[str] = []
    for role in SNAPSHOT_ROLES + VALIDITY_ONLY_ROLES:
        entity_id = manifest.entity_ids.get(role)
        if entity_id is None:
            continue
        try:
            value = ha_api.get_state(entity_id)
        except Exception as error:
            logger.warning("Sensor fuer Rolle '%s' (%s) liefert keinen gueltigen Wert: %s", role, entity_id, error)
            invalid.append(role)
            continue
        if not _is_finite_number(value):
            logger.warning("Sensor fuer Rolle '%s' (%s) liefert keinen endlichen Wert: %r", role, entity_id, value)
            invalid.append(role)
            continue
        if role in SNAPSHOT_ROLES:
            roles[role] = value

    return SnapshotRead(roles=roles, invalid_roles=tuple(invalid))


def publish_snapshot(mqtt_client, seq: str, trigger: str | None, roles: dict[str, float]) -> None:
    """Eine Nachricht auf up/snapshot (Schema 2)."""
    mqtt_client.publish_snapshot({
        "schema": SNAPSHOT_SCHEMA_VERSION,
        "seq": seq,
        "trigger": trigger,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "roles": roles,
    })

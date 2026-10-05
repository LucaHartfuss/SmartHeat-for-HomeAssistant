"""Snapshot fuer den Server (Schema 4, Hersteller-Abstraktion Spec 4.1, Plan 2 P2-8): Raum-Soll und die Hebel des
Hebelsatzes lesen, pruefen und als eine Nachricht publizieren."""
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeGuard

from smartheat_core import wallclock
from smartheat_runtime.roles import ChannelManifest

logger = logging.getLogger(__name__)

SNAPSHOT_SCHEMA_VERSION = 4

# Durchsetzen (smartheat_core.enforce): zurueckgesetzter Eingriff, nur KPI. Muss zu messages.MANUAL_OVERRIDE_* auf dem
# Server passen (Contract-Check 16).
MANUAL_OVERRIDE_KEY = "manual_override"
MANUAL_OVERRIDE_FIELDS = ("levers", "erkannt")

TARGET_ROLE = "room_target"
# Nur auf Gueltigkeit geprueft, nicht gesendet: ohne gueltiges room_actual scheitern Comfort- und Notfall-Boost still.
VALIDITY_ONLY_ROLES = ("room_actual",)


def _is_finite_number(value) -> TypeGuard[float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(frozen=True)
class SnapshotRead:
    room_target: float | None
    levers: dict[str, float]
    invalid: tuple[str, ...]
    # Plan 3b: gelesene, aber nicht schreibbare Hebel (BindingDescription.readonly_levers), stehen auch in `levers`.
    readonly: tuple[str, ...] = ()


def _read_role(manifest: ChannelManifest, signals, role: str, invalid: list[str]) -> float | None:
    entity_id = manifest.entity_ids.get(role)
    if entity_id is None:
        invalid.append(role)
        return None
    try:
        value = signals.get_state(entity_id)
    except Exception as error:
        logger.warning("Sensor fuer Rolle '%s' (%s) liefert keinen gueltigen Wert: %s", role, entity_id, error)
        invalid.append(role)
        return None
    if not _is_finite_number(value):
        logger.warning("Sensor fuer Rolle '%s' (%s) liefert keinen endlichen Wert: %r", role, entity_id, value)
        invalid.append(role)
        return None
    return value


def read_snapshot(manifest: ChannelManifest, signals, binding, known: Mapping[str, float | None]) -> SnapshotRead:
    """Liest jeden Hebel des Hebelsatzes und das Raum-Soll; prueft room_actual. `known` traegt Hebelwerte, die nicht
    gelesen werden (eigener Schreibwert kurz nach dem Schreiben, A3-02; Wiederherstellungspunkt bei ruhender Zone).
    Ungueltig heisst: Lesen wirft oder der Wert ist nicht endlich. Meldet selbst nichts: das macht die Zustellung
    einmal pro Fehlerbeginn."""
    invalid: list[str] = []
    levers: dict[str, float] = {}
    for lever in binding.description.lever_set.levers:
        if not binding.has(lever):
            continue
        if lever in known:
            value = known[lever]
        else:
            try:
                value = binding.read(lever)
            except Exception as error:
                logger.warning("Hebel '%s' (%s) liefert keinen gueltigen Wert: %s", lever, binding.ref(lever), error)
                invalid.append(lever)
                continue
        if not _is_finite_number(value):
            logger.warning("Hebel '%s' (%s) liefert keinen endlichen Wert: %r", lever, binding.ref(lever), value)
            invalid.append(lever)
            continue
        levers[lever] = value
    readonly: list[str] = []
    for lever in binding.description.readonly_levers:
        if not binding.has(lever):
            continue
        # Nur gelesen (Spec 3.3/2, eingefrorene Steigung): ein Lesefehler ist kein Datenfehler, der Hebel fehlt dann.
        try:
            value = binding.read(lever)
        except Exception as error:
            logger.warning("Nur lesbarer Hebel '%s' (%s) nicht lesbar, wird weggelassen: %s", lever, binding.ref(lever), error)
            continue
        if not _is_finite_number(value):
            logger.warning("Nur lesbarer Hebel '%s' (%s) nicht endlich, wird weggelassen: %r", lever, binding.ref(lever), value)
            continue
        levers[lever] = value
        readonly.append(lever)
    room_target = _read_role(manifest, signals, TARGET_ROLE, invalid)
    for role in VALIDITY_ONLY_ROLES:
        if role in manifest.entity_ids:
            _read_role(manifest, signals, role, invalid)
    return SnapshotRead(room_target=room_target, levers=levers, invalid=tuple(invalid), readonly=tuple(readonly))


def publish_snapshot(
    mqtt_client, seq: str, trigger: str | None, room_target: float, levers: dict[str, float],
    manual_override: dict | None = None, readonly: tuple[str, ...] = (),
) -> None:
    """Eine Nachricht auf up/snapshot (Schema 4); manual_override nur, wenn einer ansteht. `readonly` nennt die nur
    gelesenen Hebel (Plan 3b, Weishaupt-Basis: Steigung), sonst leer."""
    payload = {
        "schema": SNAPSHOT_SCHEMA_VERSION,
        "seq": seq,
        "trigger": trigger,
        "ts": wallclock.now().isoformat(timespec="seconds"),
        "room_target": room_target,
        "levers": levers,
        "readonly": list(readonly),
    }
    if manual_override is not None:
        payload[MANUAL_OVERRIDE_KEY] = {field: manual_override[field] for field in MANUAL_OVERRIDE_FIELDS}
    mqtt_client.publish_snapshot(payload)

import logging
import math
from datetime import datetime
from pathlib import Path

from heizungsbruecke.backup_store import load_backup, save_backup
from heizungsbruecke.boost import BoostDecision
from heizungsbruecke.clamping import clamp
from heizungsbruecke.emergency_boost import EmergencyBoostDecision
from heizungsbruecke.manifest import SNAPSHOT_ROLES, OPTIONAL_SNAPSHOT_ROLES, ChannelManifest

_CLAMPED_ROLES = ("curve_current", "offset_current")

logger = logging.getLogger(__name__)

SNAPSHOT_SCHEMA_VERSION = 2


def _is_finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def publish_snapshot(
    manifest: ChannelManifest, ha_api, mqtt_client, seq: str, notify_service: str = "",
    trigger: str | None = None, computed_values: dict[str, float | None] | None = None,
) -> None:
    """Publishes one snapshot as ONE message on up/snapshot (Schema 2, Design-Spec
    2026-09-25). A required role whose entity cannot be read (dead sensor -> HA reports
    'unavailable', or an HTTP failure) is left out of `roles` instead of aborting the
    snapshot -- the server then answers `rejected` ("fehlende Rolle: ..."), which still
    counts as an ack, so a dead sensor no longer causes a false Notbetrieb (B3). The
    boost-failsafe evaluation after this call is unaffected either way (I3).

    If `notify_service` is configured, a push notification is sent per broken required
    role and tick -- deliberately not deduplicated, a broken sensor should keep nagging.
    Notifying is best effort. Optional roles never notify; a missing or non-finite
    optional value is simply left out.
    """
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

    for role in SNAPSHOT_ROLES:
        entity_id = manifest.entity_ids.get(role)
        if entity_id is None:
            continue
        try:
            roles[role] = ha_api.get_state(entity_id)
        except Exception:
            logger.warning(
                "Sensor fuer Rolle '%s' (%s) liefert keinen gueltigen Wert, "
                "fehlt in diesem Snapshot",
                role, entity_id,
            )
            if notify_service:
                try:
                    ha_api.send_notification(
                        notify_service,
                        f"Heizungsbruecke: Sensor fuer '{role}' ({entity_id}) liefert keinen "
                        f"gueltigen Wert - bitte pruefen (z.B. Batterie).",
                    )
                except Exception:
                    logger.warning(
                        "Push-Benachrichtigung fuer Rolle '%s' konnte nicht gesendet werden", role
                    )

    mqtt_client.publish_snapshot({
        "schema": SNAPSHOT_SCHEMA_VERSION,
        "seq": seq,
        "trigger": trigger,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "roles": roles,
    })


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

    skip_live_write = False
    if role in _CLAMPED_ROLES:
        backup = load_backup(backup_path)
        skip_live_write = backup.get("boost_active", False) or backup.get("emergency_boost_active", False)
        backup[role] = clamped
        save_backup(backup_path, backup)

    if skip_live_write:
        # Design-Spec 2026-09-16 Abschnitt D / 2026-09-23 Abschnitt 3: der boost- oder
        # notfall-boost-erzwungene Live-Wert bleibt unberuehrt -- backup.json ist bereits
        # aktuell (siehe oben) und wird beim Boost-/Notfall-Boost-Ende automatisch
        # wiederhergestellt.
        logger.info(
            "Down-Nachricht fuer Rolle '%s' waehrend aktivem (Notfall-)Boost nur in "
            "backup.json gespeichert, Live-Entity bleibt auf dem Boost-Wert.",
            role,
        )
        return

    ha_api.set_number_value(entity_id, clamped)


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
    """Writes the (clamped) boost values on the inactive -> active TRANSITION only, and
    restores the last known-good (backed-up) values, also clamped, on the active ->
    inactive transition. Returns the boost-active state to carry into the next tick.

    Rate-of-execution fix (whole-branch review finding): this function used to run once
    per hour (the old poll_interval_seconds cadence); it now runs as often as every 30s
    (local_check_interval_seconds, inside _run_local_check). decide_boost always returns
    the same boost_curve_value/boost_offset_value for the whole duration of an active
    boost, so re-asserting them on every steady-state call (boost_was_active already
    True) serves no purpose -- the live device already holds them from the transition
    write -- and would turn a single boost episode into 120-480 live writes instead of
    1-4. curve_current/offset_current are cloud-backed on client1 (mypyllant), so each
    superfluous write is a real third-party API call, risking rate-limiting/lockout.
    """
    if decision.active:
        if not boost_was_active:
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


def apply_emergency_decision(
    decision: EmergencyBoostDecision,
    emergency_was_active: bool,
    manifest: ChannelManifest,
    ha_api,
    curve_min: float,
    curve_max: float,
    offset_min: float,
    offset_max: float,
    backup_path: Path,
) -> bool:
    """Gleicher Schreib-/Wiederherstellungs-Mechanismus wie apply_boost_decision, aber
    mit eigenem Notfall-Boost-Zustand (siehe __main__.py's `emergency_boost_active` in
    backup.json) -- kollidiert dadurch nie mit dem Comfort-Boost-Zustand (`boost_active`).
    Die Wiederherstellung beim Beenden liest DIESELBEN backup.json-Felder
    (curve_current/offset_current) wie apply_boost_decision -- beide bedeuten "der
    zuletzt vom Server tatsaechlich bestaetigte Wert", von handle_down_message bei jeder
    echten Down-Nachricht aktuell gehalten, unabhaengig davon, welcher Boost (falls
    ueberhaupt einer) das Live-Geraet gerade ueberschreibt.
    """
    if decision.active:
        if not emergency_was_active:
            if "curve_current" in manifest.entity_ids:
                ha_api.set_number_value(
                    manifest.entity_ids["curve_current"], clamp(decision.curve_value, curve_min, curve_max)
                )
            if "offset_current" in manifest.entity_ids:
                ha_api.set_number_value(
                    manifest.entity_ids["offset_current"], clamp(decision.offset_value, offset_min, offset_max)
                )
            logger.warning("Notfall-Boost aktiv: Sollwerte auf Maximalwerte gesetzt")
        return True

    if emergency_was_active:
        backup = load_backup(backup_path)
        if "curve_current" in backup and "curve_current" in manifest.entity_ids:
            ha_api.set_number_value(
                manifest.entity_ids["curve_current"], clamp(backup["curve_current"], curve_min, curve_max)
            )
        if "offset_current" in backup and "offset_current" in manifest.entity_ids:
            ha_api.set_number_value(
                manifest.entity_ids["offset_current"], clamp(backup["offset_current"], offset_min, offset_max)
            )
        logger.warning("Notfall-Boost beendet: Werte aus Backup wiederhergestellt (sofern vorhanden)")
        return False

    return False

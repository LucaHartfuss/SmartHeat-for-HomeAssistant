import json
import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path

from heizungsbruecke.boost import decide_boost
from heizungsbruecke.bridge import apply_boost_decision, handle_down_message, publish_snapshot
from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.manifest import ManifestError, build_manifest
from heizungsbruecke.mqtt_client import BridgeMqttClient
from heizungsbruecke.profiles import UnknownProfileError, resolve_local_clamps

OPTIONS_PATH = Path("/data/options.json")
BACKUP_PATH = Path("/data/backup.json")

logger = logging.getLogger(__name__)


def _resolve_effective_options(options: dict) -> dict:
    """Returns a copy of `options` with curve_min/curve_max/offset_min/offset_max
    guaranteed present, resolved from the configured profile's local clamp
    defaults with any explicitly-set option value taking precedence. Raises
    UnknownProfileError if the profile has no local defaults and one of the
    four fields is still missing after that.
    """
    clamps = resolve_local_clamps(options["profile"], options)
    return {
        **options,
        "curve_min": clamps.curve_min,
        "curve_max": clamps.curve_max,
        "offset_min": clamps.offset_min,
        "offset_max": clamps.offset_max,
    }


def _validate_boost_config(options: dict) -> str | None:
    """Returns a German error message if the configured boost values fall outside
    the configured safety clamps, or None if the config is valid. A misconfigured
    boost value is a startup-time error, not something to silently clamp, since the
    boost path is the one write path that runs with no server oversight.
    """
    curve_min, curve_max = options["curve_min"], options["curve_max"]
    offset_min, offset_max = options["offset_min"], options["offset_max"]
    boost_curve_value = options["boost_curve_value"]
    boost_offset_value = options["boost_offset_value"]

    if not (curve_min <= boost_curve_value <= curve_max):
        return (
            f"boost_curve_value ({boost_curve_value}) liegt ausserhalb des konfigurierten "
            f"Bereichs [curve_min={curve_min}, curve_max={curve_max}]"
        )
    if not (offset_min <= boost_offset_value <= offset_max):
        return (
            f"boost_offset_value ({boost_offset_value}) liegt ausserhalb des konfigurierten "
            f"Bereichs [offset_min={offset_min}, offset_max={offset_max}]"
        )
    return None


def _make_down_callback(role, manifest, ha_api, options, write_lock):
    def _callback(client, userdata, message):
        try:
            payload = json.loads(message.payload)
            with write_lock:
                handle_down_message(
                    role=role,
                    value=payload["v"],
                    manifest=manifest,
                    ha_api=ha_api,
                    curve_min=options["curve_min"],
                    curve_max=options["curve_max"],
                    offset_min=options["offset_min"],
                    offset_max=options["offset_max"],
                    backup_path=BACKUP_PATH,
                )
        except Exception:
            logger.exception("Fehler bei der Verarbeitung einer Down-Nachricht fuer Rolle '%s'", role)
    return _callback


def _run_tick(manifest, ha_api, mqtt_client, options, write_lock, boost_was_active: bool) -> bool:
    """Runs one poll cycle: publish the snapshot, then (if room roles are configured)
    evaluate and apply the local boost decision. Returns the boost-active state to
    carry into the next tick. An individual unreadable sensor only costs that role its
    snapshot value (handled inside publish_snapshot, see I3); any remaining I/O failure
    propagates -- the caller (main's loop) is responsible for catching and logging so a
    single bad tick doesn't kill the whole process (see I2).
    """
    seq = str(uuid.uuid4())
    publish_snapshot(
        manifest=manifest,
        ha_api=ha_api,
        mqtt_client=mqtt_client,
        seq=seq,
        notify_service=options.get("notify_service", ""),
    )
    logger.info("Snapshot veroeffentlicht, seq=%s", seq)

    if "room_actual" in manifest.entity_ids and "room_target" in manifest.entity_ids:
        room_actual = ha_api.get_state(manifest.entity_ids["room_actual"])
        room_target = ha_api.get_state(manifest.entity_ids["room_target"])
        decision = decide_boost(
            room_actual=room_actual,
            room_target=room_target,
            threshold_k=options.get("boost_threshold_k", 0.5),
            boost_curve_value=options["boost_curve_value"],
            boost_offset_value=options["boost_offset_value"],
        )
        with write_lock:
            boost_was_active = apply_boost_decision(
                decision=decision,
                boost_was_active=boost_was_active,
                manifest=manifest,
                ha_api=ha_api,
                curve_min=options["curve_min"],
                curve_max=options["curve_max"],
                offset_min=options["offset_min"],
                offset_max=options["offset_max"],
                backup_path=BACKUP_PATH,
            )

    return boost_was_active


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    options = json.loads(OPTIONS_PATH.read_text())

    try:
        manifest = build_manifest(options)
    except ManifestError as error:
        print(f"FEHLER: {error}", file=sys.stderr)
        sys.exit(1)

    try:
        options = _resolve_effective_options(options)
    except UnknownProfileError as error:
        print(f"FEHLER: {error}", file=sys.stderr)
        sys.exit(1)

    boost_config_error = _validate_boost_config(options)
    if boost_config_error:
        print(f"FEHLER: {boost_config_error}", file=sys.stderr)
        sys.exit(1)

    ha_api = HomeAssistantApi(base_url="http://supervisor", token=os.environ["SUPERVISOR_TOKEN"])
    mqtt_client = BridgeMqttClient(
        host=options.get("mqtt_host", "127.0.0.1"),
        port=options["mqtt_port"],
        tenant_id=options["tenant_id"],
    )

    write_lock = threading.Lock()

    for role in ("curve_current", "offset_current"):
        if role in manifest.entity_ids:
            mqtt_client.subscribe_down(
                role=role, on_message=_make_down_callback(role, manifest, ha_api, options, write_lock)
            )
    mqtt_client.loop_start()

    boost_was_active = False

    while True:
        try:
            boost_was_active = _run_tick(manifest, ha_api, mqtt_client, options, write_lock, boost_was_active)
        except Exception:
            logger.exception("Fehler im Poll-Loop, wird beim naechsten Tick erneut versucht")

        time.sleep(options.get("poll_interval_seconds", 3600))


if __name__ == "__main__":
    main()

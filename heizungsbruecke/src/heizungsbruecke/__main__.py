import json
import os
import sys
import time
import uuid
from pathlib import Path

from heizungsbruecke.boost import decide_boost
from heizungsbruecke.bridge import handle_down_message, publish_snapshot
from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.manifest import ManifestError, build_manifest
from heizungsbruecke.mqtt_client import BridgeMqttClient

OPTIONS_PATH = Path("/data/options.json")
BACKUP_PATH = Path("/data/backup.json")


def _make_down_callback(role, manifest, ha_api, options):
    def _callback(client, userdata, message):
        payload = json.loads(message.payload)
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
    return _callback


def main() -> None:
    options = json.loads(OPTIONS_PATH.read_text())

    try:
        manifest = build_manifest(options)
    except ManifestError as error:
        print(f"FEHLER: {error}", file=sys.stderr)
        sys.exit(1)

    ha_api = HomeAssistantApi(base_url="http://supervisor", token=os.environ["SUPERVISOR_TOKEN"])
    mqtt_client = BridgeMqttClient(
        host=options.get("mqtt_host", "127.0.0.1"),
        port=options["mqtt_port"],
        tenant_id=options["tenant_id"],
    )

    for role in ("curve_current", "offset_current"):
        if role in manifest.entity_ids:
            mqtt_client.subscribe_down(role=role, on_message=_make_down_callback(role, manifest, ha_api, options))
    mqtt_client.loop_start()

    while True:
        publish_snapshot(manifest=manifest, ha_api=ha_api, mqtt_client=mqtt_client, seq=str(uuid.uuid4()))

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
            if decision.active:
                if "curve_current" in manifest.entity_ids:
                    ha_api.set_number_value(manifest.entity_ids["curve_current"], decision.curve_value)
                if "offset_current" in manifest.entity_ids:
                    ha_api.set_number_value(manifest.entity_ids["offset_current"], decision.offset_value)

        time.sleep(options.get("poll_interval_seconds", 3600))


if __name__ == "__main__":
    main()

from unittest.mock import MagicMock

from heizungsbruecke.bridge import publish_snapshot, handle_down_message
from heizungsbruecke.manifest import ChannelManifest
from heizungsbruecke.backup_store import load_backup


def test_publish_snapshot_reads_each_entity_and_publishes():
    manifest = ChannelManifest(entity_ids={
        "room_actual": "climate.wohnzimmer_thermostat",
        "outdoor_temp": "sensor.aussentemperatur",
    })
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda entity_id: {
        "climate.wohnzimmer_thermostat": 19.5,
        "sensor.aussentemperatur": 3.2,
    }[entity_id]
    mqtt_client = MagicMock()

    publish_snapshot(manifest=manifest, ha_api=ha_api, mqtt_client=mqtt_client, seq="tick-1")

    mqtt_client.publish_value.assert_any_call(role="room_actual", value=19.5, seq="tick-1")
    mqtt_client.publish_value.assert_any_call(role="outdoor_temp", value=3.2, seq="tick-1")


def test_handle_down_message_clamps_curve_value_before_writing(tmp_path):
    manifest = ChannelManifest(entity_ids={"curve_current": "number.weishaupt_heizkurve_steigung"})
    ha_api = MagicMock()
    backup_path = tmp_path / "backup.json"

    handle_down_message(
        role="curve_current",
        value=99.0,
        manifest=manifest,
        ha_api=ha_api,
        curve_min=0.3,
        curve_max=0.5,
        offset_min=2.0,
        offset_max=4.0,
        backup_path=backup_path,
    )

    ha_api.set_number_value.assert_called_once_with("number.weishaupt_heizkurve_steigung", 0.5)


def test_handle_down_message_persists_clamped_value_to_backup(tmp_path):
    manifest = ChannelManifest(entity_ids={"offset_current": "number.weishaupt_heizkurve_niveau"})
    ha_api = MagicMock()
    backup_path = tmp_path / "backup.json"

    handle_down_message(
        role="offset_current",
        value=1.0,
        manifest=manifest,
        ha_api=ha_api,
        curve_min=0.3,
        curve_max=0.5,
        offset_min=2.0,
        offset_max=4.0,
        backup_path=backup_path,
    )

    assert load_backup(backup_path) == {"offset_current": 2.0}


def test_handle_down_message_ignores_roles_without_clamp_range(tmp_path):
    manifest = ChannelManifest(entity_ids={"outdoor_temp": "sensor.aussentemperatur"})
    ha_api = MagicMock()
    backup_path = tmp_path / "backup.json"

    handle_down_message(
        role="outdoor_temp",
        value=3.2,
        manifest=manifest,
        ha_api=ha_api,
        curve_min=0.3,
        curve_max=0.5,
        offset_min=2.0,
        offset_max=4.0,
        backup_path=backup_path,
    )

    ha_api.set_number_value.assert_called_once_with("sensor.aussentemperatur", 3.2)
    assert load_backup(backup_path) == {}

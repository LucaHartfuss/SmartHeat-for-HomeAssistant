from unittest.mock import MagicMock

from heizungsbruecke.bridge import publish_snapshot, handle_down_message, apply_boost_decision
from heizungsbruecke.boost import BoostDecision
from heizungsbruecke.manifest import ChannelManifest
from heizungsbruecke.backup_store import load_backup, save_backup


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


def test_handle_down_message_refuses_unrecognized_role(tmp_path):
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

    ha_api.set_number_value.assert_not_called()
    assert load_backup(backup_path) == {}


def test_apply_boost_decision_clamps_before_writing_when_active(tmp_path):
    manifest = ChannelManifest(entity_ids={
        "curve_current": "number.weishaupt_heizkurve_steigung",
        "offset_current": "number.weishaupt_heizkurve_niveau",
    })
    ha_api = MagicMock()
    decision = BoostDecision(active=True, curve_value=99.0, offset_value=-50.0)
    backup_path = tmp_path / "backup.json"

    new_state = apply_boost_decision(
        decision=decision,
        boost_was_active=False,
        manifest=manifest,
        ha_api=ha_api,
        curve_min=0.3,
        curve_max=0.5,
        offset_min=2.0,
        offset_max=4.0,
        backup_path=backup_path,
    )

    assert new_state is True
    ha_api.set_number_value.assert_any_call("number.weishaupt_heizkurve_steigung", 0.5)
    ha_api.set_number_value.assert_any_call("number.weishaupt_heizkurve_niveau", 2.0)


def test_apply_boost_decision_restores_backup_on_transition_to_inactive(tmp_path):
    manifest = ChannelManifest(entity_ids={
        "curve_current": "number.weishaupt_heizkurve_steigung",
        "offset_current": "number.weishaupt_heizkurve_niveau",
    })
    ha_api = MagicMock()
    backup_path = tmp_path / "backup.json"
    save_backup(backup_path, {"curve_current": 0.4, "offset_current": 3.0})
    decision = BoostDecision(active=False, curve_value=None, offset_value=None)

    new_state = apply_boost_decision(
        decision=decision,
        boost_was_active=True,
        manifest=manifest,
        ha_api=ha_api,
        curve_min=0.3,
        curve_max=0.5,
        offset_min=2.0,
        offset_max=4.0,
        backup_path=backup_path,
    )

    assert new_state is False
    ha_api.set_number_value.assert_any_call("number.weishaupt_heizkurve_steigung", 0.4)
    ha_api.set_number_value.assert_any_call("number.weishaupt_heizkurve_niveau", 3.0)


def test_apply_boost_decision_restore_is_clamped_defense_in_depth(tmp_path):
    manifest = ChannelManifest(entity_ids={"curve_current": "number.weishaupt_heizkurve_steigung"})
    ha_api = MagicMock()
    backup_path = tmp_path / "backup.json"
    save_backup(backup_path, {"curve_current": 99.0})
    decision = BoostDecision(active=False, curve_value=None, offset_value=None)

    apply_boost_decision(
        decision=decision,
        boost_was_active=True,
        manifest=manifest,
        ha_api=ha_api,
        curve_min=0.3,
        curve_max=0.5,
        offset_min=2.0,
        offset_max=4.0,
        backup_path=backup_path,
    )

    ha_api.set_number_value.assert_called_once_with("number.weishaupt_heizkurve_steigung", 0.5)


def test_apply_boost_decision_no_write_when_backup_missing_on_transition(tmp_path):
    manifest = ChannelManifest(entity_ids={"curve_current": "number.weishaupt_heizkurve_steigung"})
    ha_api = MagicMock()
    backup_path = tmp_path / "backup.json"  # never created -> load_backup returns {}
    decision = BoostDecision(active=False, curve_value=None, offset_value=None)

    new_state = apply_boost_decision(
        decision=decision,
        boost_was_active=True,
        manifest=manifest,
        ha_api=ha_api,
        curve_min=0.3,
        curve_max=0.5,
        offset_min=2.0,
        offset_max=4.0,
        backup_path=backup_path,
    )

    assert new_state is False
    ha_api.set_number_value.assert_not_called()


def test_apply_boost_decision_steady_state_inactive_does_nothing(tmp_path):
    manifest = ChannelManifest(entity_ids={"curve_current": "number.weishaupt_heizkurve_steigung"})
    ha_api = MagicMock()
    backup_path = tmp_path / "backup.json"
    save_backup(backup_path, {"curve_current": 0.4})
    decision = BoostDecision(active=False, curve_value=None, offset_value=None)

    new_state = apply_boost_decision(
        decision=decision,
        boost_was_active=False,
        manifest=manifest,
        ha_api=ha_api,
        curve_min=0.3,
        curve_max=0.5,
        offset_min=2.0,
        offset_max=4.0,
        backup_path=backup_path,
    )

    assert new_state is False
    ha_api.set_number_value.assert_not_called()

from unittest.mock import MagicMock

import pytest

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


def _manifest_with_one_broken_sensor():
    manifest = ChannelManifest(entity_ids={
        "room_actual": "climate.wohnzimmer_thermostat",
        "dat": "sensor.kaputt",
        "outdoor_temp": "sensor.aussentemperatur",
    })
    ha_api = MagicMock()

    def _get_state(entity_id):
        if entity_id == "sensor.kaputt":
            raise ValueError("could not convert string to float: 'unavailable'")
        return {"climate.wohnzimmer_thermostat": 19.5, "sensor.aussentemperatur": 3.2}[entity_id]

    ha_api.get_state.side_effect = _get_state
    return manifest, ha_api


def test_publish_snapshot_skips_broken_role_and_publishes_the_others():
    manifest, ha_api = _manifest_with_one_broken_sensor()
    mqtt_client = MagicMock()

    publish_snapshot(manifest=manifest, ha_api=ha_api, mqtt_client=mqtt_client, seq="tick-1")

    published_roles = {call.kwargs["role"] for call in mqtt_client.publish_value.call_args_list}
    assert published_roles == {"room_actual", "outdoor_temp"}


def test_publish_snapshot_notifies_when_notify_service_is_configured():
    manifest, ha_api = _manifest_with_one_broken_sensor()
    mqtt_client = MagicMock()

    publish_snapshot(
        manifest=manifest,
        ha_api=ha_api,
        mqtt_client=mqtt_client,
        seq="tick-1",
        notify_service="notify.mobile_app_lucas_iphone",
    )

    ha_api.send_notification.assert_called_once()
    service, message = ha_api.send_notification.call_args.args
    assert service == "notify.mobile_app_lucas_iphone"
    assert "dat" in message
    assert "sensor.kaputt" in message


def test_publish_snapshot_does_not_notify_when_notify_service_is_empty():
    manifest, ha_api = _manifest_with_one_broken_sensor()
    mqtt_client = MagicMock()

    publish_snapshot(manifest=manifest, ha_api=ha_api, mqtt_client=mqtt_client, seq="tick-1")

    ha_api.send_notification.assert_not_called()


def test_publish_snapshot_survives_a_failing_notification():
    manifest, ha_api = _manifest_with_one_broken_sensor()
    ha_api.send_notification.side_effect = RuntimeError("notify service nicht erreichbar")
    mqtt_client = MagicMock()

    publish_snapshot(
        manifest=manifest,
        ha_api=ha_api,
        mqtt_client=mqtt_client,
        seq="tick-1",
        notify_service="notify.mobile_app_lucas_iphone",
    )

    published_roles = {call.kwargs["role"] for call in mqtt_client.publish_value.call_args_list}
    assert published_roles == {"room_actual", "outdoor_temp"}


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


def test_handle_down_message_writes_live_entity_when_boost_inactive(tmp_path):
    manifest = ChannelManifest(entity_ids={"curve_current": "number.curve"})
    ha_api = MagicMock()
    backup_path = tmp_path / "backup.json"
    save_backup(backup_path, {"boost_active": False})

    handle_down_message(
        role="curve_current", value=0.5, manifest=manifest, ha_api=ha_api,
        curve_min=0.3, curve_max=0.8, offset_min=2.0, offset_max=4.0, backup_path=backup_path,
    )

    ha_api.set_number_value.assert_called_once_with("number.curve", 0.5)
    assert load_backup(backup_path)["curve_current"] == 0.5


def test_handle_down_message_skips_live_write_when_boost_active(tmp_path):
    # Design-Spec 2026-09-16, Abschnitt D: waehrend eines aktiven Boosts darf eine
    # eingehende Down-Nachricht den boost-erzwungenen Live-Wert nicht ueberschreiben --
    # der Wert wird trotzdem in backup.json gehalten, damit apply_boost_decision beim
    # Boost-Ende den zuletzt tatsaechlich vom Server berechneten Wert findet.
    manifest = ChannelManifest(entity_ids={"curve_current": "number.curve"})
    ha_api = MagicMock()
    backup_path = tmp_path / "backup.json"
    save_backup(backup_path, {"boost_active": True})

    handle_down_message(
        role="curve_current", value=0.5, manifest=manifest, ha_api=ha_api,
        curve_min=0.3, curve_max=0.8, offset_min=2.0, offset_max=4.0, backup_path=backup_path,
    )

    ha_api.set_number_value.assert_not_called()
    assert load_backup(backup_path)["curve_current"] == 0.5


def test_handle_down_message_treats_missing_boost_active_as_inactive(tmp_path):
    # backup.json ohne 'boost_active'-Feld (z.B. allererste Down-Nachricht ueberhaupt)
    # darf nicht faelschlich als aktiver Boost interpretiert werden.
    manifest = ChannelManifest(entity_ids={"curve_current": "number.curve"})
    ha_api = MagicMock()
    backup_path = tmp_path / "backup.json"  # nie angelegt

    handle_down_message(
        role="curve_current", value=0.5, manifest=manifest, ha_api=ha_api,
        curve_min=0.3, curve_max=0.8, offset_min=2.0, offset_max=4.0, backup_path=backup_path,
    )

    ha_api.set_number_value.assert_called_once_with("number.curve", 0.5)


def test_handle_down_message_still_clamps_before_persisting_during_boost(tmp_path):
    manifest = ChannelManifest(entity_ids={"curve_current": "number.curve"})
    ha_api = MagicMock()
    backup_path = tmp_path / "backup.json"
    save_backup(backup_path, {"boost_active": True})

    handle_down_message(
        role="curve_current", value=99.0, manifest=manifest, ha_api=ha_api,
        curve_min=0.3, curve_max=0.5, offset_min=2.0, offset_max=4.0, backup_path=backup_path,
    )

    ha_api.set_number_value.assert_not_called()
    assert load_backup(backup_path)["curve_current"] == 0.5


def test_handle_down_message_rejects_nan_without_writing_or_persisting(tmp_path):
    # I2 failure-chain closure (whole-branch review): before the clamp() NaN guard, a
    # NaN down-message value would sail through clamp() unchanged and get persisted
    # into backup.json (backup[role] = clamped) AND written to the live device. Both
    # must now be prevented -- clamp() raises before either of those lines runs.
    manifest = ChannelManifest(entity_ids={"curve_current": "number.curve"})
    ha_api = MagicMock()
    backup_path = tmp_path / "backup.json"

    with pytest.raises(ValueError):
        handle_down_message(
            role="curve_current", value=float("nan"), manifest=manifest, ha_api=ha_api,
            curve_min=0.3, curve_max=0.8, offset_min=2.0, offset_max=4.0, backup_path=backup_path,
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


def test_apply_boost_decision_no_write_when_already_active(tmp_path):
    # Rate-of-execution fix: this function used to run once per hour, now runs as
    # often as every 30s (local_check_interval_seconds). Re-writing the same boost
    # values on every steady-state call while boost stays active would turn a single
    # boost episode into 120-480 live writes instead of 1 (curve_current/offset_current
    # are cloud-backed on client1 -- mypyllant) and spam the log every 30s. Only the
    # inactive -> active TRANSITION (boost_was_active=False) should write.
    manifest = ChannelManifest(entity_ids={
        "curve_current": "number.weishaupt_heizkurve_steigung",
        "offset_current": "number.weishaupt_heizkurve_niveau",
    })
    ha_api = MagicMock()
    decision = BoostDecision(active=True, curve_value=0.5, offset_value=3.0)
    backup_path = tmp_path / "backup.json"

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

    assert new_state is True
    ha_api.set_number_value.assert_not_called()


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


def test_apply_boost_decision_rejects_nan_curve_value_on_transition_to_active(tmp_path):
    # I2 failure-chain closure: a NaN decision.curve_value (e.g. propagated from a
    # sensor read that produced NaN) must not reach the live device via clamp().
    manifest = ChannelManifest(entity_ids={"curve_current": "number.weishaupt_heizkurve_steigung"})
    ha_api = MagicMock()
    decision = BoostDecision(active=True, curve_value=float("nan"), offset_value=None)
    backup_path = tmp_path / "backup.json"

    with pytest.raises(ValueError):
        apply_boost_decision(
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

    ha_api.set_number_value.assert_not_called()


def test_apply_boost_decision_rejects_nan_from_backup_on_restore(tmp_path):
    # I2 failure-chain closure (the specific chain from the whole-branch review): if
    # backup.json ever ended up holding a NaN for curve_current (e.g. written before
    # this fix existed), the restore path must reject it via clamp() instead of
    # writing NaN to the live, cloud-backed (mypyllant) device.
    manifest = ChannelManifest(entity_ids={"curve_current": "number.weishaupt_heizkurve_steigung"})
    ha_api = MagicMock()
    backup_path = tmp_path / "backup.json"
    save_backup(backup_path, {"curve_current": float("nan")})
    decision = BoostDecision(active=False, curve_value=None, offset_value=None)

    with pytest.raises(ValueError):
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

    ha_api.set_number_value.assert_not_called()


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

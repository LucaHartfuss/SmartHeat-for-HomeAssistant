from datetime import datetime
from unittest.mock import MagicMock

import pytest

from heizungsbruecke.bridge import (
    SnapshotRead,
    apply_boost_decision,
    apply_emergency_decision,
    handle_down_message,
    publish_snapshot,
    read_snapshot_roles,
)
from heizungsbruecke.boost import BoostDecision
from heizungsbruecke.emergency_boost import EmergencyBoostDecision
from heizungsbruecke.manifest import SNAPSHOT_ROLES, OPTIONAL_SNAPSHOT_ROLES, ChannelManifest
from heizungsbruecke.backup_store import load_backup, save_backup


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


def test_handle_down_message_skips_live_write_when_emergency_boost_active(tmp_path):
    manifest = ChannelManifest(entity_ids={"curve_current": "number.curve"})
    ha_api = MagicMock()
    backup_path = tmp_path / "backup.json"
    save_backup(backup_path, {"emergency_boost_active": True})

    handle_down_message(
        role="curve_current", value=0.5, manifest=manifest, ha_api=ha_api,
        curve_min=0.3, curve_max=0.8, offset_min=2.0, offset_max=4.0, backup_path=backup_path,
    )

    ha_api.set_number_value.assert_not_called()
    assert load_backup(backup_path)["curve_current"] == 0.5


def test_apply_emergency_decision_clamps_before_writing_when_active(tmp_path):
    manifest = ChannelManifest(entity_ids={
        "curve_current": "number.weishaupt_heizkurve_steigung",
        "offset_current": "number.weishaupt_heizkurve_niveau",
    })
    ha_api = MagicMock()
    decision = EmergencyBoostDecision(active=True, curve_value=99.0, offset_value=-50.0)
    backup_path = tmp_path / "backup.json"

    new_state = apply_emergency_decision(
        decision=decision, emergency_was_active=False, manifest=manifest, ha_api=ha_api,
        curve_min=0.3, curve_max=0.5, offset_min=2.0, offset_max=4.0, backup_path=backup_path,
    )

    assert new_state is True
    ha_api.set_number_value.assert_any_call("number.weishaupt_heizkurve_steigung", 0.5)
    ha_api.set_number_value.assert_any_call("number.weishaupt_heizkurve_niveau", 2.0)


def test_apply_emergency_decision_no_write_when_already_active(tmp_path):
    manifest = ChannelManifest(entity_ids={"curve_current": "number.weishaupt_heizkurve_steigung"})
    ha_api = MagicMock()
    decision = EmergencyBoostDecision(active=True, curve_value=0.5, offset_value=3.0)
    backup_path = tmp_path / "backup.json"

    new_state = apply_emergency_decision(
        decision=decision, emergency_was_active=True, manifest=manifest, ha_api=ha_api,
        curve_min=0.3, curve_max=0.5, offset_min=2.0, offset_max=4.0, backup_path=backup_path,
    )

    assert new_state is True
    ha_api.set_number_value.assert_not_called()


def test_apply_emergency_decision_restores_backup_on_transition_to_inactive(tmp_path):
    manifest = ChannelManifest(entity_ids={
        "curve_current": "number.weishaupt_heizkurve_steigung",
        "offset_current": "number.weishaupt_heizkurve_niveau",
    })
    ha_api = MagicMock()
    backup_path = tmp_path / "backup.json"
    save_backup(backup_path, {"curve_current": 0.4, "offset_current": 3.0})
    decision = EmergencyBoostDecision(active=False, curve_value=None, offset_value=None)

    new_state = apply_emergency_decision(
        decision=decision, emergency_was_active=True, manifest=manifest, ha_api=ha_api,
        curve_min=0.3, curve_max=0.5, offset_min=2.0, offset_max=4.0, backup_path=backup_path,
    )

    assert new_state is False
    ha_api.set_number_value.assert_any_call("number.weishaupt_heizkurve_steigung", 0.4)
    ha_api.set_number_value.assert_any_call("number.weishaupt_heizkurve_niveau", 3.0)


def test_apply_emergency_decision_restore_is_clamped_defense_in_depth(tmp_path):
    manifest = ChannelManifest(entity_ids={"curve_current": "number.weishaupt_heizkurve_steigung"})
    ha_api = MagicMock()
    backup_path = tmp_path / "backup.json"
    save_backup(backup_path, {"curve_current": 99.0})
    decision = EmergencyBoostDecision(active=False, curve_value=None, offset_value=None)

    apply_emergency_decision(
        decision=decision, emergency_was_active=True, manifest=manifest, ha_api=ha_api,
        curve_min=0.3, curve_max=0.5, offset_min=2.0, offset_max=4.0, backup_path=backup_path,
    )

    ha_api.set_number_value.assert_called_once_with("number.weishaupt_heizkurve_steigung", 0.5)


def test_apply_emergency_decision_rejects_nan_curve_value_on_transition_to_active(tmp_path):
    manifest = ChannelManifest(entity_ids={"curve_current": "number.weishaupt_heizkurve_steigung"})
    ha_api = MagicMock()
    decision = EmergencyBoostDecision(active=True, curve_value=float("nan"), offset_value=None)
    backup_path = tmp_path / "backup.json"

    with pytest.raises(ValueError):
        apply_emergency_decision(
            decision=decision, emergency_was_active=False, manifest=manifest, ha_api=ha_api,
            curve_min=0.3, curve_max=0.5, offset_min=2.0, offset_max=4.0, backup_path=backup_path,
        )

    ha_api.set_number_value.assert_not_called()


def test_apply_emergency_decision_no_write_when_backup_missing_on_transition(tmp_path):
    manifest = ChannelManifest(entity_ids={"curve_current": "number.weishaupt_heizkurve_steigung"})
    ha_api = MagicMock()
    backup_path = tmp_path / "backup.json"  # never created -> load_backup returns {}
    decision = EmergencyBoostDecision(active=False, curve_value=None, offset_value=None)

    new_state = apply_emergency_decision(
        decision=decision, emergency_was_active=True, manifest=manifest, ha_api=ha_api,
        curve_min=0.3, curve_max=0.5, offset_min=2.0, offset_max=4.0, backup_path=backup_path,
    )

    assert new_state is False
    ha_api.set_number_value.assert_not_called()


def test_apply_emergency_decision_steady_state_inactive_does_nothing(tmp_path):
    manifest = ChannelManifest(entity_ids={"curve_current": "number.weishaupt_heizkurve_steigung"})
    ha_api = MagicMock()
    backup_path = tmp_path / "backup.json"
    save_backup(backup_path, {"curve_current": 0.4})
    decision = EmergencyBoostDecision(active=False, curve_value=None, offset_value=None)

    new_state = apply_emergency_decision(
        decision=decision, emergency_was_active=False, manifest=manifest, ha_api=ha_api,
        curve_min=0.3, curve_max=0.5, offset_min=2.0, offset_max=4.0, backup_path=backup_path,
    )

    assert new_state is False
    ha_api.set_number_value.assert_not_called()


def _all_roles_manifest(**extra):
    return ChannelManifest(entity_ids={
        **{role: f"sensor.{role}" for role in SNAPSHOT_ROLES},
        "room_actual": "sensor.room_actual",
        **extra,
    })


def _states_with(broken: dict):
    """get_state-Ersatz: Entities aus `broken` werfen bzw. liefern den dort hinterlegten
    Wert, alle anderen 20.0."""
    def _get_state(entity_id):
        value = broken.get(entity_id, 20.0)
        if isinstance(value, Exception):
            raise value
        return value
    return _get_state


def test_read_snapshot_roles_reads_required_roles_and_checks_room_actual():
    manifest = _all_roles_manifest(outdoor_temp="sensor.outdoor_temp", flow_temperature="sensor.flow")
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.0

    read = read_snapshot_roles(manifest, ha_api)

    assert read == SnapshotRead(roles={role: 20.0 for role in SNAPSHOT_ROLES}, invalid_roles=())
    read_entities = {call.args[0] for call in ha_api.get_state.call_args_list}
    assert read_entities == {f"sensor.{role}" for role in SNAPSHOT_ROLES} | {"sensor.room_actual"}


def test_read_snapshot_roles_marks_unreadable_role_invalid_and_reads_the_others():
    ha_api = MagicMock()
    ha_api.get_state.side_effect = _states_with({"sensor.dat": ValueError("unavailable")})

    read = read_snapshot_roles(_all_roles_manifest(), ha_api)

    assert read.invalid_roles == ("dat",)
    assert set(read.roles) == set(SNAPSHOT_ROLES) - {"dat"}


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_read_snapshot_roles_marks_non_finite_value_invalid(bad):
    ha_api = MagicMock()
    ha_api.get_state.side_effect = _states_with({"sensor.dart": bad})

    assert read_snapshot_roles(_all_roles_manifest(), ha_api).invalid_roles == ("dart",)


def test_read_snapshot_roles_checks_room_actual_but_never_sends_it():
    ha_api = MagicMock()
    ha_api.get_state.side_effect = _states_with({"sensor.room_actual": ValueError("unavailable")})

    read = read_snapshot_roles(_all_roles_manifest(), ha_api)

    assert read.invalid_roles == ("room_actual",)
    assert "room_actual" not in read.roles


def test_read_snapshot_roles_skips_unmapped_roles_without_marking_them_invalid():
    manifest = ChannelManifest(entity_ids={"heat_limit": "number.h"})
    ha_api = MagicMock()
    ha_api.get_state.return_value = 16.0

    assert read_snapshot_roles(manifest, ha_api) == SnapshotRead(roles={"heat_limit": 16.0}, invalid_roles=())


def test_read_snapshot_roles_never_sends_notifications():
    # T2-13: Meldungen kommen nur noch aus der Zustellung (einmal pro Fehlerbeginn).
    ha_api = MagicMock()
    ha_api.get_state.side_effect = ValueError("unavailable")

    read = read_snapshot_roles(_all_roles_manifest(), ha_api)

    assert read.invalid_roles == tuple(SNAPSHOT_ROLES) + ("room_actual",)
    ha_api.send_notification.assert_not_called()


def test_read_snapshot_roles_includes_valid_optional_and_computed_roles():
    manifest = _all_roles_manifest(outdoor_min_24h="sensor.omin")
    ha_api = MagicMock()
    ha_api.get_state.side_effect = _states_with({"sensor.omin": 12.0})

    read = read_snapshot_roles(manifest, ha_api, computed_values={"room_target_avg_24h": 20.4})

    assert read.roles["outdoor_min_24h"] == 12.0
    assert read.roles["room_target_avg_24h"] == 20.4
    assert set(read.roles) == set(SNAPSHOT_ROLES) | set(OPTIONAL_SNAPSHOT_ROLES)


@pytest.mark.parametrize("computed", [None, float("nan")])
def test_read_snapshot_roles_omits_unusable_optional_roles_silently(computed):
    manifest = _all_roles_manifest(outdoor_min_24h="sensor.omin")
    ha_api = MagicMock()
    ha_api.get_state.side_effect = _states_with({"sensor.omin": ValueError("unknown")})

    read = read_snapshot_roles(manifest, ha_api, computed_values={"room_target_avg_24h": computed})

    assert set(read.roles) == set(SNAPSHOT_ROLES)
    assert read.invalid_roles == ()


def test_publish_snapshot_sends_one_schema_2_message():
    mqtt_client = MagicMock()

    publish_snapshot(mqtt_client, seq="s1", trigger="daily", roles={"dat": 4.0})

    mqtt_client.publish_snapshot.assert_called_once()
    payload = mqtt_client.publish_snapshot.call_args.args[0]
    assert payload["schema"] == 2
    assert payload["seq"] == "s1"
    assert payload["trigger"] == "daily"
    assert payload["roles"] == {"dat": 4.0}
    assert datetime.fromisoformat(payload["ts"]).tzinfo is not None

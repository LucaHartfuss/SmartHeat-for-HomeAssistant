import json
import threading
from unittest.mock import MagicMock

import pytest

from heizungsbruecke.__main__ import (
    _check_failsafe_staleness,
    _load_failsafe_ctx,
    _load_failsafe_ctx_safe,
    _make_down_callback,
    _record_valid_message,
    _resolve_effective_options,
    _run_tick,
    _save_failsafe_ctx,
    _validate_boost_config,
    _validate_derived_sensor_prerequisites,
)
from heizungsbruecke.failsafe import FailsafeState
from heizungsbruecke.profiles import UnknownProfileError
from heizungsbruecke.manifest import ChannelManifest


def _base_options(**overrides):
    options = {
        "curve_min": 0.2,
        "curve_max": 0.8,
        "offset_min": 0.0,
        "offset_max": 5.0,
        "boost_curve_value": 0.5,
        "boost_offset_value": 2.0,
    }
    options.update(overrides)
    return options


def test_validate_boost_config_returns_none_when_within_range():
    assert _validate_boost_config(_base_options()) is None


def test_validate_boost_config_flags_curve_value_above_max():
    error = _validate_boost_config(_base_options(boost_curve_value=99.0))
    assert error is not None
    assert "boost_curve_value" in error


def test_validate_boost_config_flags_curve_value_below_min():
    error = _validate_boost_config(_base_options(boost_curve_value=-1.0))
    assert error is not None
    assert "boost_curve_value" in error


def test_validate_boost_config_flags_offset_value_out_of_range():
    error = _validate_boost_config(_base_options(boost_offset_value=999.0))
    assert error is not None
    assert "boost_offset_value" in error


def test_validate_boost_config_accepts_boundary_values():
    assert _validate_boost_config(_base_options(boost_curve_value=0.2)) is None
    assert _validate_boost_config(_base_options(boost_curve_value=0.8)) is None
    assert _validate_boost_config(_base_options(boost_offset_value=0.0)) is None
    assert _validate_boost_config(_base_options(boost_offset_value=5.0)) is None


def test_run_tick_publishes_snapshot_and_returns_boost_state():
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual",
        "room_target": "sensor.room_target",
    })
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.0  # actual == target -> boost stays inactive
    mqtt_client = MagicMock()
    options = _base_options()
    write_lock = threading.Lock()

    new_state = _run_tick(manifest, ha_api, mqtt_client, options, write_lock, boost_was_active=False)

    assert new_state is False
    assert mqtt_client.publish_value.call_count == 2
    published_roles = {call.kwargs["role"] for call in mqtt_client.publish_value.call_args_list}
    assert published_roles == {"room_actual", "room_target"}


def _broken_sensor_setup():
    """A manifest whose `dat` sensor is dead (HA reports 'unavailable' -> float() raises),
    while the room roles the boost failsafe needs still read fine.
    """
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual",
        "room_target": "sensor.room_target",
        "curve_current": "number.curve",
        "offset_current": "number.offset",
        "dat": "sensor.kaputt",
    })
    ha_api = MagicMock()

    def _get_state(entity_id):
        if entity_id == "sensor.kaputt":
            raise ValueError("could not convert string to float: 'unavailable'")
        return {"sensor.room_actual": 18.0, "sensor.room_target": 21.0,
                "number.curve": 0.5, "number.offset": 2.0}[entity_id]

    ha_api.get_state.side_effect = _get_state
    return manifest, ha_api


def test_run_tick_still_evaluates_boost_when_one_sensor_is_broken(tmp_path, monkeypatch):
    # I3: a single dead sensor must not abort the whole tick before the boost failsafe runs.
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    manifest, ha_api = _broken_sensor_setup()

    new_state = _run_tick(manifest, ha_api, MagicMock(), _base_options(), threading.Lock(),
                          boost_was_active=False)

    assert new_state is True
    ha_api.set_number_value.assert_any_call("number.curve", 0.5)


def test_run_tick_passes_configured_notify_service_through(tmp_path, monkeypatch):
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    manifest, ha_api = _broken_sensor_setup()
    options = _base_options(notify_service="notify.mobile_app_lucas_iphone")

    _run_tick(manifest, ha_api, MagicMock(), options, threading.Lock(), boost_was_active=False)

    ha_api.send_notification.assert_called_once()
    assert ha_api.send_notification.call_args.args[0] == "notify.mobile_app_lucas_iphone"


def test_run_tick_sends_no_notification_when_option_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    manifest, ha_api = _broken_sensor_setup()

    _run_tick(manifest, ha_api, MagicMock(), _base_options(), threading.Lock(), boost_was_active=False)

    ha_api.send_notification.assert_not_called()


def test_run_tick_propagates_exceptions_for_caller_to_handle():
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual",
        "room_target": "sensor.room_target",
    })
    ha_api = MagicMock()
    # HA completely unreachable: publish_snapshot skips every role (I3), but the boost
    # failsafe's own reads still fail -- and that error must reach the caller.
    ha_api.get_state.side_effect = RuntimeError("HA nicht erreichbar")
    mqtt_client = MagicMock()
    options = _base_options()
    write_lock = threading.Lock()

    # _run_tick itself must not swallow the error -- main()'s while-loop try/except
    # (I2) is what's responsible for catching, logging and continuing to the next tick.
    with pytest.raises(RuntimeError):
        _run_tick(manifest, ha_api, mqtt_client, options, write_lock, boost_was_active=False)


def test_resolve_effective_options_uses_profile_defaults_when_clamps_absent():
    options = {"profile": "vaillant_gastherme_heizkoerper"}

    effective = _resolve_effective_options(options)

    assert effective["curve_min"] == 0.4
    assert effective["curve_max"] == 1.5
    assert effective["offset_min"] == 20.0
    assert effective["offset_max"] == 30.0
    assert effective["boost_threshold_k"] == 0.5
    assert effective["boost_curve_value"] == 1.5
    assert effective["boost_offset_value"] == 30.0
    assert effective["profile"] == "vaillant_gastherme_heizkoerper"


def test_resolve_effective_options_ignores_explicit_override():
    options = {"profile": "vaillant_gastherme_heizkoerper", "offset_max": 28.0, "boost_curve_value": 0.1}

    effective = _resolve_effective_options(options)

    assert effective["offset_max"] == 30.0  # Profil-Default gewinnt, Override wird ignoriert
    assert effective["boost_curve_value"] == 1.5


def test_resolve_effective_options_raises_for_profile_without_defaults():
    options = {"profile": "weishaupt_waermepumpe_fussbodenheizung"}

    with pytest.raises(UnknownProfileError):
        _resolve_effective_options(options)


def test_validate_derived_sensor_prerequisites_returns_none_when_present():
    options = {"entity_room_actual": "sensor.rt", "entity_outdoor_temp": "sensor.outdoor"}

    assert _validate_derived_sensor_prerequisites(options) is None


def test_validate_derived_sensor_prerequisites_flags_missing_outdoor_temp():
    options = {"entity_room_actual": "sensor.rt"}

    error = _validate_derived_sensor_prerequisites(options)

    assert error is not None
    assert "entity_outdoor_temp" in error


def test_validate_derived_sensor_prerequisites_flags_missing_room_actual():
    options = {"entity_outdoor_temp": "sensor.outdoor"}

    error = _validate_derived_sensor_prerequisites(options)

    assert error is not None
    assert "entity_room_actual" in error


def test_load_failsafe_ctx_defaults_when_no_file(tmp_path):
    ctx = _load_failsafe_ctx(tmp_path / "does_not_exist.json")

    assert ctx["last_valid_update"] is None
    assert ctx["state"] == FailsafeState(active=False, recovery_count=0)


def test_save_and_load_failsafe_ctx_round_trip(tmp_path):
    path = tmp_path / "failsafe_state.json"
    ctx = {"last_valid_update": 12345.0, "state": FailsafeState(active=True, recovery_count=1)}

    _save_failsafe_ctx(ctx, path)
    loaded = _load_failsafe_ctx(path)

    assert loaded == ctx


def test_load_failsafe_ctx_safe_defaults_when_no_file(tmp_path):
    ctx = _load_failsafe_ctx_safe(tmp_path / "does_not_exist.json")

    assert ctx["last_valid_update"] is None
    assert ctx["state"] == FailsafeState(active=False, recovery_count=0)


def test_load_failsafe_ctx_safe_falls_back_on_corrupt_file(tmp_path):
    # Simulates power loss on the Pi's SD card mid-write: a truncated/corrupt state
    # file must not crash the whole add-on at startup.
    path = tmp_path / "failsafe_state.json"
    path.write_bytes(b"{not valid json..")

    ctx = _load_failsafe_ctx_safe(path)

    assert ctx["last_valid_update"] is None
    assert ctx["state"] == FailsafeState(active=False, recovery_count=0)


def test_load_failsafe_ctx_safe_passes_through_valid_file(tmp_path):
    path = tmp_path / "failsafe_state.json"
    ctx = {"last_valid_update": 12345.0, "state": FailsafeState(active=True, recovery_count=1)}
    _save_failsafe_ctx(ctx, path)

    loaded = _load_failsafe_ctx_safe(path)

    assert loaded == ctx


def test_record_valid_message_updates_timestamp_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr("time.time", lambda: 5000.0)
    path = tmp_path / "failsafe_state.json"
    ctx = {"last_valid_update": None, "state": FailsafeState(active=False, recovery_count=0)}
    mqtt_client = MagicMock()

    _record_valid_message(ctx, mqtt_client, path)

    assert ctx["last_valid_update"] == 5000.0
    assert ctx["state"] == FailsafeState(active=False, recovery_count=0)
    assert _load_failsafe_ctx(path) == ctx
    mqtt_client.publish_status.assert_not_called()  # state didn't change (was already inactive)


def test_record_valid_message_publishes_status_when_failsafe_exits(tmp_path, monkeypatch):
    monkeypatch.setattr("time.time", lambda: 5000.0)
    path = tmp_path / "failsafe_state.json"
    ctx = {"last_valid_update": 1.0, "state": FailsafeState(active=True, recovery_count=1)}
    mqtt_client = MagicMock()

    _record_valid_message(ctx, mqtt_client, path)

    assert ctx["state"] == FailsafeState(active=False, recovery_count=0)
    mqtt_client.publish_status.assert_called_once_with("failsafe", "OFF")


def test_check_failsafe_staleness_activates_and_publishes_status_when_stale(tmp_path, monkeypatch):
    monkeypatch.setattr("time.time", lambda: 5000.0)
    path = tmp_path / "failsafe_state.json"
    ctx = {"last_valid_update": 100.0, "state": FailsafeState(active=False, recovery_count=0)}
    mqtt_client = MagicMock()

    _check_failsafe_staleness(ctx, stale_after_seconds=3600.0, mqtt_client=mqtt_client, failsafe_path=path)

    assert ctx["state"] == FailsafeState(active=True, recovery_count=0)
    mqtt_client.publish_status.assert_called_once_with("failsafe", "ON")
    assert _load_failsafe_ctx(path) == ctx


def test_check_failsafe_staleness_noop_when_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr("time.time", lambda: 5000.0)
    path = tmp_path / "failsafe_state.json"
    ctx = {"last_valid_update": 4999.0, "state": FailsafeState(active=False, recovery_count=0)}
    mqtt_client = MagicMock()

    _check_failsafe_staleness(ctx, stale_after_seconds=3600.0, mqtt_client=mqtt_client, failsafe_path=path)

    assert ctx["state"] == FailsafeState(active=False, recovery_count=0)
    mqtt_client.publish_status.assert_not_called()


def test_make_down_callback_records_valid_message_after_successful_handling(tmp_path, monkeypatch):
    # This is the wiring itself: a successful handle_down_message must be followed,
    # inside the same write_lock, by _record_valid_message(failsafe_ctx, mqtt_client, FAILSAFE_PATH).
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    failsafe_path = tmp_path / "failsafe_state.json"
    monkeypatch.setattr("heizungsbruecke.__main__.FAILSAFE_PATH", failsafe_path)
    manifest = ChannelManifest(entity_ids={"curve_current": "number.curve"})
    ha_api = MagicMock()
    options = _base_options()
    write_lock = threading.Lock()
    failsafe_ctx = {"last_valid_update": None, "state": FailsafeState(active=False, recovery_count=0)}
    mqtt_client = MagicMock()

    recorded_calls = []
    monkeypatch.setattr(
        "heizungsbruecke.__main__._record_valid_message",
        lambda ctx, client, path: recorded_calls.append((ctx, client, path)),
    )

    callback = _make_down_callback("curve_current", manifest, ha_api, options, write_lock, failsafe_ctx, mqtt_client)
    message = MagicMock()
    message.payload = json.dumps({"v": 0.5})

    callback(client=MagicMock(), userdata=None, message=message)

    assert ha_api.set_number_value.call_count == 1  # handle_down_message did succeed
    assert recorded_calls == [(failsafe_ctx, mqtt_client, failsafe_path)]


def test_make_down_callback_does_not_record_valid_message_when_handling_fails(tmp_path, monkeypatch):
    # Mirror image of the above: if handle_down_message raises (e.g. HA unreachable),
    # a bad/failed message must not be mistaken for a valid live update.
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    monkeypatch.setattr("heizungsbruecke.__main__.FAILSAFE_PATH", tmp_path / "failsafe_state.json")
    manifest = ChannelManifest(entity_ids={"curve_current": "number.curve"})
    ha_api = MagicMock()
    ha_api.set_number_value.side_effect = RuntimeError("HA nicht erreichbar")
    options = _base_options()
    write_lock = threading.Lock()
    failsafe_ctx = {"last_valid_update": None, "state": FailsafeState(active=False, recovery_count=0)}
    mqtt_client = MagicMock()

    recorded_calls = []
    monkeypatch.setattr(
        "heizungsbruecke.__main__._record_valid_message",
        lambda ctx, client, path: recorded_calls.append((ctx, client, path)),
    )

    callback = _make_down_callback("curve_current", manifest, ha_api, options, write_lock, failsafe_ctx, mqtt_client)
    message = MagicMock()
    message.payload = json.dumps({"v": 0.5})

    callback(client=MagicMock(), userdata=None, message=message)  # must not raise -- caught and logged

    assert recorded_calls == []

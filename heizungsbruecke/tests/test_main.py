import threading
from unittest.mock import MagicMock

import pytest

from heizungsbruecke.__main__ import _resolve_effective_options, _run_tick, _validate_boost_config
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
    assert effective["profile"] == "vaillant_gastherme_heizkoerper"


def test_resolve_effective_options_keeps_explicit_override():
    options = {"profile": "vaillant_gastherme_heizkoerper", "offset_max": 28.0}

    effective = _resolve_effective_options(options)

    assert effective["offset_max"] == 28.0
    assert effective["curve_min"] == 0.4


def test_resolve_effective_options_raises_for_inactive_profile_without_full_override():
    options = {"profile": "weishaupt_waermepumpe_fussbodenheizung"}

    with pytest.raises(UnknownProfileError):
        _resolve_effective_options(options)

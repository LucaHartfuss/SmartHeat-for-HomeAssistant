import json
import logging
import threading
import time
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import requests

import heizungsbruecke.__main__ as main_module
from heizungsbruecke.ha_trigger_client import HaTriggerClient
from heizungsbruecke.__main__ import (
    TenantNotEntitledError,
    _check_entitlement,
    _connect_mqtt_with_retry,
    _end_emergency_boost_if_active,
    _ensure_derived_sensors_with_retry,
    _handle_ack,
    _handle_ack_timeout,
    _is_configured,
    _load_failsafe_ctx,
    _load_failsafe_ctx_safe,
    _load_options_safe,
    _make_down_callback,
    _maybe_publish_full_snapshot,
    _maybe_publish_telemetry,
    _resolve_effective_options,
    _run_bridge,
    _run_local_check,
    _save_emergency_active_if_changed,
    _save_failsafe_ctx,
    _validate_boost_config,
    _validate_derived_sensor_prerequisites,
    _validate_local_check_interval,
    _validate_telemetry_interval,
)
from heizungsbruecke.backup_store import load_backup, save_backup
from heizungsbruecke.failsafe import FailsafeState
from heizungsbruecke.profiles import UnknownProfileError
from heizungsbruecke.manifest import ChannelManifest


@pytest.fixture(autouse=True)
def _reset_telemetry_marker(monkeypatch):
    # _last_telemetry_publish_ts is in-memory/module-level (not per-tmp_path like
    # backup.json), so without this reset it leaks across tests and makes
    # publish_telemetry calls order-dependent.
    monkeypatch.setattr("heizungsbruecke.__main__._last_telemetry_publish_ts", None)


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


def test_is_configured_true_when_all_required_fields_present():
    options = {
        "tenant_id": "wohnung1", "profile": "vaillant_gastherme_heizkoerper",
        "mqtt_username": "wohnung1_a1b2c3d4", "mqtt_password": "geheim",
        "entity_room_actual": "sensor.rt", "entity_room_target": "sensor.target_rt",
        "entity_curve_current": "number.curve", "entity_offset_current": "number.offset",
        "entity_outdoor_temp": "sensor.outdoor", "entity_heat_limit": "number.heat_limit",
    }
    assert _is_configured(options) is True


def test_is_configured_false_when_a_required_field_is_missing():
    assert _is_configured({"tenant_id": "wohnung1"}) is False


def test_is_configured_false_for_empty_options():
    assert _is_configured({}) is False


def test_run_bridge_returns_true_when_not_configured(caplog):
    with caplog.at_level("INFO"):
        result = _run_bridge({}, MagicMock())

    assert result is True
    assert "Add-on ist noch nicht eingerichtet" in caplog.text


def test_run_bridge_returns_false_on_genuine_validation_error(monkeypatch, caplog):
    # Unlike the "not configured" case above, an add-on that IS configured but fails
    # validation (here: an unknown profile) is a genuine startup error -- main() must
    # be able to tell the two apart to give the Supervisor a non-zero exit code.
    monkeypatch.setattr(
        "heizungsbruecke.__main__.requests.get",
        lambda url, timeout: _FakeResponse({"active": True}),
    )
    options = {
        "tenant_id": "wohnung1", "profile": "does-not-exist",
        "mqtt_username": "wohnung1_a1b2c3d4", "mqtt_password": "geheim",
        "entity_room_actual": "sensor.rt", "entity_room_target": "sensor.target_rt",
        "entity_curve_current": "number.curve", "entity_offset_current": "number.offset",
        "entity_outdoor_temp": "sensor.outdoor", "entity_heat_limit": "number.heat_limit",
    }
    with caplog.at_level("ERROR"):
        result = _run_bridge(options, MagicMock())

    assert result is False
    assert "FEHLER" in caplog.text


def test_load_options_safe_defaults_when_no_file(tmp_path):
    assert _load_options_safe(tmp_path / "does_not_exist.json") == {}


def test_load_options_safe_falls_back_on_corrupt_file(tmp_path):
    # Simulates power loss on the Pi's SD card mid-write: a truncated/corrupt options
    # file must not crash the whole add-on before it can even report its state.
    path = tmp_path / "options.json"
    path.write_bytes(b"{not valid json..")

    assert _load_options_safe(path) == {}


def test_load_options_safe_passes_through_valid_file(tmp_path):
    path = tmp_path / "options.json"
    path.write_text(json.dumps({"tenant_id": "wohnung1"}))

    assert _load_options_safe(path) == {"tenant_id": "wohnung1"}


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


def test_run_local_check_evaluates_boost_without_publishing_when_nothing_changed(tmp_path, monkeypatch):
    # Publishing becomes conditional once Task 8 wires _maybe_publish_full_snapshot into
    # the end of this same function -- pre-seed last_published_target_rt to match the
    # read room_target so this test stays valid after that (it isolates the boost-only
    # path from the deliberately separate publish trigger, tested on its own in Task 8).
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    save_backup(backup_path, {"last_published_target_rt": 20.0})
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual",
        "room_target": "sensor.room_target",
    })
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.0  # actual == target, no previous target -> boost stays inactive
    mqtt_client = MagicMock()
    options = _base_options()
    write_lock = threading.Lock()

    new_state = _run_local_check(manifest, ha_api, mqtt_client, options, write_lock, boost_was_active=False, room_target=20.0)

    assert new_state is False
    mqtt_client.publish_value.assert_not_called()


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


def test_run_local_check_propagates_exceptions_for_caller_to_handle():
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
        _run_local_check(manifest, ha_api, mqtt_client, options, write_lock, boost_was_active=False, room_target=21.0)


def test_run_local_check_persists_room_target_for_next_checks_comparison(tmp_path, monkeypatch):
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    manifest = ChannelManifest(entity_ids={
        "room_actual": "climate.wohnzimmer_thermostat::current_temperature",
        "room_target": "climate.wohnzimmer_thermostat::temperature",
    })
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda entity_id: {
        "climate.wohnzimmer_thermostat::current_temperature": 19.5,
        "climate.wohnzimmer_thermostat::temperature": 21.0,
    }[entity_id]
    mqtt_client = MagicMock()
    options = {
        "boost_threshold_k": 0.5, "boost_curve_value": 1.5, "boost_offset_value": 30.0,
        "curve_min": 0.4, "curve_max": 1.5, "offset_min": 20.0, "offset_max": 30.0,
    }

    _run_local_check(manifest, ha_api, mqtt_client, options, threading.Lock(), boost_was_active=False, room_target=21.0)

    assert load_backup(tmp_path / "backup.json")["last_room_target"] == 21.0


def test_run_local_check_triggers_boost_on_target_raise_between_checks(tmp_path, monkeypatch):
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    save_backup(backup_path, {"last_room_target": 20.0})
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual",
        "room_target": "sensor.room_target",
        "curve_current": "number.curve",
        "offset_current": "number.offset",
    })
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda entity_id: {
        "sensor.room_actual": 19.0, "sensor.room_target": 21.0,
    }[entity_id]
    mqtt_client = MagicMock()
    options = {
        "boost_threshold_k": 0.5, "boost_curve_value": 1.5, "boost_offset_value": 30.0,
        "curve_min": 0.4, "curve_max": 1.5, "offset_min": 20.0, "offset_max": 30.0,
    }

    boost_was_active = _run_local_check(manifest, ha_api, mqtt_client, options, threading.Lock(), boost_was_active=False, room_target=21.0)

    assert boost_was_active is True
    ha_api.set_number_value.assert_any_call("number.curve", 1.5)
    ha_api.set_number_value.assert_any_call("number.offset", 30.0)


def test_run_local_check_uses_room_target_parameter_without_reading_it_live(tmp_path, monkeypatch):
    # Design-Spec 2026-09-22 (Stable-Target-Cache): room_target must come in as an
    # explicit parameter -- _run_local_check itself must no longer read it live via
    # ha_api.get_state, only room_actual. Wired to raise for the room_target entity_id
    # specifically, so this test fails loudly if that read still happens.
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual", "room_target": "sensor.room_target",
    })
    ha_api = MagicMock()

    def _get_state(entity_id):
        if entity_id == "sensor.room_target":
            raise AssertionError("_run_local_check must not read room_target live anymore")
        return {"sensor.room_actual": 19.0}[entity_id]

    ha_api.get_state.side_effect = _get_state
    options = _base_options()

    new_state = _run_local_check(
        manifest, ha_api, MagicMock(), options, threading.Lock(), boost_was_active=False, room_target=21.0,
    )

    assert new_state is False  # no previous_room_target recorded yet -> boost stays inactive


def test_run_local_check_returns_unchanged_state_when_room_target_parameter_is_none(tmp_path, monkeypatch, caplog):
    # Boot-priming edge case (Design-Spec 2026-09-22, Abschnitt 2): the stable-target
    # cache can still be unpopulated (e.g. a failed boot-time live read) even though
    # both roles are mapped -- must no-op instead of crashing on a None comparison
    # inside decide_boost/_maybe_publish_full_snapshot.
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual", "room_target": "sensor.room_target",
    })
    ha_api = MagicMock()

    with caplog.at_level(logging.WARNING):
        new_state = _run_local_check(
            manifest, ha_api, MagicMock(), _base_options(), threading.Lock(), boost_was_active=True, room_target=None,
        )

    assert new_state is True  # unchanged, no decision made
    ha_api.get_state.assert_not_called()
    # Whole-Branch-Review Minor #5 (final-review-report.md): this no-op must be visible
    # in the log -- otherwise it is indistinguishable from a regular skip once deployed.
    assert "room_target=None" in caplog.text


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
    assert effective["daily_trigger_time"] == "12:00"
    assert effective["day_avg_window_start"] == "14:00"
    assert effective["day_avg_window_end"] == "17:00"
    assert effective["night_avg_window_start"] == "04:00"
    assert effective["night_avg_window_end"] == "07:00"
    assert effective["avg_window_hours"] == 3.0


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


def test_ensure_derived_sensors_with_retry_returns_result_on_first_success(monkeypatch):
    expected = {"dat": "sensor.dat"}
    monkeypatch.setattr(
        "heizungsbruecke.__main__.derived_sensors.ensure_all", lambda **kwargs: expected
    )
    sleeps = []
    monkeypatch.setattr("heizungsbruecke.__main__.time.sleep", sleeps.append)

    options = {
        "tenant_id": "t1", "entity_room_actual": "sensor.rt", "entity_outdoor_temp": "sensor.outdoor",
        "avg_window_hours": 3.0,
    }
    result = _ensure_derived_sensors_with_retry(MagicMock(), options)

    assert result == expected
    assert sleeps == []


def test_ensure_derived_sensors_with_retry_recovers_after_transient_failures(monkeypatch):
    expected = {"dat": "sensor.dat"}
    attempts = {"count": 0}

    def flaky_ensure_all(**kwargs):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ConnectionError("HA Core noch nicht erreichbar")
        return expected

    monkeypatch.setattr("heizungsbruecke.__main__.derived_sensors.ensure_all", flaky_ensure_all)
    sleeps = []
    monkeypatch.setattr("heizungsbruecke.__main__.time.sleep", sleeps.append)

    options = {
        "tenant_id": "t1", "entity_room_actual": "sensor.rt", "entity_outdoor_temp": "sensor.outdoor",
        "avg_window_hours": 3.0,
    }
    result = _ensure_derived_sensors_with_retry(MagicMock(), options)

    assert result == expected
    assert attempts["count"] == 3
    assert sleeps == [5, 10]


def test_ensure_derived_sensors_with_retry_raises_last_error_after_exhausting_retries(monkeypatch):
    def always_fails(**kwargs):
        raise ConnectionError("HA Core dauerhaft nicht erreichbar")

    monkeypatch.setattr("heizungsbruecke.__main__.derived_sensors.ensure_all", always_fails)
    monkeypatch.setattr("heizungsbruecke.__main__.time.sleep", lambda seconds: None)

    options = {
        "tenant_id": "t1", "entity_room_actual": "sensor.rt", "entity_outdoor_temp": "sensor.outdoor",
        "avg_window_hours": 3.0,
    }

    with pytest.raises(ConnectionError, match="HA Core dauerhaft nicht erreichbar"):
        _ensure_derived_sensors_with_retry(MagicMock(), options)


def test_connect_mqtt_with_retry_returns_client_on_first_success(monkeypatch):
    fake_client = MagicMock()
    monkeypatch.setattr("heizungsbruecke.__main__.BridgeMqttClient", lambda **kwargs: fake_client)
    sleeps = []
    monkeypatch.setattr("heizungsbruecke.__main__.time.sleep", sleeps.append)

    options = {"tenant_id": "t1", "mqtt_username": "u", "mqtt_password": "p"}
    result = _connect_mqtt_with_retry(options)

    assert result is fake_client
    assert sleeps == []


def test_connect_mqtt_with_retry_recovers_after_transient_failures(monkeypatch):
    fake_client = MagicMock()
    attempts = {"count": 0}

    def flaky_client(**kwargs):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ConnectionRefusedError("cloudflared_access_mqtt noch nicht bereit")
        return fake_client

    monkeypatch.setattr("heizungsbruecke.__main__.BridgeMqttClient", flaky_client)
    sleeps = []
    monkeypatch.setattr("heizungsbruecke.__main__.time.sleep", sleeps.append)

    options = {"tenant_id": "t1", "mqtt_username": "u", "mqtt_password": "p"}
    result = _connect_mqtt_with_retry(options)

    assert result is fake_client
    assert attempts["count"] == 3
    assert sleeps == [5, 10]


def test_connect_mqtt_with_retry_raises_last_error_after_exhausting_retries(monkeypatch):
    def always_fails(**kwargs):
        raise ConnectionRefusedError("Broker dauerhaft nicht erreichbar")

    monkeypatch.setattr("heizungsbruecke.__main__.BridgeMqttClient", always_fails)
    monkeypatch.setattr("heizungsbruecke.__main__.time.sleep", lambda seconds: None)

    options = {"tenant_id": "t1", "mqtt_username": "u", "mqtt_password": "p"}

    with pytest.raises(ConnectionRefusedError, match="Broker dauerhaft nicht erreichbar"):
        _connect_mqtt_with_retry(options)


def test_load_failsafe_ctx_defaults_when_no_file(tmp_path):
    ctx = _load_failsafe_ctx(tmp_path / "does_not_exist.json", tmp_path / "no_backup.json")

    assert ctx["state"] == FailsafeState(active=False, awaiting_seq=None)
    assert ctx["emergency_boost_active"] is False


def test_save_and_load_failsafe_ctx_round_trip(tmp_path):
    # Design-Spec 2026-09-23, Abschnitt 4: `state.active` (failsafe_state.json) UND
    # `emergency_boost_active` (backup.json) ueberleben einen Neustart. Nur
    # `awaiting_seq` (in-Prozess-Timer-Bezug) startet bewusst frisch (siehe
    # _save_failsafe_ctx-Docstring).
    path = tmp_path / "failsafe_state.json"
    backup_path = tmp_path / "backup.json"
    ctx = {"state": FailsafeState(active=True, awaiting_seq="seq-1"), "emergency_boost_active": True}

    _save_failsafe_ctx(ctx, path)
    _save_emergency_active_if_changed(ctx["emergency_boost_active"], backup_path)
    loaded = _load_failsafe_ctx(path, backup_path)

    assert loaded["state"] == FailsafeState(active=True, awaiting_seq=None)
    assert loaded["emergency_boost_active"] is True


def test_load_failsafe_ctx_safe_defaults_when_no_file(tmp_path):
    ctx = _load_failsafe_ctx_safe(tmp_path / "does_not_exist.json", tmp_path / "no_backup.json")

    assert ctx["state"] == FailsafeState(active=False, awaiting_seq=None)
    assert ctx["emergency_boost_active"] is False


def test_load_failsafe_ctx_safe_falls_back_on_corrupt_file(tmp_path):
    # Simulates power loss on the Pi's SD card mid-write: a truncated/corrupt state
    # file must not crash the whole add-on at startup.
    path = tmp_path / "failsafe_state.json"
    path.write_bytes(b"{not valid json..")

    ctx = _load_failsafe_ctx_safe(path, tmp_path / "no_backup.json")

    assert ctx["state"] == FailsafeState(active=False, awaiting_seq=None)
    assert ctx["emergency_boost_active"] is False


def test_load_failsafe_ctx_safe_still_reads_emergency_flag_when_failsafe_file_corrupt(tmp_path):
    # A corrupt failsafe_state.json must not also discard a valid, persisted
    # emergency_boost_active=True from backup.json -- otherwise boot-priming (which then
    # sees Notbetrieb inactive) could never restore the device from its max-heat values
    # (same stranding as final-review finding C1, Scenario A).
    path = tmp_path / "failsafe_state.json"
    path.write_bytes(b"{not valid json..")
    backup_path = tmp_path / "backup.json"
    save_backup(backup_path, {"emergency_boost_active": True})

    ctx = _load_failsafe_ctx_safe(path, backup_path)

    assert ctx["state"] == FailsafeState(active=False, awaiting_seq=None)
    assert ctx["emergency_boost_active"] is True


def test_load_failsafe_ctx_safe_falls_back_on_corrupt_backup_file(tmp_path):
    path = tmp_path / "failsafe_state.json"
    save_backup(path, {"failsafe_active": True})
    backup_path = tmp_path / "backup.json"
    backup_path.write_bytes(b"{not valid json..")

    ctx = _load_failsafe_ctx_safe(path, backup_path)

    assert ctx["state"] == FailsafeState(active=True, awaiting_seq=None)
    assert ctx["emergency_boost_active"] is False


def test_load_failsafe_ctx_safe_passes_through_valid_file(tmp_path):
    path = tmp_path / "failsafe_state.json"
    backup_path = tmp_path / "backup.json"
    ctx = {"state": FailsafeState(active=True, awaiting_seq="seq-1"), "emergency_boost_active": True}
    _save_failsafe_ctx(ctx, path)
    _save_emergency_active_if_changed(ctx["emergency_boost_active"], backup_path)

    loaded = _load_failsafe_ctx_safe(path, backup_path)

    assert loaded["state"] == FailsafeState(active=True, awaiting_seq=None)
    assert loaded["emergency_boost_active"] is True


def test_make_down_callback_acks_matching_seq_after_successful_handling(tmp_path, monkeypatch):
    # This is the wiring itself: a successful handle_down_message must be followed,
    # inside the same write_lock, by _handle_ack(...) with the payload's seq.
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    failsafe_path = tmp_path / "failsafe_state.json"
    monkeypatch.setattr("heizungsbruecke.__main__.FAILSAFE_PATH", failsafe_path)
    manifest = ChannelManifest(entity_ids={"curve_current": "number.curve"})
    ha_api = MagicMock()
    options = _base_options()
    write_lock = threading.Lock()
    failsafe_ctx = {"state": FailsafeState(active=True, awaiting_seq="seq-1"), "emergency_boost_active": False}
    mqtt_client = MagicMock()

    callback = _make_down_callback("curve_current", manifest, ha_api, options, write_lock, failsafe_ctx, mqtt_client)
    message = MagicMock()
    message.payload = json.dumps({"v": 0.5, "seq": "seq-1"})
    message.retain = False

    callback(client=MagicMock(), userdata=None, message=message)

    assert ha_api.set_number_value.call_count == 1  # handle_down_message did succeed
    assert failsafe_ctx["state"] == FailsafeState(active=False, awaiting_seq=None)
    mqtt_client.publish_status.assert_any_call("failsafe", "OFF")


def test_make_down_callback_rejects_nan_without_crashing_or_poisoning_backup(tmp_path, monkeypatch):
    # I2 failure-chain closure (whole-branch review): a NaN down-message value must not
    # crash the MQTT callback and must not get persisted into backup.json.
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    monkeypatch.setattr("heizungsbruecke.__main__.FAILSAFE_PATH", tmp_path / "failsafe_state.json")
    manifest = ChannelManifest(entity_ids={"curve_current": "number.curve"})
    ha_api = MagicMock()
    options = _base_options()
    write_lock = threading.Lock()
    failsafe_ctx = {"state": FailsafeState(active=True, awaiting_seq="seq-1"), "emergency_boost_active": False}
    mqtt_client = MagicMock()

    callback = _make_down_callback("curve_current", manifest, ha_api, options, write_lock, failsafe_ctx, mqtt_client)
    message = MagicMock()
    message.payload = json.dumps({"v": float("nan"), "seq": "seq-1"})
    message.retain = False

    callback(client=MagicMock(), userdata=None, message=message)  # must not raise

    ha_api.set_number_value.assert_not_called()
    assert load_backup(backup_path) == {}
    # handle_down_message raised before _handle_ack ever ran -- a rejected message must
    # not be mistaken for a valid live update.
    assert failsafe_ctx["state"] == FailsafeState(active=True, awaiting_seq="seq-1")


def test_make_down_callback_does_not_ack_when_handling_fails(tmp_path, monkeypatch):
    # Mirror image of the above: if handle_down_message raises (e.g. HA unreachable), a
    # bad/failed message must not be mistaken for a valid live update.
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    monkeypatch.setattr("heizungsbruecke.__main__.FAILSAFE_PATH", tmp_path / "failsafe_state.json")
    manifest = ChannelManifest(entity_ids={"curve_current": "number.curve"})
    ha_api = MagicMock()
    ha_api.set_number_value.side_effect = RuntimeError("HA nicht erreichbar")
    options = _base_options()
    write_lock = threading.Lock()
    failsafe_ctx = {"state": FailsafeState(active=True, awaiting_seq="seq-1"), "emergency_boost_active": False}
    mqtt_client = MagicMock()

    callback = _make_down_callback("curve_current", manifest, ha_api, options, write_lock, failsafe_ctx, mqtt_client)
    message = MagicMock()
    message.payload = json.dumps({"v": 0.5, "seq": "seq-1"})
    message.retain = False

    callback(client=MagicMock(), userdata=None, message=message)  # must not raise -- caught and logged

    assert failsafe_ctx["state"] == FailsafeState(active=True, awaiting_seq="seq-1")


def test_make_down_callback_skips_retained_replay_without_acking(tmp_path, monkeypatch, caplog):
    # A retained MQTT message is the broker replaying the last-published value on every
    # (re)subscribe, not a fresh signal from the server -- must not be mistaken for an ack.
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    monkeypatch.setattr("heizungsbruecke.__main__.FAILSAFE_PATH", tmp_path / "failsafe_state.json")
    manifest = ChannelManifest(entity_ids={"curve_current": "number.curve"})
    ha_api = MagicMock()
    options = _base_options()
    write_lock = threading.Lock()
    failsafe_ctx = {"state": FailsafeState(active=True, awaiting_seq="seq-1"), "emergency_boost_active": False}
    mqtt_client = MagicMock()

    callback = _make_down_callback("curve_current", manifest, ha_api, options, write_lock, failsafe_ctx, mqtt_client)
    message = MagicMock()
    message.payload = json.dumps({"v": 0.5, "seq": "seq-1"})
    message.retain = True

    with caplog.at_level(logging.INFO):
        callback(client=MagicMock(), userdata=None, message=message)

    ha_api.set_number_value.assert_not_called()  # handle_down_message must not run
    assert failsafe_ctx["state"] == FailsafeState(active=True, awaiting_seq="seq-1")  # no ack recorded
    assert "retain" in caplog.text.lower()


def test_handle_ack_ignores_mismatched_seq(tmp_path):
    failsafe_path = tmp_path / "failsafe_state.json"
    failsafe_ctx = {"state": FailsafeState(active=True, awaiting_seq="seq-2"), "emergency_boost_active": False}
    mqtt_client = MagicMock()
    ha_api = MagicMock()

    _handle_ack(
        failsafe_ctx=failsafe_ctx, mqtt_client=mqtt_client, failsafe_path=failsafe_path,
        acked_seq="seq-1", ha_api=ha_api, notify_service="",
    )

    assert failsafe_ctx["state"] == FailsafeState(active=True, awaiting_seq="seq-2")
    mqtt_client.publish_status.assert_not_called()


def test_handle_ack_sends_notification_on_recovery(tmp_path):
    failsafe_path = tmp_path / "failsafe_state.json"
    failsafe_ctx = {"state": FailsafeState(active=True, awaiting_seq="seq-1"), "emergency_boost_active": False}
    mqtt_client = MagicMock()
    ha_api = MagicMock()

    _handle_ack(
        failsafe_ctx=failsafe_ctx, mqtt_client=mqtt_client, failsafe_path=failsafe_path,
        acked_seq="seq-1", ha_api=ha_api, notify_service="notify.mobile_app",
    )

    ha_api.send_notification.assert_called_once()
    args, _ = ha_api.send_notification.call_args
    assert args[0] == "notify.mobile_app"
    assert "Notbetrieb beendet" in args[1]


def test_end_emergency_boost_if_active_restores_backup_values(tmp_path, monkeypatch):
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    save_backup(backup_path, {
        "emergency_boost_active": True, "curve_current": 0.6, "offset_current": 3.0,
    })
    manifest = ChannelManifest(entity_ids={
        "curve_current": "number.curve", "offset_current": "number.offset",
    })
    ha_api = MagicMock()
    options = _base_options()
    failsafe_ctx = {"state": FailsafeState(active=False, awaiting_seq=None), "emergency_boost_active": True}

    _end_emergency_boost_if_active(failsafe_ctx, manifest, ha_api, options)

    assert failsafe_ctx["emergency_boost_active"] is False
    ha_api.set_number_value.assert_any_call("number.curve", 0.6)
    ha_api.set_number_value.assert_any_call("number.offset", 3.0)
    assert load_backup(backup_path)["emergency_boost_active"] is False


def test_end_emergency_boost_if_active_is_noop_when_not_active(tmp_path, monkeypatch):
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    manifest = ChannelManifest(entity_ids={"curve_current": "number.curve"})
    ha_api = MagicMock()
    options = _base_options()
    failsafe_ctx = {"state": FailsafeState(active=False, awaiting_seq=None), "emergency_boost_active": False}

    _end_emergency_boost_if_active(failsafe_ctx, manifest, ha_api, options)

    ha_api.set_number_value.assert_not_called()


def test_make_down_callback_ends_emergency_boost_when_notbetrieb_ends(tmp_path, monkeypatch):
    # Review Focus #3: Notbetrieb ending via a successful ack must immediately restore
    # a still-active emergency excursion, not leave the device pinned at the max value
    # until some future room_actual change happens to trigger another local check.
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    save_backup(backup_path, {"emergency_boost_active": True})
    monkeypatch.setattr("heizungsbruecke.__main__.FAILSAFE_PATH", tmp_path / "failsafe_state.json")
    manifest = ChannelManifest(entity_ids={"curve_current": "number.curve"})
    ha_api = MagicMock()
    options = _base_options()
    write_lock = threading.Lock()
    failsafe_ctx = {"state": FailsafeState(active=True, awaiting_seq="seq-1"), "emergency_boost_active": True}
    mqtt_client = MagicMock()

    callback = _make_down_callback("curve_current", manifest, ha_api, options, write_lock, failsafe_ctx, mqtt_client)
    message = MagicMock()
    message.payload = json.dumps({"v": 0.5, "seq": "seq-1"})
    message.retain = False

    callback(client=MagicMock(), userdata=None, message=message)

    assert failsafe_ctx["emergency_boost_active"] is False
    # handle_down_message skipped the live write (emergency was still active at that
    # point) but recorded 0.5 into backup.json; _end_emergency_boost_if_active then
    # restores exactly that freshly-confirmed value once Notbetrieb itself ends.
    ha_api.set_number_value.assert_called_once_with("number.curve", 0.5)


def _precedence_setup(tmp_path, monkeypatch, backup: dict, room_actual: float):
    """Harness for the Notfall- vs. Comfort-Boost precedence tests (Design-Spec
    2026-09-23, Abschnitt 3; final-review finding I2). `_base_options` gives distinct
    values for emergency max (curve_max=0.8/offset_max=5.0), comfort boost (0.5/2.0)
    and the server-confirmed backup (0.3/1.0), so the test can tell which one ends up
    live. `device` records every live write.
    """
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    monkeypatch.setattr("heizungsbruecke.__main__.FAILSAFE_PATH", tmp_path / "failsafe_state.json")
    monkeypatch.setattr("heizungsbruecke.__main__.threading.Timer", _FakeTimer)
    save_backup(backup_path, {"curve_current": 0.3, "offset_current": 1.0, **backup})
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual", "room_target": "sensor.room_target",
        "curve_current": "number.curve", "offset_current": "number.offset",
    })
    device = {}
    room = {"actual": room_actual}
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda entity_id: room["actual"] if entity_id == "sensor.room_actual" else 21.0
    ha_api.set_number_value.side_effect = lambda entity_id, value: device.__setitem__(entity_id, value)
    return backup_path, manifest, ha_api, device, room


def test_precedence_comfort_boost_activating_while_emergency_boost_steady_active(tmp_path, monkeypatch):
    # I2 (a): Comfort-Boost transitions on (target raised 20 -> 21) in a tick where
    # Notfall-Boost is already steady-active (no emergency write this tick). The live
    # device must end the tick at the emergency max values, not the comfort values --
    # while Comfort-Boost's own state still becomes active in the background.
    backup_path, manifest, ha_api, device, _ = _precedence_setup(
        tmp_path, monkeypatch,
        backup={"emergency_boost_active": True, "boost_active": False, "last_room_target": 20.0},
        room_actual=19.0,
    )
    device.update({"number.curve": 0.8, "number.offset": 5.0})  # live from the earlier emergency write
    failsafe_ctx = {"state": FailsafeState(active=True, awaiting_seq=None), "emergency_boost_active": True}

    boost_active = _run_local_check(
        manifest, ha_api, MagicMock(), _base_options(), threading.RLock(), boost_was_active=False,
        room_target=21.0, failsafe_ctx=failsafe_ctx,
    )

    assert device == {"number.curve": 0.8, "number.offset": 5.0}
    assert boost_active is True
    assert load_backup(backup_path)["boost_active"] is True
    assert failsafe_ctx["emergency_boost_active"] is True


def test_precedence_emergency_exit_via_ack_restores_comfort_values_while_comfort_still_active(tmp_path, monkeypatch):
    # I2 (b): Notbetrieb ends (matching ack) while Comfort-Boost is still logically
    # active. The emergency exit must hand the device over to the comfort-boost values,
    # not the plain server value -- and Comfort-Boost's own later exit must still work.
    backup_path, manifest, ha_api, device, room = _precedence_setup(
        tmp_path, monkeypatch,
        backup={"emergency_boost_active": True, "boost_active": True, "last_room_target": 21.0},
        room_actual=19.0,
    )
    device.update({"number.curve": 0.8, "number.offset": 5.0})
    failsafe_ctx = {"state": FailsafeState(active=True, awaiting_seq="seq-1"), "emergency_boost_active": True}
    options = _base_options()
    write_lock = threading.RLock()
    callback = _make_down_callback("curve_current", manifest, ha_api, options, write_lock, failsafe_ctx, MagicMock())
    message = MagicMock()
    message.payload = json.dumps({"v": 0.4, "seq": "seq-1"})
    message.retain = False

    callback(client=MagicMock(), userdata=None, message=message)

    assert failsafe_ctx["state"].active is False
    assert failsafe_ctx["emergency_boost_active"] is False
    assert device == {"number.curve": 0.5, "number.offset": 2.0}
    backup = load_backup(backup_path)
    assert backup["boost_active"] is True
    assert backup["emergency_boost_active"] is False
    assert backup["curve_current"] == 0.4  # fresh server value recorded, not yet live

    # Comfort-Boost's own exit still works normally once the room arrives.
    room["actual"] = 20.8
    boost_active = _run_local_check(
        manifest, ha_api, MagicMock(), options, write_lock, boost_was_active=True,
        room_target=21.0, failsafe_ctx=failsafe_ctx,
    )

    assert boost_active is False
    assert device == {"number.curve": 0.4, "number.offset": 1.0}
    assert load_backup(backup_path)["boost_active"] is False


def test_precedence_emergency_exit_via_local_check_fallback_restores_comfort_values(tmp_path, monkeypatch):
    # I2 (b), via _run_local_check's fallback branch (Notbetrieb already inactive,
    # emergency flag still set): same hand-over to the comfort values.
    backup_path, manifest, ha_api, device, _ = _precedence_setup(
        tmp_path, monkeypatch,
        backup={"emergency_boost_active": True, "boost_active": True, "last_room_target": 21.0},
        room_actual=19.0,
    )
    device.update({"number.curve": 0.8, "number.offset": 5.0})
    failsafe_ctx = {"state": FailsafeState(active=False, awaiting_seq=None), "emergency_boost_active": True}

    boost_active = _run_local_check(
        manifest, ha_api, MagicMock(), _base_options(), threading.RLock(), boost_was_active=True,
        room_target=21.0, failsafe_ctx=failsafe_ctx,
    )

    assert boost_active is True
    assert failsafe_ctx["emergency_boost_active"] is False
    assert device == {"number.curve": 0.5, "number.offset": 2.0}
    assert load_backup(backup_path)["emergency_boost_active"] is False


def test_precedence_emergency_exit_without_comfort_boost_restores_backup_values(tmp_path, monkeypatch):
    # Control case for the above: no Comfort-Boost running -> plain server value.
    backup_path, manifest, ha_api, device, _ = _precedence_setup(
        tmp_path, monkeypatch,
        backup={"emergency_boost_active": True, "boost_active": False, "last_room_target": 21.0},
        room_actual=19.0,
    )
    device.update({"number.curve": 0.8, "number.offset": 5.0})
    failsafe_ctx = {"state": FailsafeState(active=False, awaiting_seq=None), "emergency_boost_active": True}

    _run_local_check(
        manifest, ha_api, MagicMock(), _base_options(), threading.RLock(), boost_was_active=False,
        room_target=21.0, failsafe_ctx=failsafe_ctx,
    )

    assert device == {"number.curve": 0.3, "number.offset": 1.0}


def test_run_bridge_loop_survives_clamp_rejecting_a_non_finite_boost_value(monkeypatch):
    # I2 failure-chain closure: if clamp() raises inside apply_boost_decision (e.g. a
    # NaN backed-up curve_current during a boost-restore), _run_bridge's main loop
    # must survive -- this is the loop's own try/except Exception boundary around
    # _run_local_check that must catch it, not crash the whole add-on process.
    monkeypatch.setattr(
        "heizungsbruecke.__main__.requests.get",
        lambda url, timeout: _FakeResponse({"active": True}),
    )
    monkeypatch.setattr("heizungsbruecke.__main__.derived_sensors.ensure_all", lambda **kwargs: {})
    fake_mqtt_client = MagicMock()
    monkeypatch.setattr("heizungsbruecke.__main__.BridgeMqttClient", lambda **kwargs: fake_mqtt_client)
    monkeypatch.setattr("heizungsbruecke.__main__.HaTriggerClient", lambda **kwargs: MagicMock(connected=False))

    call_count = {"n": 0}

    def fake_run_local_check(manifest, ha_api, mqtt_client, options, write_lock, boost_was_active, room_target=None, failsafe_ctx=None):
        call_count["n"] += 1
        raise ValueError("clamp() erhielt einen nicht-endlichen Wert (NaN): nan")

    monkeypatch.setattr("heizungsbruecke.__main__._run_local_check", fake_run_local_check)

    def stop_after_first_sleep(seconds):
        raise SystemExit("stop test loop")

    monkeypatch.setattr("heizungsbruecke.__main__.time.sleep", stop_after_first_sleep)
    monkeypatch.setattr("heizungsbruecke.__main__.daynight_snapshot.maybe_snapshot", lambda **kwargs: None)

    options = _full_valid_options()

    with pytest.raises(SystemExit):
        _run_bridge(options, MagicMock())

    # Reached the main loop's time.sleep() (our SystemExit trigger) despite
    # _run_local_check raising -- proves the loop caught it and kept going.
    assert call_count["n"] >= 1


def test_main_runs_bridge_synchronously_without_flask(tmp_path, monkeypatch):
    # Ab dieser Aenderung gibt es keinen Flask-Server und keinen Hintergrund-Thread mehr --
    # main() ruft _run_bridge() direkt im Hauptthread auf und kehrt zurueck, sobald
    # _run_bridge() zurueckkehrt (z.B. weil das Add-on noch nicht konfiguriert ist).
    options_path = tmp_path / "options.json"
    options_path.write_text("{}")
    monkeypatch.setattr("heizungsbruecke.__main__.OPTIONS_PATH", options_path)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
    calls = []
    monkeypatch.setattr(
        "heizungsbruecke.__main__._run_bridge",
        lambda options, ha_api: calls.append((options, ha_api)) or True,
    )

    from heizungsbruecke.__main__ import main
    main()

    assert len(calls) == 1
    assert calls[0][0] == {}


def test_main_exits_nonzero_when_run_bridge_reports_a_genuine_error(tmp_path, monkeypatch):
    # main() must be able to tell "not configured" (exit 0, see test above) apart from
    # a genuine startup error, so the Supervisor sees the latter as a crash rather than
    # a quiet, expected stop.
    options_path = tmp_path / "options.json"
    options_path.write_text("{}")
    monkeypatch.setattr("heizungsbruecke.__main__.OPTIONS_PATH", options_path)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
    monkeypatch.setattr("heizungsbruecke.__main__._run_bridge", lambda options, ha_api: False)

    from heizungsbruecke.__main__ import main

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code != 0


# Entitlement check tests (Task 13)


class _FakeResponse:
    def __init__(self, json_body, status_code=200):
        self._json_body = json_body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._json_body


def _full_valid_options(**overrides):
    """Helper: returns a complete, valid options dict with all required fields for _run_bridge tests."""
    options = {
        "tenant_id": "test_tenant",
        "profile": "vaillant_gastherme_heizkoerper",
        "mqtt_username": "test_mqtt_user",
        "mqtt_password": "test_mqtt_pass",
        "entity_room_actual": "sensor.room_actual",
        "entity_room_target": "sensor.room_target",
        "entity_curve_current": "number.curve_current",
        "entity_offset_current": "number.offset_current",
        "entity_outdoor_temp": "sensor.outdoor_temp",
        "entity_heat_limit": "number.heat_limit",
        "entity_room_day_avg": "sensor.room_day_avg",
        "entity_room_night_avg": "sensor.room_night_avg",
        "entity_dat": "sensor.dat",
        "entity_dart": "sensor.dart",
        "curve_min": 0.2,
        "curve_max": 0.8,
        "offset_min": 0.0,
        "offset_max": 5.0,
        "boost_curve_value": 0.5,
        "boost_offset_value": 2.0,
    }
    options.update(overrides)
    return options


def test_run_bridge_returns_false_for_invalid_telemetry_interval(monkeypatch, caplog):
    monkeypatch.setattr(
        "heizungsbruecke.__main__.requests.get",
        lambda url, timeout: _FakeResponse({"active": True}),
    )
    options = _full_valid_options(telemetry_interval_seconds=5)
    ha_api = MagicMock()

    with caplog.at_level(logging.ERROR):
        result = _run_bridge(options, ha_api)

    assert result is False
    assert "telemetry_interval_seconds" in caplog.text


def test_run_bridge_gives_actionable_error_on_connection_refused(monkeypatch, caplog):
    def always_refused(**kwargs):
        raise ConnectionRefusedError("[Errno 111] Connection refused")
    monkeypatch.setattr("heizungsbruecke.__main__.BridgeMqttClient", always_refused)
    monkeypatch.setattr("heizungsbruecke.__main__.time.sleep", lambda seconds: None)
    monkeypatch.setattr(
        "heizungsbruecke.__main__.requests.get",
        lambda url, timeout: _FakeResponse({"active": True}),
    )
    monkeypatch.setattr(
        "heizungsbruecke.__main__.derived_sensors.ensure_all", lambda **kwargs: {}
    )

    options = _full_valid_options()

    with caplog.at_level(logging.ERROR):
        result = _run_bridge(options, MagicMock())

    assert result is False
    assert "cloudflared_access_mqtt" in caplog.text


# Entitlement check tests (Task 13)


def test_check_entitlement_passes_silently_when_active(monkeypatch):
    monkeypatch.setattr(
        "heizungsbruecke.__main__.requests.get",
        lambda url, timeout: _FakeResponse({"active": True}),
    )
    _check_entitlement("client1")  # muss nicht werfen


def test_check_entitlement_raises_when_inactive(monkeypatch):
    monkeypatch.setattr(
        "heizungsbruecke.__main__.requests.get",
        lambda url, timeout: _FakeResponse({"active": False}),
    )
    with pytest.raises(TenantNotEntitledError, match="Abo"):
        _check_entitlement("client1")


def test_check_entitlement_fails_open_on_network_error(monkeypatch, caplog):
    def _raise(url, timeout):
        raise requests.ConnectionError("accounts-api nicht erreichbar")
    monkeypatch.setattr("heizungsbruecke.__main__.requests.get", _raise)

    with caplog.at_level(logging.WARNING):
        _check_entitlement("client1")  # darf NICHT werfen -- fail open (siehe Docstring/Plan-Hinweis)

    assert "accounts-api" in caplog.text.lower() or "berechtigungspruefung" in caplog.text.lower()


def test_check_entitlement_queries_correct_url(monkeypatch):
    called_with = {}
    def _get(url, timeout):
        called_with["url"] = url
        called_with["timeout"] = timeout
        return _FakeResponse({"active": True})
    monkeypatch.setattr("heizungsbruecke.__main__.requests.get", _get)

    _check_entitlement("client1", base_url="https://accounts.hartfussha.org")

    assert called_with["url"] == "https://accounts.hartfussha.org/tenants/client1/status"
    assert called_with["timeout"] == 10


def test_check_entitlement_fails_open_on_http_error_status(monkeypatch, caplog):
    # A 5xx response triggers raise_for_status() to raise HTTPError, which should fail open
    monkeypatch.setattr(
        "heizungsbruecke.__main__.requests.get",
        lambda url, timeout: _FakeResponse({"active": True}, status_code=500),
    )

    with caplog.at_level(logging.WARNING):
        _check_entitlement("client1")  # darf NICHT werfen

    assert "berechtigungspruefung" in caplog.text.lower() or "accounts-api" in caplog.text.lower()


def test_check_entitlement_fails_open_on_malformed_response_body(monkeypatch, caplog):
    # If accounts-api returns valid JSON but not a dict (e.g., a list or null),
    # the body.get("active", True) would raise AttributeError if not caught.
    # This should also fail open, not crash.
    monkeypatch.setattr(
        "heizungsbruecke.__main__.requests.get",
        lambda url, timeout: _FakeResponse([]),  # valid JSON, but not a dict
    )

    with caplog.at_level(logging.WARNING):
        _check_entitlement("client1")  # darf NICHT werfen

    assert "berechtigungspruefung" in caplog.text.lower() or "accounts-api" in caplog.text.lower()


def test_run_local_check_persists_boost_active_true_on_transition_to_active(tmp_path, monkeypatch):
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    save_backup(backup_path, {"last_room_target": 20.0})
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual",
        "room_target": "sensor.room_target",
        "curve_current": "number.curve",
        "offset_current": "number.offset",
    })
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda entity_id: {
        "sensor.room_actual": 19.0, "sensor.room_target": 21.0,
    }[entity_id]
    options = {
        "boost_threshold_k": 0.5, "boost_curve_value": 1.5, "boost_offset_value": 30.0,
        "curve_min": 0.4, "curve_max": 1.5, "offset_min": 20.0, "offset_max": 30.0,
    }

    new_state = _run_local_check(manifest, ha_api, MagicMock(), options, threading.Lock(), boost_was_active=False, room_target=21.0)

    assert new_state is True
    assert load_backup(backup_path)["boost_active"] is True


def test_run_local_check_persists_boost_active_false_on_transition_to_inactive(tmp_path, monkeypatch):
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    save_backup(backup_path, {"last_room_target": 21.0, "boost_active": True, "curve_current": 0.6})
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual",
        "room_target": "sensor.room_target",
        "curve_current": "number.curve",
        "offset_current": "number.offset",
    })
    ha_api = MagicMock()
    # room_actual has arrived within threshold of room_target -> boost ends
    ha_api.get_state.side_effect = lambda entity_id: {
        "sensor.room_actual": 20.8, "sensor.room_target": 21.0,
    }[entity_id]
    options = {
        "boost_threshold_k": 0.5, "boost_curve_value": 1.5, "boost_offset_value": 30.0,
        "curve_min": 0.4, "curve_max": 1.5, "offset_min": 20.0, "offset_max": 30.0,
    }

    new_state = _run_local_check(manifest, ha_api, MagicMock(), options, threading.Lock(), boost_was_active=True, room_target=21.0)

    assert new_state is False
    assert load_backup(backup_path)["boost_active"] is False


def test_run_local_check_skips_all_writes_when_nothing_changed(tmp_path, monkeypatch):
    # SD-wear regression guard (Design-Spec 2026-09-16, Abschnitt A.2): with the local
    # check now running as often as every 30s, a steady-state call (same room_target,
    # boost stays inactive) must not touch backup.json at all.
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    # last_published_target_rt is pre-seeded too (matching room_target) so this stays a
    # true no-op once Task 8 wires _maybe_publish_full_snapshot into the same function --
    # without it, target_changed would trivially fire (None != 20.0) and this assertion
    # would break for reasons unrelated to what this test actually guards.
    save_backup(backup_path, {
        "last_room_target": 20.0, "boost_active": False, "last_published_target_rt": 20.0,
    })
    # Telemetry cadence marker is in-memory only now, not part of backup.json --
    # pre-seed it far in the future so its own publish doesn't fire here and confuse
    # this test's unrelated "zero writes" assertion below.
    monkeypatch.setattr("heizungsbruecke.__main__._last_telemetry_publish_ts", 9_999_999_999.0)
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual",
        "room_target": "sensor.room_target",
    })
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda entity_id: {
        "sensor.room_actual": 20.0, "sensor.room_target": 20.0,
    }[entity_id]
    save_calls = []
    monkeypatch.setattr(
        "heizungsbruecke.__main__.save_backup", lambda path, values: save_calls.append((path, values)),
    )

    _run_local_check(manifest, ha_api, MagicMock(), _base_options(), threading.Lock(), boost_was_active=False, room_target=20.0)

    assert save_calls == []


def test_maybe_publish_full_snapshot_publishes_when_target_changed(tmp_path, monkeypatch):
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    manifest = ChannelManifest(entity_ids={"room_actual": "sensor.room_actual", "room_target": "sensor.room_target"})
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.0
    mqtt_client = MagicMock()

    result = _maybe_publish_full_snapshot(
        manifest=manifest, ha_api=ha_api, mqtt_client=mqtt_client, options={},
        room_target=21.0, notify_service="", now=datetime(2026, 9, 17, 9, 0),
    )

    assert result is not None
    assert mqtt_client.publish_value.call_count == 2
    assert load_backup(tmp_path / "backup.json")["last_published_target_rt"] == 21.0


def test_maybe_publish_full_snapshot_does_not_republish_unchanged_target(tmp_path, monkeypatch):
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    save_backup(backup_path, {"last_published_target_rt": 21.0})
    manifest = ChannelManifest(entity_ids={"room_actual": "sensor.room_actual", "room_target": "sensor.room_target"})
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.0
    mqtt_client = MagicMock()

    result = _maybe_publish_full_snapshot(
        manifest=manifest, ha_api=ha_api, mqtt_client=mqtt_client, options={},
        room_target=21.0, notify_service="", now=datetime(2026, 9, 17, 9, 0),
    )

    assert result is None
    mqtt_client.publish_value.assert_not_called()


def test_maybe_publish_full_snapshot_publishes_when_daily_trigger_time_reached(tmp_path, monkeypatch):
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    save_backup(backup_path, {"last_published_target_rt": 21.0})  # unchanged target
    manifest = ChannelManifest(entity_ids={"room_actual": "sensor.room_actual", "room_target": "sensor.room_target"})
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.0
    mqtt_client = MagicMock()

    result = _maybe_publish_full_snapshot(
        manifest=manifest, ha_api=ha_api, mqtt_client=mqtt_client, options={"daily_trigger_time": "12:00"},
        room_target=21.0, notify_service="", now=datetime(2026, 9, 17, 12, 5),
    )

    assert result is not None
    assert mqtt_client.publish_value.call_count == 2
    assert load_backup(backup_path)["last_daily_trigger_date"] == "2026-09-17"


def test_maybe_publish_full_snapshot_does_not_refire_daily_trigger_same_day(tmp_path, monkeypatch):
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    save_backup(backup_path, {"last_published_target_rt": 21.0, "last_daily_trigger_date": "2026-09-17"})
    manifest = ChannelManifest(entity_ids={"room_actual": "sensor.room_actual", "room_target": "sensor.room_target"})
    ha_api = MagicMock()
    mqtt_client = MagicMock()

    result = _maybe_publish_full_snapshot(
        manifest=manifest, ha_api=ha_api, mqtt_client=mqtt_client, options={"daily_trigger_time": "12:00"},
        room_target=21.0, notify_service="", now=datetime(2026, 9, 17, 15, 0),
    )

    assert result is None
    mqtt_client.publish_value.assert_not_called()


def test_maybe_publish_full_snapshot_skips_when_nothing_triggers(tmp_path, monkeypatch):
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    save_backup(backup_path, {"last_published_target_rt": 21.0})
    manifest = ChannelManifest(entity_ids={"room_actual": "sensor.room_actual", "room_target": "sensor.room_target"})
    ha_api = MagicMock()
    mqtt_client = MagicMock()

    result = _maybe_publish_full_snapshot(
        manifest=manifest, ha_api=ha_api, mqtt_client=mqtt_client, options={"daily_trigger_time": "12:00"},
        room_target=21.0, notify_service="", now=datetime(2026, 9, 17, 9, 0),
    )

    assert result is None
    mqtt_client.publish_value.assert_not_called()
    assert load_backup(backup_path) == {"last_published_target_rt": 21.0}  # unchanged, no gratuitous write


def test_maybe_publish_full_snapshot_passes_notify_service_through(tmp_path, monkeypatch):
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    manifest = ChannelManifest(entity_ids={"room_actual": "sensor.room_actual", "dat": "sensor.kaputt"})
    ha_api = MagicMock()

    def _get_state(entity_id):
        if entity_id == "sensor.kaputt":
            raise ValueError("could not convert string to float: 'unavailable'")
        return 20.0

    ha_api.get_state.side_effect = _get_state
    mqtt_client = MagicMock()

    _maybe_publish_full_snapshot(
        manifest=manifest, ha_api=ha_api, mqtt_client=mqtt_client, options={},
        room_target=21.0, notify_service="notify.mobile_app_lucas_iphone", now=datetime(2026, 9, 17, 9, 0),
    )

    ha_api.send_notification.assert_called_once()
    assert ha_api.send_notification.call_args.args[0] == "notify.mobile_app_lucas_iphone"


class _FakeTimer:
    """Test double for threading.Timer -- captures scheduling instead of actually
    waiting, so ack-timeout tests can fire the callback synchronously."""
    instances: list = []

    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval = interval
        self.function = function
        self.args = args or ()
        self.kwargs = kwargs or {}
        self.daemon = False
        self.started = False
        _FakeTimer.instances.append(self)

    def start(self):
        self.started = True

    def fire(self):
        self.function(*self.args, **self.kwargs)


@pytest.fixture(autouse=True)
def _reset_fake_timer_instances():
    _FakeTimer.instances = []


def test_run_local_check_schedules_ack_timeout_after_publishing(tmp_path, monkeypatch):
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    monkeypatch.setattr("heizungsbruecke.__main__.threading.Timer", _FakeTimer)
    manifest = ChannelManifest(entity_ids={"room_actual": "sensor.room_actual", "room_target": "sensor.room_target"})
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.0
    mqtt_client = MagicMock()
    options = _base_options()
    failsafe_ctx = {"state": FailsafeState(active=False, awaiting_seq=None), "emergency_boost_active": False}

    _run_local_check(
        manifest, ha_api, mqtt_client, options, threading.Lock(), boost_was_active=False,
        room_target=21.0, failsafe_ctx=failsafe_ctx,
    )

    assert len(_FakeTimer.instances) == 1
    timer = _FakeTimer.instances[0]
    assert timer.interval == 30
    assert timer.started is True
    assert failsafe_ctx["state"].awaiting_seq is not None


def test_run_local_check_ack_timeout_activates_notbetrieb_when_unanswered(tmp_path, monkeypatch):
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    monkeypatch.setattr("heizungsbruecke.__main__.FAILSAFE_PATH", tmp_path / "failsafe_state.json")
    monkeypatch.setattr("heizungsbruecke.__main__.threading.Timer", _FakeTimer)
    manifest = ChannelManifest(entity_ids={"room_actual": "sensor.room_actual", "room_target": "sensor.room_target"})
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.0
    mqtt_client = MagicMock()
    options = _base_options()
    failsafe_ctx = {"state": FailsafeState(active=False, awaiting_seq=None), "emergency_boost_active": False}

    _run_local_check(
        manifest, ha_api, mqtt_client, options, threading.Lock(), boost_was_active=False,
        room_target=21.0, failsafe_ctx=failsafe_ctx,
    )
    _FakeTimer.instances[0].fire()

    assert failsafe_ctx["state"].active is True
    mqtt_client.publish_status.assert_any_call("failsafe", "ON")


def test_run_local_check_does_not_schedule_ack_timeout_when_nothing_publishes(tmp_path, monkeypatch):
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    save_backup(backup_path, {"last_published_target_rt": 20.0})
    monkeypatch.setattr("heizungsbruecke.__main__.threading.Timer", _FakeTimer)
    manifest = ChannelManifest(entity_ids={"room_actual": "sensor.room_actual", "room_target": "sensor.room_target"})
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.0
    mqtt_client = MagicMock()
    options = _base_options()
    failsafe_ctx = {"state": FailsafeState(active=False, awaiting_seq=None), "emergency_boost_active": False}

    _run_local_check(
        manifest, ha_api, mqtt_client, options, threading.Lock(), boost_was_active=False,
        room_target=20.0, failsafe_ctx=failsafe_ctx,
    )

    assert _FakeTimer.instances == []


def test_handle_ack_timeout_noop_when_seq_already_acked(tmp_path):
    failsafe_path = tmp_path / "failsafe_state.json"
    failsafe_ctx = {"state": FailsafeState(active=False, awaiting_seq=None), "emergency_boost_active": False}
    mqtt_client = MagicMock()
    ha_api = MagicMock()

    _handle_ack_timeout(
        seq="seq-1", failsafe_ctx=failsafe_ctx, mqtt_client=mqtt_client, failsafe_path=failsafe_path,
        write_lock=threading.Lock(), ha_api=ha_api, notify_service="",
    )

    assert failsafe_ctx["state"] == FailsafeState(active=False, awaiting_seq=None)
    mqtt_client.publish_status.assert_not_called()


def test_handle_ack_timeout_sends_notification_when_configured(tmp_path):
    failsafe_path = tmp_path / "failsafe_state.json"
    failsafe_ctx = {"state": FailsafeState(active=False, awaiting_seq="seq-1"), "emergency_boost_active": False}
    mqtt_client = MagicMock()
    ha_api = MagicMock()

    _handle_ack_timeout(
        seq="seq-1", failsafe_ctx=failsafe_ctx, mqtt_client=mqtt_client, failsafe_path=failsafe_path,
        write_lock=threading.Lock(), ha_api=ha_api, notify_service="notify.mobile_app",
    )

    assert failsafe_ctx["state"] == FailsafeState(active=True, awaiting_seq="seq-1")
    ha_api.send_notification.assert_called_once()
    args, _ = ha_api.send_notification.call_args
    assert args[0] == "notify.mobile_app"
    assert "Notbetrieb" in args[1]


def test_late_down_message_after_ack_timeout_ends_notbetrieb(tmp_path, monkeypatch):
    # Final-review finding I1, end-to-end through the real timer callback and the real
    # down-callback: publish -> 30s timeout fires (Notbetrieb ON) -> the server's answer
    # for that same seq arrives late -> Notbetrieb must end right away.
    backup_path = tmp_path / "backup.json"
    failsafe_path = tmp_path / "failsafe_state.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    monkeypatch.setattr("heizungsbruecke.__main__.FAILSAFE_PATH", failsafe_path)
    monkeypatch.setattr("heizungsbruecke.__main__.threading.Timer", _FakeTimer)
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual", "room_target": "sensor.room_target",
        "curve_current": "number.curve",
    })
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.0
    mqtt_client = MagicMock()
    options = _base_options()
    write_lock = threading.RLock()
    failsafe_ctx = {"state": FailsafeState(active=False, awaiting_seq=None), "emergency_boost_active": False}

    _run_local_check(
        manifest, ha_api, mqtt_client, options, write_lock, boost_was_active=False,
        room_target=21.0, failsafe_ctx=failsafe_ctx,
    )
    seq = failsafe_ctx["state"].awaiting_seq
    _FakeTimer.instances[0].fire()
    assert failsafe_ctx["state"].active is True

    callback = _make_down_callback("curve_current", manifest, ha_api, options, write_lock, failsafe_ctx, mqtt_client)
    message = MagicMock()
    message.payload = json.dumps({"v": 0.5, "seq": seq})
    message.retain = False
    callback(client=MagicMock(), userdata=None, message=message)

    assert failsafe_ctx["state"] == FailsafeState(active=False, awaiting_seq=None)
    assert load_backup(failsafe_path)["failsafe_active"] is False
    mqtt_client.publish_status.assert_called_with("failsafe", "OFF")


def test_validate_local_check_interval_accepts_absent_and_valid_values():
    assert _validate_local_check_interval({}) is None
    assert _validate_local_check_interval({"local_check_interval_seconds": 30}) is None
    assert _validate_local_check_interval({"local_check_interval_seconds": 60}) is None


def test_validate_local_check_interval_flags_value_above_thirty_six_hundred():
    error = _validate_local_check_interval({"local_check_interval_seconds": 3601})
    assert error is not None
    assert "local_check_interval_seconds" in error


def test_validate_local_check_interval_accepts_thirty_six_hundred():
    assert _validate_local_check_interval({"local_check_interval_seconds": 3600}) is None


def test_validate_telemetry_interval_accepts_absent_and_valid_values():
    assert _validate_telemetry_interval({}) is None
    assert _validate_telemetry_interval({"telemetry_interval_seconds": 10}) is None
    assert _validate_telemetry_interval({"telemetry_interval_seconds": 300}) is None


def test_validate_telemetry_interval_flags_value_below_ten():
    error = _validate_telemetry_interval({"telemetry_interval_seconds": 0})
    assert error is not None
    assert "telemetry_interval_seconds" in error


def test_validate_local_check_interval_flags_nan():
    # Task 3a (final-review-fixes-plan): value < 10/> 60 is False for NaN, so a hand-
    # edited options.json with NaN used to sail through validation unnoticed.
    error = _validate_local_check_interval({"local_check_interval_seconds": float("nan")})
    assert error is not None
    assert "local_check_interval_seconds" in error


def test_validate_local_check_interval_flags_infinity():
    error = _validate_local_check_interval({"local_check_interval_seconds": float("inf")})
    assert error is not None
    assert "local_check_interval_seconds" in error


def test_validate_telemetry_interval_flags_nan():
    error = _validate_telemetry_interval({"telemetry_interval_seconds": float("nan")})
    assert error is not None
    assert "telemetry_interval_seconds" in error


def test_validate_telemetry_interval_flags_infinity():
    error = _validate_telemetry_interval({"telemetry_interval_seconds": float("inf")})
    assert error is not None
    assert "telemetry_interval_seconds" in error


def test_validate_local_check_interval_flags_non_numeric_string_without_raising():
    # M1 (final whole-branch review, 2026-09-20): math.isnan()/math.isinf() raise
    # TypeError on a non-numeric value (e.g. a hand-edited
    # "local_check_interval_seconds": "30s" in options.json) instead of returning the
    # clean German validation error this function exists to produce.
    error = _validate_local_check_interval({"local_check_interval_seconds": "not-a-number"})
    assert error is not None
    assert "local_check_interval_seconds" in error


def test_validate_telemetry_interval_flags_non_numeric_string_without_raising():
    error = _validate_telemetry_interval({"telemetry_interval_seconds": "300s"})
    assert error is not None
    assert "telemetry_interval_seconds" in error


def test_default_local_check_interval_seconds_is_300():
    from heizungsbruecke.__main__ import DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS
    assert DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS == 300


def test_run_bridge_primes_local_check_before_mqtt_loop_start(monkeypatch):
    # Boot-Sync-Fix (Sicherheits-Review-Fund, siehe task-8-brief.md Zusatzanforderung):
    # backup.json["boost_active"] darf nach einem Neustart nicht veraltet sein, bevor
    # MQTT-Down-Nachrichten verarbeitet werden koennen. subscribe_down() allein liefert
    # noch keine Nachrichten aus -- erst loop_start() startet die Verarbeitung. Also muss
    # der allererste _run_local_check()-Aufruf synchron VOR loop_start() abgeschlossen sein.
    call_order = []

    fake_mqtt_client = MagicMock()
    fake_mqtt_client.loop_start.side_effect = lambda: call_order.append("loop_start")
    monkeypatch.setattr("heizungsbruecke.__main__.BridgeMqttClient", lambda **kwargs: fake_mqtt_client)
    monkeypatch.setattr("heizungsbruecke.__main__.HaTriggerClient", lambda **kwargs: MagicMock(connected=False))
    monkeypatch.setattr(
        "heizungsbruecke.__main__.requests.get",
        lambda url, timeout: _FakeResponse({"active": True}),
    )
    monkeypatch.setattr("heizungsbruecke.__main__.derived_sensors.ensure_all", lambda **kwargs: {})

    def fake_run_local_check(manifest, ha_api, mqtt_client, options, write_lock, boost_was_active, room_target=None, failsafe_ctx=None):
        call_order.append("_run_local_check")
        return boost_was_active

    monkeypatch.setattr("heizungsbruecke.__main__._run_local_check", fake_run_local_check)

    # Stop the loop from spinning forever: raise after the first sleep() call, which is
    # the last thing that happens inside one loop iteration.
    def stop_after_first_sleep(seconds):
        raise SystemExit("stop test loop")

    monkeypatch.setattr("heizungsbruecke.__main__.time.sleep", stop_after_first_sleep)
    monkeypatch.setattr(
        "heizungsbruecke.__main__.daynight_snapshot.maybe_snapshot", lambda **kwargs: None,
    )

    options = _full_valid_options()

    with pytest.raises(SystemExit):
        _run_bridge(options, MagicMock())

    assert "_run_local_check" in call_order
    assert "loop_start" in call_order
    # The priming call (first entry) must precede loop_start -- proves the fix, not just
    # that both got called at some point.
    assert call_order.index("_run_local_check") < call_order.index("loop_start")


def test_run_bridge_resets_stale_boost_active_when_priming_check_raises(monkeypatch, tmp_path):
    # Whole-branch review finding: if the priming _run_local_check() call itself raises,
    # backup.json["boost_active"] must not be left at a stale pre-restart value (which
    # could be True) -- that would defeat the whole point of the boot-sync fix (gating
    # a genuine down-message against a value never freshly re-derived after restart).
    # Fail-open toward "not boosting" is the safer direction (see finding writeup).
    backup_path = tmp_path / "backup.json"
    save_backup(backup_path, {"boost_active": True})
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)

    fake_mqtt_client = MagicMock()
    monkeypatch.setattr("heizungsbruecke.__main__.BridgeMqttClient", lambda **kwargs: fake_mqtt_client)
    monkeypatch.setattr("heizungsbruecke.__main__.HaTriggerClient", lambda **kwargs: MagicMock(connected=False))
    monkeypatch.setattr(
        "heizungsbruecke.__main__.requests.get",
        lambda url, timeout: _FakeResponse({"active": True}),
    )
    monkeypatch.setattr("heizungsbruecke.__main__.derived_sensors.ensure_all", lambda **kwargs: {})

    def raising_run_local_check(manifest, ha_api, mqtt_client, options, write_lock, boost_was_active, room_target=None, failsafe_ctx=None):
        raise RuntimeError("simulated HA-API hiccup at boot")

    monkeypatch.setattr("heizungsbruecke.__main__._run_local_check", raising_run_local_check)

    # Stop the loop from spinning forever, same pattern as the priming-order test above.
    def stop_after_first_sleep(seconds):
        raise SystemExit("stop test loop")

    monkeypatch.setattr("heizungsbruecke.__main__.time.sleep", stop_after_first_sleep)
    monkeypatch.setattr(
        "heizungsbruecke.__main__.daynight_snapshot.maybe_snapshot", lambda **kwargs: None,
    )

    options = _full_valid_options()

    with pytest.raises(SystemExit):
        _run_bridge(options, MagicMock())

    assert load_backup(backup_path)["boost_active"] is False


def test_run_local_check_activates_emergency_boost_when_notbetrieb_active_and_room_cold(tmp_path, monkeypatch):
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    # No last_published_target_rt seeded -> _maybe_publish_full_snapshot will see
    # target_changed=True and publish, which schedules an ack-timeout Timer (Task 6) --
    # fake it out so the test doesn't leave a real 30s background timer running.
    monkeypatch.setattr("heizungsbruecke.__main__.threading.Timer", _FakeTimer)
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual", "room_target": "sensor.room_target",
        "curve_current": "number.curve", "offset_current": "number.offset",
    })
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda entity_id: {
        "sensor.room_actual": 18.5, "sensor.room_target": 20.0,
    }[entity_id]
    mqtt_client = MagicMock()
    options = _base_options()
    failsafe_ctx = {"state": FailsafeState(active=True, awaiting_seq=None), "emergency_boost_active": False}

    _run_local_check(
        manifest, ha_api, mqtt_client, options, threading.Lock(), boost_was_active=False,
        room_target=20.0, failsafe_ctx=failsafe_ctx,
    )

    assert failsafe_ctx["emergency_boost_active"] is True
    ha_api.set_number_value.assert_any_call("number.curve", options["curve_max"])
    ha_api.set_number_value.assert_any_call("number.offset", options["offset_max"])


def test_run_local_check_does_not_activate_emergency_boost_when_notbetrieb_inactive(tmp_path, monkeypatch):
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    # See the comment in the previous test -- avoids a real 30s background timer.
    monkeypatch.setattr("heizungsbruecke.__main__.threading.Timer", _FakeTimer)
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual", "room_target": "sensor.room_target",
        "curve_current": "number.curve", "offset_current": "number.offset",
    })
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda entity_id: {
        "sensor.room_actual": 18.5, "sensor.room_target": 20.0,
    }[entity_id]
    mqtt_client = MagicMock()
    options = _base_options()
    failsafe_ctx = {"state": FailsafeState(active=False, awaiting_seq=None), "emergency_boost_active": False}

    _run_local_check(
        manifest, ha_api, mqtt_client, options, threading.Lock(), boost_was_active=False,
        room_target=20.0, failsafe_ctx=failsafe_ctx,
    )

    assert failsafe_ctx["emergency_boost_active"] is False
    ha_api.set_number_value.assert_not_called()


def test_run_local_check_ends_emergency_boost_once_notbetrieb_state_is_inactive(tmp_path, monkeypatch):
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    save_backup(backup_path, {"emergency_boost_active": True, "curve_current": 0.5})
    # See the comment in test_run_local_check_activates_emergency_boost_...  above --
    # avoids a real 30s background timer (no last_published_target_rt seeded here either).
    monkeypatch.setattr("heizungsbruecke.__main__.threading.Timer", _FakeTimer)
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual", "room_target": "sensor.room_target",
        "curve_current": "number.curve",
    })
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda entity_id: {
        "sensor.room_actual": 18.5, "sensor.room_target": 20.0,
    }[entity_id]
    mqtt_client = MagicMock()
    options = _base_options()
    # Notbetrieb ist bereits (z.B. durch einen erfolgreichen Ack) beendet, aber die
    # Notfall-Exkursion selbst lief noch -- _run_local_check muss sie beenden, statt auf
    # ihre eigene Exit-Schwelle zu warten (Review Focus #3, Rueckfallebene zu Task 4).
    failsafe_ctx = {"state": FailsafeState(active=False, awaiting_seq=None), "emergency_boost_active": True}

    _run_local_check(
        manifest, ha_api, mqtt_client, options, threading.Lock(), boost_was_active=False,
        room_target=20.0, failsafe_ctx=failsafe_ctx,
    )

    assert failsafe_ctx["emergency_boost_active"] is False
    ha_api.set_number_value.assert_any_call("number.curve", 0.5)


def test_run_bridge_resets_stale_emergency_boost_active_when_priming_check_raises(monkeypatch, tmp_path):
    # Fail-open, same rationale as test_run_bridge_resets_stale_boost_active_when_priming_check_raises:
    # a stuck emergency_boost_active=True would permanently gate out down-messages
    # (handle_down_message's skip-live-write check) with no automatic recovery.
    backup_path = tmp_path / "backup.json"
    save_backup(backup_path, {"boost_active": False, "emergency_boost_active": True})
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)

    fake_mqtt_client = MagicMock()
    monkeypatch.setattr("heizungsbruecke.__main__.BridgeMqttClient", lambda **kwargs: fake_mqtt_client)
    monkeypatch.setattr("heizungsbruecke.__main__.HaTriggerClient", lambda **kwargs: MagicMock(connected=False))
    monkeypatch.setattr(
        "heizungsbruecke.__main__.requests.get",
        lambda url, timeout: _FakeResponse({"active": True}),
    )
    monkeypatch.setattr("heizungsbruecke.__main__.derived_sensors.ensure_all", lambda **kwargs: {})

    def raising_run_local_check(manifest, ha_api, mqtt_client, options, write_lock, boost_was_active, room_target=None, failsafe_ctx=None):
        raise RuntimeError("simulated HA-API hiccup at boot")

    monkeypatch.setattr("heizungsbruecke.__main__._run_local_check", raising_run_local_check)

    def stop_after_first_sleep(seconds):
        raise SystemExit("stop test loop")

    monkeypatch.setattr("heizungsbruecke.__main__.time.sleep", stop_after_first_sleep)
    monkeypatch.setattr("heizungsbruecke.__main__.daynight_snapshot.maybe_snapshot", lambda **kwargs: None)

    options = _full_valid_options()

    with pytest.raises(SystemExit):
        _run_bridge(options, MagicMock())

    assert load_backup(backup_path)["emergency_boost_active"] is False


def _setup_restart_scenario(monkeypatch, tmp_path, failsafe_active, room_actual, trigger_client_connected):
    """Harness for the final-review C1 restart tests: runs the REAL _run_bridge boot path
    (real _load_failsafe_ctx_safe, real boot-priming _run_local_check) against persisted
    state files as they would be on disk right after an add-on restart during/after an
    emergency excursion. The live device (vaillant profile: curve_max=1.5,
    offset_max=30.0) is still physically at the emergency max values from before the
    restart; `device` tracks every live write so the test can assert where it ends up.
    """
    backup_path = tmp_path / "backup.json"
    failsafe_path = tmp_path / "failsafe_state.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    monkeypatch.setattr("heizungsbruecke.__main__.FAILSAFE_PATH", failsafe_path)
    monkeypatch.setattr("heizungsbruecke.__main__.threading.Timer", _FakeTimer)
    save_backup(failsafe_path, {"failsafe_active": failsafe_active})
    save_backup(backup_path, {
        "emergency_boost_active": True,
        "boost_active": False,
        # Last server-confirmed values (restore target).
        "curve_current": 0.9,
        "offset_current": 22.0,
        # Nothing else should trigger during boot: no target rise, no snapshot publish.
        "last_room_target": 20.0,
        "last_published_target_rt": 20.0,
        "last_daily_trigger_date": datetime.now().date().isoformat(),
    })

    device = {"number.curve_current": 1.5, "number.offset_current": 30.0}
    room = {"actual": room_actual}

    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda entity_id: {
        "sensor.room_actual": room["actual"], "sensor.room_target": 20.0,
    }.get(entity_id, device.get(entity_id, 5.0))
    ha_api.set_number_value.side_effect = lambda entity_id, value: device.__setitem__(entity_id, value)

    fake_mqtt_client = MagicMock()
    monkeypatch.setattr("heizungsbruecke.__main__.BridgeMqttClient", lambda **kwargs: fake_mqtt_client)
    monkeypatch.setattr(
        "heizungsbruecke.__main__.HaTriggerClient", lambda **kwargs: MagicMock(connected=trigger_client_connected),
    )
    monkeypatch.setattr(
        "heizungsbruecke.__main__.requests.get",
        lambda url, timeout: _FakeResponse({"active": True}),
    )
    monkeypatch.setattr("heizungsbruecke.__main__.derived_sensors.ensure_all", lambda **kwargs: {})
    monkeypatch.setattr("heizungsbruecke.__main__.daynight_snapshot.maybe_snapshot", lambda **kwargs: None)

    def stop_after_first_sleep(seconds):
        raise SystemExit("stop test loop")

    monkeypatch.setattr("heizungsbruecke.__main__.time.sleep", stop_after_first_sleep)
    return ha_api, fake_mqtt_client, device, room, backup_path


def test_restart_after_notbetrieb_ended_restores_device_during_boot_priming(monkeypatch, tmp_path):
    # Final-review C1, Scenario A: restart in the window between _handle_ack ending
    # Notbetrieb (failsafe_active=False already persisted) and _end_emergency_boost_if_active
    # finishing its restore (emergency_boost_active=True still on disk). Boot-priming must
    # restore the live device and clear the disk flag -- otherwise every future
    # down-message stays diverted into backup.json forever (handle_down_message's gate).
    ha_api, _, device, _, backup_path = _setup_restart_scenario(
        monkeypatch, tmp_path, failsafe_active=False, room_actual=19.8, trigger_client_connected=True,
    )

    with pytest.raises(SystemExit):
        _run_bridge(_full_valid_options(), ha_api)

    assert device == {"number.curve_current": 0.9, "number.offset_current": 22.0}
    assert load_backup(backup_path)["emergency_boost_active"] is False


def test_restart_during_notbetrieb_continues_emergency_hysteresis_between_thresholds(monkeypatch, tmp_path):
    # Final-review C1, Scenario B/C: restart while Notbetrieb is still active and an
    # emergency excursion is underway, room currently 0.8 K below target -- between the
    # exit threshold (0.5 K) and the wider entry threshold (1.0 K). The excursion must
    # continue from the "was active" branch (device stays at max, flag stays True), and
    # once the room reaches the exit threshold, the device must be restored -- not left
    # stranded at max heat for the rest of the outage.
    ha_api, fake_mqtt_client, device, room, backup_path = _setup_restart_scenario(
        monkeypatch, tmp_path, failsafe_active=True, room_actual=19.2, trigger_client_connected=False,
    )
    after_priming = {}

    def on_loop_start():
        # Runs right after boot-priming, before the watchdog-fallback tick.
        after_priming["device"] = dict(device)
        after_priming["emergency_on_disk"] = load_backup(backup_path)["emergency_boost_active"]
        room["actual"] = 19.6  # room has warmed up to within exit_threshold_k (0.5 K)

    fake_mqtt_client.loop_start.side_effect = on_loop_start

    with pytest.raises(SystemExit):
        _run_bridge(_full_valid_options(), ha_api)

    # Boot-priming: excursion continued, no write, flag still set.
    assert after_priming["device"] == {"number.curve_current": 1.5, "number.offset_current": 30.0}
    assert after_priming["emergency_on_disk"] is True
    # Watchdog-fallback tick: exit threshold reached -> restored to the server-confirmed values.
    assert device == {"number.curve_current": 0.9, "number.offset_current": 22.0}
    assert load_backup(backup_path)["emergency_boost_active"] is False


def test_maybe_publish_telemetry_publishes_on_first_call(tmp_path, monkeypatch):
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    mqtt_client = MagicMock()

    _maybe_publish_telemetry(
        mqtt_client=mqtt_client, options={}, room_actual=20.5,
        boost_active=False, failsafe_active=False, now=1000.0,
    )

    mqtt_client.publish_telemetry.assert_called_once()
    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert payload["room_actual"] == 20.5
    assert payload["boost_active"] is False
    assert payload["failsafe_active"] is False
    assert "ts" in payload


def test_maybe_publish_telemetry_skips_within_interval(monkeypatch):
    monkeypatch.setattr("heizungsbruecke.__main__._last_telemetry_publish_ts", 1000.0)
    mqtt_client = MagicMock()

    _maybe_publish_telemetry(
        mqtt_client=mqtt_client, options={"telemetry_interval_seconds": 300}, room_actual=20.5,
        boost_active=False, failsafe_active=False, now=1200.0,  # nur 200s vergangen
    )

    mqtt_client.publish_telemetry.assert_not_called()


def test_maybe_publish_telemetry_publishes_again_after_interval_elapsed(monkeypatch):
    monkeypatch.setattr("heizungsbruecke.__main__._last_telemetry_publish_ts", 1000.0)
    mqtt_client = MagicMock()

    _maybe_publish_telemetry(
        mqtt_client=mqtt_client, options={"telemetry_interval_seconds": 300}, room_actual=20.5,
        boost_active=True, failsafe_active=False, now=1301.0,  # 301s vergangen > 300s
    )

    mqtt_client.publish_telemetry.assert_called_once()
    assert main_module._last_telemetry_publish_ts == 1301.0


def test_maybe_publish_telemetry_does_not_touch_backup_json(tmp_path, monkeypatch):
    # SD-wear regression guard: the telemetry cadence marker must live in memory only
    # (module-level, reset by the autouse fixture above) -- persisting it to
    # backup.json on every publish would reintroduce ~288 writes/day, exactly what
    # A.2 eliminated for the other backup.json fields.
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    mqtt_client = MagicMock()

    _maybe_publish_telemetry(
        mqtt_client=mqtt_client, options={}, room_actual=20.5,
        boost_active=False, failsafe_active=False, now=1000.0,
    )

    mqtt_client.publish_telemetry.assert_called_once()
    assert not backup_path.exists()


def test_run_telemetry_tick_reads_room_actual_and_publishes(tmp_path, monkeypatch):
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    manifest = ChannelManifest(entity_ids={"room_actual": "sensor.room_actual"})
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.5
    mqtt_client = MagicMock()

    main_module._run_telemetry_tick(
        manifest, ha_api, mqtt_client, options={}, boost_active=True, failsafe_active=False,
    )

    ha_api.get_state.assert_called_once_with("sensor.room_actual")
    mqtt_client.publish_telemetry.assert_called_once()
    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert payload["room_actual"] == 20.5
    assert payload["boost_active"] is True
    assert payload["failsafe_active"] is False


def test_run_telemetry_tick_includes_configured_optional_kpi_fields(tmp_path, monkeypatch):
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual",
        "flow_temperature": "sensor.flow",
        "operating_mode": "sensor.mode",
    })
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda entity_id: {
        "sensor.room_actual": 20.5, "sensor.flow": 45.2,
    }[entity_id]
    ha_api.get_raw_state.return_value = "heating"
    mqtt_client = MagicMock()

    main_module._run_telemetry_tick(
        manifest, ha_api, mqtt_client, options={}, boost_active=False, failsafe_active=False,
    )

    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert payload["flow_temperature"] == 45.2
    assert payload["operating_mode"] == "heating"
    ha_api.get_raw_state.assert_called_once_with("sensor.mode")


def test_run_telemetry_tick_omits_unconfigured_optional_kpi_fields(tmp_path, monkeypatch):
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    manifest = ChannelManifest(entity_ids={"room_actual": "sensor.room_actual"})
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.5
    mqtt_client = MagicMock()

    main_module._run_telemetry_tick(
        manifest, ha_api, mqtt_client, options={}, boost_active=False, failsafe_active=False,
    )

    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert "flow_temperature" not in payload
    assert "operating_mode" not in payload
    assert "energy" not in payload
    ha_api.get_raw_state.assert_not_called()


def test_run_telemetry_tick_builds_energy_subobject_from_configured_channels(tmp_path, monkeypatch):
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual",
        "energy_thermal_heating": "sensor.e_thermal",
        "energy_electrical_heating": "sensor.e_elec",
    })
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda entity_id: {
        "sensor.room_actual": 20.5, "sensor.e_thermal": 1234.5, "sensor.e_elec": 300.1,
    }[entity_id]
    mqtt_client = MagicMock()

    main_module._run_telemetry_tick(
        manifest, ha_api, mqtt_client, options={}, boost_active=False, failsafe_active=False,
    )

    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert payload["energy"] == {"thermal_heating": 1234.5, "electrical_heating": 300.1}


def test_run_telemetry_tick_does_not_read_kpi_entities_when_throttled(monkeypatch):
    monkeypatch.setattr("heizungsbruecke.__main__._last_telemetry_publish_ts", time.time())
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual",
        "flow_temperature": "sensor.flow",
        "operating_mode": "sensor.mode",
        "energy_thermal_heating": "sensor.e_thermal",
    })
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.5
    mqtt_client = MagicMock()

    main_module._run_telemetry_tick(
        manifest, ha_api, mqtt_client, options={"telemetry_interval_seconds": 300},
        boost_active=False, failsafe_active=False,
    )

    mqtt_client.publish_telemetry.assert_not_called()
    ha_api.get_raw_state.assert_not_called()
    ha_api.get_state.assert_called_once_with("sensor.room_actual")  # only the pre-existing room_actual read


def test_run_telemetry_tick_omits_failing_optional_sensor_but_publishes_rest():
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual",
        "flow_temperature": "sensor.flow",
        "return_temperature": "sensor.ret",
        "operating_mode": "sensor.mode",
    })
    ha_api = MagicMock()

    def fake_get_state(entity_id):
        if entity_id == "sensor.flow":
            raise ValueError("could not convert string to float: 'unavailable'")
        return {"sensor.room_actual": 20.5, "sensor.ret": 30.0}[entity_id]

    ha_api.get_state.side_effect = fake_get_state
    ha_api.get_raw_state.return_value = "heating"
    mqtt_client = MagicMock()

    main_module._run_telemetry_tick(
        manifest, ha_api, mqtt_client, options={}, boost_active=True, failsafe_active=False,
    )

    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert payload["room_actual"] == 20.5
    assert payload["boost_active"] is True
    assert payload["return_temperature"] == 30.0
    assert payload["operating_mode"] == "heating"
    assert "flow_temperature" not in payload


def test_run_telemetry_tick_omits_operating_mode_when_sensor_unavailable():
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual",
        "operating_mode": "sensor.mode",
    })
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.5
    ha_api.get_raw_state.side_effect = ValueError("unavailable")
    mqtt_client = MagicMock()

    main_module._run_telemetry_tick(
        manifest, ha_api, mqtt_client, options={}, boost_active=False, failsafe_active=False,
    )

    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert payload["room_actual"] == 20.5
    assert "operating_mode" not in payload


def test_run_telemetry_tick_omits_non_finite_kpi_values():
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual",
        "flow_temperature": "sensor.flow",
        "return_temperature": "sensor.ret",
        "energy_thermal_heating": "sensor.e1",
        "energy_thermal_dhw": "sensor.e2",
    })
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda entity_id: {
        "sensor.room_actual": 20.5, "sensor.flow": float("nan"), "sensor.ret": 30.0,
        "sensor.e1": float("inf"), "sensor.e2": 12.0,
    }[entity_id]
    mqtt_client = MagicMock()

    main_module._run_telemetry_tick(
        manifest, ha_api, mqtt_client, options={}, boost_active=False, failsafe_active=False,
    )

    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert payload["room_actual"] == 20.5
    assert "flow_temperature" not in payload
    assert payload["return_temperature"] == 30.0
    assert payload["energy"] == {"thermal_dhw": 12.0}


def test_run_telemetry_tick_omits_energy_key_when_all_channels_fail():
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual",
        "energy_thermal_heating": "sensor.e1",
        "energy_thermal_dhw": "sensor.e2",
    })
    ha_api = MagicMock()

    def fake_get_state(entity_id):
        if entity_id == "sensor.room_actual":
            return 20.5
        raise ValueError("unavailable")

    ha_api.get_state.side_effect = fake_get_state
    mqtt_client = MagicMock()

    main_module._run_telemetry_tick(
        manifest, ha_api, mqtt_client, options={}, boost_active=False, failsafe_active=False,
    )

    payload = mqtt_client.publish_telemetry.call_args.args[0]
    assert payload["room_actual"] == 20.5
    assert "energy" not in payload


def test_run_telemetry_tick_skips_when_room_actual_not_mapped():
    manifest = ChannelManifest(entity_ids={})
    ha_api = MagicMock()
    mqtt_client = MagicMock()

    main_module._run_telemetry_tick(
        manifest, ha_api, mqtt_client, options={}, boost_active=False, failsafe_active=False,
    )

    ha_api.get_state.assert_not_called()
    mqtt_client.publish_telemetry.assert_not_called()


def test_run_telemetry_tick_survives_exception_without_propagating(monkeypatch, caplog):
    manifest = ChannelManifest(entity_ids={"room_actual": "sensor.room_actual"})
    ha_api = MagicMock()
    ha_api.get_state.side_effect = OSError("SD-Karte voll")
    mqtt_client = MagicMock()

    with caplog.at_level("ERROR"):
        main_module._run_telemetry_tick(
            manifest, ha_api, mqtt_client, options={}, boost_active=False, failsafe_active=False,
        )  # must not raise

    assert "Telemetrie" in caplog.text


def test_strip_attribute_suffix_removes_climate_attribute_syntax():
    assert main_module._strip_attribute_suffix("climate.wohnzimmer::temperature") == "climate.wohnzimmer"


def test_strip_attribute_suffix_passes_through_plain_entity_id():
    assert main_module._strip_attribute_suffix("sensor.target_rt") == "sensor.target_rt"


def test_build_ha_trigger_client_includes_room_roles_and_daily_time():
    manifest = ChannelManifest(entity_ids={
        "room_target": "climate.wohnzimmer::temperature", "room_actual": "sensor.rt",
    })
    ha_api = MagicMock()
    ha_api.websocket_url.return_value = "ws://x/api/websocket"
    ha_api.token = "tok"
    options = {"daily_trigger_time": "12:00"}
    boost_state = main_module._BoostStateBox(active=False)

    with patch("heizungsbruecke.__main__.HaTriggerClient") as fake_cls:
        main_module._build_ha_trigger_client(
            manifest=manifest, ha_api=ha_api, options=options, mqtt_client=MagicMock(),
            write_lock=threading.RLock(), boost_state=boost_state, failsafe_ctx=None,
            stable_target=main_module._StableTargetBox(value=None),
        )

    _, kwargs = fake_cls.call_args
    assert kwargs["ws_url"] == "ws://x/api/websocket"
    assert kwargs["token"] == "tok"
    assert {
        "platform": "state", "entity_id": "climate.wohnzimmer",
        "attribute": "temperature", "for": {"seconds": 10},
    } in kwargs["triggers"]
    assert {"platform": "state", "entity_id": "sensor.rt"} in kwargs["triggers"]
    assert {"platform": "time", "at": "12:00"} in kwargs["triggers"]


def test_build_ha_trigger_client_omits_attribute_for_plain_room_target_entity():
    # Design-Spec 2026-09-22: the attribute filter is derived from the `::`-suffix
    # convention, not hardcoded to "temperature" -- a room_target mapped to a plain
    # entity (no climate-attribute syntax) still gets the 10s `for:` debounce, but no
    # `attribute` key (there is no secondary attribute to filter on).
    manifest = ChannelManifest(entity_ids={"room_target": "sensor.target_rt"})
    ha_api = MagicMock()
    ha_api.websocket_url.return_value = "ws://x/api/websocket"
    ha_api.token = "tok"
    boost_state = main_module._BoostStateBox(active=False)

    with patch("heizungsbruecke.__main__.HaTriggerClient") as fake_cls:
        main_module._build_ha_trigger_client(
            manifest=manifest, ha_api=ha_api, options={}, mqtt_client=MagicMock(),
            write_lock=threading.RLock(), boost_state=boost_state, failsafe_ctx=None,
            stable_target=main_module._StableTargetBox(value=None),
        )

    _, kwargs = fake_cls.call_args
    assert kwargs["triggers"] == [{"platform": "state", "entity_id": "sensor.target_rt", "for": {"seconds": 10}}]


def test_extract_attribute_suffix_returns_attribute_name():
    assert main_module._extract_attribute_suffix("climate.wohnzimmer::temperature") == "temperature"


def test_extract_attribute_suffix_returns_none_for_plain_entity_id():
    assert main_module._extract_attribute_suffix("sensor.target_rt") is None


def test_trigger_event_callback_invokes_run_local_check_and_updates_shared_box(tmp_path, monkeypatch):
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual", "room_target": "sensor.room_target",
    })
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda entity_id: {
        "sensor.room_actual": 19.0, "sensor.room_target": 21.0,
    }[entity_id]
    mqtt_client = MagicMock()
    write_lock = threading.RLock()
    boost_state = main_module._BoostStateBox(active=False)

    callback = main_module._make_trigger_event_callback(
        manifest=manifest, ha_api=ha_api, mqtt_client=mqtt_client, options=_base_options(),
        write_lock=write_lock, boost_state=boost_state, failsafe_ctx=None,
        stable_target=main_module._StableTargetBox(value=None),
    )
    callback({"platform": "state", "entity_id": "sensor.room_target"})

    assert boost_state.active is False  # no previous_room_target -> decide_boost never triggers on first tick


def test_trigger_event_callback_and_watchdog_fallback_share_lock_without_deadlock(tmp_path, monkeypatch):
    """Regression test for the concurrency fix: both call sites wrap their
    `_run_local_check` call in `with write_lock:`, while `_run_local_check` itself ALSO
    acquires the same `write_lock` internally -- only safe because write_lock is now an
    RLock. Simulates the watchdog loop already holding write_lock (its own wrapping)
    while the trigger callback fires -- exactly the reentrant-acquisition scenario a
    plain Lock would deadlock on.
    """
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual", "room_target": "sensor.room_target",
    })
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda entity_id: {
        "sensor.room_actual": 19.0, "sensor.room_target": 21.0,
    }[entity_id]
    mqtt_client = MagicMock()
    write_lock = threading.RLock()
    boost_state = main_module._BoostStateBox(active=False)

    callback = main_module._make_trigger_event_callback(
        manifest=manifest, ha_api=ha_api, mqtt_client=mqtt_client, options=_base_options(),
        write_lock=write_lock, boost_state=boost_state, failsafe_ctx=None,
        stable_target=main_module._StableTargetBox(value=None),
    )

    with write_lock:
        callback({"platform": "state", "entity_id": "sensor.room_target"})  # must not deadlock

    assert boost_state.active is False


def test_trigger_event_callback_survives_exception_without_propagating(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual", "room_target": "sensor.room_target",
    })
    ha_api = MagicMock()
    ha_api.get_state.side_effect = OSError("HA nicht erreichbar")
    write_lock = threading.RLock()
    boost_state = main_module._BoostStateBox(active=False)

    callback = main_module._make_trigger_event_callback(
        manifest=manifest, ha_api=ha_api, mqtt_client=MagicMock(), options=_base_options(),
        write_lock=write_lock, boost_state=boost_state, failsafe_ctx=None,
        stable_target=main_module._StableTargetBox(value=None),
    )

    with caplog.at_level("ERROR"):
        callback({"platform": "state", "entity_id": "sensor.room_target"})  # must not raise

    assert "lokalen Check" in caplog.text


def test_trigger_event_callback_refreshes_stable_target_when_room_target_trigger_fires(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual", "room_target": "sensor.room_target",
    })
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda entity_id: {
        "sensor.room_actual": 19.0, "sensor.room_target": 21.0,
    }[entity_id]
    write_lock = threading.RLock()
    boost_state = main_module._BoostStateBox(active=False)
    stable_target = main_module._StableTargetBox(value=18.0)  # stale pre-event cache value

    callback = main_module._make_trigger_event_callback(
        manifest=manifest, ha_api=ha_api, mqtt_client=MagicMock(), options=_base_options(),
        write_lock=write_lock, boost_state=boost_state, failsafe_ctx=None, stable_target=stable_target,
    )
    with caplog.at_level(logging.INFO):
        callback({"platform": "state", "entity_id": "sensor.room_target"})

    assert stable_target.value == 21.0  # refreshed from the live read
    # Whole-Branch-Review Minor #5 (final-review-report.md): make the refresh and its
    # source visible in the log.
    assert "room_target=21.0" in caplog.text


def test_trigger_event_callback_reuses_cached_value_for_room_actual_trigger_without_live_read(tmp_path, monkeypatch):
    # The gap this closes (Design-Spec 2026-09-22): a room_actual event during a
    # room_target debounce window must act on the last CONFIRMED-stable value, not a
    # fresh (possibly non-final) live read.
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual", "room_target": "sensor.room_target",
    })
    ha_api = MagicMock()

    def _get_state(entity_id):
        if entity_id == "sensor.room_target":
            raise AssertionError("room_actual-triggered call must reuse the cache, not read room_target live")
        return {"sensor.room_actual": 19.0}[entity_id]

    ha_api.get_state.side_effect = _get_state
    write_lock = threading.RLock()
    boost_state = main_module._BoostStateBox(active=False)
    stable_target = main_module._StableTargetBox(value=21.0)

    callback = main_module._make_trigger_event_callback(
        manifest=manifest, ha_api=ha_api, mqtt_client=MagicMock(), options=_base_options(),
        write_lock=write_lock, boost_state=boost_state, failsafe_ctx=None, stable_target=stable_target,
    )
    callback({"platform": "state", "entity_id": "sensor.room_actual"})

    assert stable_target.value == 21.0  # untouched


def test_trigger_event_callback_reuses_cached_value_for_daily_time_trigger_without_live_read(tmp_path, monkeypatch):
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual", "room_target": "sensor.room_target",
    })
    ha_api = MagicMock()

    def _get_state(entity_id):
        if entity_id == "sensor.room_target":
            raise AssertionError("time-triggered call must reuse the cache, not read room_target live")
        return {"sensor.room_actual": 19.0}[entity_id]

    ha_api.get_state.side_effect = _get_state
    write_lock = threading.RLock()
    boost_state = main_module._BoostStateBox(active=False)
    stable_target = main_module._StableTargetBox(value=21.0)

    callback = main_module._make_trigger_event_callback(
        manifest=manifest, ha_api=ha_api, mqtt_client=MagicMock(), options=_base_options(),
        write_lock=write_lock, boost_state=boost_state, failsafe_ctx=None, stable_target=stable_target,
    )
    callback({"platform": "time"})

    assert stable_target.value == 21.0  # untouched


def test_trigger_event_callback_matches_room_target_trigger_by_attribute_not_just_entity_id(tmp_path, monkeypatch):
    # Regression guard for the shared-entity case (room_actual and room_target mapped
    # to the SAME entity's different attributes -- see
    # test_run_local_check_persists_room_target_for_next_checks_comparison. Today's
    # SmartHeat-Integration wizard only ever maps entity_room_actual to a plain
    # `sensor`, never a `climate::attribute`, but _run_local_check's own
    # entity_id::attribute handling in ha_api.py is generic and doesn't enforce that
    # restriction, so this guards the general contract). Matching on entity_id alone
    # would misidentify a room_actual-triggered event on the shared entity as the
    # room_target trigger and refresh the cache from a non-final value.
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    manifest = ChannelManifest(entity_ids={
        "room_actual": "climate.wohnzimmer_thermostat::current_temperature",
        "room_target": "climate.wohnzimmer_thermostat::temperature",
    })
    ha_api = MagicMock()

    def _get_state(entity_id):
        if entity_id == "climate.wohnzimmer_thermostat::temperature":
            raise AssertionError("must not treat this as the room_target trigger firing")
        return {"climate.wohnzimmer_thermostat::current_temperature": 19.0}[entity_id]

    ha_api.get_state.side_effect = _get_state
    write_lock = threading.RLock()
    boost_state = main_module._BoostStateBox(active=False)
    stable_target = main_module._StableTargetBox(value=21.0)

    callback = main_module._make_trigger_event_callback(
        manifest=manifest, ha_api=ha_api, mqtt_client=MagicMock(), options=_base_options(),
        write_lock=write_lock, boost_state=boost_state, failsafe_ctx=None, stable_target=stable_target,
    )
    # Same entity_id as room_target, but no "attribute" key (mirrors the plain
    # room_actual trigger's own config, which has no attribute filter) -- must NOT match.
    callback({"platform": "state", "entity_id": "climate.wohnzimmer_thermostat"})

    assert stable_target.value == 21.0  # untouched


def test_trigger_event_callback_matches_room_target_trigger_with_attribute_field_set(tmp_path, monkeypatch):
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    manifest = ChannelManifest(entity_ids={
        "room_actual": "climate.wohnzimmer_thermostat::current_temperature",
        "room_target": "climate.wohnzimmer_thermostat::temperature",
    })
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda entity_id: {
        "climate.wohnzimmer_thermostat::current_temperature": 19.0,
        "climate.wohnzimmer_thermostat::temperature": 21.5,
    }[entity_id]
    write_lock = threading.RLock()
    boost_state = main_module._BoostStateBox(active=False)
    stable_target = main_module._StableTargetBox(value=21.0)

    callback = main_module._make_trigger_event_callback(
        manifest=manifest, ha_api=ha_api, mqtt_client=MagicMock(), options=_base_options(),
        write_lock=write_lock, boost_state=boost_state, failsafe_ctx=None, stable_target=stable_target,
    )
    callback({"platform": "state", "entity_id": "climate.wohnzimmer_thermostat", "attribute": "temperature"})

    assert stable_target.value == 21.5  # refreshed -- this IS the room_target trigger


def test_run_bridge_skips_local_check_fallback_when_trigger_client_connected(monkeypatch):
    monkeypatch.setattr(
        "heizungsbruecke.__main__.requests.get", lambda url, timeout: _FakeResponse({"active": True}),
    )
    monkeypatch.setattr("heizungsbruecke.__main__.derived_sensors.ensure_all", lambda **kwargs: {})
    monkeypatch.setattr("heizungsbruecke.__main__.BridgeMqttClient", lambda **kwargs: MagicMock())
    fake_trigger_client = MagicMock()
    fake_trigger_client.connected = True
    monkeypatch.setattr("heizungsbruecke.__main__.HaTriggerClient", lambda **kwargs: fake_trigger_client)

    call_count = {"n": 0}

    def fake_run_local_check(*args, **kwargs):
        call_count["n"] += 1
        return False

    monkeypatch.setattr("heizungsbruecke.__main__._run_local_check", fake_run_local_check)
    monkeypatch.setattr("heizungsbruecke.__main__._run_telemetry_tick", lambda *a, **kw: None)
    monkeypatch.setattr("heizungsbruecke.__main__.daynight_snapshot.maybe_snapshot", lambda **kwargs: None)

    def stop_after_first_sleep(seconds):
        raise SystemExit("stop test loop")

    monkeypatch.setattr("heizungsbruecke.__main__.time.sleep", stop_after_first_sleep)

    options = _full_valid_options()
    with pytest.raises(SystemExit):
        _run_bridge(options, MagicMock())

    # Exactly 1 call: the synchronous priming call before loop_start(). The main loop's
    # own conditional call must NOT have fired, since fake_trigger_client.connected=True.
    assert call_count["n"] == 1


def test_run_bridge_runs_local_check_fallback_when_trigger_client_disconnected(monkeypatch):
    monkeypatch.setattr(
        "heizungsbruecke.__main__.requests.get", lambda url, timeout: _FakeResponse({"active": True}),
    )
    monkeypatch.setattr("heizungsbruecke.__main__.derived_sensors.ensure_all", lambda **kwargs: {})
    monkeypatch.setattr("heizungsbruecke.__main__.BridgeMqttClient", lambda **kwargs: MagicMock())
    fake_trigger_client = MagicMock()
    fake_trigger_client.connected = False
    monkeypatch.setattr("heizungsbruecke.__main__.HaTriggerClient", lambda **kwargs: fake_trigger_client)

    call_count = {"n": 0}

    def fake_run_local_check(*args, **kwargs):
        call_count["n"] += 1
        return False

    monkeypatch.setattr("heizungsbruecke.__main__._run_local_check", fake_run_local_check)
    monkeypatch.setattr("heizungsbruecke.__main__._run_telemetry_tick", lambda *a, **kw: None)
    monkeypatch.setattr("heizungsbruecke.__main__.daynight_snapshot.maybe_snapshot", lambda **kwargs: None)

    def stop_after_first_sleep(seconds):
        raise SystemExit("stop test loop")

    monkeypatch.setattr("heizungsbruecke.__main__.time.sleep", stop_after_first_sleep)

    options = _full_valid_options()
    with pytest.raises(SystemExit):
        _run_bridge(options, MagicMock())

    # Priming call + the main loop's own conditional call, since connected=False.
    assert call_count["n"] == 2


def test_run_bridge_starts_the_trigger_client(monkeypatch):
    monkeypatch.setattr(
        "heizungsbruecke.__main__.requests.get", lambda url, timeout: _FakeResponse({"active": True}),
    )
    monkeypatch.setattr("heizungsbruecke.__main__.derived_sensors.ensure_all", lambda **kwargs: {})
    monkeypatch.setattr("heizungsbruecke.__main__.BridgeMqttClient", lambda **kwargs: MagicMock())
    fake_trigger_client = MagicMock()
    fake_trigger_client.connected = True
    monkeypatch.setattr("heizungsbruecke.__main__.HaTriggerClient", lambda **kwargs: fake_trigger_client)
    monkeypatch.setattr("heizungsbruecke.__main__._run_local_check", lambda *a, **kw: False)
    monkeypatch.setattr("heizungsbruecke.__main__._run_telemetry_tick", lambda *a, **kw: None)
    monkeypatch.setattr("heizungsbruecke.__main__.daynight_snapshot.maybe_snapshot", lambda **kwargs: None)
    monkeypatch.setattr(
        "heizungsbruecke.__main__.time.sleep", lambda s: (_ for _ in ()).throw(SystemExit("stop")),
    )

    options = _full_valid_options()
    with pytest.raises(SystemExit):
        _run_bridge(options, MagicMock())

    fake_trigger_client.start.assert_called_once()


def test_run_bridge_seeds_stable_target_cache_from_a_live_read_before_loop_start(monkeypatch):
    monkeypatch.setattr(
        "heizungsbruecke.__main__.requests.get", lambda url, timeout: _FakeResponse({"active": True}),
    )
    monkeypatch.setattr("heizungsbruecke.__main__.derived_sensors.ensure_all", lambda **kwargs: {})
    monkeypatch.setattr("heizungsbruecke.__main__.BridgeMqttClient", lambda **kwargs: MagicMock())
    monkeypatch.setattr("heizungsbruecke.__main__.HaTriggerClient", lambda **kwargs: MagicMock(connected=True))
    monkeypatch.setattr("heizungsbruecke.__main__._run_telemetry_tick", lambda *a, **kw: None)
    monkeypatch.setattr("heizungsbruecke.__main__.daynight_snapshot.maybe_snapshot", lambda **kwargs: None)
    monkeypatch.setattr(
        "heizungsbruecke.__main__.time.sleep", lambda s: (_ for _ in ()).throw(SystemExit("stop")),
    )

    observed_room_target = []

    def fake_run_local_check(
        manifest, ha_api, mqtt_client, options, write_lock, boost_was_active, room_target=None, failsafe_ctx=None,
    ):
        observed_room_target.append(room_target)
        return boost_was_active

    monkeypatch.setattr("heizungsbruecke.__main__._run_local_check", fake_run_local_check)

    ha_api = MagicMock()
    ha_api.get_state.return_value = 19.5  # single stand-in value, whichever entity is read

    options = _full_valid_options()
    with pytest.raises(SystemExit):
        _run_bridge(options, ha_api)

    # The synchronous priming call (before loop_start()) must receive a room_target
    # already resolved from a live read, not None.
    assert observed_room_target[0] == 19.5


def test_run_bridge_watchdog_fallback_refreshes_stable_target_cache_from_a_live_read(monkeypatch):
    monkeypatch.setattr(
        "heizungsbruecke.__main__.requests.get", lambda url, timeout: _FakeResponse({"active": True}),
    )
    monkeypatch.setattr("heizungsbruecke.__main__.derived_sensors.ensure_all", lambda **kwargs: {})
    monkeypatch.setattr("heizungsbruecke.__main__.BridgeMqttClient", lambda **kwargs: MagicMock())
    monkeypatch.setattr("heizungsbruecke.__main__.HaTriggerClient", lambda **kwargs: MagicMock(connected=False))
    monkeypatch.setattr("heizungsbruecke.__main__._run_telemetry_tick", lambda *a, **kw: None)
    monkeypatch.setattr("heizungsbruecke.__main__.daynight_snapshot.maybe_snapshot", lambda **kwargs: None)
    monkeypatch.setattr(
        "heizungsbruecke.__main__.time.sleep", lambda s: (_ for _ in ()).throw(SystemExit("stop")),
    )

    observed_room_target = []

    def fake_run_local_check(
        manifest, ha_api, mqtt_client, options, write_lock, boost_was_active, room_target=None, failsafe_ctx=None,
    ):
        observed_room_target.append(room_target)
        return boost_was_active

    monkeypatch.setattr("heizungsbruecke.__main__._run_local_check", fake_run_local_check)

    ha_api = MagicMock()
    room_target_values = iter([19.5, 22.0])  # priming read, then the watchdog fallback's own live read
    ha_api.get_state.side_effect = lambda entity_id: next(room_target_values)

    options = _full_valid_options()
    with pytest.raises(SystemExit):
        _run_bridge(options, ha_api)

    # Priming call saw the first live read; the watchdog fallback's own live read
    # (HaTriggerClient.connected=False here) produced a second, different room_target
    # -- proving it re-reads live on every fallback tick rather than reusing the
    # priming value forever.
    assert observed_room_target == [19.5, 22.0]


def test_build_ha_trigger_client_passes_on_connected_callback_that_reseeds_cache_under_write_lock():
    # Re-Review final-review-report.md, successor to d8176c2's was_connected/elif
    # watchdog-sampling approach: the re-seed responsibility now lives entirely in the
    # on_connected callback HaTriggerClient itself invokes -- this test proves
    # _build_ha_trigger_client actually wires one in, that it re-reads room_target live
    # and writes it into the shared stable_target box, and that it can be called while
    # write_lock is already held by the caller (the real HaTriggerClient invokes it
    # from its own background WS-callback thread, independently of the watchdog loop
    # -- but the lock itself must stay reentrant-safe either way).
    manifest = ChannelManifest(entity_ids={"room_target": "sensor.room_target"})
    ha_api = MagicMock()
    ha_api.websocket_url.return_value = "ws://x/api/websocket"
    ha_api.token = "tok"
    ha_api.get_state.return_value = 23.5
    write_lock = threading.RLock()
    boost_state = main_module._BoostStateBox(active=False)
    stable_target = main_module._StableTargetBox(value=None)

    with patch("heizungsbruecke.__main__.HaTriggerClient") as fake_cls:
        main_module._build_ha_trigger_client(
            manifest=manifest, ha_api=ha_api, options={}, mqtt_client=MagicMock(),
            write_lock=write_lock, boost_state=boost_state, failsafe_ctx=None,
            stable_target=stable_target,
        )

    _, kwargs = fake_cls.call_args
    on_connected = kwargs["on_connected"]
    assert callable(on_connected)

    with write_lock:  # reentrant acquisition, as the real background thread would race the loop
        on_connected()

    assert stable_target.value == 23.5


def test_make_on_connected_callback_reseeds_stable_target_and_logs(caplog):
    manifest = ChannelManifest(entity_ids={"room_target": "sensor.room_target"})
    ha_api = MagicMock()
    ha_api.get_state.return_value = 19.5
    write_lock = threading.RLock()
    stable_target = main_module._StableTargetBox(value=None)

    callback = main_module._make_on_connected_callback(
        manifest=manifest, ha_api=ha_api, write_lock=write_lock, stable_target=stable_target,
    )
    with caplog.at_level(logging.INFO):
        callback()

    assert stable_target.value == 19.5
    assert "On-Connect-Hook" in caplog.text
    assert "19.5" in caplog.text


class _AlwaysConnectedCapturingTriggerClient:
    """Test double for HaTriggerClient whose `.connected` is permanently True -- from
    the watchdog loop's own polling perspective, this client NEVER appears to have
    been disconnected, i.e. it can never expose a False->True transition no matter how
    many ticks run. Captures the `on_connected` callback passed at construction time so
    the test can invoke it directly, exactly as the real HaTriggerClient does from
    `_handle_subscribe_result` on every successful (re)connection -- this is what
    proves the stable-target re-seed no longer depends on the watchdog loop ever
    sampling a disconnected state, closing the exact gap the old d8176c2
    was_connected/elif approach could not close for an outage shorter than
    local_check_interval_seconds (see the re-review in final-review-report.md).
    """
    def __init__(self, **kwargs):
        self.on_connected = kwargs["on_connected"]
        self.connected = True
        self.start = MagicMock()


def test_on_connect_hook_reseeds_stable_target_cache_without_watchdog_ever_observing_a_transition(
    monkeypatch, caplog,
):
    monkeypatch.setattr(
        "heizungsbruecke.__main__.requests.get", lambda url, timeout: _FakeResponse({"active": True}),
    )
    monkeypatch.setattr("heizungsbruecke.__main__.derived_sensors.ensure_all", lambda **kwargs: {})
    monkeypatch.setattr("heizungsbruecke.__main__.BridgeMqttClient", lambda **kwargs: MagicMock())

    captured = {}

    def _factory(**kwargs):
        captured["client"] = _AlwaysConnectedCapturingTriggerClient(**kwargs)
        return captured["client"]

    monkeypatch.setattr("heizungsbruecke.__main__.HaTriggerClient", _factory)
    monkeypatch.setattr("heizungsbruecke.__main__._run_telemetry_tick", lambda *a, **kw: None)
    monkeypatch.setattr("heizungsbruecke.__main__.daynight_snapshot.maybe_snapshot", lambda **kwargs: None)
    monkeypatch.setattr(
        "heizungsbruecke.__main__.time.sleep", lambda s: (_ for _ in ()).throw(SystemExit("stop")),
    )

    run_local_check_room_targets = []

    def fake_run_local_check(
        manifest, ha_api, mqtt_client, options, write_lock, boost_was_active, room_target=None, failsafe_ctx=None,
    ):
        run_local_check_room_targets.append(room_target)
        return boost_was_active

    monkeypatch.setattr("heizungsbruecke.__main__._run_local_check", fake_run_local_check)

    ha_api = MagicMock()
    room_target_values = iter([15.0, 42.0])  # boot-priming read, then the on-connect-hook's own live read
    ha_api.get_state.side_effect = lambda entity_id: next(room_target_values)

    options = _full_valid_options()
    with caplog.at_level(logging.INFO):
        with pytest.raises(SystemExit):
            _run_bridge(options, ha_api)

        # Only the boot-priming call ran _run_local_check so far; `.connected` was True
        # for the one watchdog tick this test drove, so the "not connected" fallback
        # branch never fired either -- it never toggled False, exactly the point of
        # this double.
        assert run_local_check_room_targets == [15.0]

        # Now simulate the real HaTriggerClient firing its on-connect hook from its own
        # background WS-callback thread -- e.g. a reconnect after an outage that
        # started and fully resolved entirely between two watchdog ticks. The test
        # double's `.connected` never toggled False, so no watchdog-loop sampling could
        # have observed this as a transition; the re-seed must still happen.
        captured["client"].on_connected()

    assert ha_api.get_state.call_count == 2
    assert "On-Connect-Hook" in caplog.text
    assert "42.0" in caplog.text
    # The on-connect hook only refreshes the cache -- it must not itself trigger a
    # local check (the next real trigger, in practice usually room_actual, does that
    # with the now-fresh cache value).
    assert run_local_check_room_targets == [15.0]

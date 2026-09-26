import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
import requests

import heizungsbruecke.__main__ as main_module
from heizungsbruecke.ha_trigger_client import HaTriggerClient
from heizungsbruecke.__main__ import (
    ABO_ENDED_MESSAGE,
    HELPER_NOTIFICATION_MESSAGE,
    _check_timezone,
    _claim_due_tick,
    _abo_inactive_message,
    _end_emergency_boost_if_active,
    _ensure_derived_sensors_with_retry,
    _enter_abo_inactive,
    _finish_abo_grace,
    _load_failsafe_ctx,
    _load_failsafe_ctx_safe,
    _run_bridge,
    _run_local_check,
    _save_emergency_active_if_changed,
    _save_failsafe_ctx,
)
from heizungsbruecke.backup_store import load_backup, save_backup
from heizungsbruecke.delivery import DeliveryState
from heizungsbruecke.profiles import UnknownProfileError
from heizungsbruecke.manifest import ChannelManifest


@pytest.fixture(autouse=True)
def _isolate_entitlement_path(tmp_path, monkeypatch):
    monkeypatch.setattr("heizungsbruecke.__main__.ENTITLEMENT_PATH", tmp_path / "entitlement_state.json")


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


def test_run_bridge_returns_true_when_not_configured(caplog):
    with caplog.at_level("INFO"):
        result = _run_bridge({}, MagicMock())

    assert result == 0
    assert "Add-on ist noch nicht eingerichtet" in caplog.text


def test_run_bridge_returns_false_on_genuine_validation_error(monkeypatch, caplog):
    # Unlike the "not configured" case above, an add-on that IS configured but fails
    # validation (here: an unknown profile) is a genuine startup error -- main() must
    # be able to tell the two apart to give the Supervisor a non-zero exit code.
    monkeypatch.setattr(
        "heizungsbruecke.entitlement.requests.get",
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

    assert result == 1
    assert "FEHLER" in caplog.text


def test_run_local_check_evaluates_boost_without_publishing_when_nothing_changed(tmp_path, monkeypatch):
    # _run_local_check publiziert nicht mehr selbst: ob ein Tick faellig ist, entscheidet
    # danach _claim_due_tick (eigene Tests unten, Zustellung in test_runtime.py).
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    save_backup(backup_path, {"last_published_target_rt": 20.0})
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual",
        "room_target": "sensor.room_target",
    })
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.0  # actual == target, no previous target -> boost stays inactive
    options = _base_options()

    new_state = _run_local_check(manifest, ha_api, options, boost_was_active=False, room_target=20.0)

    assert new_state is False


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
    options = _base_options()

    # _run_tick itself must not swallow the error -- main()'s while-loop try/except
    # (I2) is what's responsible for catching, logging and continuing to the next tick.
    with pytest.raises(RuntimeError):
        _run_local_check(manifest, ha_api, options, boost_was_active=False, room_target=21.0)


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
    options = {
        "boost_threshold_k": 0.5, "boost_curve_value": 1.5, "boost_offset_value": 30.0,
        "curve_min": 0.4, "curve_max": 1.5, "offset_min": 20.0, "offset_max": 30.0,
    }

    _run_local_check(manifest, ha_api, options, boost_was_active=False, room_target=21.0)

    assert load_backup(tmp_path / "backup.json")["last_room_target"] == 21.0


def test_run_local_check_triggers_boost_on_target_raise_between_checks(tmp_path, monkeypatch):
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    save_backup(backup_path, {"last_room_target": 20.0, "curve_current": 0.9, "offset_current": 22.0})
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

    boost_was_active = _run_local_check(manifest, ha_api, options, boost_was_active=False, room_target=21.0)

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
        manifest, ha_api, options, boost_was_active=False, room_target=21.0,
    )

    assert new_state is False  # no previous_room_target recorded yet -> boost stays inactive


def test_run_local_check_returns_unchanged_state_when_room_target_parameter_is_none(tmp_path, monkeypatch, caplog):
    # Boot-priming edge case (Design-Spec 2026-09-22, Abschnitt 2): the stable-target
    # cache can still be unpopulated (e.g. a failed boot-time live read) even though
    # both roles are mapped -- must no-op instead of crashing on a None comparison
    # inside decide_boost.
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual", "room_target": "sensor.room_target",
    })
    ha_api = MagicMock()

    with caplog.at_level(logging.WARNING):
        new_state = _run_local_check(
            manifest, ha_api, _base_options(), boost_was_active=True, room_target=None,
        )

    assert new_state is True  # unchanged, no decision made
    ha_api.get_state.assert_not_called()
    # Whole-Branch-Review Minor #5 (final-review-report.md): this no-op must be visible
    # in the log -- otherwise it is indistinguishable from a regular skip once deployed.
    assert "room_target=None" in caplog.text


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


def test_load_failsafe_ctx_defaults_when_no_file(tmp_path):
    ctx = _load_failsafe_ctx(tmp_path / "does_not_exist.json", tmp_path / "no_backup.json")

    assert ctx["delivery"].notbetrieb is False
    assert ctx["emergency_boost_active"] is False


def test_save_and_load_failsafe_ctx_round_trip(tmp_path):
    # Design-Spec 2026-09-23, Abschnitt 4: `delivery` (failsafe_state.json) UND
    # `emergency_boost_active` (backup.json) ueberleben einen Neustart.
    path = tmp_path / "failsafe_state.json"
    backup_path = tmp_path / "backup.json"
    ctx = {"delivery": DeliveryState(notbetrieb=True), "emergency_boost_active": True}

    _save_failsafe_ctx(ctx, path)
    _save_emergency_active_if_changed(ctx["emergency_boost_active"], backup_path)
    loaded = _load_failsafe_ctx(path, backup_path)

    assert loaded["delivery"].notbetrieb is True
    assert loaded["emergency_boost_active"] is True


def test_load_failsafe_ctx_safe_defaults_when_no_file(tmp_path):
    ctx = _load_failsafe_ctx_safe(tmp_path / "does_not_exist.json", tmp_path / "no_backup.json")

    assert ctx["delivery"].notbetrieb is False
    assert ctx["emergency_boost_active"] is False


def test_load_failsafe_ctx_safe_falls_back_on_corrupt_file(tmp_path):
    # Simulates power loss on the Pi's SD card mid-write: a truncated/corrupt state
    # file must not crash the whole add-on at startup.
    path = tmp_path / "failsafe_state.json"
    path.write_bytes(b"{not valid json..")

    ctx = _load_failsafe_ctx_safe(path, tmp_path / "no_backup.json")

    assert ctx["delivery"].notbetrieb is False
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

    assert ctx["delivery"].notbetrieb is False
    assert ctx["emergency_boost_active"] is True


def test_load_failsafe_ctx_safe_falls_back_on_corrupt_backup_file(tmp_path):
    path = tmp_path / "failsafe_state.json"
    save_backup(path, {"failsafe_active": True})
    backup_path = tmp_path / "backup.json"
    backup_path.write_bytes(b"{not valid json..")

    ctx = _load_failsafe_ctx_safe(path, backup_path)

    assert ctx["delivery"].notbetrieb is True
    assert ctx["emergency_boost_active"] is False


def test_load_failsafe_ctx_safe_passes_through_valid_file(tmp_path):
    path = tmp_path / "failsafe_state.json"
    backup_path = tmp_path / "backup.json"
    ctx = {"delivery": DeliveryState(notbetrieb=True), "emergency_boost_active": True}
    _save_failsafe_ctx(ctx, path)
    _save_emergency_active_if_changed(ctx["emergency_boost_active"], backup_path)

    loaded = _load_failsafe_ctx_safe(path, backup_path)

    assert loaded["delivery"].notbetrieb is True
    assert loaded["emergency_boost_active"] is True


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
    failsafe_ctx = {"delivery": DeliveryState(notbetrieb=False), "emergency_boost_active": True}

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
    failsafe_ctx = {"delivery": DeliveryState(notbetrieb=False), "emergency_boost_active": False}

    _end_emergency_boost_if_active(failsafe_ctx, manifest, ha_api, options)

    ha_api.set_number_value.assert_not_called()


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
    failsafe_ctx = {"delivery": DeliveryState(notbetrieb=True), "emergency_boost_active": True}

    boost_active = _run_local_check(
        manifest, ha_api, _base_options(), boost_was_active=False,
        room_target=21.0, failsafe_ctx=failsafe_ctx,
    )

    assert device == {"number.curve": 0.8, "number.offset": 5.0}
    assert boost_active is True
    assert load_backup(backup_path)["boost_active"] is True
    assert failsafe_ctx["emergency_boost_active"] is True


def test_precedence_emergency_exit_via_local_check_fallback_restores_comfort_values(tmp_path, monkeypatch):
    # I2 (b), via _run_local_check's fallback branch (Notbetrieb already inactive,
    # emergency flag still set): same hand-over to the comfort values.
    backup_path, manifest, ha_api, device, _ = _precedence_setup(
        tmp_path, monkeypatch,
        backup={"emergency_boost_active": True, "boost_active": True, "last_room_target": 21.0},
        room_actual=19.0,
    )
    device.update({"number.curve": 0.8, "number.offset": 5.0})
    failsafe_ctx = {"delivery": DeliveryState(notbetrieb=False), "emergency_boost_active": True}

    boost_active = _run_local_check(
        manifest, ha_api, _base_options(), boost_was_active=True,
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
    failsafe_ctx = {"delivery": DeliveryState(notbetrieb=False), "emergency_boost_active": True}

    _run_local_check(
        manifest, ha_api, _base_options(), boost_was_active=False,
        room_target=21.0, failsafe_ctx=failsafe_ctx,
    )

    assert device == {"number.curve": 0.3, "number.offset": 1.0}


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
        lambda options, ha_api: calls.append((options, ha_api)) or 0,
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
    monkeypatch.setattr("heizungsbruecke.__main__._run_bridge", lambda options, ha_api: 1)

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
        "heizungsbruecke.entitlement.requests.get",
        lambda url, timeout: _FakeResponse({"active": True}),
    )
    options = _full_valid_options(telemetry_interval_seconds=5)
    ha_api = MagicMock()

    with caplog.at_level(logging.ERROR):
        result = _run_bridge(options, ha_api)

    assert result == 1
    assert "telemetry_interval_seconds" in caplog.text


def test_run_local_check_persists_boost_active_true_on_transition_to_active(tmp_path, monkeypatch):
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    save_backup(backup_path, {"last_room_target": 20.0, "curve_current": 0.9, "offset_current": 22.0})
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

    new_state = _run_local_check(manifest, ha_api, options, boost_was_active=False, room_target=21.0)

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

    new_state = _run_local_check(manifest, ha_api, options, boost_was_active=True, room_target=21.0)

    assert new_state is False
    assert load_backup(backup_path)["boost_active"] is False


def test_run_local_check_skips_all_writes_when_nothing_changed(tmp_path, monkeypatch):
    # SD-wear regression guard (Design-Spec 2026-09-16, Abschnitt A.2): with the local
    # check now running as often as every 30s, a steady-state call (same room_target,
    # boost stays inactive) must not touch backup.json at all.
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    # target_history is also pre-seeded (Task 9) so that record_change returns unchanged
    # and no backup write happens for steady-state (same target value).
    save_backup(backup_path, {
        "last_room_target": 20.0, "boost_active": False, "last_published_target_rt": 20.0,
        "target_history": [[0, 20.0]],
    })
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

    _run_local_check(manifest, ha_api, _base_options(), boost_was_active=False, room_target=20.0)

    assert save_calls == []


def test_run_local_check_activates_emergency_boost_when_notbetrieb_active_and_room_cold(tmp_path, monkeypatch):
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    save_backup(tmp_path / "backup.json", {"curve_current": 0.5, "offset_current": 2.0})
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual", "room_target": "sensor.room_target",
        "curve_current": "number.curve", "offset_current": "number.offset",
    })
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda entity_id: {
        "sensor.room_actual": 18.5, "sensor.room_target": 20.0,
    }[entity_id]
    options = _base_options()
    failsafe_ctx = {"delivery": DeliveryState(notbetrieb=True), "emergency_boost_active": False}

    _run_local_check(
        manifest, ha_api, options, boost_was_active=False,
        room_target=20.0, failsafe_ctx=failsafe_ctx,
    )

    assert failsafe_ctx["emergency_boost_active"] is True
    ha_api.set_number_value.assert_any_call("number.curve", options["curve_max"])
    ha_api.set_number_value.assert_any_call("number.offset", options["offset_max"])


def test_run_local_check_does_not_activate_emergency_boost_when_notbetrieb_inactive(tmp_path, monkeypatch):
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual", "room_target": "sensor.room_target",
        "curve_current": "number.curve", "offset_current": "number.offset",
    })
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda entity_id: {
        "sensor.room_actual": 18.5, "sensor.room_target": 20.0,
    }[entity_id]
    options = _base_options()
    failsafe_ctx = {"delivery": DeliveryState(notbetrieb=False), "emergency_boost_active": False}

    _run_local_check(
        manifest, ha_api, options, boost_was_active=False,
        room_target=20.0, failsafe_ctx=failsafe_ctx,
    )

    assert failsafe_ctx["emergency_boost_active"] is False
    ha_api.set_number_value.assert_not_called()


def test_run_local_check_ends_emergency_boost_once_notbetrieb_state_is_inactive(tmp_path, monkeypatch):
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    save_backup(backup_path, {"emergency_boost_active": True, "curve_current": 0.5})
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual", "room_target": "sensor.room_target",
        "curve_current": "number.curve",
    })
    ha_api = MagicMock()
    ha_api.get_state.side_effect = lambda entity_id: {
        "sensor.room_actual": 18.5, "sensor.room_target": 20.0,
    }[entity_id]
    options = _base_options()
    # Notbetrieb ist bereits (z.B. durch einen erfolgreichen Ack) beendet, aber die
    # Notfall-Exkursion selbst lief noch -- _run_local_check muss sie beenden, statt auf
    # ihre eigene Exit-Schwelle zu warten (Review Focus #3, Rueckfallebene zu Task 4).
    failsafe_ctx = {"delivery": DeliveryState(notbetrieb=False), "emergency_boost_active": True}

    _run_local_check(
        manifest, ha_api, options, boost_was_active=False,
        room_target=20.0, failsafe_ctx=failsafe_ctx,
    )

    assert failsafe_ctx["emergency_boost_active"] is False
    ha_api.set_number_value.assert_any_call("number.curve", 0.5)


def test_run_local_check_seeds_and_records_target_history(tmp_path, monkeypatch):
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    monkeypatch.setattr("heizungsbruecke.__main__.time.time", lambda: 1_000_000.0)
    manifest = ChannelManifest(entity_ids={"room_actual": "sensor.room_actual", "room_target": "sensor.room_target"})
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.0

    _run_local_check(manifest, ha_api, _base_options(), boost_was_active=False, room_target=21.0)
    assert load_backup(backup_path)["target_history"] == [[1_000_000.0, 21.0]]

    monkeypatch.setattr("heizungsbruecke.__main__.time.time", lambda: 1_003_600.0)
    _run_local_check(manifest, ha_api, _base_options(), boost_was_active=False, room_target=22.0)
    assert load_backup(backup_path)["target_history"] == [[1_000_000.0, 21.0], [1_003_600.0, 22.0]]


def test_run_local_check_sanitizes_null_target_history(tmp_path, monkeypatch, caplog):
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    save_backup(backup_path, {"target_history": None, "last_room_target": 20.0})
    monkeypatch.setattr("heizungsbruecke.__main__.time.time", lambda: 1_000_000.0)
    manifest = ChannelManifest(entity_ids={"room_actual": "sensor.room_actual", "room_target": "sensor.room_target"})
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.0

    with caplog.at_level(logging.WARNING):
        _run_local_check(
            manifest, ha_api, _base_options(),
            boost_was_active=False, room_target=21.0,
        )

    assert load_backup(backup_path)["target_history"] == [[1_000_000.0, 21.0]]
    assert "target_history" in caplog.text


def test_run_local_check_sanitizes_malformed_target_history_entries(tmp_path, monkeypatch, caplog):
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    save_backup(backup_path, {"target_history": [["x", 1]], "last_room_target": 20.0})
    monkeypatch.setattr("heizungsbruecke.__main__.time.time", lambda: 1_000_000.0)
    manifest = ChannelManifest(entity_ids={"room_actual": "sensor.room_actual", "room_target": "sensor.room_target"})
    ha_api = MagicMock()
    ha_api.get_state.return_value = 20.0

    with caplog.at_level(logging.WARNING):
        _run_local_check(
            manifest, ha_api, _base_options(),
            boost_was_active=False, room_target=21.0,
        )

    assert load_backup(backup_path)["target_history"] == [[1_000_000.0, 21.0]]
    assert "target_history" in caplog.text


ABO_NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone(timedelta(hours=2)))


def _abo_paths(tmp_path, monkeypatch):
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", tmp_path / "backup.json")
    monkeypatch.setattr("heizungsbruecke.__main__.FAILSAFE_PATH", tmp_path / "failsafe_state.json")
    monkeypatch.setattr("heizungsbruecke.__main__.ENTITLEMENT_PATH", tmp_path / "entitlement_state.json")


def test_abo_inactive_message_contains_grace_end_date():
    assert _abo_inactive_message(ABO_NOW) == (
        "SmartHeat: Abo inaktiv. Die Heizung läuft noch bis 25.10.2026 im Notbetrieb weiter, "
        "danach bleiben die zuletzt gelernten Werte fest eingestellt."
    )


def test_enter_abo_inactive_activates_notbetrieb_stops_mqtt_and_notifies_all_channels(tmp_path, monkeypatch):
    _abo_paths(tmp_path, monkeypatch)
    failsafe_ctx = {"delivery": DeliveryState(notbetrieb=False), "emergency_boost_active": False}
    ha_api = MagicMock()
    mqtt_client = MagicMock()

    _enter_abo_inactive(failsafe_ctx, mqtt_client, ha_api, "notify.handy", ABO_NOW)

    assert failsafe_ctx["abo_inactive_since"] == ABO_NOW
    assert failsafe_ctx["delivery"].notbetrieb is True
    assert load_backup(tmp_path / "failsafe_state.json")["failsafe_active"] is True
    mqtt_client.stop.assert_called_once()
    expected = _abo_inactive_message(ABO_NOW)
    ha_api.send_notification.assert_called_once_with("notify.handy", expected)
    ha_api.create_persistent_notification.assert_called_once_with("SmartHeat", expected, "smartheat_abo_inaktiv")


def test_enter_abo_inactive_does_not_repeat_notification_when_already_marked(tmp_path, monkeypatch, caplog):
    _abo_paths(tmp_path, monkeypatch)
    from heizungsbruecke import entitlement
    entitlement.mark_inactive(tmp_path / "entitlement_state.json", ABO_NOW - timedelta(days=5))
    failsafe_ctx = {"delivery": DeliveryState(notbetrieb=False), "emergency_boost_active": False}
    ha_api = MagicMock()

    with caplog.at_level(logging.WARNING):
        _enter_abo_inactive(failsafe_ctx, None, ha_api, "notify.handy", ABO_NOW)

    assert failsafe_ctx["abo_inactive_since"] == ABO_NOW - timedelta(days=5)
    ha_api.send_notification.assert_not_called()
    ha_api.create_persistent_notification.assert_not_called()
    assert "20.10.2026" in caplog.text  # Fristende weiterhin im Log sichtbar


def test_enter_abo_inactive_is_noop_when_already_in_mode(tmp_path, monkeypatch):
    _abo_paths(tmp_path, monkeypatch)
    failsafe_ctx = {
        "delivery": DeliveryState(notbetrieb=True), "emergency_boost_active": False,
        "abo_inactive_since": ABO_NOW,
    }
    mqtt_client = MagicMock()

    _enter_abo_inactive(failsafe_ctx, mqtt_client, MagicMock(), "", ABO_NOW)

    mqtt_client.stop.assert_not_called()


def test_enter_abo_inactive_survives_failing_notification_channels(tmp_path, monkeypatch):
    _abo_paths(tmp_path, monkeypatch)
    failsafe_ctx = {"delivery": DeliveryState(notbetrieb=False), "emergency_boost_active": False}
    ha_api = MagicMock()
    ha_api.send_notification.side_effect = RuntimeError("push kaputt")
    ha_api.create_persistent_notification.side_effect = RuntimeError("ha kaputt")
    mqtt_client = MagicMock()
    mqtt_client.stop.side_effect = RuntimeError("paho kaputt")

    _enter_abo_inactive(failsafe_ctx, mqtt_client, ha_api, "notify.handy", ABO_NOW)  # darf nicht werfen

    assert failsafe_ctx["delivery"].notbetrieb is True


def test_run_local_check_in_abo_inactive_mode_runs_emergency_boost(tmp_path, monkeypatch):
    _abo_paths(tmp_path, monkeypatch)
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual", "room_target": "sensor.room_target",
        "curve_current": "number.curve", "offset_current": "number.offset",
    })
    ha_api = MagicMock()
    ha_api.get_state.return_value = 18.0  # deutlich unter Soll -> Notfall-Boost
    failsafe_ctx = {
        "delivery": DeliveryState(notbetrieb=True), "emergency_boost_active": False,
        "abo_inactive_since": ABO_NOW,
    }

    _run_local_check(
        manifest, ha_api, _base_options(), boost_was_active=False,
        room_target=21.0, failsafe_ctx=failsafe_ctx,
    )

    assert failsafe_ctx["emergency_boost_active"] is True
    ha_api.set_number_value.assert_any_call("number.curve", 0.8)


def test_finish_abo_grace_mid_emergency_boost_restores_learned_values(tmp_path, monkeypatch):
    _abo_paths(tmp_path, monkeypatch)
    save_backup(tmp_path / "backup.json", {
        "curve_current": 0.4, "offset_current": 9.0,  # offset liegt ueber offset_max=5.0 -> geclampt
        "emergency_boost_active": True, "boost_active": True,
    })
    manifest = ChannelManifest(entity_ids={"curve_current": "number.curve", "offset_current": "number.offset"})
    ha_api = MagicMock()
    failsafe_ctx = {"delivery": DeliveryState(notbetrieb=True), "emergency_boost_active": True,
                    "abo_inactive_since": ABO_NOW}

    _finish_abo_grace(
        manifest, ha_api, _base_options(notify_service="notify.handy"), failsafe_ctx,
        always_restore=True, final_notice=True,
    )

    ha_api.set_number_value.assert_any_call("number.curve", 0.4)
    ha_api.set_number_value.assert_any_call("number.offset", 5.0)
    backup = load_backup(tmp_path / "backup.json")
    assert backup["boost_active"] is False
    assert backup["emergency_boost_active"] is False
    assert failsafe_ctx["emergency_boost_active"] is False
    ha_api.send_notification.assert_called_once_with("notify.handy", ABO_ENDED_MESSAGE)
    ha_api.create_persistent_notification.assert_called_once_with("SmartHeat", ABO_ENDED_MESSAGE, "smartheat_abo_inaktiv")


def test_finish_abo_grace_counts_restore_as_done_when_saving_flags_fails(tmp_path, monkeypatch, caplog):
    _abo_paths(tmp_path, monkeypatch)
    save_backup(tmp_path / "backup.json", {"curve_current": 0.4, "offset_current": 2.0, "emergency_boost_active": True})
    manifest = ChannelManifest(entity_ids={"curve_current": "number.curve", "offset_current": "number.offset"})
    ha_api = MagicMock()
    failsafe_ctx = {"delivery": DeliveryState(notbetrieb=True), "emergency_boost_active": True}

    def _broken_save(path, values):
        raise OSError("SD-Karte kaputt")

    monkeypatch.setattr("heizungsbruecke.__main__.save_backup", _broken_save)

    with caplog.at_level(logging.ERROR):
        result = _finish_abo_grace(
            manifest, ha_api, _base_options(), failsafe_ctx, always_restore=True, final_notice=True,
        )

    assert result is True
    assert failsafe_ctx["abo_finished"] is True
    assert failsafe_ctx["emergency_boost_active"] is False
    ha_api.set_number_value.assert_any_call("number.curve", 0.4)
    assert "SD-Karte kaputt" in caplog.text


def test_finish_abo_grace_keeps_flags_when_restore_write_fails(tmp_path, monkeypatch):
    # Review Focus 3: scheitert die Wiederherstellung, muss der naechste Start es erneut versuchen.
    _abo_paths(tmp_path, monkeypatch)
    save_backup(tmp_path / "backup.json", {"curve_current": 0.4, "emergency_boost_active": True})
    manifest = ChannelManifest(entity_ids={"curve_current": "number.curve"})
    ha_api = MagicMock()
    ha_api.set_number_value.side_effect = RuntimeError("HA nicht erreichbar")
    failsafe_ctx = {"delivery": DeliveryState(notbetrieb=True), "emergency_boost_active": True}

    result = _finish_abo_grace(manifest, ha_api, _base_options(notify_service="notify.handy"), failsafe_ctx, always_restore=True, final_notice=True)  # darf nicht werfen

    assert result is False
    assert load_backup(tmp_path / "backup.json")["emergency_boost_active"] is True
    assert failsafe_ctx["emergency_boost_active"] is True
    assert not failsafe_ctx.get("abo_finished")
    ha_api.send_notification.assert_not_called()
    ha_api.create_persistent_notification.assert_not_called()


def test_finish_abo_grace_without_forced_restore_and_without_flags_writes_nothing(tmp_path, monkeypatch, caplog):
    _abo_paths(tmp_path, monkeypatch)
    save_backup(tmp_path / "backup.json", {"curve_current": 0.4, "boost_active": False})
    manifest = ChannelManifest(entity_ids={"curve_current": "number.curve"})
    ha_api = MagicMock()
    failsafe_ctx = {"delivery": DeliveryState(notbetrieb=True), "emergency_boost_active": False}

    with caplog.at_level(logging.INFO):
        _finish_abo_grace(manifest, ha_api, _base_options(notify_service="notify.handy"), failsafe_ctx, always_restore=False, final_notice=False)

    ha_api.set_number_value.assert_not_called()
    ha_api.send_notification.assert_not_called()
    ha_api.create_persistent_notification.assert_not_called()
    assert "Frist" in caplog.text


def _abo_finish_race_setup(tmp_path, monkeypatch):
    """Fristende erfolgreich abgeschlossen, danach laeuft ein noch eingestellter
    lokaler Check (Final-Review I-1). Raum deutlich unter
    Soll, Notbetrieb noch aktiv -> ohne Sperre wuerde der Notfall-Boost erneut starten."""
    _abo_paths(tmp_path, monkeypatch)
    save_backup(tmp_path / "backup.json", {
        "curve_current": 0.4, "offset_current": 2.0, "emergency_boost_active": True,
    })
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual", "room_target": "sensor.room_target",
        "curve_current": "number.curve", "offset_current": "number.offset",
    })
    ha_api = MagicMock()
    ha_api.get_state.return_value = 18.0
    failsafe_ctx = {
        "delivery": DeliveryState(notbetrieb=True), "emergency_boost_active": True,
        "abo_inactive_since": ABO_NOW,
    }
    assert _finish_abo_grace(
        manifest, ha_api, _base_options(), failsafe_ctx, always_restore=True, final_notice=True,
    ) is True
    assert failsafe_ctx["abo_finished"] is True
    ha_api.reset_mock()
    return manifest, ha_api, failsafe_ctx


def test_run_local_check_after_abo_grace_finished_writes_nothing(tmp_path, monkeypatch):
    manifest, ha_api, failsafe_ctx = _abo_finish_race_setup(tmp_path, monkeypatch)
    backup_before = load_backup(tmp_path / "backup.json")

    result = _run_local_check(
        manifest, ha_api, _base_options(), boost_was_active=False,
        room_target=21.0, failsafe_ctx=failsafe_ctx,
    )

    assert result is False
    ha_api.set_number_value.assert_not_called()
    assert failsafe_ctx["emergency_boost_active"] is False
    assert load_backup(tmp_path / "backup.json") == backup_before
    assert load_backup(tmp_path / "backup.json")["emergency_boost_active"] is False


def _failing_mark_inactive(path, now):
    raise OSError("SD-Karte kaputt")


def test_enter_abo_inactive_with_failing_entitlement_persist_still_enters_mode(tmp_path, monkeypatch, caplog):
    # Final-Review Minor 4: ein Schreibfehler beim Persistieren von inactive_since darf
    # weder werfen noch den Notbetrieb verhindern.
    _abo_paths(tmp_path, monkeypatch)
    monkeypatch.setattr("heizungsbruecke.__main__.entitlement.mark_inactive", _failing_mark_inactive)
    failsafe_ctx = {"delivery": DeliveryState(notbetrieb=False), "emergency_boost_active": False}
    ha_api = MagicMock()
    mqtt_client = MagicMock()

    with caplog.at_level(logging.ERROR):
        _enter_abo_inactive(failsafe_ctx, mqtt_client, ha_api, "notify.handy", ABO_NOW)

    assert failsafe_ctx["abo_inactive_since"] == ABO_NOW
    assert failsafe_ctx["delivery"].notbetrieb is True
    assert load_backup(tmp_path / "failsafe_state.json")["failsafe_active"] is True
    mqtt_client.stop.assert_called_once()
    ha_api.create_persistent_notification.assert_called_once_with(
        "SmartHeat", _abo_inactive_message(ABO_NOW), "smartheat_abo_inaktiv",
    )
    assert "SD-Karte kaputt" in caplog.text


def test_run_local_check_keeps_target_rise_pending_when_boost_has_no_restore_point(tmp_path, monkeypatch):
    # B5 (Bewusste Abweichung 11): ein mangels Wiederherstellungspunkt ausgesetzter
    # Comfort-Boost muss beim naechsten Check erneut starten koennen.
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    save_backup(backup_path, {"last_room_target": 20.0})
    manifest = ChannelManifest(entity_ids={
        "room_actual": "sensor.room_actual", "room_target": "sensor.room_target",
        "curve_current": "number.curve", "offset_current": "number.offset",
    })
    states = {"sensor.room_actual": 19.0, "number.curve": RuntimeError("Cloud nicht erreichbar"), "number.offset": 22.0}

    def _get_state(entity_id):
        value = states[entity_id]
        if isinstance(value, Exception):
            raise value
        return value

    ha_api = MagicMock()
    ha_api.get_state.side_effect = _get_state
    options = {
        "boost_threshold_k": 0.5, "boost_curve_value": 1.5, "boost_offset_value": 30.0,
        "curve_min": 0.4, "curve_max": 1.5, "offset_min": 20.0, "offset_max": 30.0,
    }

    first = _run_local_check(manifest, ha_api, options, boost_was_active=False, room_target=21.0)

    assert first is False
    assert load_backup(backup_path)["last_room_target"] == 20.0
    ha_api.set_number_value.assert_not_called()

    states["number.curve"] = 0.9
    second = _run_local_check(manifest, ha_api, options, boost_was_active=False, room_target=21.0)

    assert second is True
    assert load_backup(backup_path)["last_room_target"] == 21.0
    ha_api.set_number_value.assert_any_call("number.curve", 1.5)


_DERIVED_OPTIONS = {
    "tenant_id": "t1", "entity_room_actual": "sensor.rt", "entity_outdoor_temp": "sensor.outdoor",
    "avg_window_hours": 3.0,
}


def test_ensure_derived_sensors_with_retry_keeps_retrying_every_five_minutes_after_budget(monkeypatch, caplog):
    expected = {"dat": "sensor.dat"}
    attempts = {"count": 0}

    def flaky_ensure_all(**kwargs):
        attempts["count"] += 1
        if attempts["count"] <= 9:
            raise ConnectionError("HA Core noch nicht bereit")
        return expected

    monkeypatch.setattr("heizungsbruecke.__main__.derived_sensors.ensure_all", flaky_ensure_all)
    sleeps = []
    monkeypatch.setattr("heizungsbruecke.__main__.time.sleep", sleeps.append)
    ha_api = MagicMock()

    with caplog.at_level(logging.ERROR):
        result = _ensure_derived_sensors_with_retry(ha_api, _DERIVED_OPTIONS)

    assert result == expected
    assert sleeps == [5, 10, 20, 40, 60, 60, 60, 300, 300]
    ha_api.create_persistent_notification.assert_called_once_with(
        "SmartHeat", HELPER_NOTIFICATION_MESSAGE, "smartheat_hilfssensoren",
    )
    assert len([record for record in caplog.records if record.levelno == logging.ERROR]) == 1


def test_ensure_derived_sensors_with_retry_survives_failing_notification(monkeypatch):
    attempts = {"count": 0}

    def flaky_ensure_all(**kwargs):
        attempts["count"] += 1
        if attempts["count"] <= 8:
            raise ConnectionError("HA Core noch nicht bereit")
        return {}

    monkeypatch.setattr("heizungsbruecke.__main__.derived_sensors.ensure_all", flaky_ensure_all)
    monkeypatch.setattr("heizungsbruecke.__main__.time.sleep", lambda seconds: None)
    ha_api = MagicMock()
    ha_api.create_persistent_notification.side_effect = RuntimeError("HA kaputt")

    assert _ensure_derived_sensors_with_retry(ha_api, _DERIVED_OPTIONS) == {}


def test_check_timezone_warns_on_mismatch(monkeypatch, caplog):
    monkeypatch.setenv("TZ", "UTC")
    ha_api = MagicMock()
    ha_api.get_config.return_value = {"time_zone": "Europe/Berlin"}

    with caplog.at_level(logging.WARNING):
        _check_timezone(ha_api)

    assert "Europe/Berlin" in caplog.text
    assert "UTC" in caplog.text


def test_check_timezone_is_quiet_when_matching(monkeypatch, caplog):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    ha_api = MagicMock()
    ha_api.get_config.return_value = {"time_zone": "Europe/Berlin"}

    with caplog.at_level(logging.WARNING):
        _check_timezone(ha_api)

    assert caplog.records == []


def test_check_timezone_survives_failing_config_query(caplog):
    ha_api = MagicMock()
    ha_api.get_config.side_effect = requests.ConnectionError("HA nicht erreichbar")

    with caplog.at_level(logging.WARNING):
        _check_timezone(ha_api)  # darf nicht werfen

    assert "Zeitzone" in caplog.text


def test_claim_due_tick_claims_target_change_and_books_it(tmp_path, monkeypatch):
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    save_backup(backup_path, {"last_published_target_rt": 21.0})

    trigger = _claim_due_tick({"daily_trigger_time": "12:00"}, 20.5, datetime(2026, 9, 17, 9, 0))

    assert trigger == "target_change"
    assert load_backup(backup_path)["last_published_target_rt"] == 20.5


def test_claim_due_tick_returns_none_and_writes_nothing_when_nothing_is_due(tmp_path, monkeypatch):
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    save_backup(backup_path, {"last_published_target_rt": 21.0})

    assert _claim_due_tick({"daily_trigger_time": "12:00"}, 21.0, datetime(2026, 9, 17, 9, 0)) is None
    assert load_backup(backup_path) == {"last_published_target_rt": 21.0}


def test_claim_due_tick_claims_daily_once_per_day(tmp_path, monkeypatch):
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    save_backup(backup_path, {"last_published_target_rt": 21.0})
    options = {"daily_trigger_time": "12:00"}

    assert _claim_due_tick(options, 21.0, datetime(2026, 9, 17, 12, 5)) == "daily"
    assert load_backup(backup_path)["last_daily_trigger_date"] == "2026-09-17"
    assert _claim_due_tick(options, 21.0, datetime(2026, 9, 17, 15, 0)) is None


def test_claim_due_tick_prefers_target_change_but_also_books_daily(tmp_path, monkeypatch):
    backup_path = tmp_path / "backup.json"
    monkeypatch.setattr("heizungsbruecke.__main__.BACKUP_PATH", backup_path)
    save_backup(backup_path, {"last_published_target_rt": 21.0})

    trigger = _claim_due_tick({"daily_trigger_time": "12:00"}, 22.0, datetime(2026, 9, 17, 12, 5))

    assert trigger == "target_change"
    assert load_backup(backup_path)["last_daily_trigger_date"] == "2026-09-17"

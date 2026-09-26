import logging
from unittest.mock import MagicMock

import pytest
import requests

from heizungsbruecke.__main__ import (
    HELPER_NOTIFICATION_MESSAGE,
    _check_timezone,
    _ensure_derived_sensors_with_retry,
    _run_bridge,
)


@pytest.fixture(autouse=True)
def _isolate_data_paths(tmp_path, monkeypatch):
    for name in ("BACKUP_PATH", "FAILSAFE_PATH", "ENTITLEMENT_PATH", "DERIVED_SENSORS_PATH", "DAYNIGHT_SNAPSHOT_PATH"):
        monkeypatch.setattr(f"heizungsbruecke.config.{name}", tmp_path / f"{name.lower()}.json")


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
    }
    options.update(overrides)
    return options


def test_run_bridge_returns_zero_when_not_configured(caplog):
    with caplog.at_level("INFO"):
        result = _run_bridge({}, MagicMock())

    assert result == 0
    assert "Add-on ist noch nicht eingerichtet" in caplog.text


def test_run_bridge_returns_one_for_unknown_profile(monkeypatch, caplog):
    # Konfiguriert, aber ungueltig: ein echter Startfehler, main() muss ihn von "nicht
    # konfiguriert" unterscheiden koennen.
    monkeypatch.setattr(
        "heizungsbruecke.entitlement.requests.get", lambda url, timeout: _FakeResponse({"active": True}),
    )

    with caplog.at_level("ERROR"):
        result = _run_bridge(_full_valid_options(profile="does-not-exist"), MagicMock())

    assert result == 1
    assert "FEHLER" in caplog.text


def test_run_bridge_returns_one_for_invalid_telemetry_interval(monkeypatch, caplog):
    monkeypatch.setattr(
        "heizungsbruecke.entitlement.requests.get", lambda url, timeout: _FakeResponse({"active": True}),
    )

    with caplog.at_level(logging.ERROR):
        result = _run_bridge(_full_valid_options(telemetry_interval_seconds=5), MagicMock())

    assert result == 1
    assert "telemetry_interval_seconds" in caplog.text


def test_main_runs_bridge_synchronously(tmp_path, monkeypatch):
    options_path = tmp_path / "options.json"
    options_path.write_text("{}")
    monkeypatch.setattr("heizungsbruecke.config.OPTIONS_PATH", options_path)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
    calls = []
    monkeypatch.setattr(
        "heizungsbruecke.__main__._run_bridge", lambda options, ha_api: calls.append((options, ha_api)) or 0,
    )

    from heizungsbruecke.__main__ import main
    main()

    assert len(calls) == 1
    assert calls[0][0] == {}


def test_main_exits_nonzero_when_run_bridge_reports_a_genuine_error(tmp_path, monkeypatch):
    options_path = tmp_path / "options.json"
    options_path.write_text("{}")
    monkeypatch.setattr("heizungsbruecke.config.OPTIONS_PATH", options_path)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
    monkeypatch.setattr("heizungsbruecke.__main__._run_bridge", lambda options, ha_api: 1)

    from heizungsbruecke.__main__ import main

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code != 0


_DERIVED_OPTIONS = {
    "tenant_id": "t1", "entity_room_actual": "sensor.rt", "entity_outdoor_temp": "sensor.outdoor",
    "avg_window_hours": 3.0,
}


def test_ensure_derived_sensors_with_retry_returns_result_on_first_success(monkeypatch):
    expected = {"dat": "sensor.dat"}
    monkeypatch.setattr("heizungsbruecke.derived_sensors.ensure_all", lambda **kwargs: expected)
    sleeps = []
    monkeypatch.setattr("heizungsbruecke.__main__.time.sleep", sleeps.append)

    assert _ensure_derived_sensors_with_retry(MagicMock(), _DERIVED_OPTIONS) == expected
    assert sleeps == []


def test_ensure_derived_sensors_with_retry_recovers_after_transient_failures(monkeypatch):
    expected = {"dat": "sensor.dat"}
    attempts = {"count": 0}

    def flaky_ensure_all(**kwargs):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ConnectionError("HA Core noch nicht erreichbar")
        return expected

    monkeypatch.setattr("heizungsbruecke.derived_sensors.ensure_all", flaky_ensure_all)
    sleeps = []
    monkeypatch.setattr("heizungsbruecke.__main__.time.sleep", sleeps.append)

    assert _ensure_derived_sensors_with_retry(MagicMock(), _DERIVED_OPTIONS) == expected
    assert attempts["count"] == 3
    assert sleeps == [5, 10]


def test_ensure_derived_sensors_with_retry_keeps_retrying_every_five_minutes_after_budget(monkeypatch, caplog):
    expected = {"dat": "sensor.dat"}
    attempts = {"count": 0}

    def flaky_ensure_all(**kwargs):
        attempts["count"] += 1
        if attempts["count"] <= 9:
            raise ConnectionError("HA Core noch nicht bereit")
        return expected

    monkeypatch.setattr("heizungsbruecke.derived_sensors.ensure_all", flaky_ensure_all)
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

    monkeypatch.setattr("heizungsbruecke.derived_sensors.ensure_all", flaky_ensure_all)
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

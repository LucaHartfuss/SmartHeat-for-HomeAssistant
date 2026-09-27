import logging
from unittest.mock import MagicMock

import pytest
import requests

from heizungsbruecke.__main__ import (
    DERIVED_SENSORS_RETRY_DELAYS_SECONDS,
    StartupError,
    _check_timezone,
    _ensure_derived_sensors_with_retry,
    _retry_with_budget,
    _run_bridge,
    _wait_for_required_entities,
)
from heizungsbruecke.derived_sensors import DerivedSensors


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


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr("heizungsbruecke.__main__.time.sleep", recorded.append)
    return recorded


def _reachable(*answers):
    """ha_api, dessen is_reachable nacheinander `answers` liefert und danach True."""
    ha_api = MagicMock()
    sequence = list(answers)
    ha_api.is_reachable.side_effect = lambda: sequence.pop(0) if sequence else True
    return ha_api


def _full_valid_options(**overrides):
    options = {
        "tenant_id": "test_tenant",
        "verteilsystem": "Heizkoerper",
        "daily_trigger_time": "12:00",
        "day_avg_window_start": "14:00", "day_avg_window_end": "17:00",
        "night_avg_window_start": "04:00", "night_avg_window_end": "07:00",
        "mqtt_username": "test_mqtt_user",
        "mqtt_password": "test_mqtt_pass",
        "room_sensors": ["sensor.room_actual"],
        "entity_room_target": "sensor.room_target",
        "entity_curve_current": "number.curve_current",
        "entity_offset_current": "number.offset_current",
        "entity_outdoor_temp": "sensor.outdoor_temp",
        "entity_heat_limit": "number.heat_limit",
        "entity_room_day_avg": "sensor.room_day_avg",
        "entity_room_night_avg": "sensor.room_night_avg",
        "entity_dat": "sensor.dat",
        "entity_dart": "sensor.dart",
        "accounts_api_base_url": "https://accounts.example.test",
    }
    options.update(overrides)
    return options


def test_run_bridge_returns_zero_when_not_configured(caplog):
    with caplog.at_level("INFO"):
        result = _run_bridge({}, MagicMock())

    assert result == 0
    assert "Add-on ist noch nicht eingerichtet" in caplog.text


def test_run_bridge_returns_one_for_verteilsystem_without_safety_values(monkeypatch, caplog, sleeps):
    # Konfiguriert, aber ungueltig: ein echter Startfehler, main() muss ihn von "nicht
    # konfiguriert" unterscheiden koennen.
    monkeypatch.setattr(
        "heizungsbruecke.entitlement.requests.get", lambda url, timeout: _FakeResponse({"active": True}),
    )

    with caplog.at_level("ERROR"):
        result = _run_bridge(_full_valid_options(verteilsystem="Fussbodenheizung"), MagicMock())

    assert result == 1
    assert "FEHLER" in caplog.text
    assert "Fussbodenheizung" in caplog.text


def test_run_bridge_with_0_17_0_options_fails_loudly_as_outdated(caplog, sleeps):
    old = {k: v for k, v in _full_valid_options().items() if k not in (
        "verteilsystem", "daily_trigger_time", "day_avg_window_start", "day_avg_window_end",
        "night_avg_window_start", "night_avg_window_end", "room_sensors",
    )}
    old["profile"] = "vaillant_gastherme_heizkoerper"

    with caplog.at_level("INFO"):
        result = _run_bridge(old, MagicMock())

    assert result == 1
    assert "Konfiguration veraltet" in caplog.text
    assert "noch nicht eingerichtet" not in caplog.text


def test_run_bridge_returns_one_for_invalid_telemetry_interval(monkeypatch, caplog, sleeps):
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
    "tenant_id": "t1", "room_sensors": ["sensor.rt"], "entity_outdoor_temp": "sensor.outdoor",
    "avg_window_hours": 3.0,
}


def test_retry_with_budget_returns_first_success(sleeps):
    assert _retry_with_budget(_reachable(), lambda: "ok", lambda error: "x") == "ok"
    assert sleeps == []


def test_retry_with_budget_raises_startup_error_after_budget_when_ha_is_reachable(sleeps):
    def _always_fails():
        raise RuntimeError("kaputt")

    with pytest.raises(StartupError, match="Beschreibung: kaputt"):
        _retry_with_budget(_reachable(), _always_fails, lambda error: f"Beschreibung: {error}")

    assert sleeps == list(DERIVED_SENSORS_RETRY_DELAYS_SECONDS)


def test_unreachable_ha_never_becomes_a_start_error(sleeps):
    """Review Focus 3: solange HA nicht antwortet (hier 30 Pruefungen lang, weit ueber dem
    Budget von 7 Versuchen), zaehlt das Budget nicht."""
    ha = {"down_checks": 30}

    def _is_reachable():
        if ha["down_checks"] > 0:
            ha["down_checks"] -= 1
            return False
        return True

    def _attempt():
        if ha["down_checks"] > 0:
            raise ConnectionError("HA startet noch")
        return "ok"

    ha_api = MagicMock()
    ha_api.is_reachable.side_effect = _is_reachable

    assert _retry_with_budget(ha_api, _attempt, lambda error: "x") == "ok"
    assert len(sleeps) == 29
    assert max(sleeps) == 60


def test_missing_entity_is_tolerated_within_budget(sleeps):
    ha_api = _reachable()
    seen = {"n": 0}

    def _exists(entity_id):
        if entity_id == "number.heat_limit":
            seen["n"] += 1
            return seen["n"] > 2
        return True

    ha_api.entity_exists.side_effect = _exists

    _wait_for_required_entities(ha_api, _full_valid_options())

    assert sleeps == [5, 10]


def test_missing_entity_after_budget_names_the_entity(sleeps):
    ha_api = _reachable()
    ha_api.entity_exists.side_effect = lambda entity_id: entity_id != "number.heat_limit"

    with pytest.raises(StartupError, match="number.heat_limit"):
        _wait_for_required_entities(ha_api, _full_valid_options())


def test_required_entities_include_every_room_sensor_without_attribute_suffix(sleeps):
    ha_api = _reachable()
    checked = []
    ha_api.entity_exists.side_effect = lambda entity_id: checked.append(entity_id) or True

    _wait_for_required_entities(ha_api, _full_valid_options(
        room_sensors=["sensor.a", "climate.wz::current_temperature"], entity_room_target="climate.wz::temperature",
    ))

    assert "climate.wz" in checked and "sensor.a" in checked
    assert not any("::" in entity_id for entity_id in checked)


def test_helper_creation_failure_after_budget_is_a_start_error(monkeypatch, sleeps):
    def _broken(**kwargs):
        raise RuntimeError("Template-Flow abgelehnt")

    monkeypatch.setattr("heizungsbruecke.derived_sensors.ensure_all", _broken)

    with pytest.raises(StartupError, match="Hilfs-Entities konnten nicht angelegt werden"):
        _ensure_derived_sensors_with_retry(_reachable(), {**_DERIVED_OPTIONS})


def test_ensure_derived_sensors_with_retry_recovers_after_transient_failures(monkeypatch, sleeps):
    expected = DerivedSensors({"dat": "sensor.dat"}, (), "f")
    attempts = {"count": 0}

    def flaky_ensure_all(**kwargs):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ConnectionError("HA Core noch nicht erreichbar")
        return expected

    monkeypatch.setattr("heizungsbruecke.derived_sensors.ensure_all", flaky_ensure_all)

    assert _ensure_derived_sensors_with_retry(_reachable(), _DERIVED_OPTIONS) == expected
    assert sleeps == [5, 10]


def _status_calls(ha_api):
    return [(c.args[1], c.args[2]) for c in ha_api.set_state.call_args_list]


def test_start_with_0_18_0_options_reports_outdated_configuration(sleeps):
    """Review Focus 1: client1 nach dem Update, bevor der Wizard neu durchlaufen ist."""
    old = {k: v for k, v in _full_valid_options().items() if k != "room_sensors"}
    old["entity_room_actual"] = "sensor.room_actual"
    ha_api = _reachable()

    assert _run_bridge(old, ha_api) == 1

    states = _status_calls(ha_api)
    assert [state for state, _ in states] == ["startet", "konfigurationsfehler"]
    assert "Konfiguration veraltet – bitte SmartHeat-Einrichtung erneut durchführen" in states[-1][1]["grund"]
    ha_api.create_persistent_notification.assert_called_once()
    assert ha_api.create_persistent_notification.call_args.args[2] == "smartheat_konfiguration"
    ha_api.send_notification.assert_not_called()
    ha_api.set_number_value.assert_not_called()


def test_repeated_start_error_does_not_push_again(sleeps):
    """Review Focus 4."""
    options = _full_valid_options(room_sensors=["light.kaputt"], notify_services=["notify.mobile_app_a"])
    ha_api = _reachable()

    assert _run_bridge(options, ha_api) == 1
    assert _run_bridge(options, ha_api) == 1

    assert ha_api.send_notification.call_count == 1


def test_repeated_helper_failure_with_changing_error_text_does_not_push_again(monkeypatch, sleeps):
    """Review Focus 4: HA vergibt je Lauf eine neue Flow-ID; der Meldezustand darf daran nicht
    haengen, sonst meldet jeder Neustart erneut."""
    run = {"flow_id": ""}

    def _rejected(**kwargs):
        raise requests.HTTPError(
            "400 Client Error: Bad Request for url: "
            f"http://supervisor/core/api/config/config_entries/flow/{run['flow_id']}"
        )

    monkeypatch.setattr("heizungsbruecke.derived_sensors.ensure_all", _rejected)
    options = _full_valid_options(notify_services=["notify.mobile_app_a"])
    ha_api = _reachable()

    for flow_id in ("0a1b2c3d", "9f8e7d6c"):
        run["flow_id"] = flow_id
        assert _run_bridge(options, ha_api) == 1

    assert ha_api.send_notification.call_count == 1
    ha_api.create_persistent_notification.assert_called_once()
    grund = _status_calls(ha_api)[-1][1]["grund"]
    assert "Hilfs-Entities konnten nicht angelegt werden" in grund
    assert "9f8e7d6c" in grund  # der Status zeigt den aktuellen Fehler im Detail


def test_repeated_missing_entity_does_not_push_again(sleeps):
    options = _full_valid_options(notify_services=["notify.mobile_app_a"])
    ha_api = _reachable()
    ha_api.entity_exists.side_effect = lambda entity_id: entity_id != "number.heat_limit"

    assert _run_bridge(options, ha_api) == 1
    assert _run_bridge(options, ha_api) == 1

    assert ha_api.send_notification.call_count == 1
    assert "Entity fehlt in Home Assistant: number.heat_limit" in _status_calls(ha_api)[-1][1]["grund"]


@pytest.mark.parametrize("overrides", [
    {"room_sensors": ["test_mqtt_pass"]},
    {"entity_outdoor_temp": "test_mqtt_pass"},
    {"battery_entities": ["test_mqtt_pass"]},
    {"accounts_api_base_url": "http://test_mqtt_pass.example"},
])
def test_start_error_reason_never_contains_credentials(sleeps, caplog, overrides):
    """Regel 6: die Startpruefung zitiert ungueltige Optionswerte (!r). Steht das MQTT-Passwort
    versehentlich in einer anderen Option, darf es weder in der Status-Entity noch in Meldungen
    oder im Log auftauchen."""
    options = _full_valid_options(notify_services=["notify.mobile_app_a"], **overrides)
    ha_api = _reachable()

    with caplog.at_level(logging.INFO):
        assert _run_bridge(options, ha_api) == 1

    grund = _status_calls(ha_api)[-1][1]["grund"]
    assert "***" in grund  # der Fehlertext haette den Wert zitiert
    assert "test_mqtt_pass" not in grund
    ha_api.send_notification.assert_called_once()
    assert "test_mqtt_pass" not in str(ha_api.send_notification.call_args)
    assert "test_mqtt_pass" not in str(ha_api.create_persistent_notification.call_args)
    assert "test_mqtt_pass" not in caplog.text


def test_missing_entity_retry_warnings_and_reason_never_contain_credentials(sleeps, caplog):
    """Regel 6 auch fuer die Warnungen je Versuch: entity_room_target wird nicht per Muster
    geprueft, eine Entity-ID mit dem Passwort erreicht also die Existenzpruefung."""
    options = _full_valid_options(entity_room_target="sensor.test_mqtt_pass", notify_services=["notify.mobile_app_a"])
    ha_api = _reachable()
    ha_api.entity_exists.side_effect = lambda entity_id: entity_id != "sensor.test_mqtt_pass"

    with caplog.at_level(logging.INFO):
        assert _run_bridge(options, ha_api) == 1

    assert "Start noch nicht moeglich" in caplog.text  # die Warnungen je Versuch sind geloggt
    assert "sensor.***" in caplog.text
    assert "test_mqtt_pass" not in caplog.text
    grund = _status_calls(ha_api)[-1][1]["grund"]
    assert grund == "Entity fehlt in Home Assistant: sensor.***"
    assert "test_mqtt_pass" not in str(ha_api.send_notification.call_args)
    assert "test_mqtt_pass" not in str(ha_api.create_persistent_notification.call_args)


def test_start_waits_for_ha_before_reporting(sleeps):
    old = {k: v for k, v in _full_valid_options().items() if k != "room_sensors"}
    ha_api = _reachable(False, False)

    assert _run_bridge(old, ha_api) == 1

    assert sleeps[:2] == [5, 10]
    assert ha_api.set_state.call_count == 2


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

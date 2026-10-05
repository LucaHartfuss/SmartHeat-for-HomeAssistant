import json
import logging
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests
from conftest import FakeClock
from fakes import ACCESS_OPTIONS

from heizungsbruecke import __main__
from heizungsbruecke.__main__ import (
    DERIVED_SENSORS_RETRY_DELAYS_SECONDS,
    IdleBridge,
    StartupError,
    _check_timezone,
    _ensure_derived_sensors_with_retry,
    _retry_with_budget,
    _start_bridge,
    _wait_for_required_entities,
)
from heizungsbruecke.derived_sensors import DerivedSensors
from heizungsbruecke.ha_api import HomeAssistantApi


@pytest.fixture(autouse=True)
def _isolate_data_paths(tmp_path, monkeypatch):
    for name in ("BACKUP_PATH", "FAILSAFE_PATH", "ENTITLEMENT_PATH", "DERIVED_SENSORS_PATH"):
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
        **ACCESS_OPTIONS,
        "mqtt_username": "test_mqtt_user",
        "mqtt_password": "test_mqtt_pass",
        "room_sensors": ["sensor.room_actual"],
        "entity_room_target": "sensor.room_target",
        "entity_curve_current": "number.curve_current",
        "entity_shift_current": "climate.zone",
        "entity_min_flow": "number.min_flow",
        "entity_outdoor_temp": "sensor.outdoor_temp",
        "entity_heat_limit": "number.heat_limit",
        "accounts_api_base_url": "https://accounts.example.test",
    }
    options.update(overrides)
    return options


def test_unconfigured_addon_idles_without_status_event(caplog):
    ha_api = MagicMock()

    with caplog.at_level("INFO"):
        result = _start_bridge({}, ha_api)

    assert isinstance(result, IdleBridge)
    assert result.reason == "nicht_eingerichtet"
    assert "Add-on ist noch nicht eingerichtet" in caplog.text
    ha_api.fire_event.assert_not_called()


def test_start_with_the_old_configuration_reports_outdated_not_silence(sleeps):
    ha_api = MagicMock()
    options = {**_full_valid_options(), "mqtt_username": "client1_alt", "mqtt_password": "x9alt7"}
    for key in ("transport", "installation_token", "tls_certificate", "tls_private_key"):
        options.pop(key, None)

    result = _start_bridge(options, ha_api)

    assert result.reason == "konfigurationsfehler"
    last = _status_calls(ha_api)[-1]
    assert last["status"] == "konfigurationsfehler"
    assert "Konfiguration veraltet" in last["grund"]


@pytest.mark.parametrize("secret_key", ["mqtt_password", "installation_token", "tls_private_key"])
def test_start_errors_never_show_secret_option_values(secret_key):
    options = {secret_key: "GEHEIM-123", "notify_services": ["notify.GEHEIM-123"]}
    text = __main__._without_credentials("Option 'notify_services' (['notify.GEHEIM-123']) ungueltig", options)
    assert "GEHEIM-123" not in text


def test_a_secret_with_special_characters_is_redacted_in_its_repr_form_too():
    options = {"tls_private_key": "-----BEGIN-----\nabc'\n-----END-----"}
    text = __main__._without_credentials(f"Option ungueltig: {options['tls_private_key']!r}", options)
    assert "abc" not in text


def test_verteilsystem_without_safety_values_is_a_configuration_error(monkeypatch, caplog, sleeps):
    monkeypatch.setattr(
        "smartheat_runtime.entitlement.requests.get", lambda url, timeout: _FakeResponse({"active": True}),
    )

    with caplog.at_level("ERROR"):
        result = _start_bridge(_full_valid_options(verteilsystem="Deckenheizung"), MagicMock())

    assert result.reason == "konfigurationsfehler"
    assert "FEHLER" in caplog.text
    assert "Deckenheizung" in caplog.text


def test_0_17_0_options_are_outdated_not_unconfigured(caplog, sleeps):
    old = {k: v for k, v in _full_valid_options().items() if k not in (
        "verteilsystem", "daily_trigger_time", "room_sensors",
    )}
    old["profile"] = "vaillant_gastherme_heizkoerper"

    with caplog.at_level("INFO"):
        result = _start_bridge(old, MagicMock())

    assert result.reason == "konfigurationsfehler"
    assert "Konfiguration veraltet" in caplog.text
    assert "noch nicht eingerichtet" not in caplog.text


def test_zone_as_room_target_is_a_configuration_error_without_writes(caplog, sleeps):
    ha_api = MagicMock()
    options = _full_valid_options(entity_room_target="climate.zone::temperature", entity_shift_current="climate.zone")

    with caplog.at_level(logging.ERROR):
        result = _start_bridge(options, ha_api)

    assert result.reason == "konfigurationsfehler"
    assert "Raum-Soll" in caplog.text
    ha_api.set_climate_temperature.assert_not_called()
    ha_api.set_number_value.assert_not_called()
    ha_api.set_hvac_mode.assert_not_called()


def test_invalid_telemetry_interval_is_a_configuration_error(caplog, sleeps):
    with caplog.at_level(logging.ERROR):
        result = _start_bridge(_full_valid_options(telemetry_interval_seconds=5), MagicMock())

    assert result.reason == "konfigurationsfehler"
    assert "telemetry_interval_seconds" in caplog.text


def test_outdated_options_idle_with_heartbeat_and_recheck_without_second_push(monkeypatch, clock, sleeps):
    """Review Focus 1: client1 nach dem Update. Kein Exit (Watchdog-Karussell), Lebenszeichen,
    nach 900 s Neupruefung per os.execv, derselbe Fehler meldet nicht erneut."""
    old = {k: v for k, v in _full_valid_options(notify_services=["notify.mobile_app_a"]).items() if k != "room_sensors"}
    old["entity_room_actual"] = "sensor.room_actual"
    ha_api = _reachable()
    execs = []
    monkeypatch.setattr("os.execv", lambda path, args: execs.append(args))

    bridge = _start_bridge(old, ha_api, clock=clock)

    assert bridge.reason == "konfigurationsfehler"
    assert ha_api.send_notification.call_count == 1
    sent = len(_status_calls(ha_api))
    clock.advance(300)
    bridge.worker.run_pending()
    assert len(_status_calls(ha_api)) == sent + 1
    assert _status_calls(ha_api)[-1]["status"] == "konfigurationsfehler"
    assert execs == []

    clock.advance(600)
    bridge.worker.run_pending()

    assert execs == [[sys.executable, "-m", "heizungsbruecke"]]
    assert _start_bridge(old, ha_api, clock=clock).reason == "konfigurationsfehler"  # neuer Prozess
    assert ha_api.send_notification.call_count == 1
    ha_api.set_number_value.assert_not_called()


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


def test_main_never_exits_on_its_own(tmp_path, monkeypatch):
    # Befund Supervisor-Watchdog: auch ein Exit 0 wuerde neu gestartet.
    options_path = tmp_path / "options.json"
    options_path.write_text("{}")
    monkeypatch.setattr("heizungsbruecke.config.OPTIONS_PATH", options_path)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
    monkeypatch.setattr("heizungsbruecke.__main__._run_bridge", lambda options, ha_api: 1)

    from heizungsbruecke.__main__ import main

    assert main() is None  # kein SystemExit


_DERIVED_OPTIONS = {"tenant_id": "t1", "room_sensors": ["sensor.rt"], "entity_outdoor_temp": "sensor.outdoor"}


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


@pytest.mark.parametrize("booting_state", ["NOT_RUNNING", "STARTING"])
def test_budget_only_counts_once_ha_is_running(monkeypatch, sleeps, booting_state):
    """Review I2: HAs HTTP antwortet schon im Bootstrap, mypyllant laedt erst danach. Solange
    /api/config nicht RUNNING meldet (hier 30 Abfragen lang, weit ueber dem Budget), fehlt die
    Entity, ohne dass daraus ein Startfehler wird."""
    ha = {"booting_checks": 30}

    def _config():
        if ha["booting_checks"] > 0:
            ha["booting_checks"] -= 1
            return {"state": booting_state, "time_zone": "Europe/Berlin"}
        return {"state": "RUNNING", "time_zone": "Europe/Berlin"}

    ha_api = HomeAssistantApi(base_url="http://supervisor", token="t")
    monkeypatch.setattr(ha_api, "get_config", _config)
    monkeypatch.setattr(
        ha_api, "entity_exists", lambda entity_id: ha["booting_checks"] == 0 or entity_id != "number.heat_limit",
    )

    _wait_for_required_entities(ha_api, _full_valid_options())

    assert len(sleeps) == 29  # nur Warten auf RUNNING, kein Budget verbraucht
    assert ha["booting_checks"] == 0


def test_budget_expires_when_entity_stays_missing_while_ha_is_running(monkeypatch, sleeps):
    ha_api = HomeAssistantApi(base_url="http://supervisor", token="t")
    monkeypatch.setattr(ha_api, "get_config", lambda: {"state": "RUNNING"})
    monkeypatch.setattr(ha_api, "entity_exists", lambda entity_id: entity_id != "number.heat_limit")

    with pytest.raises(StartupError, match="number.heat_limit"):
        _wait_for_required_entities(ha_api, _full_valid_options())

    assert sleeps == list(DERIVED_SENSORS_RETRY_DELAYS_SECONDS)


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
    expected = DerivedSensors({"room_actual": "sensor.smartheat_t1_raumtemperatur"}, (), "f")
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
    """Die gesendeten Status-Events (Daten), in Reihenfolge."""
    return [c.args[1] for c in ha_api.fire_event.call_args_list if c.args[0] == "smartheat_status"]


def test_start_with_0_18_0_options_reports_outdated_configuration(sleeps):
    """Review Focus 1: client1 nach dem Update, bevor der Wizard neu durchlaufen ist."""
    old = {k: v for k, v in _full_valid_options().items() if k != "room_sensors"}
    old["entity_room_actual"] = "sensor.room_actual"
    ha_api = _reachable()

    assert _start_bridge(old, ha_api).reason == "konfigurationsfehler"

    states = _status_calls(ha_api)
    assert [event["status"] for event in states] == ["startet", "konfigurationsfehler"]
    assert "Konfiguration veraltet – bitte SmartHeat-Einrichtung erneut durchführen" in states[-1]["grund"]
    ha_api.create_persistent_notification.assert_called_once()
    assert ha_api.create_persistent_notification.call_args.args[2] == "smartheat_konfiguration"
    ha_api.send_notification.assert_not_called()
    ha_api.set_number_value.assert_not_called()


def test_start_with_0_23_0_options_reports_outdated_configuration_without_writing(sleeps):
    """Review Focus 3: Update mit der options.json von 0.23.0 (entity_offset_current, keine
    Parallelverschiebung/kein Mindestvorlauf): Ruhezustand, kein Schreiben auf die Anlage."""
    old = {k: v for k, v in _full_valid_options().items() if k not in ("entity_shift_current", "entity_min_flow")}
    old["entity_offset_current"] = "number.zuhause_circuit_0_min_flow_temperature_setpoint"
    ha_api = _reachable()

    bridge = _start_bridge(old, ha_api)

    assert bridge.reason == "konfigurationsfehler"
    grund = _status_calls(ha_api)[-1]["grund"]
    assert grund.startswith("Konfiguration veraltet – bitte SmartHeat-Einrichtung erneut durchführen")
    assert "'entity_shift_current' fehlt" in grund
    ha_api.set_number_value.assert_not_called()
    ha_api.set_climate_temperature.assert_not_called()
    ha_api.set_hvac_mode.assert_not_called()


def test_required_entities_include_zone_and_min_flow(sleeps):
    ha_api = _reachable()
    checked = []
    ha_api.entity_exists.side_effect = lambda entity_id: checked.append(entity_id) or True

    _wait_for_required_entities(ha_api, _full_valid_options())

    assert {"climate.zone", "number.min_flow"} <= set(checked)
    assert "number.offset_current" not in checked


def test_repeated_start_error_does_not_push_again(sleeps):
    """Review Focus 4."""
    options = _full_valid_options(room_sensors=["light.kaputt"], notify_services=["notify.mobile_app_a"])
    ha_api = _reachable()

    assert _start_bridge(options, ha_api).reason == "konfigurationsfehler"
    assert _start_bridge(options, ha_api).reason == "konfigurationsfehler"

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
        assert _start_bridge(options, ha_api).reason == "konfigurationsfehler"

    assert ha_api.send_notification.call_count == 1  # kein zweiter Push
    # Die HA-Benachrichtigung wird je Start neu angelegt (ersetzt die vorige per notification_id,
    # sie ueberlebt keinen HA-Neustart) und traegt den aktuellen Fehlertext (Review I3).
    persistent = ha_api.create_persistent_notification.call_args_list
    assert len(persistent) == 2
    assert {c.args[2] for c in persistent} == {"smartheat_konfiguration"}
    assert "9f8e7d6c" in persistent[-1].args[1]
    grund = _status_calls(ha_api)[-1]["grund"]
    assert "Hilfs-Entities konnten nicht angelegt werden" in grund
    assert "9f8e7d6c" in grund  # der Status zeigt den aktuellen Fehler im Detail


def test_repeated_missing_entity_does_not_push_again(sleeps):
    options = _full_valid_options(notify_services=["notify.mobile_app_a"])
    ha_api = _reachable()
    ha_api.entity_exists.side_effect = lambda entity_id: entity_id != "number.heat_limit"

    assert _start_bridge(options, ha_api).reason == "konfigurationsfehler"
    assert _start_bridge(options, ha_api).reason == "konfigurationsfehler"

    assert ha_api.send_notification.call_count == 1
    assert "Entity fehlt in Home Assistant: number.heat_limit" in _status_calls(ha_api)[-1]["grund"]


@pytest.mark.parametrize("overrides", [
    {"room_sensors": ["test_mqtt_pass"]},
    {"entity_outdoor_temp": "test_mqtt_pass"},
    {"battery_entities": ["test_mqtt_pass"]},
    {"accounts_api_base_url": "http://test_mqtt_pass.example"},
])
def test_start_error_reason_never_contains_credentials(sleeps, caplog, overrides):
    """Regel 6: die Startpruefung zitiert ungueltige Optionswerte (!r). Steht das MQTT-Passwort
    versehentlich in einer anderen Option, darf es weder im Status-Event noch in Meldungen
    oder im Log auftauchen."""
    options = _full_valid_options(notify_services=["notify.mobile_app_a"], **overrides)
    ha_api = _reachable()

    with caplog.at_level(logging.INFO):
        assert _start_bridge(options, ha_api).reason == "konfigurationsfehler"

    grund = _status_calls(ha_api)[-1]["grund"]
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
        assert _start_bridge(options, ha_api).reason == "konfigurationsfehler"

    assert "Start noch nicht moeglich" in caplog.text  # die Warnungen je Versuch sind geloggt
    assert "sensor.***" in caplog.text
    assert "test_mqtt_pass" not in caplog.text
    grund = _status_calls(ha_api)[-1]["grund"]
    assert grund == "Entity fehlt in Home Assistant: sensor.***"
    assert "test_mqtt_pass" not in str(ha_api.send_notification.call_args)
    assert "test_mqtt_pass" not in str(ha_api.create_persistent_notification.call_args)


def test_start_waits_for_ha_before_reporting(sleeps):
    old = {k: v for k, v in _full_valid_options().items() if k != "room_sensors"}
    ha_api = _reachable(False, False)

    assert _start_bridge(old, ha_api).reason == "konfigurationsfehler"

    assert sleeps[:2] == [5, 10]
    assert len(_status_calls(ha_api)) == 2


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


def test_on_telemetry_hands_the_waerme_callback_to_the_tick(make_store, monkeypatch):
    from types import SimpleNamespace

    from heizungsbruecke.__main__ import _on_telemetry

    captured = {}
    monkeypatch.setattr(
        "heizungsbruecke.__main__.telemetry.run_telemetry_tick", lambda *args, **kwargs: captured.update(kwargs),
    )
    from smartheat_core.binding import VAILLANT_MYPYLLANT

    rt = SimpleNamespace(
        worker=MagicMock(), options={}, store=make_store(), mqtt_client=MagicMock(), manifest=MagicMock(),
        ha_api=MagicMock(), notifier=MagicMock(), status=MagicMock(),
        override=SimpleNamespace(binding=SimpleNamespace(description=VAILLANT_MYPYLLANT)),
    )
    rt.mqtt_client.is_connected.return_value = True

    _on_telemetry(rt, None)

    assert captured["waerme"](21.0, {"flow_temperature": 26.0}, {"flow_setpoint": 38.0}) is False
    assert captured["energy"] is None  # Vaillant: Summenzaehler, unveraendert (Plan 3b)


def _prime_rt(monkeypatch, capture):
    """Laufzeit-Attrappe fuer _prime: nur Heizgrenzen-Erfassung unter Test, alles Uebrige still."""
    from types import SimpleNamespace

    from heizungsbruecke import __main__ as main_module

    monkeypatch.setattr(main_module.regulation, "read_room_target_live", lambda rt: 20.5)
    monkeypatch.setattr(main_module.regulation, "run_local_check", lambda rt: None)
    monkeypatch.setattr(main_module.derived, "sync", lambda rt: None)
    monkeypatch.setattr(main_module, "_prepare_zone", lambda rt: None)
    rt = SimpleNamespace(
        override=SimpleNamespace(capture_originals=capture),
        store=SimpleNamespace(
            state=SimpleNamespace(restore_point={"room_setpoint": 21.0}, stable_target=None), update=lambda **kw: None,
        ),
    )
    return main_module, rt


def test_prime_captures_the_heat_limit_original(monkeypatch):
    capture = MagicMock()
    main_module, rt = _prime_rt(monkeypatch, capture)

    main_module._prime(rt)

    capture.assert_called_once_with()


def test_prime_heat_limit_capture_failure_does_not_abort_startup(monkeypatch, caplog):
    capture = MagicMock(side_effect=RuntimeError("Speicher voll"))
    main_module, rt = _prime_rt(monkeypatch, capture)
    ran = []
    monkeypatch.setattr(main_module.regulation, "run_local_check", lambda rt: ran.append(True))

    with caplog.at_level(logging.ERROR):
        main_module._prime(rt)

    assert ran == [True]
    assert "Heizgrenze" in caplog.text


@pytest.mark.parametrize("failures, down_seconds, expect_query", [
    (3, 900, True), (2, 900, False), (3, 899, False), (0, 2000, False),
])
def test_connection_check_queries_the_status_after_long_repeated_failures(failures, down_seconds, expect_query, monkeypatch):
    rt = SimpleNamespace(
        worker=MagicMock(), options={}, clock=FakeClock(), mqtt_down_since=None,
        mqtt_client=MagicMock(), store=SimpleNamespace(state=SimpleNamespace(
            abo_inactive_since=None, delivery=SimpleNamespace(pending="offen"))),
    )
    rt.mqtt_client.is_connected.return_value = False
    rt.mqtt_client.connect_failures = failures
    called = []
    monkeypatch.setattr(__main__.abo, "handle_connection_failing", lambda runtime: called.append(runtime))
    __main__._on_connection_check(rt, None)  # setzt mqtt_down_since
    rt.clock.advance(down_seconds)
    __main__._on_connection_check(rt, None)
    assert bool(called) is expect_query


def test_mqtt_connected_resets_the_connection_failing_throttle():
    rt = SimpleNamespace(
        status=MagicMock(), notifier=MagicMock(), auth_rejected_queried_at=5.0, auth_rejected_last_status="x",
        connection_failing_queried_at=7.0,
    )
    rt.status.flags.zugang_abgelehnt = False
    rt.status.flags.gestartet = True
    with patch.object(__main__.ticks, "deliver"):
        __main__._on_mqtt_connected(rt, None)
    assert rt.connection_failing_queried_at is None


def test_connection_check_starts_no_probe_tick_when_the_query_found_the_abo_inactive(monkeypatch):
    state = SimpleNamespace(abo_inactive_since=None, delivery=SimpleNamespace(pending=None))
    rt = SimpleNamespace(
        worker=MagicMock(), options={}, clock=FakeClock(), mqtt_down_since=None,
        mqtt_client=MagicMock(), store=SimpleNamespace(state=state),
    )
    rt.mqtt_client.is_connected.return_value = False
    rt.mqtt_client.connect_failures = 3
    monkeypatch.setattr(__main__.abo, "handle_connection_failing",
                        lambda runtime: setattr(state, "abo_inactive_since", "jetzt"))
    probes = []
    monkeypatch.setattr(__main__.ticks, "start_probe_tick", lambda *args: probes.append(args))
    __main__._on_connection_check(rt, None)
    rt.clock.advance(900)
    __main__._on_connection_check(rt, None)
    assert probes == []


def test_unwritable_temp_storage_is_a_configuration_error_not_a_crash(monkeypatch, caplog, sleeps):
    from tls_helpers import ca_pem, issue, make_ca

    ca = make_ca()
    key, cert = issue(ca, "client1")
    transport = json.dumps({"kind": "iot_core", "host": "h.example", "port": 8883, "alpn": None,
                            "ca_pem": ca_pem(ca), "client_id": "test_tenant"})
    options = _full_valid_options(transport=transport, tls_certificate=cert, tls_private_key=key,
                                  mqtt_username="", mqtt_password="")

    def boom(*args, **kwargs):
        raise OSError("Platte voll")

    monkeypatch.setattr("smartheat_transport.connect.tempfile.TemporaryDirectory", boom)
    ha_api = MagicMock()
    with caplog.at_level(logging.ERROR):
        result = _start_bridge(options, ha_api)

    assert result.reason == "konfigurationsfehler"
    assert "nicht ablegbar" in caplog.text
    assert key not in caplog.text


@pytest.mark.parametrize("status", [__main__.abo.entitlement.ACTIVE, __main__.abo.entitlement.UNKNOWN])
def test_connection_check_still_starts_the_probe_tick_after_an_unclear_or_active_query(status, monkeypatch):
    """Regression: ein normaler Ausfall (Abo aktiv oder Abfrage unklar) darf den Notbetrieb nicht verhindern."""
    state = SimpleNamespace(abo_inactive_since=None, delivery=SimpleNamespace(pending=None))
    rt = SimpleNamespace(
        worker=MagicMock(), options={}, clock=FakeClock(), mqtt_down_since=None, connection_failing_queried_at=None,
        mqtt_client=MagicMock(), store=SimpleNamespace(state=state), ha_api=MagicMock(),
    )
    rt.mqtt_client.is_connected.return_value = False
    rt.mqtt_client.connect_failures = 3
    monkeypatch.setattr(__main__.abo.entitlement, "query_from_options", lambda options: status)
    probes = []
    monkeypatch.setattr(__main__.ticks, "start_probe_tick", lambda *args: probes.append(args))
    __main__._on_connection_check(rt, None)
    rt.clock.advance(900)
    __main__._on_connection_check(rt, None)
    assert len(probes) == 1
    assert state.abo_inactive_since is None
    rt.ha_api.send_notification.assert_not_called()

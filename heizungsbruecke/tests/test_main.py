"""Start, Startfehler und Start-Hilfen des HA-Hosts im Geraete-Pfad (bis 0.36 ueber __main__._start_bridge mit
options.json): Konfigurationsfehler mit Ruhezustand, Neupruefung und Meldezustand, Warten auf HA, Budget fuer fehlende
Entities und Hilfssensoren, Zeitzone. Die hostneutralen Handler der Laufzeit (Telemetrie, _prime, Verbindungswaechter)
stehen mit der echten Laufzeit in test_runtime.py, der Einstieg main() in test_main_geraet.py."""
import logging
from unittest.mock import MagicMock, call

import pytest
import requests
from geraet_helpers import konfiguration_aus_optionen
from test_runtime import _start_bridge, env  # noqa: F401  (Fixture)

from heizungsbruecke import config, dokument
from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.helper_templates import outdoor_temperature_template, room_temperature_template
from heizungsbruecke.host import (
    DERIVED_SENSORS_RETRY_DELAYS_SECONDS,
    StartupError,
    _check_timezone,
    _ensure_derived_sensors_with_retry,
    _retry_with_budget,
    _wait_for_required_entities,
)

#: Ungueltiger Tagestick im Konfigurationsdokument: ein Konfigurationsfehler beim Start. (Die Grenzen der Intervalle
#: prueft nur noch config_check beim Annehmen des Dokuments, nicht mehr der Host.)
KONFIGURATIONSFEHLER = {"daily_trigger_time": "25:00"}


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr("heizungsbruecke.host.time.sleep", recorded.append)
    return recorded


def _reachable(*answers):
    """ha_api, dessen is_reachable nacheinander `answers` liefert und danach True."""
    ha_api = MagicMock()
    sequence = list(answers)
    ha_api.is_reachable.side_effect = lambda: sequence.pop(0) if sequence else True
    return ha_api


def _bindung(**overrides):
    """Die HA-Bindung, die der Host aus dem Konfigurationsdokument liest (dokument.bindung)."""
    options = {
        "tenant_id": "test_tenant",
        "verteilsystem": "Heizkoerper",
        "daily_trigger_time": "12:00",
        "room_sensors": ["sensor.room_actual"],
        "entity_room_target": "sensor.room_target",
        "entity_curve_current": "number.curve_current",
        "entity_shift_current": "climate.zone",
        "entity_min_flow": "number.min_flow",
        "entity_outdoor_temp": "sensor.outdoor_temp",
        "entity_heat_limit": "number.heat_limit",
        **overrides,
    }
    return dokument.bindung(konfiguration_aus_optionen(options))


def _status_events(env):
    """Die gesendeten Status-Ereignisse (Daten), in Reihenfolge."""
    return [data for kind, data in env.ha.events if kind == "smartheat_status"]


def _konfiguration_notices(env):
    return [entry for entry in env.ha.persistent if entry[0] == "smartheat_konfiguration"]


# --- Konfigurationsfehler beim Start ---

def test_verteilsystem_without_safety_values_is_a_configuration_error(env, caplog, sleeps):
    with caplog.at_level("ERROR"):
        result = _start_bridge(env, verteilsystem="Deckenheizung")

    assert result.reason == "konfigurationsfehler"
    assert "FEHLER" in caplog.text
    assert "Deckenheizung" in caplog.text


def test_alte_konfiguration_ohne_parameter_startet_nicht(env, caplog, sleeps):
    """Review Focus 1 (Spec Stufe 3 S4b 4.3): gespeicherte Konfiguration aus 1.0.0 ohne bindungsparameter:
    Konfigurationsfehler statt Absturz, nichts wird geschrieben."""
    with caplog.at_level(logging.ERROR):
        result = _start_bridge(env, bindungsparameter=None)

    assert result.reason == "konfigurationsfehler"
    assert "Bindungsparameter" in caplog.text
    assert env.ha.writes == []


def test_invalid_daily_trigger_time_is_a_configuration_error(env, caplog, sleeps):
    with caplog.at_level(logging.ERROR):
        result = _start_bridge(env, **KONFIGURATIONSFEHLER)

    assert result.reason == "konfigurationsfehler"
    assert "Tagestick" in caplog.text


def test_configuration_error_idles_with_heartbeat_and_rechecks_without_second_push(env, sleeps):
    """Review Focus 1: kein Exit (Watchdog-Karussell), Lebenszeichen, nach 900 s Neupruefung per Neustart des Hosts
    (Rueckruf neustart, Spec Stufe 3 S6 4.2), derselbe Fehler meldet nicht erneut. Bis 0.36 ausgeloest von veralteten
    Optionen, jetzt von einem ungueltigen Dokument."""
    neustarts = []

    bridge = _start_bridge(env, neustart=lambda: neustarts.append(1), **KONFIGURATIONSFEHLER)

    assert bridge.reason == "konfigurationsfehler"
    assert len(env.ha.pushes) == 1
    sent = len(_status_events(env))
    env.clock.advance(300)
    bridge.worker.run_pending()
    assert len(_status_events(env)) == sent + 1
    assert _status_events(env)[-1]["status"] == "konfigurationsfehler"
    assert neustarts == []

    env.clock.advance(600)
    bridge.worker.run_pending()

    assert neustarts == [1]
    assert _start_bridge(env, **KONFIGURATIONSFEHLER).reason == "konfigurationsfehler"  # neuer Prozess
    assert len(env.ha.pushes) == 1
    assert env.ha.writes == []


def test_repeated_start_error_does_not_push_again(env, sleeps):
    """Review Focus 4."""
    assert _start_bridge(env, **KONFIGURATIONSFEHLER).reason == "konfigurationsfehler"
    assert _start_bridge(env, **KONFIGURATIONSFEHLER).reason == "konfigurationsfehler"

    assert len(env.ha.pushes) == 1


def test_repeated_helper_failure_with_changing_error_text_does_not_push_again(env, monkeypatch, sleeps):
    """Review Focus 4: HA vergibt je Lauf eine neue Flow-ID; der Meldezustand darf daran nicht
    haengen, sonst meldet jeder Neustart erneut."""
    run = {"flow_id": ""}

    def _rejected(**kwargs):
        raise requests.HTTPError(
            "400 Client Error: Bad Request for url: "
            f"http://supervisor/core/api/config/config_entries/flow/{run['flow_id']}"
        )

    monkeypatch.setattr("heizungsbruecke.derived_sensors.ensure_all", _rejected)

    for flow_id in ("0a1b2c3d", "9f8e7d6c"):
        run["flow_id"] = flow_id
        assert _start_bridge(env).reason == "konfigurationsfehler"

    assert len(env.ha.pushes) == 1  # kein zweiter Push
    # Die HA-Benachrichtigung wird je Start neu angelegt (ersetzt die vorige per notification_id,
    # sie ueberlebt keinen HA-Neustart) und traegt den aktuellen Fehlertext (Review I3).
    persistent = _konfiguration_notices(env)
    assert len(persistent) == 2
    assert "9f8e7d6c" in persistent[-1][1]
    grund = _status_events(env)[-1]["grund"]
    assert "Hilfs-Entities konnten nicht angelegt werden" in grund
    assert "9f8e7d6c" in grund  # der Status zeigt den aktuellen Fehler im Detail


def test_repeated_missing_entity_does_not_push_again(env, sleeps):
    env.ha.missing.add("number.heat_limit")

    assert _start_bridge(env).reason == "konfigurationsfehler"
    assert _start_bridge(env).reason == "konfigurationsfehler"

    assert len(env.ha.pushes) == 1
    assert "Entity fehlt in Home Assistant: number.heat_limit" in _status_events(env)[-1]["grund"]


def test_start_waits_for_ha_before_reporting(env, sleeps):
    answers = [False, False]
    env.ha.is_reachable = lambda: answers.pop(0) if answers else True

    assert _start_bridge(env, **KONFIGURATIONSFEHLER).reason == "konfigurationsfehler"

    assert sleeps[:2] == [5, 10]
    assert [event["status"] for event in _status_events(env)] == ["startet", "konfigurationsfehler"]


# --- Budget fuer fehlende Entities und Hilfssensoren ---

_DERIVED_BINDUNG = _bindung(room_sensors=["sensor.rt"], entity_outdoor_temp="sensor.outdoor")


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

    _wait_for_required_entities(ha_api, _bindung())

    assert len(sleeps) == 29  # nur Warten auf RUNNING, kein Budget verbraucht
    assert ha["booting_checks"] == 0


def test_budget_expires_when_entity_stays_missing_while_ha_is_running(monkeypatch, sleeps):
    ha_api = HomeAssistantApi(base_url="http://supervisor", token="t")
    monkeypatch.setattr(ha_api, "get_config", lambda: {"state": "RUNNING"})
    monkeypatch.setattr(ha_api, "entity_exists", lambda entity_id: entity_id != "number.heat_limit")

    with pytest.raises(StartupError, match="number.heat_limit"):
        _wait_for_required_entities(ha_api, _bindung())

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

    _wait_for_required_entities(ha_api, _bindung())

    assert sleeps == [5, 10]


def test_missing_entity_after_budget_names_the_entity(sleeps):
    ha_api = _reachable()
    ha_api.entity_exists.side_effect = lambda entity_id: entity_id != "number.heat_limit"

    with pytest.raises(StartupError, match="number.heat_limit"):
        _wait_for_required_entities(ha_api, _bindung())


def test_required_entities_include_every_room_sensor_without_attribute_suffix(sleeps):
    ha_api = _reachable()
    checked = []
    ha_api.entity_exists.side_effect = lambda entity_id: checked.append(entity_id) or True

    _wait_for_required_entities(ha_api, _bindung(
        room_sensors=["sensor.a", "climate.wz::current_temperature"], entity_room_target="climate.wz::temperature",
    ))

    assert "climate.wz" in checked and "sensor.a" in checked
    assert not any("::" in entity_id for entity_id in checked)


def test_required_entities_include_zone_and_min_flow(sleeps):
    ha_api = _reachable()
    checked = []
    ha_api.entity_exists.side_effect = lambda entity_id: checked.append(entity_id) or True

    _wait_for_required_entities(ha_api, _bindung())

    assert {"climate.zone", "number.min_flow"} <= set(checked)
    assert "number.offset_current" not in checked


def test_helper_creation_failure_after_budget_is_a_start_error(monkeypatch, sleeps):
    def _broken(**kwargs):
        raise RuntimeError("Template-Flow abgelehnt")

    monkeypatch.setattr("heizungsbruecke.derived_sensors.ensure_all", _broken)

    with pytest.raises(StartupError, match="Hilfs-Entities konnten nicht angelegt werden"):
        _ensure_derived_sensors_with_retry(_reachable(), _DERIVED_BINDUNG, "t1")


def test_ensure_derived_sensors_with_retry_recovers_after_transient_failures(monkeypatch, sleeps, tmp_path):
    """Die echten Hilfssensoren (derived_sensors.ensure_all) an einer HA-Grenze, die zweimal nicht erreichbar ist: nach
    5 und 10 s Pause legt der dritte Versuch Raum- und Aussen-Vorlage aus Raumfuehler, Wetter-Entity und Anlage an."""
    monkeypatch.setattr(config, "DERIVED_SENSORS_PATH", tmp_path / "derived_sensors.json")
    ha_api = MagicMock(spec=HomeAssistantApi)
    ha_api.is_reachable.return_value = True
    ha_api.entity_exists.return_value = False
    fehlversuche = [ConnectionError("HA Core noch nicht erreichbar")] * 2

    def _create(name, template):
        if fehlversuche:
            raise fehlversuche.pop()
        return "sensor.smartheat_t1_aussentemperatur" if "Außentemperatur" in name else "sensor.smartheat_t1_raumtemperatur"

    ha_api.create_template_sensor.side_effect = _create
    bindung = _bindung(room_sensors=["sensor.rt"], entity_outdoor_temp="weather.home")

    result = _ensure_derived_sensors_with_retry(ha_api, bindung, "t1")

    assert result.entity_ids == {"room_actual": "sensor.smartheat_t1_raumtemperatur",
                                 "outdoor_temp": "sensor.smartheat_t1_aussentemperatur"}
    assert sleeps == [5, 10]
    assert ha_api.create_template_sensor.call_args_list[-2:] == [  # Wirkung als Aufruf an der HA-Grenze
        call(name="SmartHeat t1 Raumtemperatur", template=room_temperature_template(["sensor.rt"])),
        call(name="SmartHeat t1 Außentemperatur", template=outdoor_temperature_template("weather.home")),
    ]


# --- Zeitzone ---

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

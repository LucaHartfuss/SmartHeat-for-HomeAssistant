"""Start, Startfehler und Start-Hilfen des HA-Hosts im Geraete-Pfad (bis 0.36 ueber __main__._start_bridge mit
options.json): Konfigurationsfehler mit Ruhezustand, Neupruefung und Meldezustand, Warten auf HA, Budget fuer fehlende
Entities und Hilfssensoren, Zeitzone; dazu hostneutrale Handler der Laufzeit (Telemetrie, _prime,
Verbindungswaechter). Der Einstieg main() selbst steht in test_main_geraet.py."""
import logging
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests
from conftest import FakeClock
from fakes import runtime_config
from geraet_helpers import konfiguration_aus_optionen
from test_runtime import _start_bridge, env  # noqa: F401  (Fixture)

from heizungsbruecke import dokument
from heizungsbruecke.derived_sensors import DerivedSensors
from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.host import (
    DERIVED_SENSORS_RETRY_DELAYS_SECONDS,
    StartupError,
    _check_timezone,
    _ensure_derived_sensors_with_retry,
    _retry_with_budget,
    _wait_for_required_entities,
)
from smartheat_runtime import app

#: Ungueltiges Telemetrie-Intervall im Konfigurationsdokument: ein Konfigurationsfehler beim Start.
KONFIGURATIONSFEHLER = {"telemetry_interval_seconds": 5}


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


def _options(**overrides):
    """Die Optionen, die der Host aus dem Konfigurationsdokument liest (dokument.optionen)."""
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
    return dokument.optionen(konfiguration_aus_optionen(options))


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


def test_invalid_telemetry_interval_is_a_configuration_error(env, caplog, sleeps):
    with caplog.at_level(logging.ERROR):
        result = _start_bridge(env, **KONFIGURATIONSFEHLER)

    assert result.reason == "konfigurationsfehler"
    assert "telemetry_interval_seconds" in caplog.text


def test_configuration_error_idles_with_heartbeat_and_rechecks_without_second_push(env, monkeypatch, sleeps):
    """Review Focus 1: kein Exit (Watchdog-Karussell), Lebenszeichen, nach 900 s Neupruefung per os.execv, derselbe
    Fehler meldet nicht erneut. Bis 0.36 ausgeloest von veralteten Optionen, jetzt von einem ungueltigen Dokument."""
    execs = []
    monkeypatch.setattr("os.execv", lambda path, args: execs.append(args))
    monkeypatch.setattr("sys.orig_argv", ["python", "-m", "heizungsbruecke"])  # Add-on-Start (restart_process)

    bridge = _start_bridge(env, **KONFIGURATIONSFEHLER)

    assert bridge.reason == "konfigurationsfehler"
    assert len(env.ha.pushes) == 1
    sent = len(_status_events(env))
    env.clock.advance(300)
    bridge.worker.run_pending()
    assert len(_status_events(env)) == sent + 1
    assert _status_events(env)[-1]["status"] == "konfigurationsfehler"
    assert execs == []

    env.clock.advance(600)
    bridge.worker.run_pending()

    assert execs == [[sys.executable, "-m", "heizungsbruecke"]]
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

    _wait_for_required_entities(ha_api, _options())

    assert len(sleeps) == 29  # nur Warten auf RUNNING, kein Budget verbraucht
    assert ha["booting_checks"] == 0


def test_budget_expires_when_entity_stays_missing_while_ha_is_running(monkeypatch, sleeps):
    ha_api = HomeAssistantApi(base_url="http://supervisor", token="t")
    monkeypatch.setattr(ha_api, "get_config", lambda: {"state": "RUNNING"})
    monkeypatch.setattr(ha_api, "entity_exists", lambda entity_id: entity_id != "number.heat_limit")

    with pytest.raises(StartupError, match="number.heat_limit"):
        _wait_for_required_entities(ha_api, _options())

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

    _wait_for_required_entities(ha_api, _options())

    assert sleeps == [5, 10]


def test_missing_entity_after_budget_names_the_entity(sleeps):
    ha_api = _reachable()
    ha_api.entity_exists.side_effect = lambda entity_id: entity_id != "number.heat_limit"

    with pytest.raises(StartupError, match="number.heat_limit"):
        _wait_for_required_entities(ha_api, _options())


def test_required_entities_include_every_room_sensor_without_attribute_suffix(sleeps):
    ha_api = _reachable()
    checked = []
    ha_api.entity_exists.side_effect = lambda entity_id: checked.append(entity_id) or True

    _wait_for_required_entities(ha_api, _options(
        room_sensors=["sensor.a", "climate.wz::current_temperature"], entity_room_target="climate.wz::temperature",
    ))

    assert "climate.wz" in checked and "sensor.a" in checked
    assert not any("::" in entity_id for entity_id in checked)


def test_required_entities_include_zone_and_min_flow(sleeps):
    ha_api = _reachable()
    checked = []
    ha_api.entity_exists.side_effect = lambda entity_id: checked.append(entity_id) or True

    _wait_for_required_entities(ha_api, _options())

    assert {"climate.zone", "number.min_flow"} <= set(checked)
    assert "number.offset_current" not in checked


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


# --- Hostneutrale Handler der Laufzeit ---

def test_on_telemetry_hands_the_energy_normalizer_to_the_tick(make_store, monkeypatch):
    from smartheat_core.binding import VAILLANT_MYPYLLANT
    from smartheat_runtime.app import _on_telemetry

    captured = {}
    monkeypatch.setattr(
        "smartheat_runtime.app.telemetry.run_telemetry_tick", lambda *args, **kwargs: captured.update(kwargs),
    )
    rt = SimpleNamespace(
        worker=MagicMock(), config=runtime_config(), store=make_store(), mqtt_client=MagicMock(),
        manifest=MagicMock(), signals=MagicMock(), notifier=MagicMock(), status=MagicMock(),
        override=SimpleNamespace(binding=SimpleNamespace(description=VAILLANT_MYPYLLANT)),
    )
    rt.mqtt_client.is_connected.return_value = True

    _on_telemetry(rt, None)

    assert "waerme" not in captured  # Audit 4 P-B: die Erkennung liegt beim Server
    assert callable(captured["energy"])  # Vaillant: Tageszaehler werden normalisiert (Audit 4 P-B, A4-49)


def _prime_rt(monkeypatch, capture):
    """Laufzeit-Attrappe fuer _prime: nur Heizgrenzen-Erfassung unter Test, alles Uebrige still."""
    monkeypatch.setattr(app.regulation, "read_room_target_live", lambda rt: 20.5)
    monkeypatch.setattr(app.regulation, "run_local_check", lambda rt: None)
    monkeypatch.setattr(app.derived, "sync", lambda rt: None)
    monkeypatch.setattr(app, "_prepare_zone", lambda rt: None)
    return SimpleNamespace(
        override=SimpleNamespace(capture_originals=capture),
        store=SimpleNamespace(
            state=SimpleNamespace(restore_point={"room_setpoint": 21.0}, stable_target=None), update=lambda **kw: None,
        ),
    )


def test_prime_captures_the_heat_limit_original(monkeypatch):
    capture = MagicMock()
    rt = _prime_rt(monkeypatch, capture)

    app._prime(rt)

    capture.assert_called_once_with()


def test_prime_heat_limit_capture_failure_does_not_abort_startup(monkeypatch, caplog):
    capture = MagicMock(side_effect=RuntimeError("Speicher voll"))
    rt = _prime_rt(monkeypatch, capture)
    ran = []
    monkeypatch.setattr(app.regulation, "run_local_check", lambda rt: ran.append(True))

    with caplog.at_level(logging.ERROR):
        app._prime(rt)

    assert ran == [True]
    assert "Heizgrenze" in caplog.text


@pytest.mark.parametrize("failures, down_seconds, expect_query", [
    (3, 900, True), (2, 900, False), (3, 899, False), (0, 2000, False),
])
def test_connection_check_queries_the_status_after_long_repeated_failures(failures, down_seconds, expect_query, monkeypatch):
    rt = SimpleNamespace(
        worker=MagicMock(), config=runtime_config(), clock=FakeClock(), mqtt_down_since=None,
        mqtt_client=MagicMock(), store=SimpleNamespace(state=SimpleNamespace(
            abo_inactive_since=None, delivery=SimpleNamespace(pending="offen"))),
    )
    rt.mqtt_client.is_connected.return_value = False
    rt.mqtt_client.connect_failures = failures
    called = []
    monkeypatch.setattr(app.abo, "handle_connection_failing", lambda runtime: called.append(runtime))
    app._on_connection_check(rt, None)  # setzt mqtt_down_since
    rt.clock.advance(down_seconds)
    app._on_connection_check(rt, None)
    assert bool(called) is expect_query


def test_mqtt_connected_resets_the_connection_failing_throttle():
    rt = SimpleNamespace(
        status=MagicMock(), notifier=MagicMock(), auth_rejected_queried_at=5.0, auth_rejected_last_status="x",
        connection_failing_queried_at=7.0,
    )
    rt.status.flags.zugang_abgelehnt = False
    rt.status.flags.gestartet = True
    with patch.object(app.ticks, "deliver"):
        app._on_mqtt_connected(rt, None)
    assert rt.connection_failing_queried_at is None


def test_connection_check_starts_no_probe_tick_when_the_query_found_the_abo_inactive(monkeypatch):
    state = SimpleNamespace(abo_inactive_since=None, delivery=SimpleNamespace(pending=None))
    rt = SimpleNamespace(
        worker=MagicMock(), config=runtime_config(), clock=FakeClock(), mqtt_down_since=None,
        mqtt_client=MagicMock(), store=SimpleNamespace(state=state),
    )
    rt.mqtt_client.is_connected.return_value = False
    rt.mqtt_client.connect_failures = 3
    monkeypatch.setattr(app.abo, "handle_connection_failing",
                        lambda runtime: setattr(state, "abo_inactive_since", "jetzt"))
    probes = []
    monkeypatch.setattr(app.ticks, "start_probe_tick", lambda *args: probes.append(args))
    app._on_connection_check(rt, None)
    rt.clock.advance(900)
    app._on_connection_check(rt, None)
    assert probes == []


@pytest.mark.parametrize("status", [app.abo.entitlement.ACTIVE, app.abo.entitlement.UNKNOWN])
def test_connection_check_still_starts_the_probe_tick_after_an_unclear_or_active_query(status, monkeypatch):
    """Regression: ein normaler Ausfall (Abo aktiv oder Abfrage unklar) darf den Notbetrieb nicht verhindern."""
    state = SimpleNamespace(abo_inactive_since=None, delivery=SimpleNamespace(pending=None))
    rt = SimpleNamespace(
        worker=MagicMock(), config=runtime_config(), clock=FakeClock(), mqtt_down_since=None, connection_failing_queried_at=None,
        mqtt_client=MagicMock(), store=SimpleNamespace(state=state), ha_api=MagicMock(),
    )
    rt.mqtt_client.is_connected.return_value = False
    rt.mqtt_client.connect_failures = 3
    monkeypatch.setattr(app.abo.entitlement, "query", lambda config: status)
    probes = []
    monkeypatch.setattr(app.ticks, "start_probe_tick", lambda *args: probes.append(args))
    app._on_connection_check(rt, None)
    rt.clock.advance(900)
    app._on_connection_check(rt, None)
    assert len(probes) == 1
    assert state.abo_inactive_since is None
    rt.ha_api.send_notification.assert_not_called()

"""Betrieb von __main__ ueber den Regel-Worker (Design-Spec 2026-09-26): Boot,
Tick-Zustellung, Datenfehler, Notbetrieb, lokale Checks und Abo-Pfade. Getrieben ueber
_start_bridge mit Fake-Uhr (tests/conftest.py), Fake-HA, Fake-MQTT und Fake-Trigger-Client."""
import copy
import json
import logging
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import heizungsbruecke.__main__ as main_module
from heizungsbruecke import abo, backup_store, entitlement, ticks
from heizungsbruecke.backup_store import load_backup, save_backup
from heizungsbruecke.delivery import DeliveryState
from heizungsbruecke.derived_sensors import DerivedSensors
from heizungsbruecke.override import OWN_WRITE_SETTLE_SECONDS
from heizungsbruecke.runtime import Runtime
from heizungsbruecke.status import ADDON_VERSION

OPTIONS = {
    "tenant_id": "test_tenant",
    "verteilsystem": "Heizkoerper",  # Clamps 0.4-1.5 / 15-25 (Mindestvorlauf 20-30), Boost 1.5/25
    "daily_trigger_time": "12:00",  # Tagestick 12:00
    "mqtt_username": "u",
    "mqtt_password": "p",
    "room_sensors": ["sensor.room_actual"],
    "entity_room_target": "sensor.room_target",
    "entity_curve_current": "number.curve_current",
    "entity_shift_current": "number.shift_current",
    "entity_min_flow": "number.min_flow",
    "entity_outdoor_temp": "sensor.outdoor_temp",
    "entity_heat_limit": "number.heat_limit",
    "notify_services": ["notify.handy"],
    "accounts_api_base_url": "https://accounts.example.test",
}

DERIVED: dict[str, str] = {}

NOTBETRIEB_ON = "Heizungsbrücke: Server antwortet nicht, Notbetrieb aktiv. Die Heizung wird bei Bedarf lokal abgesichert."


class FakeHa:
    token = "tok"

    def __init__(self):
        self.states = {
            "sensor.room_actual": 20.0, "sensor.room_target": 21.0,
            "number.curve_current": 0.9, "number.shift_current": 22.0,
            "sensor.outdoor_temp": 5.0, "number.heat_limit": 16.0,
            # Raum-Soll 21.0 -> kein Mindestvorlauf-Schreiben beim Start
            "number.min_flow": 21.0,
        }
        self.writes = []
        self.pushes = []
        self.persistent = []
        self.dismissed = []
        self.events = []
        self.write_error = None

    def get_state(self, entity_id):
        value = self.states[entity_id]
        if isinstance(value, Exception):
            raise value
        return value

    def set_number_value(self, entity_id, value):
        if self.write_error is not None:
            raise self.write_error
        self.states[entity_id] = value
        self.writes.append((entity_id, value))

    def set_climate_temperature(self, entity_id, value):
        self.set_number_value(f"{entity_id}::temperature", value)

    def set_hvac_mode(self, entity_id, mode):
        self.states[entity_id] = mode
        self.writes.append((entity_id, mode))

    def send_notification(self, service, message):
        self.pushes.append(message)

    def create_persistent_notification(self, title, message, notification_id):
        self.persistent.append((notification_id, message))

    def dismiss_persistent_notification(self, notification_id):
        self.dismissed.append(notification_id)

    def get_config(self):
        return {"time_zone": "Europe/Berlin", "state": "RUNNING"}

    def websocket_url(self):
        return "ws://x/api/websocket"

    def fire_event(self, event_type, data):
        self.events.append((event_type, copy.deepcopy(data)))

    def is_reachable(self):
        return True

    def entity_exists(self, entity_id):
        return True

    def get_raw_state(self, entity_id):
        value = self.states[entity_id]
        if isinstance(value, Exception):
            raise value
        if value in ("unavailable", "unknown", ""):
            raise ValueError(value)
        return str(value)


_OMIT = object()


class FakeMqtt:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.snapshots = []
        self.telemetry = []
        self.setpoints_callback = None
        self.publish_error = None
        self.loop_started = False
        self.stopped = False
        self.connected = True

    def is_connected(self):
        return self.connected

    def subscribe_setpoints(self, on_message):
        self.setpoints_callback = on_message

    def loop_start(self):
        self.loop_started = True

    def publish_snapshot(self, payload):
        if self.publish_error is not None:
            raise self.publish_error
        self.snapshots.append(payload)

    def publish_telemetry(self, payload):
        self.telemetry.append(payload)

    def stop(self):
        self.stopped = True

    def answer(self, seq, status="ok", curve=0.95, shift=23.0, reason=None, schema=3):
        """Server-Antwort ueber den echten paho-Callback einspeisen (schema=_OMIT: Feld fehlt)."""
        message = MagicMock()
        message.retain = False
        payload = {
            "schema": schema, "seq": seq, "ts": "x", "status": status, "curve": curve, "shift": shift, "reason": reason,
        }
        if schema is _OMIT:
            del payload["schema"]
        message.payload = json.dumps(payload)
        self.setpoints_callback(None, None, message)


class FakeTriggerClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.connected = True
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


@pytest.fixture
def env(tmp_path, monkeypatch, clock):
    paths = {}
    for name in ("BACKUP_PATH", "FAILSAFE_PATH", "ENTITLEMENT_PATH", "DERIVED_SENSORS_PATH"):
        paths[name] = tmp_path / f"{name.lower()}.json"
        monkeypatch.setattr(f"heizungsbruecke.config.{name}", paths[name])
    env_derived = {"replaced": ()}
    monkeypatch.setattr(
        "heizungsbruecke.derived_sensors.ensure_all",
        lambda **kwargs: DerivedSensors({**DERIVED, "room_actual": "sensor.room_actual"}, env_derived["replaced"], "fp1"),
    )
    abo = {"status": entitlement.ACTIVE, "queries": 0}

    def _query_status(tenant_id, base_url, username, password):
        # Jede Abo-Abfrage (Boot, Tick-Zustellung, abgelehnte Anmeldung, Fristende) nutzt die
        # Basis-URL aus den Optionen (Spec TP3, 2.5) und die MQTT-Zugangsdaten (Spec TP8, 3.1).
        assert base_url == OPTIONS["accounts_api_base_url"]
        assert (username, password) == (OPTIONS["mqtt_username"], OPTIONS["mqtt_password"])
        abo["queries"] += 1
        return abo["status"]

    monkeypatch.setattr("heizungsbruecke.entitlement.query_status", _query_status)
    mqtt_clients, trigger_clients = [], []

    def _mqtt_factory(**kwargs):
        mqtt_clients.append(FakeMqtt(**kwargs))
        return mqtt_clients[-1]

    def _trigger_factory(**kwargs):
        trigger_clients.append(FakeTriggerClient(**kwargs))
        return trigger_clients[-1]

    monkeypatch.setattr("heizungsbruecke.triggers.BridgeMqttClient", _mqtt_factory)
    monkeypatch.setattr("heizungsbruecke.triggers.HaTriggerClient", _trigger_factory)
    return SimpleNamespace(
        clock=clock, paths=paths, abo=abo, ha=FakeHa(), mqtt_clients=mqtt_clients, trigger_clients=trigger_clients,
        derived=env_derived,
    )


# --- Harness ---
# Einzige Stellen dieser Datei, die Interna der Bruecke kennen. Der Umbau (TP5) passt nur
# diesen Abschnitt und die Fixture `env` an; die Szenarien darunter pruefen nur die
# Aussensicht: HA-Writes, Pushes, MQTT-Publishes, Dateiinhalte.

ABO_ENDED = (
    "SmartHeat: Abo seit 30 Tagen inaktiv. Die Heizungssteuerung ist beendet, "
    "die zuletzt gelernten Werte bleiben eingestellt."
)


def _start_bridge(env, **option_overrides):
    return main_module._start_bridge({**OPTIONS, **option_overrides}, env.ha, clock=env.clock)


def _is_running(bridge) -> bool:
    return isinstance(bridge, Runtime)


def _delivery(bridge):
    return bridge.store.state.delivery


def _awaiting_ack(bridge) -> bool:
    return _delivery(bridge).pending.phase == "awaiting_ack"


def _boost_active(bridge) -> bool:
    return bridge.store.state.boost_active


def _stable_target(bridge):
    return bridge.store.state.stable_target


def _abo_inactive_since(bridge):
    return bridge.store.state.abo_inactive_since


def _mark_abo_finished(bridge) -> None:
    bridge.store.update(abo_finished=True)


def _override_options(bridge, **values) -> None:
    bridge.options.update(values)  # dasselbe Dict wie in bridge.override


def _raise_oserror(*args, **kwargs):
    raise OSError("Datentraeger kaputt")


def _break_backup_writes(monkeypatch) -> None:
    monkeypatch.setattr("heizungsbruecke.backup_store.save_backup", _raise_oserror)


def _fail_next_snapshot_read(monkeypatch, error: Exception) -> None:
    original = ticks.read_snapshot_roles
    failures = [error]

    def _flaky(*args, **kwargs):
        if failures:
            raise failures.pop()
        return original(*args, **kwargs)

    monkeypatch.setattr("heizungsbruecke.ticks.read_snapshot_roles", _flaky)


def _quiet_backup(env, **extra):
    """Backup, bei dem beim Start weder ein Tick faellig ist noch ein Boost startet."""
    save_backup(env.paths["BACKUP_PATH"], {
        "last_room_target": 21.0, "last_published_target_rt": 21.0,
        "last_daily_trigger_date": datetime.now().date().isoformat(),
        "curve_current": 0.9, "shift_current": 22.0, **extra,
    })


def _start(env, **option_overrides):
    bridge = _start_bridge(env, **option_overrides)
    assert _is_running(bridge)
    bridge.worker.run_pending()
    return bridge


def _regulation_writes(env):
    """Schreibvorgaenge ohne den Mindestvorlauf, der jeder Raum-Soll-Aenderung folgt (min_flow.py)."""
    return [write for write in env.ha.writes if write[0] != "number.min_flow"]


def _mqtt(env):
    return env.mqtt_clients[-1]


def _backup(env):
    return load_backup(env.paths["BACKUP_PATH"])


def _failsafe_file(env):
    return load_backup(env.paths["FAILSAFE_PATH"])


def _trigger(env, bridge, entity_id):
    env.trigger_clients[-1].kwargs["on_trigger_event"]({"platform": "state", "entity_id": entity_id, "attribute": None})
    bridge.worker.run_pending()


def _set_room_target(env, bridge, value):
    env.ha.states["sensor.room_target"] = value
    _trigger(env, bridge, "sensor.room_target")


def _advance(env, bridge, seconds):
    env.clock.advance(seconds)
    bridge.worker.run_pending()


def _answer(env, bridge, seq, **kwargs):
    _mqtt(env).answer(seq, **kwargs)
    bridge.worker.run_pending()


# --- Boot ---

def test_start_runs_priming_check_before_mqtt_loop_start(env, monkeypatch):
    _quiet_backup(env)
    order = []
    read_state = env.ha.get_state

    def _spy(entity_id):
        if entity_id == "sensor.room_actual":
            order.append("check")
        return read_state(entity_id)

    env.ha.get_state = _spy
    monkeypatch.setattr(FakeMqtt, "loop_start", lambda self: order.append("loop_start"))

    _start(env)

    assert order[:2] == ["check", "loop_start"]


def test_boot_prepares_zone_before_first_snapshot(env, monkeypatch):
    # Plan-Praezisierung 11: Zone meldet 0 (heizt gerade nicht) und steht nicht auf Manuell.
    env.ha.states.update({"climate.zone": "auto", "climate.zone::temperature": 0.0})
    order = []
    monkeypatch.setattr(FakeMqtt, "loop_start", lambda self: order.append(list(env.ha.writes)))

    bridge = _start(env, entity_shift_current="climate.zone")

    assert order == [[("climate.zone", "heat_cool"), ("climate.zone::temperature", 21.0)]]  # vor MQTT
    assert env.ha.states["climate.zone::temperature"] == 21.0  # Raum-Soll als Startwert
    assert _backup(env)["shift_current"] == 21.0

    _trigger(env, bridge, "sensor.room_target")  # erster Tick

    assert _mqtt(env).snapshots[0]["roles"]["shift_current"] == 21.0


def test_boot_keeps_a_usable_zone_shift(env):
    env.ha.states.update({"climate.zone": "heat_cool", "climate.zone::temperature": 20.5})

    _start(env, entity_shift_current="climate.zone")

    assert env.ha.writes == []


def _backup_before_tp11(env, **extra):
    """backup.json von 0.23.0: Wiederherstellungspunkt nur mit Steigung (am Anschlag), keine
    Parallelverschiebung; beim Start waere sonst kein Tick faellig."""
    backup = {
        "last_room_target": 21.0, "last_published_target_rt": 21.0,
        "last_daily_trigger_date": datetime.now().date().isoformat(),
        "curve_current": 1.5, **extra,
    }
    save_backup(env.paths["BACKUP_PATH"], backup)


def test_first_start_without_shift_restore_point_reseeds_curve_and_ticks_at_once(env):
    # 0.23.0 -> 0.24.0: der alte Punkt (1.5, am Anschlag) darf den von Hand gesetzten Anlagenwert
    # nicht per Durchsetzung ueberschreiben, und der Erstkontakt muss mit den Istwerten starten.
    _backup_before_tp11(env)
    env.ha.states.update({"number.curve_current": 1.05, "sensor.room_actual": 21.0})

    bridge = _start(env)

    assert _backup(env)["curve_current"] == 1.05
    _trigger(env, bridge, "sensor.room_actual")  # naechster lokaler Check
    snapshot = _mqtt(env).snapshots[0]
    assert (snapshot["trigger"], snapshot["roles"]["curve_current"]) == ("target_change", 1.05)

    _settle(env, bridge)
    _advance(env, bridge, 300)
    _advance(env, bridge, 300)  # zwei Durchsetzungsrunden ohne Serverantwort
    assert [write for write in env.ha.writes if write[0] == "number.curve_current"] == []
    assert "manual_override_pending" not in _backup(env)


@pytest.mark.parametrize("live,seeded", [(1.8, 1.5), (0.3, 0.4), (1.07, 1.05)])
def test_first_start_reseeds_curve_clamped_and_rounded(env, live, seeded):
    _backup_before_tp11(env)
    env.ha.states["number.curve_current"] = live

    _start(env)

    assert _backup(env)["curve_current"] == seeded


def test_first_start_drops_the_old_curve_point_when_the_plant_is_unreadable(env):
    _backup_before_tp11(env)
    env.ha.states["number.curve_current"] = ValueError("unavailable")

    _start(env)

    assert "curve_current" not in _backup(env)


def test_first_start_during_a_boost_keeps_the_curve_point(env):
    # Die Anlage steht auf Boost-Werten: der Live-Wert ist kein Wiederherstellungspunkt.
    _backup_before_tp11(env, boost_active=True)
    env.ha.states.update({"number.curve_current": 1.5, "sensor.room_actual": 20.0})

    _start(env)

    assert _backup(env)["curve_current"] == 1.5


def test_restart_with_shift_restore_point_keeps_curve_and_does_not_tick(env):
    _quiet_backup(env)
    env.ha.states["number.curve_current"] = 1.05
    bridge = _start(env)

    _trigger(env, bridge, "sensor.room_actual")

    assert _backup(env)["curve_current"] == 0.9
    assert _mqtt(env).snapshots == []


def test_zone_preparation_failed_at_start_is_retried_on_the_next_local_check(env):
    env.ha.states.update({"climate.zone": "unavailable", "climate.zone::temperature": 0.0})
    bridge = _start(env, entity_shift_current="climate.zone")
    assert env.ha.writes == []

    env.ha.states["climate.zone"] = "auto"
    _trigger(env, bridge, "sensor.room_actual")

    assert env.ha.writes[:2] == [("climate.zone", "heat_cool"), ("climate.zone::temperature", 21.0)]
    assert _backup(env)["shift_current"] == 21.0

    writes = len(env.ha.writes)
    env.ha.states["climate.zone"] = "auto"  # erfolgreich vorbereitet: kein weiterer Versuch
    _trigger(env, bridge, "sensor.room_actual")
    assert ("climate.zone", "heat_cool") not in env.ha.writes[writes:]


def test_boot_writes_min_flow_when_it_differs_from_the_room_target(env):
    env.ha.states["number.min_flow"] = 25.0

    _start(env)

    assert ("number.min_flow", 21.0) in env.ha.writes


def test_room_target_change_writes_min_flow(env):
    _quiet_backup(env)
    bridge = _start(env)
    assert env.ha.writes == []

    _set_room_target(env, bridge, 22.0)

    assert ("number.min_flow", 22.0) in env.ha.writes


def _min_flow_writes(env):
    return [value for entity_id, value in env.ha.writes if entity_id == "number.min_flow"]


def test_min_flow_change_back_within_the_settle_window_is_written(env):
    # Die Cloud spiegelt den eigenen Schreibvorgang erst mit dem naechsten Poll: HA zeigt noch 21, die
    # Anlage steht schon auf 22. Zurueck auf 21 muss geschrieben werden, sonst bleibt die Anlage
    # auf 22 und die Durchsetzung meldet spaeter einen falschen Eingriff.
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 22.0)
    env.ha.states["number.min_flow"] = 21.0  # HA hinkt nach

    _set_room_target(env, bridge, 21.0)

    assert _min_flow_writes(env) == [22.0, 21.0]


def test_min_flow_is_not_rewritten_while_ha_lags(env):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 22.0)
    env.ha.states["number.min_flow"] = 21.0  # HA hinkt nach
    env.trigger_clients[-1].connected = False  # Watchdog liest room_target in jedem Takt frisch

    _advance(env, bridge, 300)
    _advance(env, bridge, 300)

    assert _min_flow_writes(env) == [22.0]


def test_room_actual_trigger_does_not_touch_min_flow(env):
    _quiet_backup(env)
    bridge = _start(env)
    env.ha.states["sensor.room_target"] = 22.0  # noch nicht entprellt gemeldet

    _trigger(env, bridge, "sensor.room_actual")

    assert not any(entity_id == "number.min_flow" for entity_id, _ in env.ha.writes)


def test_telemetry_contains_regulation_fields(env):
    _start(env)

    payload = _mqtt(env).telemetry[-1]

    assert payload["room_target"] == 21.0 and payload["outdoor_temp"] == 5.0


def test_priming_failure_keeps_boost_flags_and_the_next_check_restores(env):
    # N4: der naechste erfolgreiche Check beendet den Notfall-Boost regulaer inkl. Zurueckschreiben.
    _quiet_backup(env, emergency_boost_active=True)
    save_backup(env.paths["FAILSAFE_PATH"], {"failsafe_active": False})
    env.ha.states.update({"number.curve_current": 1.5, "number.shift_current": 25.0})
    env.ha.states["sensor.room_actual"] = RuntimeError("HA-API-Hickup beim Booten")

    bridge = _start(env)

    assert _backup(env)["emergency_boost_active"] is True
    assert env.ha.writes == []

    env.ha.states["sensor.room_actual"] = 20.0
    _trigger(env, bridge, "sensor.room_actual")

    assert env.ha.writes == [("number.curve_current", 0.9), ("number.shift_current", 22.0)]
    assert _backup(env)["emergency_boost_active"] is False


def test_restart_after_notbetrieb_ended_restores_device_during_priming(env):
    # Final-Review C1, Szenario A.
    _quiet_backup(env, emergency_boost_active=True, boost_active=False)
    save_backup(env.paths["FAILSAFE_PATH"], {"failsafe_active": False})
    env.ha.states.update({"number.curve_current": 1.5, "number.shift_current": 25.0, "sensor.room_actual": 19.8})

    _start(env)

    assert (env.ha.states["number.curve_current"], env.ha.states["number.shift_current"]) == (0.9, 22.0)
    assert _backup(env)["emergency_boost_active"] is False


def test_restart_during_notbetrieb_continues_emergency_hysteresis(env):
    # Final-Review C1, Szenario B/C: 0.8 K unter Soll liegt zwischen Exit- (0.5) und Einstiegsschwelle (1.0).
    _quiet_backup(env, emergency_boost_active=True)
    save_backup(env.paths["FAILSAFE_PATH"], {"failsafe_active": True})
    env.ha.states.update({"number.curve_current": 1.5, "number.shift_current": 25.0, "sensor.room_actual": 20.2})

    bridge = _start(env)

    assert env.ha.writes == []
    assert _backup(env)["emergency_boost_active"] is True

    env.ha.states["sensor.room_actual"] = 20.6
    _trigger(env, bridge, "sensor.room_actual")

    assert (env.ha.states["number.curve_current"], env.ha.states["number.shift_current"]) == (0.9, 22.0)
    assert _backup(env)["emergency_boost_active"] is False


@pytest.mark.parametrize("content,notbetrieb", [
    ('{"failsafe_active": true}', True),  # Format von 0.15.0
    ("[1, 2]", False),
    ("{kaputt", False),
])
def test_start_reads_old_or_broken_failsafe_file(env, content, notbetrieb):
    # Review Focus 4.
    _quiet_backup(env)
    env.paths["FAILSAFE_PATH"].write_text(content)

    bridge = _start(env)

    assert _delivery(bridge) == DeliveryState(notbetrieb=notbetrieb)
    assert _mqtt(env).snapshots == []


def test_persisted_pending_tick_is_resumed_with_same_seq_and_ends_notbetrieb(env):
    _quiet_backup(env)
    save_backup(env.paths["FAILSAFE_PATH"], {
        "failsafe_active": True, "datenfehler": None, "pending": {"seq": "alt-1", "trigger": "daily"},
    })

    bridge = _start(env)

    assert [(s["seq"], s["trigger"]) for s in _mqtt(env).snapshots] == [("alt-1", "daily")]

    _answer(env, bridge, "alt-1", curve=0.95, shift=23.0)

    assert (env.ha.states["number.curve_current"], env.ha.states["number.shift_current"]) == (0.95, 23.0)
    assert _failsafe_file(env) == {"failsafe_active": False, "datenfehler": None, "pending": None}
    assert env.ha.pushes == ["Heizungsbrücke: Serververbindung wiederhergestellt, Notbetrieb beendet."]


def test_unreachable_broker_does_not_exit_and_tick_runs_into_notbetrieb(env):
    # B10: kein Exit; zwei unbeantwortete Versuche -> Notbetrieb nach ~60 s.
    _quiet_backup(env)
    bridge = _start(env)

    _set_room_target(env, bridge, 20.5)  # Absenkung: Tick, kein Boost
    seq = _mqtt(env).snapshots[0]["seq"]
    _advance(env, bridge, 30)

    assert [s["seq"] for s in _mqtt(env).snapshots] == [seq, seq]
    assert _delivery(bridge).notbetrieb is False

    _advance(env, bridge, 30)

    assert _delivery(bridge).notbetrieb is True
    assert _failsafe_file(env)["failsafe_active"] is True
    assert env.ha.pushes == [NOTBETRIEB_ON]
    assert env.abo["queries"] == 2  # Start + zweiter Timeout


def test_notbetrieb_creates_and_recovery_dismisses_a_persistent_notification(env):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]
    _advance(env, bridge, 30)
    _advance(env, bridge, 30)

    assert ("smartheat_notbetrieb", NOTBETRIEB_ON) in env.ha.persistent

    _answer(env, bridge, seq)

    assert "smartheat_notbetrieb" in env.ha.dismissed


def test_source_change_is_announced_once_and_not_critical(env):
    env.derived["replaced"] = ("room_temperature",)
    _quiet_backup(env)

    _start(env)
    _start(env)

    changed = [text for text in env.ha.pushes if "Quelle der Raum- oder Außentemperatur" in text]
    assert len(changed) == 1
    assert not any(entry[0] == "smartheat_quellwechsel" for entry in env.ha.persistent)


# --- Status-Event (Spec TP7 1.1, 3.1) ---

def _status_states(env):
    """Folge der Gesamtzustaende, aufeinanderfolgende Wiederholungen zusammengefasst (Lebenszeichen
    und Aenderungen anderer Felder senden denselben Zustand erneut)."""
    states = [data["status"] for kind, data in env.ha.events if kind == "smartheat_status"]
    return [state for i, state in enumerate(states) if i == 0 or states[i - 1] != state]


def _last_event(env):
    return [data for kind, data in env.ha.events if kind == "smartheat_status"][-1]


def _connect(env, bridge):
    _mqtt(env).kwargs["on_connected"](None)
    bridge.worker.run_pending()


def test_status_goes_from_startet_to_regelt_when_mqtt_connects(env):
    _quiet_backup(env)
    bridge = _start(env)
    assert _status_states(env) == ["startet"]

    _connect(env, bridge)

    assert _status_states(env) == ["startet", "regelt"]
    assert _last_event(env)["addon_version"] == ADDON_VERSION


def test_status_is_abo_inaktiv_right_after_start_without_mqtt(env):
    _quiet_backup(env)
    env.abo["status"] = entitlement.INACTIVE

    _start(env)

    assert env.mqtt_clients == []
    assert _status_states(env) == ["startet", "abo_inaktiv"]


def test_status_carries_the_setup_id_from_the_options(env):
    _quiet_backup(env)
    _start(env, setup_id="wizard-42")

    assert env.ha.events[0][1]["setup_id"] == "wizard-42"


def test_status_is_sent_again_when_the_ha_connection_comes_back(env):
    _quiet_backup(env)
    bridge = _start(env)
    _connect(env, bridge)
    before = len(env.ha.events)

    env.trigger_clients[-1].kwargs["on_connected"]()
    bridge.worker.run_pending()

    assert len(env.ha.events) == before + 1
    assert _last_event(env)["status"] == "regelt"


def test_status_event_carries_the_full_state(env):
    _quiet_backup(env, notify_states={"raumfuehler:sensor.a": "ausgefallen", "batterie:sensor.b": "niedrig"})
    bridge = _start(env)
    _connect(env, bridge)
    _set_room_target(env, bridge, 20.5)

    _answer(env, bridge, _mqtt(env).snapshots[0]["seq"], curve=0.95, shift=23.0)

    event = _last_event(env)
    assert (event["schema"], event["tenant_id"], event["status"], event["boost"], event["abo"]) == (
        1, "test_tenant", "regelt", "keiner", "aktiv",
    )
    assert (event["kurve"], event["parallelverschiebung"]) == (0.95, 23.0)
    assert event["letzte_serverantwort"] is not None
    assert event["hinweise"] == {
        "raumfuehler_ausgefallen": ["sensor.a"], "batterie_niedrig": ["sensor.b"], "manueller_eingriff": None,
    }


def test_status_follows_notbetrieb_and_a_rejected_answer(env):
    _quiet_backup(env)
    bridge = _start(env)
    _connect(env, bridge)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]
    _advance(env, bridge, 30)
    _advance(env, bridge, 30)
    assert _status_states(env)[-1] == "notbetrieb"
    assert _last_event(env)["letzte_serverantwort"] is None

    _answer(env, bridge, seq, status="rejected", reason="unplausibler Wert für dat: 99")

    event = _last_event(env)
    assert (event["status"], event["notbetrieb"]) == ("datenfehler", False)
    assert event["datenfehler"] == {"art": "server", "rollen": []}
    assert event["letzte_serverantwort"] is not None


def test_open_critical_notifications_are_recreated_when_the_ha_connection_comes_back(env):
    """Review I3: nach einem HA-Neustart fehlen die persistent_notifications (nur im Speicher von
    HA). Beim Wiederverbinden legt das Add-on die offenen kritischen neu an, ohne Push."""
    _quiet_backup(
        env, notify_states={"abo": "inaktiv", "batterie:sensor.x": "niedrig"},
        notify_messages={"abo": "SmartHeat: Abo inaktiv"},
    )
    env.abo["status"] = entitlement.UNKNOWN  # Abo-Meldung bleibt offen
    bridge = _start(env)
    pushes_before = list(env.ha.pushes)
    env.ha.persistent.clear()

    env.trigger_clients[-1].kwargs["on_connected"]()
    bridge.worker.run_pending()

    assert env.ha.persistent == [("smartheat_abo", "SmartHeat: Abo inaktiv")]
    assert env.ha.pushes == pushes_before


def test_successful_start_clears_an_earlier_configuration_error(env):
    _quiet_backup(env)
    save_backup(env.paths["BACKUP_PATH"], {**_backup(env), "notify_states": {"konfiguration": "fehler:alt"}})

    _start(env)

    assert "smartheat_konfiguration" in env.ha.dismissed
    assert any("Einrichtung in Ordnung" in text for text in env.ha.pushes)


# --- Zustellung und Antworten ---

def test_answer_writes_values_and_closes_tick(env):
    _quiet_backup(env)
    bridge = _start(env)

    _set_room_target(env, bridge, 20.5)
    snapshot = _mqtt(env).snapshots[0]
    _answer(env, bridge, snapshot["seq"], curve=0.95, shift=23.0)

    assert snapshot["trigger"] == "target_change"
    assert snapshot["roles"]["room_target"] == 20.5
    assert set(snapshot["roles"]) == {"heat_limit", "room_target", "curve_current", "shift_current"}
    assert ("number.min_flow", 20.5) in env.ha.writes  # Mindestvorlauf folgt dem Raum-Soll
    assert _regulation_writes(env) == [("number.curve_current", 0.95), ("number.shift_current", 23.0)]
    assert _backup(env)["curve_current"] == 0.95
    assert _delivery(bridge).pending is None
    assert env.ha.pushes == []


def test_skipped_answer_is_applied_like_ok(env):
    bridge = _start(env)
    _connect(env, bridge)
    _trigger(env, bridge, "sensor.room_target")  # erster Tick (noch kein Soll gemeldet)
    _answer(env, bridge, _mqtt(env).snapshots[0]["seq"], status="skipped", curve=1.0, shift=20.5,
            reason="zu_wenig_daten")
    assert (env.ha.states["number.curve_current"], env.ha.states["number.shift_current"]) == (1.0, 20.5)
    assert bridge.store.state.delivery.datenfehler is None


def test_dead_room_actual_holds_tick_and_reports_once(env, caplog):
    # Review Focus 1.
    _quiet_backup(env)
    bridge = _start(env)
    env.ha.states["sensor.room_actual"] = ValueError("could not convert string to float: 'unavailable'")

    with caplog.at_level(logging.ERROR):
        _set_room_target(env, bridge, 20.5)

    assert _mqtt(env).snapshots == []
    assert env.ha.pushes == [
        "Heizungsbrücke: Sensor(en) ohne gültigen Wert: room_actual (sensor.room_actual). Die Heizkurve "
        "bleibt unverändert, bis die Werte wieder verfügbar sind (z. B. Batterie prüfen)."
    ]
    assert "lokalen Check" in caplog.text

    _advance(env, bridge, 30)  # Retry, Fuehler weiter tot

    assert _mqtt(env).snapshots == []
    assert len(env.ha.pushes) == 1

    env.ha.states["sensor.room_actual"] = 20.0
    _advance(env, bridge, 300)
    snapshot = _mqtt(env).snapshots[0]
    _answer(env, bridge, snapshot["seq"])

    assert "room_actual" not in snapshot["roles"]
    assert env.ha.pushes[-1] == "Heizungsbrücke: Messwerte wieder gültig, Heizkurve wird wieder angepasst."


def test_publish_error_does_not_break_retry_chain(env):
    # Review Focus 2.
    _quiet_backup(env)
    bridge = _start(env)
    _mqtt(env).publish_error = RuntimeError("paho kaputt")

    _set_room_target(env, bridge, 20.5)
    assert _mqtt(env).snapshots == []

    _mqtt(env).publish_error = None
    _advance(env, bridge, 30)

    assert len(_mqtt(env).snapshots) == 1


def test_unexpected_attempt_error_does_not_break_retry_chain(env, monkeypatch):
    # Review Focus 2: auch ein unerwarteter Fehler im attempt speist ein Ereignis zurueck.
    _quiet_backup(env)
    bridge = _start(env)
    _fail_next_snapshot_read(monkeypatch, RuntimeError("unerwartet"))

    _set_room_target(env, bridge, 20.5)
    assert _mqtt(env).snapshots == []
    assert _awaiting_ack(bridge) is True

    _advance(env, bridge, 30)

    assert len(_mqtt(env).snapshots) == 1


def test_rejected_answer_reports_server_fault_once_and_retries_same_seq(env):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]
    reason = "unplausibler Wert für dat: 99 (erlaubt -40–45)"

    _answer(env, bridge, seq, status="rejected", curve=None, shift=None, reason=reason)

    assert env.ha.pushes == [f"Heizungsbrücke: Server hat die Messwerte abgelehnt ({reason}). Die Heizkurve bleibt unverändert."]
    assert _delivery(bridge).notbetrieb is False

    _advance(env, bridge, 30)
    _answer(env, bridge, seq, status="rejected", curve=None, shift=None, reason=reason)

    assert [s["seq"] for s in _mqtt(env).snapshots] == [seq, seq]
    assert len(env.ha.pushes) == 1
    assert _failsafe_file(env)["datenfehler"] == {"source": "server", "detail": [reason]}


@pytest.mark.parametrize("answer", [
    {"status": "maybe"},
    {"status": "ok", "curve": float("nan")},
    {"status": "ok", "curve": None},
])
def test_invalid_answer_counts_as_server_fault_without_writing(env, answer):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)

    _answer(env, bridge, _mqtt(env).snapshots[0]["seq"], **answer)

    assert _regulation_writes(env) == []
    assert "ungültige Serverantwort" in env.ha.pushes[-1]


@pytest.mark.parametrize("schema", [_OMIT, None, 2, "3", True, 3.0])
def test_answer_with_unknown_schema_counts_as_server_fault_without_writing(env, schema):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)

    _answer(env, bridge, _mqtt(env).snapshots[0]["seq"], schema=schema)

    assert _regulation_writes(env) == []
    assert "ungültige Serverantwort" in env.ha.pushes[-1]
    assert "unbekanntes Schema" in env.ha.pushes[-1]


def test_unknown_schema_for_a_foreign_seq_is_ignored(env):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    pushes_before = list(env.ha.pushes)

    _answer(env, bridge, "fremde-seq", schema=2)

    assert _regulation_writes(env) == []
    assert env.ha.pushes == pushes_before
    assert _awaiting_ack(bridge)


def test_failed_value_write_is_retried_with_same_seq(env):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]
    env.ha.write_error = RuntimeError("HA nicht erreichbar")

    _answer(env, bridge, seq)

    assert _delivery(bridge).pending.seq == seq

    env.ha.write_error = None
    _advance(env, bridge, 30)

    assert [s["seq"] for s in _mqtt(env).snapshots] == [seq, seq]


def test_server_values_are_not_written_when_restore_point_cannot_be_saved(env, monkeypatch):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]
    real_save = backup_store.save_backup
    broken = {"on": True}

    def _save(path, content):
        if broken["on"] and path == env.paths["BACKUP_PATH"]:
            raise OSError("Datentraeger voll")
        return real_save(path, content)

    monkeypatch.setattr("heizungsbruecke.backup_store.save_backup", _save)
    _answer(env, bridge, seq)

    assert _regulation_writes(env) == []
    assert _delivery(bridge).pending is not None and _delivery(bridge).pending.seq == seq

    broken["on"] = False
    _advance(env, bridge, 30)  # Ack-Timeout (delivery.ACK_TIMEOUT_SECONDS)
    assert _mqtt(env).snapshots[-1]["seq"] == seq


def test_answer_during_boost_only_updates_backup(env):
    _quiet_backup(env)
    bridge = _start(env)

    _set_room_target(env, bridge, 22.0)  # Erhoehung: Comfort-Boost + Tick
    assert _regulation_writes(env) == [("number.curve_current", 1.5), ("number.shift_current", 25.0)]

    _answer(env, bridge, _mqtt(env).snapshots[0]["seq"], curve=0.95, shift=23.0)

    assert _regulation_writes(env) == [("number.curve_current", 1.5), ("number.shift_current", 25.0)]
    assert (_backup(env)["curve_current"], _backup(env)["shift_current"]) == (0.95, 23.0)


def test_foreign_seq_and_duplicate_answers_are_ignored(env):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]

    _answer(env, bridge, "fremd")
    assert _regulation_writes(env) == []

    _mqtt(env).answer(seq)
    _mqtt(env).answer(seq)  # QoS-1-Doppelzustellung
    bridge.worker.run_pending()

    assert _regulation_writes(env) == [("number.curve_current", 0.95), ("number.shift_current", 23.0)]


def _start_in_notbetrieb_with_emergency_boost(env):
    _quiet_backup(env)
    save_backup(env.paths["FAILSAFE_PATH"], {"failsafe_active": True, "pending": {"seq": "alt-1", "trigger": "daily"}})
    env.ha.states["sensor.room_actual"] = 19.5  # > 1 K unter Soll -> Notfall-Boost beim Priming
    bridge = _start(env)
    assert (env.ha.states["number.curve_current"], env.ha.states["number.shift_current"]) == (1.5, 25.0)
    return bridge


def test_notbetrieb_end_via_answer_restores_device_from_emergency_boost(env):
    bridge = _start_in_notbetrieb_with_emergency_boost(env)

    _answer(env, bridge, "alt-1", curve=0.95, shift=23.0)

    assert (env.ha.states["number.curve_current"], env.ha.states["number.shift_current"]) == (0.95, 23.0)
    assert _backup(env)["emergency_boost_active"] is False


def test_notbetrieb_end_hands_device_to_running_comfort_boost(env):
    # Laeuft der Comfort-Boost noch, gehen am Notbetriebsende dessen Werte auf die Anlage;
    # an seinem eigenen Ende die zwischenzeitlich vom Server gelieferten.
    bridge = _start_in_notbetrieb_with_emergency_boost(env)
    _override_options(bridge, boost_curve_value=1.0, boost_shift_value=24.0)

    _set_room_target(env, bridge, 22.0)  # Comfort-Boost startet, Notfall-Boost haelt die Anlage

    assert _regulation_writes(env) == [("number.curve_current", 1.5), ("number.shift_current", 25.0)]

    _answer(env, bridge, _mqtt(env).snapshots[-1]["seq"], curve=0.95, shift=23.0)

    assert (env.ha.states["number.curve_current"], env.ha.states["number.shift_current"]) == (1.0, 24.0)
    assert _backup(env)["boost_active"] is True
    assert _backup(env)["curve_current"] == 0.95

    env.ha.states["sensor.room_actual"] = 21.6
    _trigger(env, bridge, "sensor.room_actual")

    assert (env.ha.states["number.curve_current"], env.ha.states["number.shift_current"]) == (0.95, 23.0)
    assert _backup(env)["boost_active"] is False


def test_failsafe_state_write_failure_does_not_stop_regulation(env, caplog):
    _quiet_backup(env)
    bridge = _start(env)
    env.paths["FAILSAFE_PATH"].unlink(missing_ok=True)
    env.paths["FAILSAFE_PATH"].mkdir()

    with caplog.at_level(logging.ERROR):
        _set_room_target(env, bridge, 20.5)

    assert len(_mqtt(env).snapshots) == 1
    assert "failsafe_state.json" in caplog.text


# --- Lokaler Check und Trigger ---

def test_on_connected_rereads_room_target_and_runs_local_check(env):
    # B7: Sollwertaenderung waehrend der WS-Trennung wird sofort verarbeitet.
    _quiet_backup(env)
    bridge = _start(env)
    env.ha.states["sensor.room_target"] = 22.0

    env.trigger_clients[-1].kwargs["on_connected"]()
    bridge.worker.run_pending()

    assert _stable_target(bridge) == 22.0
    assert _boost_active(bridge) is True
    assert _mqtt(env).snapshots[-1]["trigger"] == "target_change"


def test_watchdog_runs_local_check_only_while_ws_disconnected(env):
    _quiet_backup(env)
    bridge = _start(env)
    env.ha.states["sensor.room_target"] = 20.5

    _advance(env, bridge, 300)  # verbunden: kein Fallback
    assert _stable_target(bridge) == 21.0

    env.trigger_clients[-1].connected = False
    _advance(env, bridge, 300)

    assert _stable_target(bridge) == 20.5
    assert len(_mqtt(env).snapshots) == 1


def test_local_check_keeps_cached_target_when_room_target_unreadable(env, caplog):
    _quiet_backup(env)
    bridge = _start(env)
    env.ha.states["sensor.room_target"] = ValueError("unavailable")

    with caplog.at_level(logging.WARNING):
        _trigger(env, bridge, "sensor.room_target")

    assert _stable_target(bridge) == 21.0
    assert "room_target nicht lesbar" in caplog.text


def test_local_check_after_abo_finished_does_nothing(env):
    _quiet_backup(env)
    bridge = _start(env)
    _mark_abo_finished(bridge)

    _set_room_target(env, bridge, 22.0)

    assert env.ha.writes == []
    assert _mqtt(env).snapshots == []
    assert _stable_target(bridge) == 21.0


def test_health_check_runs_at_start_and_every_local_check_interval(env):
    _quiet_backup(env)
    env.ha.states["sensor.wz_battery"] = 15
    bridge = _start(env, battery_entities=["sensor.wz_battery"])

    assert any("Batterie von sensor.wz_battery" in text for text in env.ha.pushes)

    env.ha.states["sensor.wz_battery"] = 90
    _advance(env, bridge, 300)

    assert any("sensor.wz_battery wieder in Ordnung" in text for text in env.ha.pushes)


def test_telemetry_runs_on_its_own_schedule(env):
    _quiet_backup(env)
    bridge = _start(env)
    assert len(_mqtt(env).telemetry) == 1

    _advance(env, bridge, 299)
    assert len(_mqtt(env).telemetry) == 1

    _advance(env, bridge, 1)
    assert len(_mqtt(env).telemetry) == 2
    assert _mqtt(env).telemetry[-1]["failsafe_active"] is False
    assert "datenfehler" not in _mqtt(env).telemetry[-1]


def test_telemetry_reports_pending_data_fault(env):
    _quiet_backup(env)
    save_backup(env.paths["FAILSAFE_PATH"], {
        "failsafe_active": False,
        "datenfehler": {"source": "local", "detail": ["dat"]},
        "pending": {"seq": "alt-1", "trigger": "daily"},
    })

    _start(env)

    assert _mqtt(env).telemetry[0]["datenfehler"] == {"source": "local", "detail": ["dat"]}


# --- Abo-Pfade ---

def test_active_status_at_start_clears_stale_inactive_state(env):
    _quiet_backup(env)
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())

    _start(env)

    assert not env.paths["ENTITLEMENT_PATH"].exists()
    assert len(env.mqtt_clients) == 1


def test_abo_reactivation_at_start_notifies_once_and_is_silent_on_next_restart(env):
    # __main__._start_bridge, Abo-active-Zweig: eine zuvor persistierte "abo"-Meldung (inaktiv
    # oder beendet) muss beim Neustart mit wieder aktivem Abo genau einmal auf "ok" zurueckgehen
    # (Push + Dismiss der persistent_notification) und beim naechsten Neustart still bleiben.
    _quiet_backup(env, notify_states={"abo": "inaktiv"})

    _start(env)

    assert env.ha.pushes == [abo.ABO_ACTIVE_MESSAGE]
    assert env.ha.dismissed == ["smartheat_abo"]

    pushes_before, dismissed_before = list(env.ha.pushes), list(env.ha.dismissed)
    _start(env)

    assert env.ha.pushes == pushes_before
    assert env.ha.dismissed == dismissed_before


def test_unknown_status_at_start_keeps_state_and_starts_normally(env):
    _quiet_backup(env)
    env.abo["status"] = entitlement.UNKNOWN
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    before = env.paths["ENTITLEMENT_PATH"].read_text()

    _start(env)

    assert env.paths["ENTITLEMENT_PATH"].read_text() == before
    assert len(env.mqtt_clients) == 1


def test_inactive_status_at_start_runs_locally_without_mqtt(env):
    _quiet_backup(env)
    env.abo["status"] = entitlement.INACTIVE
    env.ha.states["sensor.room_actual"] = 19.5  # Notfall-Boost muss auch ohne MQTT greifen

    bridge = _start(env)

    assert env.mqtt_clients == []
    assert bridge.mqtt_client is None
    assert _failsafe_file(env)["failsafe_active"] is True
    assert (env.ha.states["number.curve_current"], env.ha.states["number.shift_current"]) == (1.5, 25.0)
    assert len(env.ha.persistent) == 1
    assert "Abo inaktiv" in env.ha.pushes[0]


def test_inactive_after_grace_idles_as_abo_beendet_without_writes(env, monkeypatch):
    _quiet_backup(env)
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    monkeypatch.setattr("heizungsbruecke.entitlement.grace_expired", lambda since, now: True)

    bridge = _start_bridge(env)

    assert isinstance(bridge, main_module.IdleBridge) and bridge.reason == "abo_beendet"
    assert env.mqtt_clients == [] and env.trigger_clients == []
    assert env.ha.writes == [] and env.ha.pushes == []
    assert _status_states(env) == ["startet", "abo_beendet"]


def test_closing_start_retries_a_failed_restore_and_reports_it_once(env, monkeypatch):
    # T2-7 im Startpfad; die Wiederholung laeuft im Worker, das Lebenszeichen weiter.
    _quiet_backup(env, emergency_boost_active=True)
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    monkeypatch.setattr("heizungsbruecke.entitlement.grace_expired", lambda since, now: True)
    failures = [RuntimeError("HA nicht erreichbar")]
    original_write = env.ha.set_number_value

    def _flaky_write(entity_id, value):
        if failures:
            raise failures.pop()
        original_write(entity_id, value)

    env.ha.set_number_value = _flaky_write

    bridge = _start_bridge(env)

    assert bridge.reason == "abo_beendet"
    assert env.trigger_clients == []
    assert _last_event(env)["grund"] == abo.RESTORE_FAILED_REASON
    assert env.ha.pushes == [abo.RESTORE_FAILED_MESSAGE]

    _advance(env, bridge, 300)

    assert ("number.shift_current", 22.0) in env.ha.writes
    assert _backup(env)["emergency_boost_active"] is False
    assert _last_event(env)["grund"] is None
    assert env.ha.pushes == [abo.RESTORE_FAILED_MESSAGE, abo.RESTORE_OK_MESSAGE]


def test_auth_rejected_with_inactive_abo_enters_abo_mode(env):
    _quiet_backup(env)
    bridge = _start(env)
    env.abo["status"] = entitlement.INACTIVE

    _mqtt(env).kwargs["on_auth_rejected"](_mqtt(env))  # aus dem paho-Thread
    bridge.worker.run_pending()

    assert _mqtt(env).stopped is True
    assert _delivery(bridge).notbetrieb is True
    assert _failsafe_file(env)["failsafe_active"] is True
    assert len(env.ha.persistent) == 1
    assert _status_states(env)[-1] == "abo_inaktiv"


def test_inactive_rejection_after_unknown_rejection_clears_zugang_abgelehnt_silently(env, monkeypatch):
    """Fix Review Focus 1, Runde 1: eine erste Ablehnung bei noch unklarem Abo setzt
    zugang_abgelehnt; eine spaetere Ablehnung mit eindeutig inaktivem Abo wechselt in den
    Abo-inaktiv-Modus (MQTT ist danach beendet, kein Connect kann die Flagge mehr loeschen) und
    muss Flagge, Grund und Meldung selbst aufraeumen -- sonst haengt der falsche Rat ("neu
    anmelden") bis zum Fristende, und der Grund haengt sogar noch im abo_beendet-Event."""
    _quiet_backup(env)
    bridge = _start(env)
    env.abo["status"] = entitlement.UNKNOWN

    _mqtt(env).kwargs["on_auth_rejected"](_mqtt(env))
    bridge.worker.run_pending()

    assert _status_states(env)[-1] == "zugang_abgelehnt"
    assert ("smartheat_zugang", abo.ACCESS_DENIED_MESSAGE) in env.ha.persistent
    pushes_before = list(env.ha.pushes)

    env.abo["status"] = entitlement.INACTIVE
    # T2-12-Drosselung: die erste Ablehnung hat schon abgefragt und zugang_abgelehnt gesetzt,
    # daher braucht diese zweite Ablehnung 600 s Abstand, um ueberhaupt erneut abzufragen.
    env.clock.advance(600)
    _mqtt(env).kwargs["on_auth_rejected"](_mqtt(env))
    bridge.worker.run_pending()

    assert _status_states(env)[-1] == "abo_inaktiv"
    assert _last_event(env)["grund"] is None
    assert "smartheat_zugang" in env.ha.dismissed
    new_pushes = env.ha.pushes[len(pushes_before):]
    assert new_pushes == [abo.inactive_message(entitlement.load_inactive_since(env.paths["ENTITLEMENT_PATH"]))]
    assert abo.ACCESS_DENIED_MESSAGE not in new_pushes and abo.ACCESS_OK_MESSAGE not in new_pushes

    monkeypatch.setattr("heizungsbruecke.entitlement.grace_expired", lambda since, now: True)
    _advance(env, bridge, 300)

    assert _status_states(env)[-1] == "abo_beendet"
    assert _last_event(env)["grund"] is None  # nicht mehr der stehengebliebene Zugangsgrund


@pytest.mark.parametrize("status", [entitlement.ACTIVE, entitlement.UNKNOWN, entitlement.REJECTED])
def test_auth_rejected_with_active_or_unknown_abo_reports_zugang_abgelehnt(env, caplog, status):
    _quiet_backup(env)
    bridge = _start(env)
    env.abo["status"] = status

    with caplog.at_level(logging.ERROR):
        _mqtt(env).kwargs["on_auth_rejected"](_mqtt(env))
        bridge.worker.run_pending()

    assert _mqtt(env).stopped is False
    assert "abgelehnt" in caplog.text
    assert _status_states(env)[-1] == "zugang_abgelehnt"
    assert _last_event(env)["grund"] == abo.ACCESS_DENIED_REASON
    assert ("smartheat_zugang", abo.ACCESS_DENIED_MESSAGE) in env.ha.persistent


def test_rejected_status_at_start_starts_normally(env):
    # Spec TP8 3.2: REJECTED beim Start wie UNKNOWN -- die MQTT-Anmeldung klaert den Rest.
    _quiet_backup(env)
    env.abo["status"] = entitlement.REJECTED

    bridge = _start(env)

    assert _is_running(bridge)
    assert _abo_inactive_since(bridge) is None
    assert _mqtt(env).stopped is False


def test_rejected_status_when_the_server_is_silent_is_no_abo_mode(env):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    env.abo["status"] = entitlement.REJECTED

    _advance(env, bridge, 30)
    _advance(env, bridge, 30)

    assert _delivery(bridge).notbetrieb is True
    assert _abo_inactive_since(bridge) is None


def test_successful_connect_clears_zugang_abgelehnt(env):
    _quiet_backup(env)
    bridge = _start(env)
    _mqtt(env).kwargs["on_auth_rejected"](_mqtt(env))
    bridge.worker.run_pending()

    _connect(env, bridge)

    assert _status_states(env)[-2:] == ["zugang_abgelehnt", "regelt"]
    assert "smartheat_zugang" in env.ha.dismissed
    assert env.ha.pushes[-1] == abo.ACCESS_OK_MESSAGE


def test_repeated_auth_rejections_are_coalesced_into_one_entitlement_query(env):
    # T10c: paho meldet waehrend seines Backoffs mehrfach; jede Abfrage blockiert den
    # Worker bis zu 10 s -- noch nicht verarbeitete Meldungen ergeben nur eine Abfrage.
    _quiet_backup(env)
    bridge = _start(env)
    queries_before = env.abo["queries"]

    for _ in range(3):
        _mqtt(env).kwargs["on_auth_rejected"](_mqtt(env))
    bridge.worker.run_pending()

    assert env.abo["queries"] == queries_before + 1


def test_auth_rejected_in_abo_inactive_mode_does_not_query_again(env):
    # T10c: nach dem Wechsel in den Abo-inaktiv-Modus ist jede weitere Abfrage sinnlos.
    _quiet_backup(env)
    bridge = _start(env)
    env.abo["status"] = entitlement.INACTIVE
    _mqtt(env).kwargs["on_auth_rejected"](_mqtt(env))
    bridge.worker.run_pending()
    queries_before = env.abo["queries"]

    _mqtt(env).kwargs["on_auth_rejected"](_mqtt(env))
    bridge.worker.run_pending()

    assert env.abo["queries"] == queries_before


def _reject(env, bridge):
    _mqtt(env).kwargs["on_auth_rejected"](_mqtt(env))
    bridge.worker.run_pending()


def _access_errors(caplog):
    # nur die Zeilen aus handle_auth_rejected, nicht andere ERRORs aus Takten, die beim Vorstellen der Uhr laufen
    return [r for r in caplog.records if r.levelno == logging.ERROR and "MQTT-Anmeldung" in r.getMessage()]


def test_auth_rejections_query_at_most_every_ten_minutes(env, caplog):
    _quiet_backup(env)
    bridge = _start(env)
    env.abo["status"] = entitlement.REJECTED
    queries_before = env.abo["queries"]

    with caplog.at_level(logging.ERROR):
        _reject(env, bridge)
        env.clock.advance(300)
        _reject(env, bridge)
        env.clock.advance(299)
        _reject(env, bridge)

    assert env.abo["queries"] == queries_before + 1
    assert len(_access_errors(caplog)) == 1

    env.clock.advance(1)
    _reject(env, bridge)
    assert env.abo["queries"] == queries_before + 2


def test_error_line_repeats_only_when_the_result_changes(env, caplog):
    _quiet_backup(env)
    bridge = _start(env)
    env.abo["status"] = entitlement.REJECTED

    with caplog.at_level(logging.ERROR):
        _reject(env, bridge)
        env.clock.advance(600)
        _reject(env, bridge)
        env.abo["status"] = entitlement.UNKNOWN
        env.clock.advance(600)
        _reject(env, bridge)

    assert len(_access_errors(caplog)) == 2


def test_suspension_during_zugang_abgelehnt_is_seen_after_the_interval(env):
    _quiet_backup(env)
    bridge = _start(env)
    env.abo["status"] = entitlement.REJECTED
    _reject(env, bridge)

    env.abo["status"] = entitlement.INACTIVE
    env.clock.advance(600)
    _reject(env, bridge)

    assert _status_states(env)[-1] == "abo_inaktiv"


def test_successful_connect_resets_the_throttle(env, caplog):
    # Nach einem erfolgreichen Connect ist eine neue Ablehnung wieder "die erste": sofortige
    # Abfrage und wieder eine ERROR-Zeile, auch bei unveraendertem Ergebnis.
    _quiet_backup(env)
    bridge = _start(env)
    env.abo["status"] = entitlement.REJECTED
    with caplog.at_level(logging.ERROR):
        _reject(env, bridge)
        _connect(env, bridge)
        queries_before = env.abo["queries"]

        _reject(env, bridge)

    assert env.abo["queries"] == queries_before + 1
    assert len(_access_errors(caplog)) == 2


def test_unexpected_entitlement_query_error_counts_as_unknown(env, monkeypatch):
    # T10b: eine unerwartet werfende Abo-Abfrage nach dem zweiten Timeout laeuft ueber
    # _fallback_follow_up als "unbekannt" -- Notbetrieb mit Retry, kein Abo-inaktiv-Modus.
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]

    def _broken_query(tenant_id, base_url, username, password):
        raise RuntimeError("unerwartet")

    monkeypatch.setattr("heizungsbruecke.entitlement.query_status", _broken_query)
    _advance(env, bridge, 30)
    _advance(env, bridge, 30)

    assert _delivery(bridge).notbetrieb is True
    assert _abo_inactive_since(bridge) is None
    assert _mqtt(env).stopped is False
    assert env.ha.pushes == [NOTBETRIEB_ON]

    _advance(env, bridge, 300)

    assert [s["seq"] for s in _mqtt(env).snapshots] == [seq, seq, seq]


def test_second_timeout_with_inactive_abo_enters_abo_mode_instead_of_alarm(env):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    env.abo["status"] = entitlement.INACTIVE

    _advance(env, bridge, 30)
    _advance(env, bridge, 30)

    assert _abo_inactive_since(bridge) is not None
    assert _mqtt(env).stopped is True
    assert NOTBETRIEB_ON not in env.ha.pushes
    assert any("Abo inaktiv" in text for text in env.ha.pushes)
    assert _delivery(bridge).pending is None


def test_grace_end_during_runtime_restores_notifies_and_idles(env, monkeypatch):
    _quiet_backup(env, emergency_boost_active=True)
    env.ha.states.update({"number.curve_current": 1.5, "number.shift_current": 25.0})
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    answers = iter([False, True])  # Startpruefung, dann grace_check
    monkeypatch.setattr("heizungsbruecke.entitlement.grace_expired", lambda since, now: next(answers, True))

    bridge = _start_bridge(env)
    bridge.worker.run_pending()

    assert bridge.idle is True
    assert env.trigger_clients[-1].stopped is True
    assert (env.ha.states["number.curve_current"], env.ha.states["number.shift_current"]) == (0.9, 22.0)
    assert env.ha.persistent[-1] == ("smartheat_abo", ABO_ENDED)
    assert _backup(env)["emergency_boost_active"] is False
    assert _status_states(env)[-1] == "abo_beendet"

    queries, events = env.abo["queries"], len(env.ha.events)
    _advance(env, bridge, 3600)

    assert env.abo["queries"] == queries  # keine Handler mehr ausser dem Lebenszeichen
    assert len(env.ha.events) > events


def test_grace_end_with_failing_restore_keeps_running_and_retries(env, monkeypatch):
    _quiet_backup(env, emergency_boost_active=True)
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    answers = iter([False])  # Startpruefung, danach ist die Frist abgelaufen
    monkeypatch.setattr("heizungsbruecke.entitlement.grace_expired", lambda since, now: next(answers, True))
    bridge = _start_bridge(env)
    env.ha.write_error = RuntimeError("HA nicht erreichbar")

    assert bridge.worker.run_pending() is None
    assert env.trigger_clients[-1].stopped is False

    env.ha.write_error = None
    env.clock.advance(300)

    bridge.worker.run_pending()
    assert bridge.idle is True
    assert env.ha.pushes.count(abo.RESTORE_FAILED_MESSAGE) == 1
    assert env.ha.pushes[-2:] == [ABO_ENDED, abo.RESTORE_OK_MESSAGE]
    ended = [entry for entry in env.ha.persistent if entry[1] == ABO_ENDED]
    assert len(ended) == 1


def test_inactive_restart_within_grace_does_not_notify_again(env):
    _quiet_backup(env)
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())

    _start(env)

    assert env.mqtt_clients == []
    assert env.ha.pushes == []
    assert env.ha.persistent == []


def test_inactive_after_grace_restores_leftover_boost_once(env, monkeypatch):
    _quiet_backup(env, emergency_boost_active=True)
    env.ha.states.update({"number.curve_current": 1.5, "number.shift_current": 25.0})
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    monkeypatch.setattr("heizungsbruecke.entitlement.grace_expired", lambda since, now: True)

    assert _start_bridge(env).reason == "abo_beendet"

    assert (env.ha.states["number.curve_current"], env.ha.states["number.shift_current"]) == (0.9, 22.0)
    assert _backup(env)["emergency_boost_active"] is False


def _start_just_before_grace_end(env, monkeypatch):
    _quiet_backup(env, emergency_boost_active=True)
    env.ha.states.update({"number.curve_current": 1.5, "number.shift_current": 25.0})
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    answers = iter([False])  # Startpruefung, danach ist die Frist abgelaufen
    monkeypatch.setattr("heizungsbruecke.entitlement.grace_expired", lambda since, now: next(answers, True))
    exec_calls = []
    monkeypatch.setattr("os.execv", lambda path, args: exec_calls.append((path, args)))
    bridge = _start_bridge(env)
    return bridge, exec_calls


def test_grace_end_with_reactivated_abo_restarts_instead_of_restoring(env, monkeypatch):
    # T2-5: kein faelschliches Zuruecksetzen am Fristende, wenn das Abo wieder aktiv ist.
    bridge, exec_calls = _start_just_before_grace_end(env, monkeypatch)
    env.abo["status"] = entitlement.ACTIVE

    bridge.worker.run_pending()

    assert exec_calls == [(sys.executable, [sys.executable, "-m", "heizungsbruecke"])]
    assert not env.paths["ENTITLEMENT_PATH"].exists()
    assert (env.ha.states["number.curve_current"], env.ha.states["number.shift_current"]) == (1.5, 25.0)
    assert all(message != ABO_ENDED for _, message in env.ha.persistent)


@pytest.mark.parametrize("status", [entitlement.INACTIVE, entitlement.UNKNOWN])
def test_grace_end_finishes_when_abo_not_active(env, monkeypatch, status):
    bridge, exec_calls = _start_just_before_grace_end(env, monkeypatch)
    env.abo["status"] = status

    bridge.worker.run_pending()
    assert bridge.idle is True
    assert exec_calls == []
    assert (env.ha.states["number.curve_current"], env.ha.states["number.shift_current"]) == (0.9, 22.0)


def test_grace_end_idles_even_if_saving_flags_fails(env, monkeypatch):
    # T2-8: Werte stehen schon auf dem Geraet -> Wiederherstellung gilt als erfolgt, Ruhezustand.
    bridge, _ = _start_just_before_grace_end(env, monkeypatch)

    _break_backup_writes(monkeypatch)

    bridge.worker.run_pending()
    assert bridge.idle is True
    assert (env.ha.states["number.curve_current"], env.ha.states["number.shift_current"]) == (0.9, 22.0)


# --- Sicherheitsnetz TP5: Boost-Vorrang, Serverwerte waehrend Boosts, Abo-Fristende, Neustart ---

def test_emergency_start_during_comfort_boost_writes_max_values_and_both_end_together(env):
    _quiet_backup(env)
    bridge = _start(env)
    _override_options(bridge, boost_curve_value=1.0, boost_shift_value=24.0)

    _set_room_target(env, bridge, 22.0)  # Comfort-Boost + Tick
    _advance(env, bridge, 30)
    _advance(env, bridge, 30)  # zweiter Ack-Timeout -> Notbetrieb

    assert _delivery(bridge).notbetrieb is True

    _trigger(env, bridge, "sensor.room_actual")  # 20.0 bei Soll 22.0: > 1 K darunter

    assert _regulation_writes(env) == [
        ("number.curve_current", 1.0), ("number.shift_current", 24.0),
        ("number.curve_current", 1.5), ("number.shift_current", 25.0),
    ]

    env.ha.states["sensor.room_actual"] = 21.6  # beide Schwellen erreicht
    _trigger(env, bridge, "sensor.room_actual")

    assert _regulation_writes(env)[-2:] == [("number.curve_current", 0.9), ("number.shift_current", 22.0)]
    assert (_backup(env)["boost_active"], _backup(env)["emergency_boost_active"]) == (False, False)


def test_answer_during_emergency_boost_is_written_once_when_notbetrieb_ends(env):
    bridge = _start_in_notbetrieb_with_emergency_boost(env)

    _answer(env, bridge, "alt-1", curve=0.95, shift=23.0)

    assert env.ha.writes == [
        ("number.curve_current", 1.5), ("number.shift_current", 25.0),
        ("number.curve_current", 0.95), ("number.shift_current", 23.0),
    ]


def test_comfort_boost_end_restores_values_answered_during_the_boost(env):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 22.0)
    _answer(env, bridge, _mqtt(env).snapshots[0]["seq"], curve=0.95, shift=23.0)

    env.ha.states["sensor.room_actual"] = 21.6
    _trigger(env, bridge, "sensor.room_actual")

    assert _regulation_writes(env) == [
        ("number.curve_current", 1.5), ("number.shift_current", 25.0),
        ("number.curve_current", 0.95), ("number.shift_current", 23.0),
    ]
    assert _backup(env)["boost_active"] is False


def test_grace_end_without_boost_restores_learned_values(env, monkeypatch):
    _quiet_backup(env)
    env.ha.states.update({"number.curve_current": 1.2, "number.shift_current": 26.0})
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    answers = iter([False])  # Startpruefung, danach ist die Frist abgelaufen
    monkeypatch.setattr("heizungsbruecke.entitlement.grace_expired", lambda since, now: next(answers, True))

    bridge = _start_bridge(env)

    bridge.worker.run_pending()
    assert bridge.idle is True
    assert env.ha.writes == [("number.curve_current", 0.9), ("number.shift_current", 22.0)]
    assert env.ha.persistent[-1] == ("smartheat_abo", ABO_ENDED)


def test_grace_end_during_comfort_boost_restores_learned_values(env, monkeypatch):
    _quiet_backup(env)
    env.ha.states["sensor.room_actual"] = 21.2  # nah genug am Soll: kein Notfall-Boost
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    answers = iter([False, False])  # Startpruefung und erster grace_check
    monkeypatch.setattr("heizungsbruecke.entitlement.grace_expired", lambda since, now: next(answers, True))
    bridge = _start(env)

    _set_room_target(env, bridge, 22.0)

    assert _regulation_writes(env) == [("number.curve_current", 1.5), ("number.shift_current", 25.0)]

    env.clock.advance(300)

    bridge.worker.run_pending()
    assert bridge.idle is True
    assert _regulation_writes(env)[-2:] == [("number.curve_current", 0.9), ("number.shift_current", 22.0)]
    assert _backup(env)["boost_active"] is False


def test_restart_keeps_unknown_backup_keys_and_resumes_open_tick_with_fault(env):
    _quiet_backup(env, zukunft={"x": 1})
    save_backup(env.paths["FAILSAFE_PATH"], {
        "failsafe_active": False,
        "datenfehler": {"source": "local", "detail": ["dat"]},
        "pending": {"seq": "alt-1", "trigger": "daily"},
    })

    bridge = _start(env)

    assert [s["seq"] for s in _mqtt(env).snapshots] == ["alt-1"]

    _answer(env, bridge, "alt-1", curve=0.95, shift=23.0)

    assert env.ha.pushes == ["Heizungsbrücke: Messwerte wieder gültig, Heizkurve wird wieder angepasst."]
    assert _failsafe_file(env) == {"failsafe_active": False, "datenfehler": None, "pending": None}

    _set_room_target(env, bridge, 20.5)
    backup = _backup(env)

    assert backup["zukunft"] == {"x": 1}
    assert backup["last_published_target_rt"] == 20.5
    assert backup["curve_current"] == 0.95


def test_restart_during_comfort_boost_continues_and_ends_on_arrival(env):
    # N1: der persistierte Comfort-Boost laeuft weiter und endet ueber die Ankunftsschwelle.
    _quiet_backup(env, boost_active=True, last_room_target=22.0, last_published_target_rt=22.0)
    env.ha.states.update({"sensor.room_target": 22.0, "number.curve_current": 1.5, "number.shift_current": 25.0})

    bridge = _start(env)

    assert _regulation_writes(env) == []
    assert _backup(env)["boost_active"] is True

    env.ha.states["sensor.room_actual"] = 21.6
    _trigger(env, bridge, "sensor.room_actual")

    assert _regulation_writes(env) == [("number.curve_current", 0.9), ("number.shift_current", 22.0)]
    assert _backup(env)["boost_active"] is False


def test_priming_fills_the_target_cache_even_if_the_disk_is_read_only(env, monkeypatch):
    # N5: 0.17.0-0.19.0 speicherten boost_active=False vor dem Fuellen des Caches; ein nicht
    # beschreibbarer Datentraeger liess den Cache dann leer.
    _quiet_backup(env, boost_active=True, last_room_target=22.0, last_published_target_rt=22.0)
    env.ha.states["sensor.room_target"] = 22.0
    _break_backup_writes(monkeypatch)

    bridge = _start(env)

    assert _stable_target(bridge) == 22.0
    assert _boost_active(bridge) is True


def test_notbetrieb_end_with_unwritable_device_restores_on_next_check(env):
    # Review Focus 4.
    bridge = _start_in_notbetrieb_with_emergency_boost(env)
    env.ha.write_error = RuntimeError("myVAILLANT-Cloud nicht erreichbar")

    _answer(env, bridge, "alt-1", curve=0.95, shift=23.0)

    assert _delivery(bridge).notbetrieb is False
    assert _backup(env)["emergency_boost_active"] is True

    env.ha.write_error = None
    _trigger(env, bridge, "sensor.room_actual")

    assert (env.ha.states["number.curve_current"], env.ha.states["number.shift_current"]) == (0.95, 23.0)
    assert _backup(env)["emergency_boost_active"] is False


def test_answer_during_boost_with_unwritable_device_is_acked(env):
    # Review Focus 3: waehrend eines Boosts wird nichts geschrieben, also gibt es keinen Schreibfehler.
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 22.0)
    env.ha.write_error = RuntimeError("myVAILLANT-Cloud nicht erreichbar")

    _answer(env, bridge, _mqtt(env).snapshots[0]["seq"])

    assert _delivery(bridge).pending is None
    assert env.ha.pushes == []


# --- F2: doppelte oder verspaetete Antworten ---

def test_duplicate_rejected_answer_does_not_skip_a_retry_stage(env):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]

    _mqtt(env).answer(seq, status="rejected", curve=None, shift=None, reason="x")
    _mqtt(env).answer(seq, status="rejected", curve=None, shift=None, reason="x")  # QoS-1-Doppelzustellung
    bridge.worker.run_pending()
    _advance(env, bridge, 30)  # erste Datenfehler-Stufe

    assert [s["seq"] for s in _mqtt(env).snapshots] == [seq, seq]


# --- F1: Anlage nicht beschreibbar ---

WRITE_DETAIL = "curve_current (number.curve_current): myVAILLANT-Cloud nicht erreichbar"
WRITE_FAULT = (
    f"Heizungsbrücke: Neue Heizkurve konnte nicht an die Anlage übertragen werden ({WRITE_DETAIL}). "
    "Wird automatisch erneut versucht."
)


def test_unwritable_device_is_reported_once_and_retried_without_notbetrieb(env):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]
    env.ha.write_error = RuntimeError("myVAILLANT-Cloud nicht erreichbar")

    _answer(env, bridge, seq)
    _advance(env, bridge, 30)  # Retry nach 30 s, gleiche seq
    _answer(env, bridge, seq)  # Anlage weiter nicht beschreibbar
    _advance(env, bridge, 30)  # frueher: zweiter Ack-Timeout -> Notbetrieb

    assert _delivery(bridge).notbetrieb is False
    assert env.ha.pushes == [WRITE_FAULT]
    assert _failsafe_file(env)["datenfehler"] == {"source": "write", "detail": [WRITE_DETAIL]}

    env.ha.write_error = None
    _advance(env, bridge, 270)  # dritter Versuch 300 s nach dem zweiten
    _answer(env, bridge, seq)

    assert [s["seq"] for s in _mqtt(env).snapshots] == [seq, seq, seq]
    assert env.ha.writes[-2:] == [("number.curve_current", 0.95), ("number.shift_current", 23.0)]
    assert env.ha.pushes[-1] == "Heizungsbrücke: Anlage wieder erreichbar, Heizkurve übertragen."
    assert _failsafe_file(env)["datenfehler"] is None


# --- F3: Broker-Ausfall ---

def test_no_snapshot_is_queued_without_broker_and_connect_retries_at_once(env):
    _quiet_backup(env)
    bridge = _start(env)
    _mqtt(env).connected = False

    _set_room_target(env, bridge, 20.5)
    _advance(env, bridge, 30)
    _advance(env, bridge, 30)  # zwei unbeantwortete Versuche -> Notbetrieb, naechster Versuch in 5 min

    assert _mqtt(env).snapshots == []
    assert _delivery(bridge).notbetrieb is True

    _mqtt(env).connected = True
    _mqtt(env).kwargs["on_connected"](_mqtt(env))  # aus dem paho-Thread
    bridge.worker.run_pending()

    assert len(_mqtt(env).snapshots) == 1

    _answer(env, bridge, _mqtt(env).snapshots[0]["seq"])
    _advance(env, bridge, 300)  # der vorher geplante 5-min-Retry laeuft ins Leere

    assert _delivery(bridge).notbetrieb is False
    assert len(_mqtt(env).snapshots) == 1


def test_mqtt_connect_while_awaiting_answer_does_not_send_again(env):
    # Review Focus 5.
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)

    _mqtt(env).kwargs["on_connected"](_mqtt(env))
    bridge.worker.run_pending()

    assert len(_mqtt(env).snapshots) == 1


def test_open_tick_at_start_is_sent_on_connect_without_waiting_for_the_ack_timeout(env, monkeypatch):
    # N3: 0.17.0-0.19.0 warteten hier 30 s und zaehlten einen Serverausfall.
    _quiet_backup(env)
    save_backup(env.paths["FAILSAFE_PATH"], {"failsafe_active": False, "pending": {"seq": "alt-1", "trigger": "daily"}})
    link = {"up": False}
    monkeypatch.setattr(FakeMqtt, "is_connected", lambda self: link["up"])

    bridge = _start(env)
    assert _mqtt(env).snapshots == []

    link["up"] = True
    _mqtt(env).kwargs["on_connected"](_mqtt(env))
    bridge.worker.run_pending()

    assert [s["seq"] for s in _mqtt(env).snapshots] == ["alt-1"]
    assert _delivery(bridge).server_failures == 0


# --- Lebenszeichen und Abmelden (Spec TP7 1.1, 3.3) ---

def test_heartbeat_sends_the_full_status_every_300_s(env):
    _quiet_backup(env)
    bridge = _start(env)
    before = len(env.ha.events)

    _advance(env, bridge, 300)
    assert len(env.ha.events) == before + 1
    _advance(env, bridge, 300)
    assert len(env.ha.events) == before + 2


def test_sign_off_during_emergency_boost_restores_clears_and_idles(env):
    """Review Focus 4 (Add-on-Seite): Integration entfernt -> Anlage auf den
    Wiederherstellungspunkt, alle Meldungen weg, kein Push, keine Regelung mehr."""
    _quiet_backup(
        env, emergency_boost_active=True,
        notify_states={"notbetrieb": "aktiv", "batterie:sensor.x": "niedrig"},
        notify_messages={"notbetrieb": NOTBETRIEB_ON},
    )
    env.ha.states.update({"number.curve_current": 1.5, "number.shift_current": 25.0})

    bridge = _start_bridge(env, abgemeldet=True)

    assert isinstance(bridge, main_module.IdleBridge) and bridge.reason == "abgemeldet"
    assert env.ha.writes == [("number.curve_current", 0.9), ("number.shift_current", 22.0)]
    assert _backup(env)["emergency_boost_active"] is False
    assert {"smartheat_notbetrieb", "smartheat_batterie_sensor_x"} <= set(env.ha.dismissed)
    assert "notify_states" not in _backup(env)
    assert env.ha.pushes == []
    assert env.mqtt_clients == [] and env.trigger_clients == []
    assert _status_states(env) == ["startet", "abgemeldet"]
    assert _last_event(env)["grund"] is None
    assert env.abo["queries"] == 0


def test_sign_off_without_boost_writes_nothing(env):
    _quiet_backup(env)

    bridge = _start_bridge(env, abgemeldet=True)

    assert bridge.reason == "abgemeldet"
    assert env.ha.writes == []


def test_sign_off_retries_a_failed_restore_with_reason(env):
    _quiet_backup(env, boost_active=True)
    env.ha.write_error = RuntimeError("Cloud weg")

    bridge = _start_bridge(env, abgemeldet=True)

    assert _last_event(env)["status"] == "abgemeldet"
    assert _last_event(env)["grund"] == main_module.SIGN_OFF_RESTORE_FAILED.format(seconds=300)

    env.ha.write_error = None
    _advance(env, bridge, 300)

    assert env.ha.writes == [("number.curve_current", 0.9), ("number.shift_current", 22.0)]
    assert _last_event(env)["grund"] is None


def test_sign_off_with_invalid_configuration_does_not_write(env):
    _quiet_backup(env, boost_active=True)

    bridge = _start_bridge(env, abgemeldet=True, verteilsystem="Unbekannt")

    assert bridge.reason == "abgemeldet"
    assert env.ha.writes == []
    assert _last_event(env)["grund"] == main_module.SIGN_OFF_INVALID_CONFIG


def test_sign_off_without_mqtt_credentials_still_restores(env):
    """TP7-Gates: die Integration leert beim Entfernen die Zugangsdaten. Startet der Pi danach
    neu, waehrend das Zuruecksetzen noch scheitert, muss der Abmelde-Pfad trotzdem laufen."""
    _quiet_backup(env, boost_active=True)

    bridge = _start_bridge(env, abgemeldet=True, mqtt_username="", mqtt_password="")

    assert bridge.reason == "abgemeldet"
    assert env.ha.writes == [("number.curve_current", 0.9), ("number.shift_current", 22.0)]


def test_sign_off_failed_restore_notifies_with_the_restore_values(env):
    _quiet_backup(env, boost_active=True, notify_states={"notbetrieb": "aktiv"})
    env.ha.write_error = RuntimeError("Cloud weg")

    _start_bridge(env, abgemeldet=True)

    message = main_module.SIGN_OFF_RESTORE_FAILED_MESSAGE.format(werte="Kurve 0,9, Parallelverschiebung 22")
    assert env.ha.pushes == [message]
    assert ("smartheat_wiederherstellung", message) in env.ha.persistent
    assert "smartheat_notbetrieb" in env.ha.dismissed
    assert _backup(env)["notify_states"] == {"wiederherstellung": "fehlgeschlagen"}


def test_sign_off_failed_restore_after_a_restart_renews_the_notification_without_push(env):
    message = main_module.SIGN_OFF_RESTORE_FAILED_MESSAGE.format(werte="Kurve 0,9, Parallelverschiebung 22")
    _quiet_backup(
        env, boost_active=True,
        notify_states={"wiederherstellung": "fehlgeschlagen"}, notify_messages={"wiederherstellung": message},
    )
    env.ha.write_error = RuntimeError("Cloud weg")

    _start_bridge(env, abgemeldet=True)

    assert env.ha.pushes == []
    assert ("smartheat_wiederherstellung", message) in env.ha.persistent
    assert "smartheat_wiederherstellung" not in env.ha.dismissed


def test_sign_off_retry_success_reports_ok_and_dismisses_the_notification(env):
    _quiet_backup(env, boost_active=True)
    env.ha.write_error = RuntimeError("Cloud weg")
    bridge = _start_bridge(env, abgemeldet=True)

    env.ha.write_error = None
    _advance(env, bridge, 300)

    assert env.ha.pushes[-1] == abo.RESTORE_OK_MESSAGE
    assert env.ha.dismissed[-1] == "smartheat_wiederherstellung"
    assert "notify_states" not in _backup(env) or _backup(env)["notify_states"] == {}


def test_sign_off_with_invalid_configuration_during_a_boost_asks_for_manual_values(env):
    _quiet_backup(env, emergency_boost_active=True)

    _start_bridge(env, abgemeldet=True, verteilsystem="Unbekannt")

    message = main_module.SIGN_OFF_NOT_RESTORED_MESSAGE.format(werte="Kurve 0,9, Parallelverschiebung 22")
    assert env.ha.pushes == [message]
    assert ("smartheat_wiederherstellung", message) in env.ha.persistent


def test_sign_off_with_invalid_configuration_without_boost_stays_silent(env):
    _quiet_backup(env)

    _start_bridge(env, abgemeldet=True, verteilsystem="Unbekannt")

    assert env.ha.pushes == []


# --- Durchsetzung: zurueckgesetzter Eingriff reist als KPI mit (TP11, Spec 5.3) ---

OVERRIDE = {"curve": 1.3, "shift": 24.5, "erkannt": "2026-10-01T08:00:00+02:00"}


def _settle(env, bridge):
    """Die Durchsetzung wertet erst nach OWN_WRITE_SETTLE_SECONDS Laufzeit bzw. seit dem letzten
    eigenen Schreiben aus (Override.settled)."""
    _advance(env, bridge, OWN_WRITE_SETTLE_SECONDS + 1)


def test_manual_override_travels_with_the_next_snapshot_and_is_cleared_after_the_answer(env):
    _quiet_backup(env)
    bridge = _start(env)
    _settle(env, bridge)
    env.ha.states.update({"number.curve_current": 1.3, "number.shift_current": 24.5})
    _advance(env, bridge, 300)
    _advance(env, bridge, 300)  # zwei Runden mit Abweichung

    assert _backup(env)["manual_override_pending"]["curve"] == 1.3
    assert _last_event(env)["hinweise"]["manueller_eingriff"]["kurve"] == 1.3
    assert _regulation_writes(env) == [("number.curve_current", 0.9), ("number.shift_current", 22.0)]  # zurueckgesetzt

    _set_room_target(env, bridge, 20.5)
    snapshot = _mqtt(env).snapshots[-1]
    assert set(snapshot["manual_override"]) == {"curve", "shift", "erkannt"}

    _answer(env, bridge, snapshot["seq"], curve=0.95, shift=23.0)
    assert "manual_override_pending" not in _backup(env)

    _settle(env, bridge)  # Anlage steht wieder auf den gelernten Werten, eigenes Schreiben eingeschwungen
    assert _last_event(env)["hinweise"]["manueller_eingriff"] is None


def test_pending_manual_override_survives_a_restart(env):
    _quiet_backup(env, manual_override_pending=OVERRIDE)
    bridge = _start(env)

    _set_room_target(env, bridge, 20.5)

    assert _mqtt(env).snapshots[-1]["manual_override"] == OVERRIDE


def test_rejected_answer_keeps_the_pending_manual_override(env):
    _quiet_backup(env, manual_override_pending=OVERRIDE)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)

    _answer(env, bridge, _mqtt(env).snapshots[-1]["seq"], status="rejected", reason="x")

    assert _backup(env)["manual_override_pending"] == OVERRIDE


def test_manual_override_detected_during_an_open_tick_survives_a_same_seq_retry(env):
    """Fix Runde 1, Befund 2: der Server verarbeitet eine bereits gesehene seq idempotent aus
    dem Cache (generic/tick.py). Ein zwischen dem ersten Publish und einem Retry derselben seq
    neu erkannter Eingriff darf nicht mit dem Retry reisen -- der Server saehe ihn dann nie,
    waehrend die (verspaetete) Antwort ihn beim Client trotzdem als erledigt loeschen wuerde
    (KPI-Verlust). Er muss bis zur naechsten seq warten."""
    _quiet_backup(env)
    bridge = _start(env, local_check_interval_seconds=5)
    _settle(env, bridge)
    _set_room_target(env, bridge, 20.5)  # erster Tick, seq X, noch ohne Eingriff
    seq = _mqtt(env).snapshots[0]["seq"]
    assert "manual_override" not in _mqtt(env).snapshots[0]

    env.ha.states.update({"number.curve_current": 1.3, "number.shift_current": 24.5})
    _advance(env, bridge, 5)
    _advance(env, bridge, 5)  # zwei Runden mit Abweichung, seq X wartet noch auf Antwort
    assert _backup(env)["manual_override_pending"]["curve"] == 1.3

    _advance(env, bridge, 20)  # Ack-Timeout (30 s seit dem Publish) loest den Retry derselben seq aus
    assert [s["seq"] for s in _mqtt(env).snapshots] == [seq, seq]
    assert "manual_override" not in _mqtt(env).snapshots[-1]  # gepinnt: wie beim ersten Publish

    _answer(env, bridge, seq, curve=0.95, shift=23.0)  # verspaetete Antwort auf den Retry trifft ein

    assert _backup(env)["manual_override_pending"]["curve"] == 1.3  # ueberlebt, der Server hat ihn nie gesehen


def test_manual_override_detected_while_a_tick_is_open_survives_its_answer(env):
    """Fix Runde 1, Befund 3c: ein zwischen Publish und Antwort neu erkannter Eingriff (noch ohne
    Retry) darf die Antwort auf den urspruenglichen, eingriffslosen Versuch nicht loeschen
    (sent=None schuetzt ihn ueber die Praezisierung-4-Pruefung in _clear_sent_manual_override)."""
    _quiet_backup(env)
    bridge = _start(env, local_check_interval_seconds=5)
    _settle(env, bridge)
    _set_room_target(env, bridge, 20.5)  # seq X, noch ohne Eingriff
    seq = _mqtt(env).snapshots[-1]["seq"]

    env.ha.states.update({"number.curve_current": 1.3, "number.shift_current": 24.5})
    _advance(env, bridge, 5)
    _advance(env, bridge, 5)  # zwei Runden: Eingriff erkannt, seq X ist noch offen

    _answer(env, bridge, seq, curve=0.95, shift=23.0)  # Antwort auf den urspruenglichen Versuch

    assert _backup(env)["manual_override_pending"]["curve"] == 1.3


def test_ok_answer_clears_the_pending_manual_override_even_if_the_write_then_fails(env):
    """Fix Runde 1, Befund 3a: Praezisierung 4 gilt auch dann, wenn das anschliessende Schreiben
    auf die Anlage scheitert -- die vorhandenen DeviceWriteError-Tests setzten bisher nie
    manual_override_pending, die Abdeckung fehlte."""
    _quiet_backup(env, manual_override_pending=OVERRIDE)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[-1]["seq"]
    env.ha.write_error = RuntimeError("HA nicht erreichbar")

    _answer(env, bridge, seq, curve=0.95, shift=23.0)

    assert "manual_override_pending" not in _backup(env)
    assert _delivery(bridge).pending.seq == seq  # Retry derselben seq wegen Schreibfehler laeuft weiter


def test_skipped_answer_clears_the_pending_manual_override(env):
    """Fix Runde 1, Befund 3b: skipped (Schema 3, nicht gelernt) zaehlt wie ok als Antwort mit Werten
    (Praezisierung 4)."""
    _quiet_backup(env, manual_override_pending=OVERRIDE)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[-1]["seq"]

    _answer(env, bridge, seq, status="skipped", curve=0.95, shift=23.0)

    assert "manual_override_pending" not in _backup(env)


def test_a_failed_publish_does_not_pin_a_manual_override(env):
    """Fix Runde 1, Befund 3d: ein Publish-Fehler pinnt nichts -- der naechste erfolgreiche
    Versuch derselben seq liest den aktuellen Stand frisch."""
    _quiet_backup(env, manual_override_pending=OVERRIDE)
    bridge = _start(env)
    _mqtt(env).publish_error = RuntimeError("paho kaputt")

    _set_room_target(env, bridge, 20.5)

    assert _mqtt(env).snapshots == []
    assert bridge.manual_override_sent is None

    _mqtt(env).publish_error = None
    _advance(env, bridge, 30)

    assert _mqtt(env).snapshots[-1]["manual_override"] == OVERRIDE
    assert bridge.manual_override_sent == OVERRIDE

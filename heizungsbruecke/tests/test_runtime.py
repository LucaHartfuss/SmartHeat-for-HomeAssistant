"""Betrieb von __main__ ueber den Regel-Worker (Design-Spec 2026-09-26): Boot,
Tick-Zustellung, Datenfehler, Notbetrieb, lokale Checks und Abo-Pfade. Getrieben ueber
_start_bridge mit Fake-Uhr (tests/conftest.py), Fake-HA, Fake-MQTT und Fake-Trigger-Client."""
import json
import logging
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import heizungsbruecke.__main__ as main_module
from heizungsbruecke import entitlement
from heizungsbruecke.backup_store import load_backup, save_backup
from heizungsbruecke.delivery import DeliveryState
from heizungsbruecke.manifest import ChannelManifest
from heizungsbruecke.worker import Event, RegulationWorker

OPTIONS = {
    "tenant_id": "test_tenant",
    "profile": "vaillant_gastherme_heizkoerper",  # Clamps 0.4-1.5 / 20-30, Boost 1.5/30, Tagestick 12:00
    "mqtt_username": "u",
    "mqtt_password": "p",
    "entity_room_actual": "sensor.room_actual",
    "entity_room_target": "sensor.room_target",
    "entity_curve_current": "number.curve_current",
    "entity_offset_current": "number.offset_current",
    "entity_outdoor_temp": "sensor.outdoor_temp",
    "entity_heat_limit": "number.heat_limit",
    "entity_dat": "sensor.dat",
    "entity_dart": "sensor.dart",
    "notify_service": "notify.handy",
}

DERIVED = {
    "_room_12h_avg": "sensor.room_12h_avg",
    "room_day_avg": "sensor.room_day_avg",
    "room_night_avg": "sensor.room_night_avg",
}

NOTBETRIEB_ON = "Heizungsbrücke: Server antwortet nicht, Notbetrieb aktiv. Die Heizung wird bei Bedarf lokal abgesichert."


class FakeHa:
    token = "tok"

    def __init__(self):
        self.states = {
            "sensor.room_actual": 20.0, "sensor.room_target": 21.0,
            "number.curve_current": 0.9, "number.offset_current": 22.0,
            "sensor.outdoor_temp": 5.0, "number.heat_limit": 16.0,
            "sensor.room_day_avg": 20.5, "sensor.room_night_avg": 19.5,
            "sensor.dat": 4.0, "sensor.dart": 20.2, "sensor.room_12h_avg": 20.1,
        }
        self.writes = []
        self.pushes = []
        self.persistent = []
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

    def set_input_number_value(self, entity_id, value):
        self.states[entity_id] = value

    def send_notification(self, service, message):
        self.pushes.append(message)

    def create_persistent_notification(self, title, message, notification_id):
        self.persistent.append((notification_id, message))

    def get_config(self):
        return {"time_zone": "Europe/Berlin"}

    def websocket_url(self):
        return "ws://x/api/websocket"


class FakeMqtt:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.snapshots = []
        self.telemetry = []
        self.status = {}
        self.setpoints_callback = None
        self.publish_error = None
        self.loop_started = False
        self.stopped = False

    def publish_discovery(self, component, object_id, config):
        pass

    def publish_status(self, object_id, payload):
        self.status[object_id] = payload

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

    def answer(self, seq, status="ok", curve=0.95, offset=23.0, reason=None):
        """Server-Antwort ueber den echten paho-Callback einspeisen."""
        message = MagicMock()
        message.retain = False
        message.payload = json.dumps({
            "schema": 2, "seq": seq, "ts": "x", "status": status, "curve": curve, "offset": offset, "reason": reason,
        })
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
    for name in ("BACKUP_PATH", "FAILSAFE_PATH", "ENTITLEMENT_PATH", "DERIVED_SENSORS_PATH", "DAYNIGHT_SNAPSHOT_PATH"):
        paths[name] = tmp_path / f"{name.lower()}.json"
        monkeypatch.setattr(f"heizungsbruecke.__main__.{name}", paths[name])
    monkeypatch.setattr("heizungsbruecke.__main__.derived_sensors.ensure_all", lambda **kwargs: dict(DERIVED))
    monkeypatch.setattr("heizungsbruecke.__main__.daynight_snapshot.maybe_snapshot", lambda **kwargs: None)
    abo = {"status": entitlement.ACTIVE, "queries": 0}

    def _query_status(tenant_id, base_url):
        abo["queries"] += 1
        return abo["status"]

    monkeypatch.setattr("heizungsbruecke.__main__.entitlement.query_status", _query_status)
    mqtt_clients, trigger_clients = [], []

    def _mqtt_factory(**kwargs):
        mqtt_clients.append(FakeMqtt(**kwargs))
        return mqtt_clients[-1]

    def _trigger_factory(**kwargs):
        trigger_clients.append(FakeTriggerClient(**kwargs))
        return trigger_clients[-1]

    monkeypatch.setattr("heizungsbruecke.__main__.BridgeMqttClient", _mqtt_factory)
    monkeypatch.setattr("heizungsbruecke.__main__.HaTriggerClient", _trigger_factory)
    return SimpleNamespace(
        clock=clock, paths=paths, abo=abo, ha=FakeHa(), mqtt_clients=mqtt_clients, trigger_clients=trigger_clients,
    )


def _quiet_backup(env, **extra):
    """Backup, bei dem beim Start weder ein Tick faellig ist noch ein Boost startet."""
    save_backup(env.paths["BACKUP_PATH"], {
        "last_room_target": 21.0, "last_published_target_rt": 21.0,
        "last_daily_trigger_date": datetime.now().date().isoformat(),
        "curve_current": 0.9, "offset_current": 22.0, **extra,
    })


def _start(env, **option_overrides):
    bridge = main_module._start_bridge({**OPTIONS, **option_overrides}, env.ha, clock=env.clock)
    assert isinstance(bridge, main_module._Bridge)
    bridge.worker.run_pending()
    return bridge


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
    original = main_module._run_local_check

    def _spy(*args, **kwargs):
        order.append("check")
        return original(*args, **kwargs)

    monkeypatch.setattr(main_module, "_run_local_check", _spy)
    monkeypatch.setattr(FakeMqtt, "loop_start", lambda self: order.append("loop_start"))

    _start(env)

    assert order[:2] == ["check", "loop_start"]


def test_priming_failure_resets_stale_boost_flags(env):
    # Fail-open (whole-branch review finding): veraltete Boost-Flags nach Neustart.
    _quiet_backup(env, boost_active=True, emergency_boost_active=True)
    env.ha.states["sensor.room_actual"] = RuntimeError("HA-API-Hickup beim Booten")

    bridge = _start(env)

    assert _backup(env)["boost_active"] is False
    assert _backup(env)["emergency_boost_active"] is False
    assert bridge.boost_active is False


def test_restart_after_notbetrieb_ended_restores_device_during_priming(env):
    # Final-Review C1, Szenario A.
    _quiet_backup(env, emergency_boost_active=True, boost_active=False)
    save_backup(env.paths["FAILSAFE_PATH"], {"failsafe_active": False})
    env.ha.states.update({"number.curve_current": 1.5, "number.offset_current": 30.0, "sensor.room_actual": 19.8})

    _start(env)

    assert (env.ha.states["number.curve_current"], env.ha.states["number.offset_current"]) == (0.9, 22.0)
    assert _backup(env)["emergency_boost_active"] is False


def test_restart_during_notbetrieb_continues_emergency_hysteresis(env):
    # Final-Review C1, Szenario B/C: 0.8 K unter Soll liegt zwischen Exit- (0.5) und Einstiegsschwelle (1.0).
    _quiet_backup(env, emergency_boost_active=True)
    save_backup(env.paths["FAILSAFE_PATH"], {"failsafe_active": True})
    env.ha.states.update({"number.curve_current": 1.5, "number.offset_current": 30.0, "sensor.room_actual": 20.2})

    bridge = _start(env)

    assert env.ha.writes == []
    assert _backup(env)["emergency_boost_active"] is True

    env.ha.states["sensor.room_actual"] = 20.6
    _trigger(env, bridge, "sensor.room_actual")

    assert (env.ha.states["number.curve_current"], env.ha.states["number.offset_current"]) == (0.9, 22.0)
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

    assert bridge.failsafe_ctx["delivery"] == DeliveryState(notbetrieb=notbetrieb)
    assert _mqtt(env).snapshots == []


def test_persisted_pending_tick_is_resumed_with_same_seq_and_ends_notbetrieb(env):
    _quiet_backup(env)
    save_backup(env.paths["FAILSAFE_PATH"], {
        "failsafe_active": True, "datenfehler": None, "pending": {"seq": "alt-1", "trigger": "daily"},
    })

    bridge = _start(env)

    assert [(s["seq"], s["trigger"]) for s in _mqtt(env).snapshots] == [("alt-1", "daily")]

    _answer(env, bridge, "alt-1", curve=0.95, offset=23.0)

    assert (env.ha.states["number.curve_current"], env.ha.states["number.offset_current"]) == (0.95, 23.0)
    assert _mqtt(env).status["failsafe"] == "OFF"
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
    assert bridge.failsafe_ctx["delivery"].notbetrieb is False

    _advance(env, bridge, 30)

    assert bridge.failsafe_ctx["delivery"].notbetrieb is True
    assert _mqtt(env).status["failsafe"] == "ON"
    assert _failsafe_file(env)["failsafe_active"] is True
    assert env.ha.pushes == [NOTBETRIEB_ON]
    assert env.abo["queries"] == 2  # Start + zweiter Timeout


# --- Zustellung und Antworten ---

def test_answer_writes_values_and_closes_tick(env):
    _quiet_backup(env)
    bridge = _start(env)

    _set_room_target(env, bridge, 20.5)
    snapshot = _mqtt(env).snapshots[0]
    _answer(env, bridge, snapshot["seq"], curve=0.95, offset=23.0)

    assert snapshot["trigger"] == "target_change"
    assert snapshot["roles"]["room_target"] == 20.5
    assert 20.5 <= snapshot["roles"]["room_target_avg_24h"] <= 21.0  # zeitgewichtet ueber 21.0 -> 20.5
    assert "room_actual" not in snapshot["roles"]
    assert env.ha.writes == [("number.curve_current", 0.95), ("number.offset_current", 23.0)]
    assert _backup(env)["curve_current"] == 0.95
    assert bridge.failsafe_ctx["delivery"].pending is None
    assert env.ha.pushes == []


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
    original = main_module.read_snapshot_roles
    failures = [RuntimeError("unerwartet")]

    def _flaky_read(*args, **kwargs):
        if failures:
            raise failures.pop()
        return original(*args, **kwargs)

    monkeypatch.setattr(main_module, "read_snapshot_roles", _flaky_read)

    _set_room_target(env, bridge, 20.5)
    assert _mqtt(env).snapshots == []
    assert bridge.failsafe_ctx["delivery"].pending.awaiting_ack is True

    _advance(env, bridge, 30)

    assert len(_mqtt(env).snapshots) == 1


def test_rejected_answer_reports_server_fault_once_and_retries_same_seq(env):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]
    reason = "unplausibler Wert für dat: 99 (erlaubt -40–45)"

    _answer(env, bridge, seq, status="rejected", curve=None, offset=None, reason=reason)

    assert env.ha.pushes == [f"Heizungsbrücke: Server hat die Messwerte abgelehnt ({reason}). Die Heizkurve bleibt unverändert."]
    assert bridge.failsafe_ctx["delivery"].notbetrieb is False

    _advance(env, bridge, 30)
    _answer(env, bridge, seq, status="rejected", curve=None, offset=None, reason=reason)

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

    assert env.ha.writes == []
    assert "ungültige Serverantwort" in env.ha.pushes[-1]


def test_failed_value_write_is_not_acked_and_retried_with_same_seq(env):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]
    env.ha.write_error = RuntimeError("HA nicht erreichbar")

    _answer(env, bridge, seq)

    assert bridge.failsafe_ctx["delivery"].pending.seq == seq

    env.ha.write_error = None
    _advance(env, bridge, 30)

    assert [s["seq"] for s in _mqtt(env).snapshots] == [seq, seq]


def test_answer_during_boost_only_updates_backup(env):
    _quiet_backup(env)
    bridge = _start(env)

    _set_room_target(env, bridge, 22.0)  # Erhoehung: Comfort-Boost + Tick
    assert env.ha.writes == [("number.curve_current", 1.5), ("number.offset_current", 30.0)]

    _answer(env, bridge, _mqtt(env).snapshots[0]["seq"], curve=0.95, offset=23.0)

    assert env.ha.writes == [("number.curve_current", 1.5), ("number.offset_current", 30.0)]
    assert (_backup(env)["curve_current"], _backup(env)["offset_current"]) == (0.95, 23.0)


def test_foreign_seq_and_duplicate_answers_are_ignored(env):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]

    _answer(env, bridge, "fremd")
    assert env.ha.writes == []

    _mqtt(env).answer(seq)
    _mqtt(env).answer(seq)  # QoS-1-Doppelzustellung
    bridge.worker.run_pending()

    assert env.ha.writes == [("number.curve_current", 0.95), ("number.offset_current", 23.0)]


@pytest.mark.parametrize("raw,retain", [
    (b'{"seq": "s1"}', True), (b"[]", False), (b'"x"', False), (b"{kaputt", False), (b"", False),
])
def test_setpoints_callback_drops_retained_and_non_object_payloads(clock, raw, retain):
    worker = RegulationWorker(clock=clock)
    seen = []
    worker.register(main_module.EV_SETPOINTS, seen.append)
    message = MagicMock()
    message.payload = raw
    message.retain = retain

    main_module._make_setpoints_callback(worker)(None, None, message)  # darf nicht werfen
    worker.run_pending()

    assert seen == []


def test_setpoints_callback_posts_fresh_answer_to_worker(clock):
    worker = RegulationWorker(clock=clock)
    seen = []
    worker.register(main_module.EV_SETPOINTS, seen.append)
    message = MagicMock()
    message.payload = b'{"seq": "s1", "status": "ok"}'
    message.retain = False

    main_module._make_setpoints_callback(worker)(None, None, message)
    worker.run_pending()

    assert seen == [Event(main_module.EV_SETPOINTS, {"payload": {"seq": "s1", "status": "ok"}})]


def _start_in_notbetrieb_with_emergency_boost(env):
    _quiet_backup(env)
    save_backup(env.paths["FAILSAFE_PATH"], {"failsafe_active": True, "pending": {"seq": "alt-1", "trigger": "daily"}})
    env.ha.states["sensor.room_actual"] = 19.5  # > 1 K unter Soll -> Notfall-Boost beim Priming
    bridge = _start(env)
    assert (env.ha.states["number.curve_current"], env.ha.states["number.offset_current"]) == (1.5, 30.0)
    return bridge


def test_notbetrieb_end_via_answer_restores_device_from_emergency_boost(env):
    bridge = _start_in_notbetrieb_with_emergency_boost(env)

    _answer(env, bridge, "alt-1", curve=0.95, offset=23.0)

    assert (env.ha.states["number.curve_current"], env.ha.states["number.offset_current"]) == (0.95, 23.0)
    assert _backup(env)["emergency_boost_active"] is False


def test_notbetrieb_end_hands_device_to_running_comfort_boost(env):
    # Final-Review I2 (b): laeuft der Comfort-Boost noch, gehen dessen Werte aufs Geraet.
    bridge = _start_in_notbetrieb_with_emergency_boost(env)
    bridge.options = {**bridge.options, "boost_curve_value": 1.0, "boost_offset_value": 25.0}
    save_backup(env.paths["BACKUP_PATH"], {**_backup(env), "boost_active": True})

    _answer(env, bridge, "alt-1", curve=0.95, offset=23.0)

    assert (env.ha.states["number.curve_current"], env.ha.states["number.offset_current"]) == (1.0, 25.0)
    assert _backup(env)["boost_active"] is True
    assert _backup(env)["curve_current"] == 0.95


def test_failsafe_state_write_failure_does_not_stop_regulation(env, monkeypatch, caplog):
    _quiet_backup(env)
    bridge = _start(env)

    def _broken_save(ctx, path):
        raise OSError("SD-Karte kaputt")

    monkeypatch.setattr(main_module, "_save_failsafe_ctx", _broken_save)
    with caplog.at_level(logging.ERROR):
        _set_room_target(env, bridge, 20.5)

    assert len(_mqtt(env).snapshots) == 1
    assert "failsafe_state.json" in caplog.text


# --- Lokaler Check und Trigger ---

def test_trigger_callback_coalesces_flood_and_keeps_room_target_flag(clock):
    # Review Focus 5.
    worker = RegulationWorker(clock=clock)
    seen = []
    worker.register(main_module.EV_LOCAL_CHECK, seen.append)
    manifest = ChannelManifest(entity_ids={"room_actual": "sensor.room_actual", "room_target": "sensor.room_target"})
    callback = main_module._make_trigger_event_callback(manifest, worker)

    for _ in range(10):
        callback({"platform": "state", "entity_id": "sensor.room_actual", "attribute": None})
    callback({"platform": "state", "entity_id": "sensor.room_target", "attribute": None})
    callback({"platform": "time"})
    worker.run_pending()

    assert seen == [Event(main_module.EV_LOCAL_CHECK, {"room_target_fired": True})]


@pytest.mark.parametrize("trigger,fired", [
    ({"platform": "state", "entity_id": "climate.wz", "attribute": "temperature"}, True),
    ({"platform": "state", "entity_id": "climate.wz", "attribute": "current_temperature"}, False),
    ({"platform": "state", "entity_id": "climate.wz"}, False),
    ({"platform": "time"}, False),
])
def test_trigger_callback_matches_room_target_by_entity_and_attribute(clock, trigger, fired):
    worker = RegulationWorker(clock=clock)
    seen = []
    worker.register(main_module.EV_LOCAL_CHECK, seen.append)
    manifest = ChannelManifest(entity_ids={
        "room_actual": "climate.wz::current_temperature", "room_target": "climate.wz::temperature",
    })

    main_module._make_trigger_event_callback(manifest, worker)(trigger)
    worker.run_pending()

    assert seen == [Event(main_module.EV_LOCAL_CHECK, {"room_target_fired": fired})]


@pytest.mark.parametrize("room_actual,room_target,expected_room_triggers", [
    (
        "climate.wz::current_temperature", "climate.wz::temperature",
        [
            {"platform": "state", "entity_id": "climate.wz", "attribute": "temperature", "for": {"seconds": 10}},
            {"platform": "state", "entity_id": "climate.wz", "attribute": "current_temperature"},
        ],
    ),
    (
        "sensor.room_actual", "sensor.room_target",
        [
            {"platform": "state", "entity_id": "sensor.room_target", "for": {"seconds": 10}},
            {"platform": "state", "entity_id": "sensor.room_actual"},
        ],
    ),
])
def test_build_ha_trigger_client_sets_attribute_on_both_room_triggers(env, clock, room_actual, room_target, expected_room_triggers):
    # B7: der room_actual-Trigger bekommt wie room_target ein attribute, wenn gemappt.
    stub = SimpleNamespace(
        manifest=ChannelManifest(entity_ids={"room_actual": room_actual, "room_target": room_target}),
        options={"daily_trigger_time": "12:00"}, ha_api=FakeHa(), worker=RegulationWorker(clock=clock),
    )

    client = main_module._build_ha_trigger_client(stub)

    assert client.kwargs["triggers"] == expected_room_triggers + [{"platform": "time", "at": "12:00"}]


def test_on_connected_rereads_room_target_and_runs_local_check(env):
    # B7: Sollwertaenderung waehrend der WS-Trennung wird sofort verarbeitet.
    _quiet_backup(env)
    bridge = _start(env)
    env.ha.states["sensor.room_target"] = 22.0

    env.trigger_clients[-1].kwargs["on_connected"]()
    bridge.worker.run_pending()

    assert bridge.stable_target == 22.0
    assert bridge.boost_active is True
    assert _mqtt(env).snapshots[-1]["trigger"] == "target_change"


def test_watchdog_runs_local_check_only_while_ws_disconnected(env):
    _quiet_backup(env)
    bridge = _start(env)
    env.ha.states["sensor.room_target"] = 20.5

    _advance(env, bridge, 300)  # verbunden: kein Fallback
    assert bridge.stable_target == 21.0

    env.trigger_clients[-1].connected = False
    _advance(env, bridge, 300)

    assert bridge.stable_target == 20.5
    assert len(_mqtt(env).snapshots) == 1


def test_local_check_keeps_cached_target_when_room_target_unreadable(env, caplog):
    _quiet_backup(env)
    bridge = _start(env)
    env.ha.states["sensor.room_target"] = ValueError("unavailable")

    with caplog.at_level(logging.WARNING):
        _trigger(env, bridge, "sensor.room_target")

    assert bridge.stable_target == 21.0
    assert "room_target nicht lesbar" in caplog.text


def test_local_check_after_abo_finished_does_nothing(env):
    _quiet_backup(env)
    bridge = _start(env)
    bridge.failsafe_ctx["abo_finished"] = True

    _set_room_target(env, bridge, 22.0)

    assert env.ha.writes == []
    assert _mqtt(env).snapshots == []
    assert bridge.stable_target == 21.0


def test_telemetry_runs_on_its_own_schedule(env):
    _quiet_backup(env)
    bridge = _start(env)
    assert len(_mqtt(env).telemetry) == 1

    _advance(env, bridge, 299)
    assert len(_mqtt(env).telemetry) == 1

    _advance(env, bridge, 1)
    assert len(_mqtt(env).telemetry) == 2
    assert _mqtt(env).telemetry[-1]["failsafe_active"] is False


# --- Abo-Pfade ---

def test_active_status_at_start_clears_stale_inactive_state(env):
    _quiet_backup(env)
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())

    _start(env)

    assert not env.paths["ENTITLEMENT_PATH"].exists()
    assert len(env.mqtt_clients) == 1


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
    assert (env.ha.states["number.curve_current"], env.ha.states["number.offset_current"]) == (1.5, 30.0)
    assert len(env.ha.persistent) == 1
    assert "Abo inaktiv" in env.ha.pushes[0]


def test_inactive_after_grace_exits_cleanly_without_writes(env, monkeypatch):
    _quiet_backup(env)
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    monkeypatch.setattr("heizungsbruecke.__main__.entitlement.grace_expired", lambda since, now: True)

    assert main_module._run_bridge(OPTIONS, env.ha) == 0

    assert env.mqtt_clients == []
    assert env.ha.writes == []
    assert env.ha.pushes == []


def test_closing_start_retries_failed_restore_until_success(env, monkeypatch):
    _quiet_backup(env, emergency_boost_active=True)
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    monkeypatch.setattr("heizungsbruecke.__main__.entitlement.grace_expired", lambda since, now: True)
    sleeps = []
    monkeypatch.setattr("heizungsbruecke.__main__.time.sleep", sleeps.append)
    failures = [RuntimeError("HA nicht erreichbar")]

    original_write = env.ha.set_number_value

    def _flaky_write(entity_id, value):
        if failures:
            raise failures.pop()
        original_write(entity_id, value)

    env.ha.set_number_value = _flaky_write

    assert main_module._run_bridge(OPTIONS, env.ha) == 0

    assert sleeps == [300]
    assert env.trigger_clients == []
    assert ("number.offset_current", 22.0) in env.ha.writes
    assert _backup(env)["emergency_boost_active"] is False


def test_auth_rejected_with_inactive_abo_enters_abo_mode(env):
    _quiet_backup(env)
    bridge = _start(env)
    env.abo["status"] = entitlement.INACTIVE

    _mqtt(env).kwargs["on_auth_rejected"](_mqtt(env))  # aus dem paho-Thread
    bridge.worker.run_pending()

    assert _mqtt(env).stopped is True
    assert bridge.failsafe_ctx["delivery"].notbetrieb is True
    assert _failsafe_file(env)["failsafe_active"] is True
    assert len(env.ha.persistent) == 1


@pytest.mark.parametrize("status", [entitlement.ACTIVE, entitlement.UNKNOWN])
def test_auth_rejected_with_active_or_unknown_abo_only_logs(env, caplog, status):
    _quiet_backup(env)
    bridge = _start(env)
    env.abo["status"] = status

    with caplog.at_level(logging.ERROR):
        _mqtt(env).kwargs["on_auth_rejected"](_mqtt(env))
        bridge.worker.run_pending()

    assert _mqtt(env).stopped is False
    assert "abgelehnt" in caplog.text


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


def test_unexpected_entitlement_query_error_counts_as_unknown(env, monkeypatch):
    # T10b: eine unerwartet werfende Abo-Abfrage nach dem zweiten Timeout laeuft ueber
    # _fallback_follow_up als "unbekannt" -- Notbetrieb mit Retry, kein Abo-inaktiv-Modus.
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]

    def _broken_query(tenant_id, base_url):
        raise RuntimeError("unerwartet")

    monkeypatch.setattr("heizungsbruecke.__main__.entitlement.query_status", _broken_query)
    _advance(env, bridge, 30)
    _advance(env, bridge, 30)

    assert bridge.failsafe_ctx["delivery"].notbetrieb is True
    assert bridge.failsafe_ctx.get("abo_inactive_since") is None
    assert _mqtt(env).stopped is False
    assert _mqtt(env).status["failsafe"] == "ON"
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

    assert bridge.failsafe_ctx.get("abo_inactive_since") is not None
    assert _mqtt(env).stopped is True
    assert NOTBETRIEB_ON not in env.ha.pushes
    assert any("Abo inaktiv" in text for text in env.ha.pushes)
    assert bridge.failsafe_ctx["delivery"].pending is None


def test_grace_end_during_runtime_restores_notifies_and_exits(env, monkeypatch):
    _quiet_backup(env, emergency_boost_active=True)
    env.ha.states.update({"number.curve_current": 1.5, "number.offset_current": 30.0})
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    answers = iter([False, True])  # Startpruefung, dann grace_check
    monkeypatch.setattr("heizungsbruecke.__main__.entitlement.grace_expired", lambda since, now: next(answers, True))

    bridge = main_module._start_bridge(OPTIONS, env.ha, clock=env.clock)

    assert bridge.worker.run_pending() == 0
    assert env.trigger_clients[-1].stopped is True
    assert (env.ha.states["number.curve_current"], env.ha.states["number.offset_current"]) == (0.9, 22.0)
    assert env.ha.persistent[-1] == ("smartheat_abo_inaktiv", main_module.ABO_ENDED_MESSAGE)
    assert _backup(env)["emergency_boost_active"] is False


def test_grace_end_with_failing_restore_keeps_running_and_retries(env, monkeypatch):
    _quiet_backup(env, emergency_boost_active=True)
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    answers = iter([False])  # Startpruefung, danach ist die Frist abgelaufen
    monkeypatch.setattr("heizungsbruecke.__main__.entitlement.grace_expired", lambda since, now: next(answers, True))
    bridge = main_module._start_bridge(OPTIONS, env.ha, clock=env.clock)
    env.ha.write_error = RuntimeError("HA nicht erreichbar")

    assert bridge.worker.run_pending() is None
    assert env.trigger_clients[-1].stopped is False

    env.ha.write_error = None
    env.clock.advance(300)

    assert bridge.worker.run_pending() == 0
    ended = [entry for entry in env.ha.persistent if entry[1] == main_module.ABO_ENDED_MESSAGE]
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
    env.ha.states.update({"number.curve_current": 1.5, "number.offset_current": 30.0})
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    monkeypatch.setattr("heizungsbruecke.__main__.entitlement.grace_expired", lambda since, now: True)

    assert main_module._run_bridge(OPTIONS, env.ha) == 0

    assert (env.ha.states["number.curve_current"], env.ha.states["number.offset_current"]) == (0.9, 22.0)
    assert _backup(env)["emergency_boost_active"] is False


def _start_just_before_grace_end(env, monkeypatch):
    _quiet_backup(env, emergency_boost_active=True)
    env.ha.states.update({"number.curve_current": 1.5, "number.offset_current": 30.0})
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    answers = iter([False])  # Startpruefung, danach ist die Frist abgelaufen
    monkeypatch.setattr("heizungsbruecke.__main__.entitlement.grace_expired", lambda since, now: next(answers, True))
    exec_calls = []
    monkeypatch.setattr("heizungsbruecke.__main__.os.execv", lambda path, args: exec_calls.append((path, args)))
    bridge = main_module._start_bridge(OPTIONS, env.ha, clock=env.clock)
    return bridge, exec_calls


def test_grace_end_with_reactivated_abo_restarts_instead_of_restoring(env, monkeypatch):
    # T2-5: kein faelschliches Zuruecksetzen am Fristende, wenn das Abo wieder aktiv ist.
    bridge, exec_calls = _start_just_before_grace_end(env, monkeypatch)
    env.abo["status"] = entitlement.ACTIVE

    bridge.worker.run_pending()

    assert exec_calls == [(sys.executable, [sys.executable, "-m", "heizungsbruecke"])]
    assert not env.paths["ENTITLEMENT_PATH"].exists()
    assert (env.ha.states["number.curve_current"], env.ha.states["number.offset_current"]) == (1.5, 30.0)
    assert all(message != main_module.ABO_ENDED_MESSAGE for _, message in env.ha.persistent)


@pytest.mark.parametrize("status", [entitlement.INACTIVE, entitlement.UNKNOWN])
def test_grace_end_finishes_when_abo_not_active(env, monkeypatch, status):
    bridge, exec_calls = _start_just_before_grace_end(env, monkeypatch)
    env.abo["status"] = status

    assert bridge.worker.run_pending() == 0
    assert exec_calls == []
    assert (env.ha.states["number.curve_current"], env.ha.states["number.offset_current"]) == (0.9, 22.0)


def test_grace_end_exits_even_if_saving_flags_fails(env, monkeypatch):
    # T2-8: Werte stehen schon auf dem Geraet -> Wiederherstellung gilt als erfolgt, Exit 0.
    bridge, _ = _start_just_before_grace_end(env, monkeypatch)

    def _broken_save(path, values):
        raise OSError("SD-Karte kaputt")

    monkeypatch.setattr("heizungsbruecke.__main__.save_backup", _broken_save)

    assert bridge.worker.run_pending() == 0
    assert (env.ha.states["number.curve_current"], env.ha.states["number.offset_current"]) == (0.9, 22.0)

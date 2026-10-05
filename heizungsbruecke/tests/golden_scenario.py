"""Golden-Master des ganzen Add-ons (Spec SHG 3.4 Punkt 1). Ein fester Szenario-Lauf mit Fake-HA, Fake-Broker,
Fake-Accounts-API und fester Uhr zeichnet jede Aussenwirkung auf: schreibende HA-Aufrufe, Push- und HA-Benachrichtigungen,
HA-Ereignisse, MQTT-Publishes, Start/Stopp von MQTT und Trigger-Client, Abo-Abfragen, Prozess-Neustarts und nach jedem
Schritt den Inhalt aller Dateien im Datenverzeichnis. Lesende HA-Aufrufe und Logs zaehlen nicht.

Erzeugt auf dem Stand vor G1 (GOLDEN_UPDATE=1), danach nie neu. Die Konstanten QUERY_STATUS, MQTT_CLIENT,
TRIGGER_CLIENT, RESTART und install_wall_clock sind die einzigen Stellen, die die G1-Tasks anpassen (Patch-Ziele)."""
import copy
import itertools
import json
import time
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from conftest import FakeClock
from fakes import ACCESS_OPTIONS, FakeHa

import heizungsbruecke.__main__ as main_module
from heizungsbruecke.derived_sensors import DerivedSensors

QUERY_STATUS = "smartheat_runtime.entitlement.query_status"
MQTT_CLIENT = "heizungsbruecke.triggers.BridgeMqttClient"
TRIGGER_CLIENT = "heizungsbruecke.triggers.HaTriggerClient"
RESTART = "heizungsbruecke.abo.restart_process"

BERLIN = ZoneInfo("Europe/Berlin")
START = datetime(2026, 1, 15, 8, 0, tzinfo=BERLIN)
FILES = {
    "BACKUP_PATH": "backup.json", "FAILSAFE_PATH": "failsafe_state.json",
    "ENTITLEMENT_PATH": "entitlement_state.json", "DERIVED_SENSORS_PATH": "derived_sensors.json",
}
OPTIONS = {
    "tenant_id": "golden",
    "setup_id": "setup-golden-1",
    "verteilsystem": "Heizkoerper",
    "daily_trigger_time": "12:00",
    **ACCESS_OPTIONS,
    "mqtt_username": "u",
    "mqtt_password": "p",
    "room_sensors": ["sensor.room_actual"],
    "battery_entities": ["sensor.thermostat_battery"],
    "entity_room_target": "sensor.room_target",
    "entity_curve_current": "number.curve_current",
    "entity_shift_current": "climate.zone",
    "entity_min_flow": "number.min_flow",
    "entity_outdoor_temp": "sensor.outdoor_temp",
    "entity_heat_limit": "number.heat_limit",
    "entity_flow_temperature": "sensor.flow_temperature",
    "entity_flow_setpoint": "sensor.flow_setpoint",
    "notify_services": ["notify.handy"],
    "accounts_api_base_url": "https://accounts.example.test",
}
STATES = {
    "climate.zone": "auto", "climate.zone::temperature": 22.0,
    "sensor.flow_temperature": 35.0, "sensor.flow_setpoint": 35.0,
    "sensor.thermostat_battery": "80",
}


class FrozenWall:
    """Feste Wanduhr in Europe/Berlin; laeuft nur ueber advance()/set()."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value

    def advance(self, seconds: float) -> None:
        self._now = (self._now.astimezone(ZoneInfo("UTC")) + timedelta(seconds=seconds)).astimezone(BERLIN)


def install_wall_clock(monkeypatch, wall: FrozenWall) -> None:
    monkeypatch.setattr("smartheat_core.wallclock._now", wall.now)


class RecordingHa(FakeHa):
    """FakeHa, die jede schreibende Wirkung in `log` eintraegt (Reihenfolge bleibt erhalten)."""

    def __init__(self, log: list) -> None:
        super().__init__()
        self._log = log
        self.states.update(STATES)

    def _write(self, service: str, key: str, value) -> None:
        if self.write_error is not None:
            self._log.append(["ha", service, key, value, "fehler"])
            raise self.write_error
        self._log.append(["ha", service, key, value])
        self.states[key] = value
        self.writes.append((key, value))

    def set_number_value(self, entity_id, value):
        self._write("number.set_value", entity_id, value)

    def set_climate_temperature(self, entity_id, value):
        self._write("climate.set_temperature", f"{entity_id}::temperature", value)

    def set_hvac_mode(self, entity_id, mode):
        self._write("climate.set_hvac_mode", entity_id, mode)

    def select_option(self, entity_id, option):
        self._write("select.select_option", entity_id, option)

    def set_preset_mode(self, entity_id, preset):
        self._write("climate.set_preset_mode", f"{entity_id}::preset_mode", preset)

    def send_notification(self, service, message):
        self._log.append(["ha", "notify", service, message])

    def create_persistent_notification(self, title, message, notification_id):
        self._log.append(["ha", "persistent_notification.create", notification_id, title, message])

    def dismiss_persistent_notification(self, notification_id):
        self._log.append(["ha", "persistent_notification.dismiss", notification_id])

    def fire_event(self, event_type, data):
        self._log.append(["ha", "event", event_type, copy.deepcopy(data)])


class RecordingMqtt:
    def __init__(self, log: list, kwargs: dict) -> None:
        self._log = log
        self.kwargs = kwargs
        self.connect_failures = 0
        self.connected = True
        self.snapshots: list[dict] = []
        self.setpoints_callback = None

    def is_connected(self):
        return self.connected

    def subscribe_setpoints(self, on_message):
        self.setpoints_callback = on_message

    def loop_start(self):
        self._log.append(["mqtt", "loop_start"])

    def stop(self):
        self._log.append(["mqtt", "stop"])

    def publish_snapshot(self, payload):
        self._log.append(["mqtt", "snapshot", copy.deepcopy(payload)])
        self.snapshots.append(copy.deepcopy(payload))

    def publish_telemetry(self, payload):
        self._log.append(["mqtt", "telemetry", copy.deepcopy(payload)])


class RecordingTrigger:
    def __init__(self, log: list, kwargs: dict) -> None:
        self._log = log
        self.kwargs = kwargs
        self.connected = True

    def start(self):
        self._log.append(["trigger", "start"])

    def stop(self):
        self._log.append(["trigger", "stop"])


class World:
    def __init__(self, tmp_path, monkeypatch) -> None:
        self.log: list = []
        self.record: list[dict] = []
        self.clock = FakeClock()
        self.wall = FrozenWall(START)
        self.data = tmp_path / "data"
        self.data.mkdir(parents=True)
        self.ha = RecordingHa(self.log)
        self.abo = {"status": "active"}
        self.mqtt: list[RecordingMqtt] = []
        self.triggers: list[RecordingTrigger] = []
        self.bridge = None
        self._patch(monkeypatch)

    def _patch(self, monkeypatch) -> None:
        for name, file_name in FILES.items():
            monkeypatch.setattr(f"heizungsbruecke.config.{name}", self.data / file_name)
        monkeypatch.setattr(
            "heizungsbruecke.derived_sensors.ensure_all",
            lambda **kwargs: DerivedSensors({"room_actual": "sensor.room_actual"}, (), "fp-golden"),
        )
        monkeypatch.setattr(QUERY_STATUS, self._query_status)
        monkeypatch.setattr(MQTT_CLIENT, self._new_mqtt)
        monkeypatch.setattr(TRIGGER_CLIENT, self._new_trigger)
        monkeypatch.setattr(RESTART, lambda: self.log.append(["prozess", "neustart"]))
        counter = itertools.count(1)
        monkeypatch.setattr("uuid.uuid4", lambda: uuid.UUID(int=next(counter)))
        # datetime.now().astimezone() im Add-on liefert die Systemzone: fuer den Lauf fest auf Berlin setzen.
        monkeypatch.setenv("TZ", "Europe/Berlin")
        time.tzset()
        install_wall_clock(monkeypatch, self.wall)

    def _query_status(self, tenant_id, base_url, token):
        self.log.append(["accounts", "status", tenant_id, base_url])
        return self.abo["status"]

    def _new_mqtt(self, *args, **kwargs):
        self.mqtt.append(RecordingMqtt(self.log, kwargs))
        return self.mqtt[-1]

    def _new_trigger(self, **kwargs):
        self.triggers.append(RecordingTrigger(self.log, kwargs))
        return self.triggers[-1]

    # --- Bausteine der Schritte ---

    def run(self) -> None:
        self.bridge.worker.run_pending()

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)
        self.wall.advance(seconds)
        self.run()

    def start_process(self, **overrides) -> None:
        self.bridge = main_module._start_bridge({**OPTIONS, **overrides}, self.ha, clock=self.clock)
        self.run()

    def connect(self) -> None:
        self.mqtt[-1].kwargs["on_connected"](None)
        self.triggers[-1].kwargs["on_connected"]()
        self.run()

    def trigger(self, entity_id: str) -> None:
        self.triggers[-1].kwargs["on_trigger_event"]({"platform": "state", "entity_id": entity_id, "attribute": None})
        self.run()

    def set_room_target(self, value: float) -> None:
        self.ha.states["sensor.room_target"] = value
        self.trigger("sensor.room_target")

    def set_room_actual(self, value: float) -> None:
        self.ha.states["sensor.room_actual"] = value
        self.trigger("sensor.room_actual")

    def last_seq(self) -> str:
        return self.mqtt[-1].snapshots[-1]["seq"]

    def answer(self, status="ok", reason=None, curve=0.95, shift=23.0, heat_limit=16.0) -> None:
        payload = {
            "schema": 4, "seq": self.last_seq(), "ts": "2026-01-15T00:00:00+01:00", "status": status, "reason": reason,
            "levers": {"curve": curve, "room_setpoint": shift, "heat_limit": heat_limit},
            "learned": {"curve": 1.0, "heat_limit": 16.0},
        }
        self.mqtt[-1].setpoints_callback(None, None, SimpleNamespace(retain=False, payload=json.dumps(payload)))
        self.run()

    def state(self):
        return self.bridge.store.state

    def step(self, name: str, action) -> None:
        self.log.clear()
        action()
        files = {path.name: json.loads(path.read_text()) for path in sorted(self.data.glob("*.json"))}
        self.record.append({"schritt": name, "wirkungen": copy.deepcopy(self.log), "dateien": files})


def _expect(condition: bool, what: str) -> None:
    """Plausibilitaet: der Schritt hat den beabsichtigten Pfad wirklich durchlaufen."""
    if not condition:
        raise AssertionError(f"Golden-Master-Szenario: {what}")


def _wrote(world: World, key: str) -> bool:
    return any(entry[0] == "ha" and len(entry) >= 3 and entry[2] == key for entry in world.log)


def run_scenario(tmp_path, monkeypatch) -> list[dict]:
    w = World(tmp_path, monkeypatch)

    def erster_start():
        w.start_process()
        w.connect()
        _expect(len(w.mqtt[-1].snapshots) >= 1, "erster Start publiziert einen Snapshot")

    def erste_antwort():
        w.answer(curve=0.95, shift=23.0)
        _expect(w.state().delivery.pending is None, "Antwort beendet den Tick")

    def tagestick():
        w.wall.set(datetime(2026, 1, 15, 12, 0, 30, tzinfo=BERLIN))
        w.advance(30)
        w.trigger("sensor.room_actual")
        _expect(w.mqtt[-1].snapshots[-1]["trigger"] == "daily", "Tagestick")
        w.answer(curve=1.0, shift=23.5)

    def soll_senken():
        w.set_room_target(20.5)
        _expect(w.mqtt[-1].snapshots[-1]["trigger"] == "target_change", "Tick bei Soll-Aenderung")
        w.answer(curve=1.0, shift=23.0)

    def soll_erhoehen_komfort_boost():
        w.set_room_target(22.0)
        _expect(w.state().boost_active, "Comfort-Boost bei Soll-Erhoehung")
        w.answer(curve=1.0, shift=24.0)

    def boost_ende():
        w.set_room_actual(22.0)
        _expect(not w.state().boost_active, "Comfort-Boost endet bei erreichtem Soll")

    def notbetrieb():
        w.set_room_target(21.5)
        w.advance(30)  # erster Ack-Timeout, sofortige Wiederholung
        w.advance(30)  # zweiter Ack-Timeout -> Notbetrieb
        _expect(w.state().delivery.notbetrieb, "Notbetrieb nach zwei Ack-Timeouts")

    def notfall_boost():
        w.set_room_actual(18.0)
        _expect(w.state().emergency_boost_active, "Notfall-Boost bei kaltem Raum im Notbetrieb")

    def rueckkehr_aus_notbetrieb():
        w.advance(300)  # naechster Zustellversuch
        w.answer(curve=1.0, shift=23.5)
        _expect(not w.state().delivery.notbetrieb, "Antwort beendet den Notbetrieb")
        w.set_room_actual(21.5)

    def datenfehler_lokal():
        # room_actual wird bei jedem Versuch frisch geprueft (Hebel dagegen kurz nach eigenem Schreiben nicht gelesen).
        kept = w.ha.states["sensor.room_actual"]
        w.ha.states["sensor.room_actual"] = ValueError("unavailable")
        w.set_room_target(21.0)
        fault = w.state().delivery.datenfehler
        _expect(fault is not None and fault.source == "local", "lokaler Datenfehler")
        w.ha.states["sensor.room_actual"] = kept
        w.advance(30)
        w.answer(curve=1.0, shift=23.0)

    def datenfehler_server():
        w.set_room_target(21.5)
        w.answer(status="rejected", reason="Testgrund")
        fault = w.state().delivery.datenfehler
        _expect(fault is not None and fault.source == "server", "Datenfehler vom Server")
        w.set_room_target(21.0)
        w.answer(curve=1.0, shift=23.0)

    def datenfehler_anlage():
        w.set_room_target(20.5)  # Senkung: eine Erhoehung wuerde den Comfort-Boost starten, der Serverwerte zurueckhaelt
        w.ha.write_error = RuntimeError("Cloud nicht erreichbar")
        w.answer(curve=1.05, shift=23.5)
        fault = w.state().delivery.datenfehler
        _expect(fault is not None and fault.source == "write", "Datenfehler beim Schreiben")
        w.ha.write_error = None
        w.advance(30)
        w.answer(curve=1.05, shift=23.5)

    def durchsetzen():
        w.ha.states["number.curve_current"] = 1.4  # Eingriff in der Hersteller-App
        written = False
        for _ in range(10):
            w.advance(300)
            written = written or _wrote(w, "number.curve_current")
        _expect(written, "Durchsetzen schreibt die Steigung zurueck")

    def hinweis_batterie():
        w.ha.states["sensor.thermostat_battery"] = "15"
        w.advance(300)
        _expect(any(entry[:2] == ["ha", "notify"] for entry in w.log), "Batterie-Hinweis als Push")

    def neustart_mit_zustand():
        w.start_process()
        w.connect()

    def abo_inaktiv():
        w.abo["status"] = "inactive"
        w.start_process()
        _expect(w.state().abo_inactive_since is not None, "Abo-inaktiv-Modus")

    def abo_wieder_aktiv():
        w.wall.advance(31 * 24 * 3600)
        w.abo["status"] = "active"
        w.advance(300)
        _expect(["prozess", "neustart"] in w.log, "Abo wieder aktiv fuehrt zum Prozess-Neustart")

    def abo_erneut_inaktiv():
        w.abo["status"] = "inactive"
        w.start_process()
        _expect(w.state().abo_inactive_since is not None, "erneut im Abo-inaktiv-Modus")

    def fristende():
        w.wall.advance(31 * 24 * 3600)
        w.advance(300)
        _expect(w.state().abo_finished, "Fristende nach 30 Tagen")

    def abmelden():
        w.abo["status"] = "active"
        w.start_process(abgemeldet=True)
        _expect(getattr(w.bridge, "reason", None) == "abgemeldet", "Abmelden fuehrt in den Ruhezustand")

    for name, action in (
        ("erster_start", erster_start), ("erste_antwort", erste_antwort), ("tagestick", tagestick),
        ("soll_senken", soll_senken), ("soll_erhoehen_komfort_boost", soll_erhoehen_komfort_boost),
        ("boost_ende", boost_ende), ("notbetrieb", notbetrieb), ("notfall_boost", notfall_boost),
        ("rueckkehr_aus_notbetrieb", rueckkehr_aus_notbetrieb), ("datenfehler_lokal", datenfehler_lokal),
        ("datenfehler_server", datenfehler_server), ("datenfehler_anlage", datenfehler_anlage),
        ("durchsetzen", durchsetzen), ("hinweis_batterie", hinweis_batterie),
        ("neustart_mit_zustand", neustart_mit_zustand), ("abo_inaktiv", abo_inaktiv), ("abo_wieder_aktiv", abo_wieder_aktiv),
        ("abo_erneut_inaktiv", abo_erneut_inaktiv), ("fristende", fristende),
        ("abmelden", abmelden),
    ):
        w.step(name, action)
    return w.record


def render(record: list[dict]) -> str:
    return json.dumps(record, ensure_ascii=False, indent=1) + "\n"

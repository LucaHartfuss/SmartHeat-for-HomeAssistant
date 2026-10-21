"""Golden-Master des ganzen Add-ons (Spec SHG 3.4 Punkt 1). Ein fester Szenario-Lauf mit Fake-HA, dem Link einer
Geraete-Attrappe (geraet_helpers.FakeGeraet) und fester Uhr zeichnet jede Aussenwirkung auf: schreibende HA-Aufrufe,
Push- und HA-Benachrichtigungen, HA-Ereignisse, Nachrichten des Links, Start/Stopp des Trigger-Clients,
Prozess-Neustarts und nach jedem Schritt den Inhalt aller Dateien im Datenverzeichnis. Lesende HA-Aufrufe und Logs
zaehlen nicht.

Erzeugt auf dem Stand vor G1 (GOLDEN_UPDATE=1), mit 1.0.0 (Spec 5c, Plan D Task 10) einmal neu im Geraete-Pfad: die
Laufzeit startet auf dem Konfigurationsdokument aus OPTIONS, das Abo kommt aus dem Dokument, Snapshots und Telemetrie
gehen ueber den Link. Der Master von 0.36 bleibt als addon_scenario_0_36.json; normalisieren() haelt fest, dass sich
beide nur in den ERLAUBT genannten Punkten unterscheiden. TRIGGER_CLIENT und install_wall_clock sind die Patch-Ziele;
den Prozess-Neustart zeichnet der Rueckruf neustart auf, den start_process app.start uebergibt (Spec Stufe 3 S6 4.2)."""
import copy
import itertools
import json
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import geraet_helpers
from conftest import FakeClock
from fakes import FakeHa, FakeTriggerClient, trigger_klasse

from heizungsbruecke.derived_sensors import DerivedSensors
from heizungsbruecke.version import ADDON_VERSION

TRIGGER_CLIENT = "heizungsbruecke.geraet_trigger.HaTriggerClient"

BERLIN = ZoneInfo("Europe/Berlin")
START = datetime(2026, 1, 15, 8, 0, tzinfo=BERLIN)
FILES = {
    "BACKUP_PATH": "backup.json", "FAILSAFE_PATH": "failsafe_state.json",
    "ENTITLEMENT_PATH": "entitlement_state.json", "DERIVED_SENSORS_PATH": "derived_sensors.json",
    "AUFRAEUMEN_PATH": "aufraeumen.json", "RUNTIME_DIR": "runtime", "ROOM_TARGET_PATH": "runtime/room_target.json",
    "ABGEMELDET_PATH": "runtime/abgemeldet.json", "RASTUNG_PATH": "device/sicherheitssatz.json",
}
#: Testdaten; die Laufzeit liest sie als Konfigurationsdokument (geraet_helpers.konfiguration_aus_optionen).
OPTIONS = {
    "tenant_id": "golden",
    "verteilsystem": "Heizkoerper",
    "daily_trigger_time": "12:00",
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
    "notify_services": ["notify.mobile_app_handy"],
}
STATES = {
    "climate.zone": "auto", "climate.zone::temperature": 22.0,
    "sensor.flow_temperature": 35.0, "sensor.flow_setpoint": 35.0,
    "sensor.thermostat_battery": "80",
    # echtes HA: die Klasse steht im Zustand (lokale Positivliste der Rollen, A6-02)
    "sensor.flow_temperature::device_class": "temperature", "sensor.flow_setpoint::device_class": "temperature",
    "sensor.thermostat_battery::device_class": "battery",
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


class RecordingTrigger(FakeTriggerClient):
    """Fake-Trigger-Client des Szenarios: schreibt start/stop ins Protokoll (`log` setzt trigger_klasse)."""

    log: list

    def start(self) -> None:
        self.log.append(["trigger", "start"])

    def stop(self) -> None:
        self.log.append(["trigger", "stop"])


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
        self.triggers: list[RecordingTrigger] = []
        self.bridge = None
        self.geraet = None
        self._patch(monkeypatch)

    def _patch(self, monkeypatch) -> None:
        for name, file_name in FILES.items():
            monkeypatch.setattr(f"heizungsbruecke.config.{name}", self.data / file_name)
        monkeypatch.setattr(
            "heizungsbruecke.derived_sensors.ensure_all",
            lambda **kwargs: DerivedSensors({"room_actual": "sensor.room_actual"}, (), "fp-golden"),
        )
        monkeypatch.setattr(TRIGGER_CLIENT, trigger_klasse(self.triggers, RecordingTrigger, log=self.log))
        counter = itertools.count(1)
        monkeypatch.setattr("uuid.uuid4", lambda: uuid.UUID(int=next(counter)))
        # datetime.now().astimezone() im Add-on liefert die Systemzone: fuer den Lauf fest auf Berlin setzen.
        monkeypatch.setenv("TZ", "Europe/Berlin")
        time.tzset()
        install_wall_clock(monkeypatch, self.wall)

    # --- Bausteine der Schritte ---

    def run(self) -> None:
        self.bridge.worker.run_pending()

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)
        self.wall.advance(seconds)
        self.run()

    def start_process(self, *, abgemeldet: bool = False) -> None:
        """Neuer Prozess: die Laufzeit auf dem Dokument aus OPTIONS; das Abo liest sie bei jeder Pruefung frisch aus
        self.abo (das Szenario schaltet es mitten im Lauf um)."""
        self.bridge, self.geraet = geraet_helpers.start_bridge(
            self, options=OPTIONS, abo=lambda: self.abo["status"], abgemeldet=abgemeldet, log=self.log,
            neustart=lambda: self.log.append(["prozess", "neustart"]))
        self.run()

    def connect(self) -> None:
        self.geraet.channel.verbunden()
        self.triggers[-1].kwargs["on_connected"]()
        self.run()

    def trigger(self, entity_id: str) -> None:
        """HA meldet eine Zustandsaenderung; die Soll-Entity geht in den Soll-Eingang, alles andere ist ein lokaler
        Check."""
        value = self.ha.states.get(entity_id)
        self.triggers[-1].kwargs["on_trigger_event"]({
            "platform": "state", "entity_id": entity_id, "attribute": None,
            "to_state": {"state": value if isinstance(value, int | float | str) else "unavailable"}})
        self.run()

    def set_room_target(self, value: float) -> None:
        self.ha.states["sensor.room_target"] = value
        self.trigger("sensor.room_target")

    def set_room_actual(self, value: float) -> None:
        self.ha.states["sensor.room_actual"] = value
        self.trigger("sensor.room_actual")

    def snapshots(self) -> list[dict]:
        return self.geraet.sent.of("up/snapshot")

    def last_seq(self) -> str:
        return self.snapshots()[-1]["seq"]

    def answer(self, status="ok", reason=None, curve=0.95, shift=23.0, heat_limit=16.0) -> None:
        payload = {
            "schema": 4, "seq": self.last_seq(), "ts": "2026-01-15T00:00:00+01:00", "status": status, "reason": reason,
            "levers": {"curve": curve, "room_setpoint": shift, "heat_limit": heat_limit},
            "learned": {"curve": 1.0, "heat_limit": 16.0},
        }
        self.geraet.channel.setpoints(payload)
        self.run()

    def state(self):
        return self.bridge.store.state

    def step(self, name: str, action) -> None:
        self.log.clear()
        action()
        files = {path.relative_to(self.data).as_posix(): json.loads(path.read_text())
                 for path in sorted(self.data.rglob("*.json"))}
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
        _expect(len(w.snapshots()) >= 1, "erster Start publiziert einen Snapshot")

    def erste_antwort():
        w.answer(curve=0.95, shift=23.0)
        _expect(w.state().delivery.pending is None, "Antwort beendet den Tick")

    def tagestick():
        w.wall.set(datetime(2026, 1, 15, 12, 0, 30, tzinfo=BERLIN))
        w.advance(30)
        w.trigger("sensor.room_actual")
        _expect(w.snapshots()[-1]["trigger"] == "daily", "Tagestick")
        w.answer(curve=1.0, shift=23.5)

    def soll_senken():
        w.set_room_target(20.5)
        _expect(w.snapshots()[-1]["trigger"] == "target_change", "Tick bei Soll-Aenderung")
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


#: Spec 5c 5: erlaubte Unterschiede zwischen dem Master von 0.36 und 1.0.0, einzeln; alles andere ist ein Fehler.
ERLAUBT = (
    "abo_abfrage: ['accounts', 'status', …] fehlt (Abo aus dem Konfigurationsdokument)",
    "mqtt_client: ['mqtt', 'loop_start'|'stop'] fehlt (Link des Geraetekerns)",
    "snapshot: ['mqtt', 'snapshot', p] -> ['link', 'up/snapshot', p]",
    "telemetrie: ['mqtt', 'telemetry', p] -> ['link', 'telemetry', p]",
    "status: smartheat_status Schema 2 -> 3 (Geraete-Felder dazu, Laufzeit-Felder gleich)",
    "dateien: runtime/room_target.json und runtime/abgemeldet.json neu; Zugangsoptionen fehlen",
    "setup_id: Statusfeld entfaellt (Stufe 3 S7); in backup.json heisst das Feld tenant_id (= Anlage 'golden')",
    "addon_version: 0.36.0 -> 1.0.0 (ADDON_VERSION)",
    "write_budget: boost_start/emergency_start nach erfolgreichem Start (Fixrunde A, A6-01)",
    "benachrichtigung: notify.mobile_app_* (Fixrunde A, A6-02)",
    "status_start: Status 'startet' statt 'notbetrieb' beim Start mit veraltetem Notbetrieb (Fixrunde A, L4-09)",
)
#: Die Schritte, in denen die Laufzeit mit veraltetem Notbetrieb neu startet (ERLAUBT "status_start"); nur dort
#: normalisiert normalisieren() "startet" + notbetrieb zu "notbetrieb".
STATUS_START_SCHRITTE = ("abo_erneut_inaktiv", "abmelden")
#: Schluessel im Schreibbudget (backup.json), die erst seit Fixrunde A auch nach einem erfolgreichen Start stehen.
START_ZAEHLER = ("boost_start", "emergency_start")
#: Was 0.36 aus den Optionen nahm und 1.0.0 aus dem Dokument bzw. der Version (ERLAUBT, letzte zwei Zeilen).
ALT_ADDON_VERSION = "0.36.0"
#: Der Benachrichtigungsdienst im Master von 0.36 und seit Fixrunde A (ERLAUBT "benachrichtigung"): nur noch Handy-Dienste.
ALT_DIENST = "notify.handy"
NEU_DIENST = "notify.mobile_app_handy"
#: Felder, die Schema 3 dem Status-Ereignis hinzufuegt (ERLAUBT "status"); woertlich, nicht aus geraet_status gelesen.
SCHEMA_3_GERAET_FELDER = ("geraet_id", "geraet_zustand", "server_verbunden", "raum_soll", "geraet_hinweise")


def _neu_benannt(value, *, alt: bool):
    """Nur im Master von 0.36: setup_id entfaellt (in backup.json, erkennbar an plant_id: -> tenant_id = Anlage),
    addon_version -> aktuelle Version (rekursiv)."""
    if not alt:
        return value
    if isinstance(value, dict):
        result = {k: _neu_benannt(v, alt=alt) for k, v in value.items()}
        if "setup_id" in result:
            del result["setup_id"]
            if "plant_id" in result:
                result["tenant_id"] = OPTIONS["tenant_id"]
        if result.get("addon_version") == ALT_ADDON_VERSION:
            result["addon_version"] = ADDON_VERSION
        return result
    if isinstance(value, list):
        return [_neu_benannt(v, alt=alt) for v in value]
    return value


def _ohne_start_zaehler(value):
    """Nur im neuen Master: die Start-Zaehler des Schreibbudgets (ERLAUBT "write_budget") entfernen, ein dadurch leeres
    write_budget faellt weg (der alte Master hat dort keinen Schluessel)."""
    if isinstance(value, dict):
        result = {k: _ohne_start_zaehler(v) for k, v in value.items()}
        budget = result.get("write_budget")
        if isinstance(budget, dict):
            rest = {k: v for k, v in budget.items() if k not in START_ZAEHLER}
            if rest:
                result["write_budget"] = rest
            else:
                del result["write_budget"]
        return result
    if isinstance(value, list):
        return [_ohne_start_zaehler(v) for v in value]
    return value


def normalisieren(record: list[dict], *, alt: bool) -> list[dict]:
    """Bringt beide Master auf eine Form, in der nur noch fachliche Wirkungen stehen (ERLAUBT oben). alt: der Master von
    0.36 (setup_id und addon_version auf die Werte von 1.0.0 umgeschrieben)."""
    result = []
    for schritt in _neu_benannt(record if alt else _ohne_start_zaehler(record), alt=alt):
        wirkungen = []
        for w in schritt["wirkungen"]:
            if w[0] == "accounts" or w[:2] in (["mqtt", "loop_start"], ["mqtt", "stop"]):
                continue
            if alt and w[:2] == ["ha", "notify"] and w[2] == ALT_DIENST:
                w = ["ha", "notify", NEU_DIENST, *w[3:]]
            if w[:2] == ["link", "up/snapshot"]:
                w = ["mqtt", "snapshot", w[2]]
            elif w[0] == "link":
                w = ["mqtt", w[1], w[2]]
            if w[:3] == ["ha", "event", "smartheat_status"]:
                # Schema 2 -> 3: nur das Schema und genau die Geraete-Felder von Schema 3 (im neuen Master); ein
                # spaeter entfallenes Laufzeit-Feld faellt so als Unterschied auf.
                weg = {"schema"} if alt else {"schema", *SCHEMA_3_GERAET_FELDER}
                w = ["ha", "event", "smartheat_status", {k: v for k, v in w[3].items() if k not in weg}]
                if (not alt and schritt["schritt"] in STATUS_START_SCHRITTE
                        and w[3]["status"] == "startet" and w[3]["notbetrieb"] is True):
                    w[3]["status"] = "notbetrieb"  # ERLAUBT "status_start": nur im neuen Master, nur in diesen Schritten
            wirkungen.append(w)
        dateien = {name: inhalt for name, inhalt in schritt["dateien"].items()
                   if Path(name).name not in ("room_target.json", "abgemeldet.json", "discovery.json")}
        result.append({"schritt": schritt["schritt"], "wirkungen": wirkungen, "dateien": dateien})
    return result


def render(record: list[dict]) -> str:
    return json.dumps(record, ensure_ascii=False, indent=1) + "\n"


_VERSION_RE = re.compile(r'"addon_version": "[^"]+"')


def render_ohne_version(record: list[dict]) -> str:
    """A5-65: die Versionsnummer ist kein Verhalten; ohne sie muss der Master nicht bei jedem Release neu geschrieben
    werden."""
    return _VERSION_RE.sub('"addon_version": "<version>"', render(record))

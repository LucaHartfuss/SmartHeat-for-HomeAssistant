"""Gemeinsame Hilfen der Laufzeit-Tests (Fixrunde B TP4, A6-90; vorher in test_runtime.py): Start der Laufzeit auf dem
Konfigurationsdokument aus OPTIONS (geraet_helpers.start_bridge) mit Fake-Uhr (tests/conftest.py), Fake-HA,
Fake-Trigger-Client und dem Link einer Geraete-Attrappe, dazu die Handgriffe der Szenarien (Trigger, Antworten, Uhr
vorstellen). Die Fixture `env` steht in conftest.py. Hier stehen die Stellen, die Interna der Bruecke kennen; die
Szenarien der Testdateien pruefen nur die Aussensicht: HA-Writes, Pushes, Nachrichten des Links, Dateiinhalte."""
import logging
import math
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import geraet_helpers
from fakes import VIESSMANN_OPTIONEN, VIESSMANN_ZUSTAND, FakeHa, trigger_klasse
from geraet_helpers import konfiguration_aus_optionen

from heizungsbruecke.derived_sensors import DerivedSensors
from smartheat_runtime import dokument_config, entitlement
from smartheat_runtime.backup_store import load_backup, save_backup
from smartheat_runtime.runtime import Runtime

OPTIONS = {
    "tenant_id": "test_tenant",
    "verteilsystem": "Heizkoerper",  # Clamps 0.4-1.5 / 15-25 (Mindestvorlauf 20-30), Boost 1.5/25
    "daily_trigger_time": "12:00",  # Tagestick 12:00
    "room_sensors": ["sensor.room_actual"],
    "entity_room_target": "sensor.room_target",
    "entity_curve_current": "number.curve_current",
    "entity_shift_current": "number.shift_current",
    "entity_min_flow": "number.min_flow",
    "entity_outdoor_temp": "sensor.outdoor_temp",
    "entity_heat_limit": "number.heat_limit",
    "notify_services": ["notify.mobile_app_handy"],
}

DERIVED: dict[str, str] = {}

WEISHAUPT = {
    "lever_set": "weishaupt_wwp", "entity_curve_current": "number.hk", "entity_shift_current": "number.normal",
    "entity_heat_limit": "number.swu", "entity_mode_select": "select.betriebsart",
    "entity_setpoint_comfort": "number.komfort", "entity_setpoint_setback": "number.absenk", "entity_min_flow": "",
}
WEISHAUPT_STATES = {
    "number.hk": 0.75, "number.normal": 20.0, "number.swu": 18.0, "select.betriebsart": "hz_operationmode_automatic",
    "number.komfort": 22.0, "number.absenk": 18.0,
}


#: Entity -> Hebel (Bereich in LocalSafety.ranges) je Hebelsatz der Laufzeit-Tests; Werte der Auswahl- und Textentities
#: sind keine Zahlen und stehen nicht in der Tabelle
SAETZE = {
    "vaillant_vrc720": (
        {}, {}, {"number.curve_current": "curve", "number.shift_current": "room_setpoint",
                 "number.heat_limit": "heat_limit", "number.min_flow": "min_flow"}),
    "viessmann_vicare": (
        VIESSMANN_OPTIONEN, VIESSMANN_ZUSTAND, {"number.slope": "curve", "number.shift": "level",
                                                "number.normal_temperature": "room_setpoint"}),
    "weishaupt_wwp": (
        WEISHAUPT, WEISHAUPT_STATES, {"number.hk": "curve", "number.normal": "room_setpoint",
                                      "number.komfort": "room_setpoint", "number.absenk": "room_setpoint",
                                      "number.swu": "heat_limit"}),
}


#: Ziele ohne Zahl (Auswahl- und Betriebsart-Entities): jedes andere Ziel muss in der Zuordnung stehen
NICHT_NUMERISCH = {"select.betriebsart", "climate.heizkreis::preset_mode"}


_OMIT = object()


class _Link:
    """Sicht der Tests auf den Link der Laufzeit (bis 0.36 ein eigener MQTT-Client): gesendete Snapshots und
    Telemetrie, Antworten des Servers ueber channel.setpoints, Verbindung des Links."""

    def __init__(self, geraet) -> None:
        self._geraet = geraet

    @property
    def snapshots(self):
        return self._geraet.sent.of("up/snapshot")

    @property
    def telemetry(self):
        return self._geraet.sent.of("telemetry")

    @property
    def connected(self):
        return self._geraet.sent.connected

    @connected.setter
    def connected(self, value):
        self._geraet.sent.connected = value

    @property
    def connect_failures(self):
        return self._geraet.sent.failures

    @connect_failures.setter
    def connect_failures(self, value):
        self._geraet.sent.failures = value

    @property
    def publish_error(self):
        return self._geraet.sent.publish_error

    @publish_error.setter
    def publish_error(self, value):
        self._geraet.sent.publish_error = value

    @property
    def stopped(self):
        return self._geraet.channel._stopped

    def verbunden(self):
        """Der Link des Geraets ist (wieder) verbunden (Geraete-Thread)."""
        self._geraet.channel.verbunden()

    def answer(self, seq, status="ok", curve=0.95, shift=23.0, reason=None, schema=4, heat_limit=16.0, levers=None):
        """Server-Antwort (Schema 4) ueber den Link einspeisen. levers=None baut die Hebel aus curve/shift/heat_limit
        (ein Wert _OMIT laesst den Hebel weg); schema=_OMIT bzw. levers=_OMIT: Feld fehlt."""
        if levers is None:
            levers = {
                name: value for name, value in (("curve", curve), ("room_setpoint", shift), ("heat_limit", heat_limit))
                if value is not _OMIT
            }
        payload = {
            "schema": schema, "seq": seq, "ts": "x", "status": status, "reason": reason, "levers": levers,
            "learned": {"curve": 1.0, "heat_limit": 16.0},
        }
        if schema is _OMIT:
            del payload["schema"]
        if levers is _OMIT:
            del payload["levers"]
        self._geraet.channel.setpoints(payload)

    def answer_raw(self, seq, **fields):
        payload = {"schema": 4, "seq": seq, "ts": "x", "status": "ok", "reason": None,
                   "levers": {"curve": 0.95, "room_setpoint": 23.0, "heat_limit": 16.0}, "learned": None, **fields}
        self._geraet.channel.setpoints(payload)


# --- Port-Fakes am Link (RuntimeChannel); ihre Signaturen bindet test_fake_gleichlauf.py an die echte Klasse ---

def loop_start_aufzeichnen(order, wert):
    """Ersatz fuer RuntimeChannel.loop_start: haelt beim MQTT-Start `wert()` in order fest (Reihenfolge am Port)."""
    def loop_start(self):
        order.append(wert())
    return loop_start


def verbindung_aus(link):
    """Ersatz fuer RuntimeChannel.is_connected: die Verbindung des Links steht in link["up"]."""
    def is_connected(self):
        return link["up"]
    return is_connected


# --- Harness: Start der Laufzeit und Handgriffe der Szenarien ---

def _abo_status(env):
    env.abo["queries"] += 1
    if isinstance(env.abo["status"], Exception):  # die Abo-Quelle wirft unerwartet
        raise env.abo["status"]
    return env.abo["status"]


def _start_bridge(env, **option_overrides):
    bridge, env.geraet = geraet_helpers.start_bridge(env, options=OPTIONS, abo=lambda: _abo_status(env),
                                                  **option_overrides)
    return bridge


def _links(env):
    """Die Links, die die Laufzeit seit dem letzten Start gebunden hat (leer: ohne Verbindung zum Server)."""
    return env.geraet.channels if env.geraet is not None else []


def _is_running(bridge) -> bool:
    return isinstance(bridge, Runtime)


def _delivery(bridge):
    return bridge.store.state.delivery


def _raise_oserror(*args, **kwargs):
    raise OSError("Datentraeger kaputt")


def _break_backup_writes(monkeypatch) -> None:
    monkeypatch.setattr("smartheat_runtime.backup_store.save_backup", _raise_oserror)


def _quiet_backup(env, **extra):
    """Backup, bei dem beim Start weder ein Tick faellig ist noch ein Boost startet."""
    save_backup(env.paths["BACKUP_PATH"], {
        "last_room_target": 21.0, "last_published_target_rt": 21.0,
        "last_daily_trigger_date": datetime.now().date().isoformat(),
        "restore_point": {"curve": 0.9, "room_setpoint": 22.0}, **extra,
    })


def _start(env, **option_overrides):
    bridge = _start_bridge(env, **option_overrides)
    assert _is_running(bridge)
    bridge.worker.run_pending()
    return bridge


def _mqtt(env):
    return _Link(env.geraet)


def _backup(env):
    return load_backup(env.paths["BACKUP_PATH"])


def _failsafe_file(env):
    return load_backup(env.paths["FAILSAFE_PATH"])


def _trigger(env, bridge, entity_id):
    """HA meldet eine Zustandsaenderung (to_state mit dem Wert aus env.ha.states). Die Soll-Entity geht als Eingabe der
    Soll-Quelle in den Soll-Eingang (Entprellung 0 s: den for: 10 s hat der HA-Trigger schon), alles andere ist ein
    lokaler Check."""
    value = env.ha.states.get(entity_id)
    env.trigger_clients[-1].kwargs["on_trigger_event"]({
        "platform": "state", "entity_id": entity_id, "attribute": None,
        "to_state": {"state": value if isinstance(value, int | float | str) else "unavailable"}})
    bridge.worker.run_pending()


def _set_room_target(env, bridge, value):
    env.ha.states["sensor.room_target"] = value
    _trigger(env, bridge, "sensor.room_target")


def _ha_verbunden(env, bridge):
    """Der HA-Trigger-Client ist (wieder) verbunden: Soll-Entity lesen, lokaler Check mit frischem Raum-Soll (so kommt
    nach dem Start der erste Tick; bis 0.36 tat das auch ein Bericht der Soll-Entity ohne Aenderung), voller Status."""
    env.trigger_clients[-1].kwargs["on_connected"]()
    bridge.worker.run_pending()


def _advance(env, bridge, seconds):
    env.clock.advance(seconds)
    bridge.worker.run_pending()


def _answer(env, bridge, seq, **kwargs):
    _mqtt(env).answer(seq, **kwargs)
    bridge.worker.run_pending()


def _last_event(env):
    return [data for kind, data in env.ha.events if kind == "smartheat_status"][-1]


def _connect(env, bridge):
    _mqtt(env).verbunden()
    bridge.worker.run_pending()


def _frische_umgebung(verzeichnis: Path, mp, clock):
    """Umgebung der Laufzeit-Tests (Dateipfade in verzeichnis, Fake-HA, Fake-Trigger-Client, Abo-Status), gestartet mit
    _start_bridge bzw. _start. Die einzige Quelle: die Fixture `env` (conftest.py) baut sich hiermit, hypothesis ruft
    sie je Beispiel direkt (ein Beispiel = eine Umgebung). `derived["replaced"]` setzt der Test fuer einen Quellwechsel
    der Hilfssensoren; `abo["queries"]` zaehlt die Abo-Pruefungen der Laufzeit (Boot, Tick-Zustellung, Fristende)."""
    paths = {}
    for name in ("BACKUP_PATH", "FAILSAFE_PATH", "ENTITLEMENT_PATH", "DERIVED_SENSORS_PATH", "AUFRAEUMEN_PATH",
                 "RASTUNG_PATH"):
        paths[name] = verzeichnis / f"{name.lower()}.json"
        mp.setattr(f"heizungsbruecke.config.{name}", paths[name])
    laufzeit = verzeichnis / "runtime"
    for name, wert in (("RUNTIME_DIR", laufzeit), ("ROOM_TARGET_PATH", laufzeit / "room_target.json"),
                       ("ABGEMELDET_PATH", laufzeit / "abgemeldet.json")):
        paths[name] = wert
        mp.setattr(f"heizungsbruecke.config.{name}", wert)
    derived = {"replaced": ()}
    mp.setattr("heizungsbruecke.derived_sensors.ensure_all",
               lambda **kwargs: DerivedSensors({**DERIVED, "room_actual": "sensor.room_actual"}, derived["replaced"],
                                               "fp1"))
    trigger = []
    mp.setattr("heizungsbruecke.geraet_trigger.HaTriggerClient", trigger_klasse(trigger))
    return SimpleNamespace(clock=clock, paths=paths, abo={"status": entitlement.ACTIVE, "queries": 0},
                           ha=FakeHa(), trigger_clients=trigger, derived=derived, geraet=None)


def _endlich(wert) -> bool:
    if isinstance(wert, bool) or not isinstance(wert, int | float):
        return False
    try:
        return math.isfinite(float(wert))
    except OverflowError:
        return False  # riesige Ganzzahl


class _Fehlerlog(logging.Handler):
    """Fehler des Regel-Workers (der Worker faengt Ausnahmen ab und loggt sie; ein Fehler dort ist ein Fund)."""

    def __init__(self):
        super().__init__(logging.ERROR)
        self.eintraege = []

    def emit(self, record):
        self.eintraege.append(record.getMessage())


def ankunftsschwelle_der_laufzeit(verzeichnis) -> float:
    """Ankunftsschwelle des Comfort-Boosts (zugleich Austrittsschwelle des Notfall-Boosts, regulation.run_local_check)
    aus der echten RuntimeConfig (dokument_config.runtime_config) des Dokuments aus OPTIONS (Vaillant x
    Heizkoerper wie client1); verzeichnis ohne Rastung."""
    konfiguration = konfiguration_aus_optionen(OPTIONS)
    runtime = dokument_config.runtime_config(konfiguration, rastung_pfad=verzeichnis / "rastung.json",
                                             entitlement_path=verzeichnis / "entitlement.json",
                                             abo_source=lambda: entitlement.ACTIVE)
    return runtime.local_safety.arrival_threshold_k

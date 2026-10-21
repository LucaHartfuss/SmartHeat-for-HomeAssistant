"""Betrieb der Laufzeit ueber den Regel-Worker (Design-Spec 2026-09-26) im Geraete-Pfad (Spec 5c 1.3): Boot,
Tick-Zustellung, Datenfehler, Notbetrieb, lokale Checks und Abo-Pfade. Getrieben ueber _start_bridge (Laufzeit auf dem
Konfigurationsdokument aus OPTIONS, geraet_helpers.start_bridge) mit Fake-Uhr (tests/conftest.py), Fake-HA,
Fake-Trigger-Client und dem Link einer Geraete-Attrappe (FakeGeraet) statt eines eigenen MQTT-Clients."""
import dataclasses
import json
import logging
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import geraet_helpers
import pytest
from bindung_beispiele import VAILLANT_MYPYLLANT
from fakes import (
    FREMDTEXT,
    FREMDTEXT_IM_LOG,
    VIESSMANN_OPTIONEN,
    VIESSMANN_ZUSTAND,
    FakeHa,
    logzeilen,
    trigger_klasse,
)

from heizungsbruecke import geraet_status
from heizungsbruecke.derived_sensors import DerivedSensors
from heizungsbruecke.version import ADDON_VERSION
from smartheat_core.enforce import RETRY_SECONDS
from smartheat_core.pipeline import LeverPipeline
from smartheat_device.laufzeit import RuntimeChannel
from smartheat_runtime import abo, app, aufraeumen, backup_store, datentraeger, dokument_config, entitlement, ticks
from smartheat_runtime.backup_store import load_backup, save_backup
from smartheat_runtime.delivery import DataFault, DeliveryState
from smartheat_runtime.runtime import Runtime
from smartheat_runtime.texts import HA_TEXTS

SETTLE = VAILLANT_MYPYLLANT.settle_seconds

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

NOTBETRIEB_ON = "Heizungsbrücke: Server antwortet nicht, Notbetrieb aktiv. Die Heizung wird bei Bedarf lokal abgesichert."


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


@pytest.fixture
def env(tmp_path, monkeypatch, clock):
    paths = {}
    for name in ("BACKUP_PATH", "FAILSAFE_PATH", "ENTITLEMENT_PATH", "DERIVED_SENSORS_PATH", "AUFRAEUMEN_PATH",
                 "RASTUNG_PATH"):
        paths[name] = tmp_path / f"{name.lower()}.json"
        monkeypatch.setattr(f"heizungsbruecke.config.{name}", paths[name])
    runtime_dir = tmp_path / "runtime"
    for name, value in (("RUNTIME_DIR", runtime_dir), ("ROOM_TARGET_PATH", runtime_dir / "room_target.json"),
                        ("ABGEMELDET_PATH", runtime_dir / "abgemeldet.json")):
        paths[name] = value
        monkeypatch.setattr(f"heizungsbruecke.config.{name}", value)
    env_derived = {"replaced": ()}
    monkeypatch.setattr(
        "heizungsbruecke.derived_sensors.ensure_all",
        lambda **kwargs: DerivedSensors({**DERIVED, "room_actual": "sensor.room_actual"}, env_derived["replaced"], "fp1"),
    )
    # Abo-Status des Konfigurationsdokuments: die Laufzeit liest ihn bei jeder Abo-Pruefung (Boot, Tick-Zustellung,
    # Fristende) frisch; queries zaehlt diese Pruefungen.
    abo_state = {"status": entitlement.ACTIVE, "queries": 0}
    trigger_clients = []
    monkeypatch.setattr("heizungsbruecke.geraet_trigger.HaTriggerClient", trigger_klasse(trigger_clients))
    return SimpleNamespace(
        clock=clock, paths=paths, abo=abo_state, ha=FakeHa(), trigger_clients=trigger_clients, derived=env_derived,
        geraet=None,
    )


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


# --- Harness ---
# Einzige Stellen dieser Datei, die Interna der Bruecke kennen. Der Umbau (TP5, Spec 5c) passt nur
# diesen Abschnitt und die Fixture `env` an; die Szenarien darunter pruefen nur die
# Aussensicht: HA-Writes, Pushes, Nachrichten des Links, Dateiinhalte.

ABO_ENDED = (
    "SmartHeat: Abo seit 30 Tagen inaktiv. Die Heizungssteuerung ist beendet, "
    "die zuletzt gelernten Werte bleiben eingestellt."
)


def _abo_status(env):
    env.abo["queries"] += 1
    if isinstance(env.abo["status"], Exception):  # die Abo-Quelle wirft unerwartet
        raise env.abo["status"]
    return env.abo["status"]


def _start_bridge(env, **option_overrides):
    bridge, env.geraet = geraet_helpers.start_bridge(env, abo=lambda: _abo_status(env), **option_overrides)
    return bridge


def _links(env):
    """Die Links, die die Laufzeit seit dem letzten Start gebunden hat (leer: ohne Verbindung zum Server)."""
    return env.geraet.channels if env.geraet is not None else []


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


def _override_comfort_boost(bridge, **values) -> None:
    """Comfort-Boost-Werte je Hebel abweichend von LocalSafety setzen (bis 0.29.0 ueber die Optionen
    boost_curve_value/boost_shift_value), damit die Tests Comfort- und Notfall-Zeile unterscheiden."""
    safety = bridge.override.safety
    bridge.override._safety = dataclasses.replace(safety, comfort_boost={**safety.comfort_boost, **values})


def _raise_oserror(*args, **kwargs):
    raise OSError("Datentraeger kaputt")


def _break_backup_writes(monkeypatch) -> None:
    monkeypatch.setattr("smartheat_runtime.backup_store.save_backup", _raise_oserror)


def _fail_next_snapshot_read(monkeypatch, error: Exception) -> None:
    original = ticks.read_snapshot
    failures = [error]

    def _flaky(*args, **kwargs):
        if failures:
            raise failures.pop()
        return original(*args, **kwargs)

    monkeypatch.setattr("smartheat_runtime.ticks.read_snapshot", _flaky)


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


def _pruefintervall(monkeypatch, sekunden):
    """Kurzes Pruefintervall wie in der E2E (Plan S8a-1 P2: Umgebung des Geraets, nicht das Konfigurationsdokument)."""
    monkeypatch.setenv(dokument_config.E2E_INTERVALLE_ENV, json.dumps({"pruefung_s": sekunden}))


def _regulation_writes(env):
    """Schreibvorgaenge ohne den Mindestvorlauf, der jeder Raum-Soll-Aenderung folgt (min_flow.py)."""
    return [write for write in env.ha.writes if write[0] != "number.min_flow"]


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


# --- Boot ---

def test_start_captures_the_heat_limit_original_before_any_write(env):
    """TP12h: der Start merkt den Ursprungswert der Heizgrenze, auch wenn danach nichts geschrieben wird."""
    _quiet_backup(env)
    _start(env)
    assert env.ha.writes == []
    assert _backup(env)["originals"] == {"heat_limit": 16.0}


def test_a_failing_capture_of_the_heat_limit_original_does_not_stop_the_start(env, monkeypatch, caplog):
    """Scheitert das Merken beim Start (Speicherfehler), laeuft der Start weiter: der lokale Check danach startet den
    Notfall-Boost (Notbetrieb, Raum 1,5 K unter dem Soll)."""
    echt = LeverPipeline.capture_originals
    fehler = [ticks.StorageError("Datentraeger voll")]

    def _beim_start_kaputt(self):  # Fehlerinjektion: nur der erste Aufruf (im Start) scheitert
        if fehler:
            raise fehler.pop()
        return echt(self)

    monkeypatch.setattr(LeverPipeline, "capture_originals", _beim_start_kaputt)
    with caplog.at_level(logging.ERROR, logger="smartheat_runtime.app"):
        _start_in_notbetrieb_with_emergency_boost(env)  # prueft die Notfall-Boost-Werte 1,5/25 auf der Anlage
    assert fehler == []
    assert "Ursprungswert der Heizgrenze beim Start nicht gespeichert" in caplog.text


def test_start_reports_originals_to_the_host_only_on_change(env):
    _quiet_backup(env, originals={"heat_limit": 16.0})
    bridge = _start(env)
    assert env.geraet.gemeldet[-1] == {"heat_limit": 16.0}
    anzahl = len(env.geraet.gemeldet)
    _advance(env, bridge, 60)  # weiteres Ereignis ohne Aenderung der Ursprungswerte
    assert len(env.geraet.gemeldet) == anzahl
    bridge.store.update(aux_originals={"mode_select": "hz_operationmode_automatic"})
    _advance(env, bridge, 60)  # ein Ereignis danach meldet die Aenderung (der Worker kennt nur einen Rueckruf)
    assert env.geraet.gemeldet[-1] == {"heat_limit": 16.0, "mode_select": "hz_operationmode_automatic"}


def test_start_runs_priming_check_before_mqtt_loop_start(env, monkeypatch):
    _quiet_backup(env)
    order = []
    read_state = env.ha.get_state

    def _spy(entity_id):
        if entity_id == "sensor.room_actual":
            order.append("check")
        return read_state(entity_id)

    env.ha.get_state = _spy
    monkeypatch.setattr(RuntimeChannel, "loop_start", loop_start_aufzeichnen(order, lambda: "loop_start"))

    _start(env)

    assert order[:2] == ["check", "loop_start"]


def test_boot_prepares_zone_before_first_snapshot(env, monkeypatch):
    # Plan-Praezisierung 11: Zone meldet 0 (heizt gerade nicht) und steht nicht auf Manuell.
    env.ha.states.update({"climate.zone": "auto", "climate.zone::temperature": 0.0})
    order = []
    monkeypatch.setattr(RuntimeChannel, "loop_start", loop_start_aufzeichnen(order, lambda: list(env.ha.writes)))

    bridge = _start(env, entity_shift_current="climate.zone")

    assert order == [[("climate.zone", "heat_cool"), ("climate.zone::temperature", 21.0)]]  # vor MQTT
    assert env.ha.states["climate.zone::temperature"] == 21.0  # Raum-Soll als Startwert
    assert _backup(env)["restore_point"]["room_setpoint"] == 21.0

    _ha_verbunden(env, bridge)  # erster Tick

    assert _mqtt(env).snapshots[0]["levers"]["room_setpoint"] == 21.0


def test_boot_keeps_a_usable_zone_shift(env):
    env.ha.states.update({"climate.zone": "heat_cool", "climate.zone::temperature": 20.5})

    _start(env, entity_shift_current="climate.zone")

    assert env.ha.writes == []


def _backup_before_tp11(env, **extra):
    """backup.json von 0.23.0: Wiederherstellungspunkt nur mit Steigung (am Anschlag), keine
    Parallelverschiebung; beim Start waere sonst kein Tick faellig. Im Schema ab 0.30.0 (restore_point mit Hebel
    curve); die Altmigration der Rollen-Schluessel bis 0.29.0 entfiel mit Audit 4 P-E."""
    backup = {
        "last_room_target": 21.0, "last_published_target_rt": 21.0,
        "last_daily_trigger_date": datetime.now().date().isoformat(),
        "restore_point": {"curve": 1.5}, **extra,
    }
    save_backup(env.paths["BACKUP_PATH"], backup)


def test_first_start_without_shift_restore_point_reseeds_curve_and_ticks_at_once(env):
    # 0.23.0 -> 0.24.0: der alte Punkt (1.5, am Anschlag) darf den von Hand gesetzten Anlagenwert
    # nicht per Durchsetzung ueberschreiben, und der Erstkontakt muss mit den Istwerten starten.
    _backup_before_tp11(env)
    env.ha.states.update({"number.curve_current": 1.05, "sensor.room_actual": 21.0})

    bridge = _start(env)

    assert _backup(env)["restore_point"]["curve"] == 1.05
    _trigger(env, bridge, "sensor.room_actual")  # naechster lokaler Check
    snapshot = _mqtt(env).snapshots[0]
    assert (snapshot["trigger"], snapshot["levers"]["curve"]) == ("target_change", 1.05)

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

    assert _backup(env)["restore_point"]["curve"] == seeded


def test_first_start_drops_the_old_curve_point_when_the_plant_is_unreadable(env):
    _backup_before_tp11(env)
    env.ha.states["number.curve_current"] = ValueError("unavailable")

    _start(env)

    assert "curve" not in _backup(env).get("restore_point", {})


def test_first_start_during_a_boost_keeps_the_curve_point(env):
    # Die Anlage steht auf Boost-Werten: der Live-Wert ist kein Wiederherstellungspunkt.
    _backup_before_tp11(env, boost_active=True)
    env.ha.states.update({"number.curve_current": 1.5, "sensor.room_actual": 20.0})

    _start(env)

    assert _backup(env)["restore_point"]["curve"] == 1.5


def test_restart_with_shift_restore_point_keeps_curve_and_does_not_tick(env):
    _quiet_backup(env)
    env.ha.states["number.curve_current"] = 1.05
    bridge = _start(env)

    _trigger(env, bridge, "sensor.room_actual")

    assert _backup(env)["restore_point"]["curve"] == 0.9
    assert _mqtt(env).snapshots == []


def test_zone_preparation_failed_at_start_is_retried_on_the_next_local_check(env):
    env.ha.states.update({"climate.zone": "unavailable", "climate.zone::temperature": 0.0})
    bridge = _start(env, entity_shift_current="climate.zone")
    assert env.ha.writes == []

    env.ha.states["climate.zone"] = "auto"
    _trigger(env, bridge, "sensor.room_actual")
    assert env.ha.writes == []  # noch innerhalb von RETRY_SECONDS nach dem Startversuch

    env.clock.advance(RETRY_SECONDS)
    _trigger(env, bridge, "sensor.room_actual")

    assert env.ha.writes[:2] == [("climate.zone", "heat_cool"), ("climate.zone::temperature", 21.0)]
    assert _backup(env)["restore_point"]["room_setpoint"] == 21.0

    writes = len(env.ha.writes)
    env.ha.states["climate.zone"] = "auto"  # erfolgreich vorbereitet: kein weiterer Versuch
    _trigger(env, bridge, "sensor.room_actual")
    assert ("climate.zone", "heat_cool") not in env.ha.writes[writes:]


def test_zone_preparation_retried_after_the_own_switch_still_writes_the_start_value(env):
    """O-TP12e-1: der erste Versuch stellt die Zone auf Manuell, das Schreiben des Startwerts scheitert. Die
    Wiederholung nach 1800 s liegt im Fenster nach dem eigenen Wechsel (2100 s, keine zweite Umstellung) und die
    Zone meldet noch 0: der Startwert wird trotzdem geschrieben, nicht uebersprungen."""
    env.ha.states.update({"climate.zone": "auto", "climate.zone::temperature": 0.0})
    env.ha.write_error = RuntimeError("Cloud nicht erreichbar")  # set_hvac_mode gelingt, die Temperatur nicht
    bridge = _start(env, entity_shift_current="climate.zone")
    assert env.ha.writes == [("climate.zone", "heat_cool")]
    assert bridge.zone_prepared is False

    env.ha.write_error = None
    env.clock.advance(1800)  # Wiederholungs-Kontingent: 30 min nach dem Fehlschlag
    _trigger(env, bridge, "sensor.room_actual")

    assert env.ha.writes == [("climate.zone", "heat_cool"), ("climate.zone::temperature", 21.0)]
    assert _backup(env)["restore_point"]["room_setpoint"] == 21.0
    assert bridge.zone_prepared is True


class _QuotaZoneHa(FakeHa):
    """Zone im Zeitprogramm (auto, meldet 0); das Umstellen auf Manuell scheitert an der Hersteller-Cloud (403 "Quota
    Exceeded"). `attempts` haelt jeden Aufruf an der HA-Grenze mit der Zeit der Fake-Uhr fest. failures=None: scheitert
    immer, sonst nur die ersten `failures` Versuche."""

    def __init__(self, clock, failures=None):
        super().__init__()
        self._clock, self._failures = clock, failures
        self.attempts: list[float] = []
        self.states.update({"climate.zone": "auto", "climate.zone::temperature": 0.0})

    def set_hvac_mode(self, entity_id, mode):
        self.attempts.append(self._clock())
        if self._failures is None or len(self.attempts) <= self._failures:
            raise RuntimeError("403 Quota Exceeded")
        super().set_hvac_mode(entity_id, mode)


def _failing_zone_preparation(env, failures=None):
    """Die Vorbereitung der Zone (climate.zone) scheitert an HA; gibt die Versuchszeitpunkte zurueck. Danach mit
    `_start(env, entity_shift_current="climate.zone")` starten."""
    env.ha = _QuotaZoneHa(env.clock, failures)
    return env.ha.attempts


def _local_checks(env, bridge, hours):
    for _ in range(int(hours * 3600 / 300)):
        _advance(env, bridge, 300)
        _trigger(env, bridge, "sensor.room_actual")


def test_failing_zone_preparation_is_retried_at_most_every_30_min_and_6_times_a_day(env, caplog):
    attempts = _failing_zone_preparation(env)
    bridge = _start(env, entity_shift_current="climate.zone")

    with caplog.at_level(logging.WARNING, logger="smartheat_runtime.app"):
        _local_checks(env, bridge, hours=12)

    assert [later - attempts[0] for later in attempts] == [0, 1800, 3600, 5400, 7200, 9000]  # Start + 5 Wiederholungen
    assert sum("Tageslimit" in record.getMessage() for record in caplog.records) == 1


def test_zone_preparation_stops_retrying_after_success(env):
    attempts = _failing_zone_preparation(env, failures=2)
    bridge = _start(env, entity_shift_current="climate.zone")

    _local_checks(env, bridge, hours=6)

    assert [later - attempts[0] for later in attempts] == [0, 1800, 3600]
    assert env.ha.states["climate.zone"] == "heat_cool" and bridge.zone_prepared is True


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
    # Final-Review C1, Szenario B/C: 0.8 K unter Soll liegt zwischen Exit- (0.5) und Einstiegsschwelle (1.0). Ein
    # Notbetrieb im Betrieb hat immer einen offenen Tick (ohne ihn stammt er aus dem Abo-inaktiv-Modus und endet bei
    # aktivem Abo vor dem ersten Check, TP12b).
    _quiet_backup(env, emergency_boost_active=True)
    save_backup(env.paths["FAILSAFE_PATH"], {
        "failsafe_active": True, "datenfehler": None, "pending": {"seq": "alt-1", "trigger": "daily"},
    })
    env.ha.states.update({"number.curve_current": 1.5, "number.shift_current": 25.0, "sensor.room_actual": 20.2})

    bridge = _start(env)

    assert env.ha.writes == []
    assert _backup(env)["emergency_boost_active"] is True

    env.ha.states["sensor.room_actual"] = 20.6
    _trigger(env, bridge, "sensor.room_actual")

    assert (env.ha.states["number.curve_current"], env.ha.states["number.shift_current"]) == (0.9, 22.0)
    assert _backup(env)["emergency_boost_active"] is False


@pytest.mark.parametrize("content", ["[1, 2]", "{kaputt"])
def test_start_reads_a_broken_failsafe_file(env, content):
    # Review Focus 4.
    _quiet_backup(env)
    env.paths["FAILSAFE_PATH"].write_text(content)

    bridge = _start(env)

    assert _delivery(bridge) == DeliveryState()
    assert _mqtt(env).snapshots == []


def test_start_with_a_notbetrieb_of_the_0_15_format_probes_the_server(env):
    _quiet_backup(env)
    env.paths["FAILSAFE_PATH"].write_text('{"failsafe_active": true}')

    bridge = _start(env)

    assert _delivery(bridge).notbetrieb is False
    assert [s["trigger"] for s in _mqtt(env).snapshots] == ["target_change"]


def test_reactivated_abo_restart_ends_notbetrieb_silently_and_probes(env):
    _quiet_backup(env)
    save_backup(env.paths["FAILSAFE_PATH"], {"failsafe_active": True, "datenfehler": None, "pending": None})

    bridge = _start(env)

    assert _delivery(bridge).notbetrieb is False
    probe = _mqtt(env).snapshots[0]
    assert probe["trigger"] == "target_change"

    _answer(env, bridge, probe["seq"], curve=0.9, shift=22.0)

    assert _delivery(bridge).pending is None
    assert env.ha.pushes == []


def test_reactivated_abo_restart_writes_no_emergency_boost(env):
    """TP12b (1): nach dem Abo-inaktiv-Modus (Notbetrieb ohne offenen Tick) startet die Laufzeit mit wieder aktivem
    Abo; 1,5 K unter dem Soll (ueber der Einstiegsschwelle 1,0 K) darf kein kurzer Notfall-Boost auf die Anlage."""
    _quiet_backup(env)
    save_backup(env.paths["FAILSAFE_PATH"], {"failsafe_active": True, "datenfehler": None, "pending": None})
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    env.ha.states["sensor.room_actual"] = 19.5

    bridge = _start(env)
    _trigger(env, bridge, "sensor.room_actual")  # naechster lokaler Check

    assert _delivery(bridge).notbetrieb is False
    assert _backup(env)["emergency_boost_active"] is False
    assert env.ha.writes == []
    assert (env.ha.states["number.curve_current"], env.ha.states["number.shift_current"]) == (0.9, 22.0)


def test_reactivated_abo_restart_ends_a_running_emergency_boost_during_priming(env, monkeypatch):
    """TP12b (1), Fix-Runde 1: lief im Abo-inaktiv-Modus schon ein Notfall-Boost (Anlage auf 1,5/25), endet er beim
    Neustart mit aktivem Abo schon im Priming und der Wiederherstellungspunkt wird geschrieben, obwohl der Raum 1,5 K
    unter dem Soll liegt (die Hysterese gilt nur im Notbetrieb)."""
    _quiet_backup(env, emergency_boost_active=True)
    save_backup(env.paths["FAILSAFE_PATH"], {"failsafe_active": True, "datenfehler": None, "pending": None})
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    env.ha.states.update({"number.curve_current": 1.5, "number.shift_current": 25.0, "sensor.room_actual": 19.5})
    order = []
    monkeypatch.setattr(RuntimeChannel, "loop_start", loop_start_aufzeichnen(order, lambda: list(env.ha.writes)))

    bridge = _start(env)

    assert order == [[("number.curve_current", 0.9), ("number.shift_current", 22.0)]]  # vor dem MQTT-Start
    assert _backup(env)["emergency_boost_active"] is False
    assert _delivery(bridge).notbetrieb is False


def test_stale_notbetrieb_with_unknown_abo_stays_until_the_probe_is_answered_without_an_end_push(env):
    """TP12b (3): der Notbetrieb aus dem Abo-inaktiv-Modus wurde nie als Notbetrieb gemeldet (dort meldet die
    Abo-Meldung); bei Abo-Status UNKNOWN bleibt er bis zur Antwort, die ihn ohne "Notbetrieb beendet" beendet."""
    _quiet_backup(env)
    save_backup(env.paths["FAILSAFE_PATH"], {"failsafe_active": True, "datenfehler": None, "pending": None})
    env.abo["status"] = entitlement.UNKNOWN

    bridge = _start(env)

    assert _delivery(bridge).notbetrieb is True
    _answer(env, bridge, _mqtt(env).snapshots[0]["seq"], curve=0.9, shift=22.0)
    assert _delivery(bridge).notbetrieb is False
    assert env.ha.pushes == []


def _reported_stale_notbetrieb(env):
    """Notbetrieb ohne offenen Tick, dessen "Notbetrieb aktiv" vor dem Abo-inaktiv-Modus erschienen ist (Meldezustand
    und Text in backup.json), Start mit Abo-Status UNKNOWN."""
    _quiet_backup(env, notify_states={"notbetrieb": "aktiv"}, notify_messages={"notbetrieb": NOTBETRIEB_ON})
    save_backup(env.paths["FAILSAFE_PATH"], {"failsafe_active": True, "datenfehler": None, "pending": None})
    env.abo["status"] = entitlement.UNKNOWN
    return _start(env)


def test_reported_stale_notbetrieb_keeps_its_notification_while_the_probe_stays_unanswered(env):
    # TP12b (3), Fix-Runde 1 (a): die gemeldete Meldung bleibt stehen, solange der Notbetrieb laeuft.
    bridge = _reported_stale_notbetrieb(env)
    _advance(env, bridge, 30)
    _advance(env, bridge, 30)  # zwei Ack-Timeouts ohne Antwort

    assert _delivery(bridge).notbetrieb is True
    assert "smartheat_notbetrieb" not in env.ha.dismissed
    assert bridge.notifier.state("notbetrieb") == "aktiv"
    assert env.ha.pushes == []


def test_reported_stale_notbetrieb_ends_with_one_end_push_on_the_answer(env):
    # TP12b (3), Fix-Runde 1 (b): wie vor Task 6 entwarnt die Antwort einmal.
    bridge = _reported_stale_notbetrieb(env)
    _answer(env, bridge, _mqtt(env).snapshots[0]["seq"], curve=0.9, shift=22.0)

    assert _delivery(bridge).notbetrieb is False
    assert env.ha.pushes == ["Heizungsbrücke: Serververbindung wiederhergestellt, Notbetrieb beendet."]
    assert env.ha.dismissed.count("smartheat_notbetrieb") == 1


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
    _mqtt(env).verbunden()
    bridge.worker.run_pending()


def test_status_goes_from_startet_to_regelt_when_mqtt_connects(env):
    _quiet_backup(env)
    bridge = _start(env, verbunden=False)
    assert _status_states(env) == ["startet"]

    _mqtt(env).connected = True
    _connect(env, bridge)

    assert _status_states(env) == ["startet", "regelt"]
    assert _last_event(env)["addon_version"] == ADDON_VERSION


def test_status_is_abo_inaktiv_right_after_start_without_mqtt(env):
    _quiet_backup(env)
    env.abo["status"] = entitlement.INACTIVE

    _start(env)

    assert _links(env) == []
    assert _status_states(env) == ["startet", "abo_inaktiv"]


def test_status_carries_the_plant_of_the_document_as_tenant_id_only(env):
    # Spec 5c 1.3 (dokument_config): tenant_id = Anlage; setup_id entfiel mit Stufe 3 S7 (A5-63).
    _quiet_backup(env)
    _start(env)

    assert env.ha.events[0][1]["tenant_id"] == OPTIONS["tenant_id"]
    assert "setup_id" not in env.ha.events[0][1]


def test_status_is_sent_again_when_the_ha_connection_comes_back(env):
    _quiet_backup(env)
    bridge = _start(env)
    _connect(env, bridge)
    before = len(env.ha.events)

    _ha_verbunden(env, bridge)

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
        geraet_status.EVENT_SCHEMA, "test_tenant", "regelt", "keiner", "aktiv",
    )
    assert event["hebelsatz"] == "vaillant_vrc720"
    assert event["hebel"] == {"curve": 0.95, "room_setpoint": 23.0, "heat_limit": 16.0, "min_flow": 20.5}
    assert event["gelernt"] == {"curve": 1.0, "heat_limit": 16.0}
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

    _ha_verbunden(env, bridge)

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
    assert snapshot["room_target"] == 20.5
    assert set(snapshot["levers"]) == {"curve", "room_setpoint", "heat_limit"}
    assert ("number.min_flow", 20.5) in env.ha.writes  # Mindestvorlauf folgt dem Raum-Soll
    assert _regulation_writes(env) == [
        ("number.curve_current", 0.95), ("number.shift_current", 23.0), ("number.heat_limit", 16.0),
    ]
    assert _backup(env)["restore_point"]["curve"] == 0.95
    assert _delivery(bridge).pending is None
    assert env.ha.pushes == []


def test_skipped_answer_is_applied_like_ok(env):
    bridge = _start(env)
    _connect(env, bridge)
    _ha_verbunden(env, bridge)  # erster Tick (noch kein Soll gemeldet)
    _answer(env, bridge, _mqtt(env).snapshots[0]["seq"], status="skipped", curve=1.0, shift=20.5,
            reason="zu_wenig_daten")
    assert (env.ha.states["number.curve_current"], env.ha.states["number.shift_current"]) == (1.0, 20.5)
    assert bridge.store.state.delivery.datenfehler is None


def test_the_skipped_log_quotes_the_server_reason_encodable_and_on_one_line(env, caplog):
    """Testoffensive T6: auch der Grund einer Antwort skipped kommt vom Server und steht mit repr im Log."""
    bridge = _start(env)
    _connect(env, bridge)
    _ha_verbunden(env, bridge)
    seq = _mqtt(env).snapshots[0]["seq"]
    with caplog.at_level(logging.INFO, logger="smartheat_runtime.ticks"):
        _answer(env, bridge, seq, status="skipped", curve=1.0, shift=20.5, reason="\ud800\nzweite")
    (meldung,) = [r.getMessage() for r in caplog.records if "nicht gelernt" in r.getMessage()]
    assert meldung == f"Server hat fuer seq={seq} nicht gelernt ('\\ud800\\nzweite'), Werte unveraendert uebernommen"
    meldung.encode("utf-8")


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

    assert "room_actual" not in snapshot and "room_actual" not in snapshot["levers"]
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


@pytest.mark.parametrize("reason", ["\ud800", "a\udfffb", "erste Zeile\nWARNING gefaelscht"],
                         ids=["surrogat", "surrogat_im_text", "zeilenumbruch"])
def test_the_warning_about_a_rejected_answer_quotes_the_server_reason_encodable_and_on_one_line(env, caplog, reason):
    """Testoffensive T6 (Fund aus dem Profil voll): der Grund kommt vom Server. Die Warnung zitiert ihn mit repr: ein
    einzelnes Surrogat brach sonst die UTF-8-Ausgabe des Logs (Logging error, Zeile verloren; unter xdist INTERNALERROR),
    ein Zeilenumbruch faelschte eine zweite Logzeile."""
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]

    with caplog.at_level(logging.WARNING, logger="smartheat_runtime.ticks"):
        _answer(env, bridge, seq, status="rejected", curve=None, shift=None, reason=reason)

    (meldung,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Server hat Snapshot")]
    assert meldung == f"Server hat Snapshot (seq={seq}) abgelehnt: {reason!r}"
    meldung.encode("utf-8")  # wirft bei einem einzelnen Surrogat
    assert "\n" not in meldung


def test_the_warning_about_unwritable_server_values_has_the_error_text_from_ha_only_escaped(env, caplog):
    """T6-Rest, R-c: scheitert das Schreiben der Serverwerte, traegt DeviceWriteError den Fehlertext von HA; in der
    Warnung steht er nur als Escape (Zeilenumbruch, U+202E, U+061C)."""
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]
    env.ha.write_error = RuntimeError(FREMDTEXT)

    with caplog.at_level(logging.WARNING, logger="smartheat_runtime.ticks"):
        _answer(env, bridge, seq, curve=0.95, shift=23.0)

    assert [zeile for zeile in logzeilen(caplog) if zeile.startswith(f"Serverwerte (seq={seq})")] == [
        f"Serverwerte (seq={seq}) konnten nicht auf die Anlage geschrieben werden: "
        f"curve (number.curve_current): {FREMDTEXT_IM_LOG}"]


@pytest.mark.parametrize("answer", [
    {"status": "maybe"},
    {"status": "ok", "curve": float("nan")},
    {"status": "ok", "curve": None},
    # JSON liefert beliebig grosse Ganzzahlen; sie passen in keinen float und gelten als ungueltig (kein OverflowError)
    pytest.param({"status": "ok", "curve": 10**400}, id="curve-gross-positiv"),
    pytest.param({"status": "ok", "curve": -10**400}, id="curve-gross-negativ"),
    pytest.param({"status": "ok", "shift": 10**400}, id="shift-gross-positiv"),
    pytest.param({"status": "ok", "heat_limit": -10**400}, id="heat_limit-gross-negativ"),
])
def test_invalid_answer_counts_as_server_fault_without_writing(env, answer):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)

    _answer(env, bridge, _mqtt(env).snapshots[0]["seq"], **answer)

    assert _regulation_writes(env) == []
    assert "ungültige Serverantwort" in env.ha.pushes[-1]


@pytest.mark.parametrize("bad", [_OMIT, None, "16", float("nan"), True, pytest.param(10**400, id="gross")])
def test_answer_without_a_valid_heat_limit_is_invalid_and_writes_nothing(env, bad):
    # Antwort eines alten Servers (Feld fehlt) oder unbrauchbarer Wert: nichts schreiben, Datenfehler.
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)

    _answer(env, bridge, _mqtt(env).snapshots[0]["seq"], heat_limit=bad)

    assert _regulation_writes(env) == []
    assert "ungültige Serverantwort" in env.ha.pushes[-1]
    assert "heat_limit" in env.ha.pushes[-1]


def test_learned_values_of_the_answer_are_stored(env):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)

    _mqtt(env).answer_raw(_mqtt(env).snapshots[0]["seq"], learned={"curve": 1.1, "heat_limit": 15.4})
    bridge.worker.run_pending()

    assert bridge.store.state.learned == {"curve": 1.1, "heat_limit": 15.4}
    assert load_backup(env.paths["BACKUP_PATH"])["learned"] == {"curve": 1.1, "heat_limit": 15.4}


@pytest.mark.parametrize("learned", [None, "x", {}, {"curve": float("nan")}, {"curve": True}, {"foo": 1.0},
    pytest.param({"curve": 10**400}, id="curve-gross-positiv"),
    pytest.param({"curve": -10**400}, id="curve-gross-negativ"),
    pytest.param({"curve": 1.0, "heat_limit": 10**400}, id="heat_limit-gross"),
])
def test_invalid_learned_values_are_ignored_without_fault(env, learned):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)

    _mqtt(env).answer_raw(_mqtt(env).snapshots[0]["seq"], learned=learned)
    bridge.worker.run_pending()

    assert bridge.store.state.learned == {}
    assert _delivery(bridge).datenfehler is None
    assert ("number.curve_current", 0.95) in _regulation_writes(env)


@pytest.mark.parametrize("huge", [10**400, -10**400], ids=["gross-positiv", "gross-negativ"])
def test_learned_integer_beyond_float_range_is_ignored_without_an_error(env, caplog, huge):
    # JSON-Ganzzahl ausserhalb des float-Bereichs: wie jeder ungueltige Lernwert uebergangen. Die Wirkung allein
    # (nichts gespeichert) zeigt es nicht -- der Worker faengt eine Ausnahme ab --, daher auch kein ERROR im Log.
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)

    with caplog.at_level(logging.ERROR):
        _mqtt(env).answer_raw(_mqtt(env).snapshots[0]["seq"], learned={"curve": huge})
        bridge.worker.run_pending()

    assert [r.getMessage() for r in caplog.records if r.name == "smartheat_runtime.worker"] == []
    assert bridge.store.state.learned == {}
    assert _delivery(bridge).datenfehler is None
    assert ("number.curve_current", 0.95) in _regulation_writes(env)


def test_unstorable_learned_values_do_not_fail_the_answer(env, monkeypatch):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    real_update = bridge.store.update

    def _update(**changes):
        if "learned" in changes:
            raise ticks.StorageError("Datentraeger kaputt")
        return real_update(**changes)

    monkeypatch.setattr(bridge.store, "update", _update)
    _mqtt(env).answer_raw(_mqtt(env).snapshots[0]["seq"], learned={"curve": 1.1, "heat_limit": 15.4})
    bridge.worker.run_pending()

    assert _delivery(bridge).datenfehler is None
    assert ("number.curve_current", 0.95) in _regulation_writes(env)


def test_answer_with_heat_limit_writes_all_three_values(env):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)

    _answer(env, bridge, _mqtt(env).snapshots[0]["seq"], curve=1.0, shift=21.0, heat_limit=16.0)

    assert ("number.heat_limit", 16.0) in _regulation_writes(env)
    assert ("number.curve_current", 1.0) in _regulation_writes(env)


@pytest.mark.parametrize("schema", [_OMIT, None, 2, 3, "3", True, 3.0])
def test_answer_with_unknown_schema_counts_as_server_fault_without_writing(env, schema):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)

    _answer(env, bridge, _mqtt(env).snapshots[0]["seq"], schema=schema)

    assert _regulation_writes(env) == []
    assert "ungültige Serverantwort" in env.ha.pushes[-1]
    assert "unbekanntes Schema" in env.ha.pushes[-1]


@pytest.mark.parametrize("levers", [
    {"curve": 1.0, "room_setpoint": 21.0},
    {"curve": 1.0, "room_setpoint": 21.0, "heat_limit": 16.0, "level": 2.0},
    {"curve": float("inf"), "room_setpoint": 21.0, "heat_limit": 16.0},
    _OMIT,
])
def test_answer_with_wrong_levers_is_a_server_fault_without_writing(env, levers):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)

    _answer(env, bridge, _mqtt(env).snapshots[0]["seq"], levers=levers)

    assert _regulation_writes(env) == []
    assert "ungültige Serverantwort" in env.ha.pushes[-1]


def test_answer_with_null_levers_is_a_server_fault_without_writing(env):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)

    _mqtt(env).answer_raw(_mqtt(env).snapshots[0]["seq"], levers=None)
    bridge.worker.run_pending()

    assert _regulation_writes(env) == []
    assert "ungültige Serverantwort" in env.ha.pushes[-1]


# (Name, Optionen, Zustand der Fake-HA, Hebel des Satzes): Vaillant ohne `level`, Viessmann mit `level`
HEBELSAETZE = {
    "vaillant": ({}, {}, ("curve", "room_setpoint", "heat_limit")),
    "viessmann": (VIESSMANN_OPTIONEN, VIESSMANN_ZUSTAND, ("curve", "level", "room_setpoint")),
}


def _erster_tick(env, satz):
    """Laufzeit des Hebelsatzes starten und den ersten Snapshot erzeugen; liefert (bridge, seq)."""
    optionen, zustand, _ = HEBELSAETZE[satz]
    env.ha.states.update(zustand)
    bridge = _start(env, **optionen)
    _ha_verbunden(env, bridge)
    assert _awaiting_ack(bridge) and _delivery(bridge).datenfehler is None  # Ausgangslage: offener Tick, kein Fehler
    return bridge, _mqtt(env).snapshots[-1]["seq"]


def _ist_serverfehler_ohne_schreiben(env, bridge, vorher):
    assert env.ha.writes == vorher
    assert "ungültige Serverantwort" in env.ha.pushes[-1]
    assert _delivery(bridge).datenfehler is not None and _delivery(bridge).datenfehler.source == "server"


def test_answer_ignores_unknown_top_level_fields(env):
    """V1b ist tolerant: ein unbekanntes Feld der obersten Ebene aendert weder Schreiben noch Zustellung."""
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    assert _awaiting_ack(bridge)

    _mqtt(env).answer_raw(_mqtt(env).snapshots[0]["seq"], fremd=1, **{"naechstes": {"a": [1, 2]}})
    bridge.worker.run_pending()

    assert _regulation_writes(env) == [
        ("number.curve_current", 0.95), ("number.shift_current", 23.0), ("number.heat_limit", 16.0),
    ]
    assert _delivery(bridge).pending is None and _delivery(bridge).datenfehler is None
    assert env.ha.pushes == []


@pytest.mark.parametrize("schlecht", ["1.0", True, [1.0]], ids=["text", "bool", "liste"])
@pytest.mark.parametrize(("satz", "hebel"), [
    ("vaillant", "curve"), ("vaillant", "room_setpoint"),
    ("viessmann", "curve"), ("viessmann", "level"), ("viessmann", "room_setpoint"),
])
def test_wrong_lever_types_are_a_server_fault_for_every_lever(env, satz, hebel, schlecht):
    """F-client-kern-27/-47: ein Hebel mit falschem Typ (Text, JSON true, Liste) ergibt einen Serverfehler, die uebrigen
    Hebel bleiben gueltig und es wird nichts geschrieben (auch nicht die gueltigen)."""
    bridge, seq = _erster_tick(env, satz)
    gueltig = {name: wert for name, wert in _mqtt(env).snapshots[-1]["levers"].items() if name in HEBELSAETZE[satz][2]}
    assert set(gueltig) == set(HEBELSAETZE[satz][2]) and hebel in gueltig  # Gegenprobe: der Hebel gehoert zum Satz
    vorher = list(env.ha.writes)

    _answer(env, bridge, seq, levers={**gueltig, hebel: schlecht})

    _ist_serverfehler_ohne_schreiben(env, bridge, vorher)


@pytest.mark.parametrize("gross", [10**400, -10**400], ids=["positiv", "negativ"])
def test_huge_integer_as_level_is_a_server_fault(env, gross, caplog):
    """Riesige Ganzzahl fuer `level` (Hebelsatz mit `level`): Serverfehler ohne Schreiben und ohne Fehler im Worker."""
    bridge, seq = _erster_tick(env, "viessmann")
    vorher = list(env.ha.writes)

    with caplog.at_level(logging.ERROR):
        _answer(env, bridge, seq, levers={"curve": 1.0, "level": gross, "room_setpoint": 21.0})

    assert [r.getMessage() for r in caplog.records if r.name == "smartheat_runtime.worker"] == []
    _ist_serverfehler_ohne_schreiben(env, bridge, vorher)


@pytest.mark.parametrize("ort", ["room_setpoint", "learned_heat_limit"])
def test_negative_huge_integer_as_room_setpoint_and_learned_heat_limit(env, ort, caplog):
    """-10**400 (nur das positive Vorzeichen war belegt): als Raum-Soll der Hebel ein Serverfehler ohne Schreiben; als
    gelernte Heizgrenze verwirft er die ganzen Lernwerte der Antwort (der Lernstand bleibt leer, kein Datenfehler, kein
    Fehler im Worker), die Hebelwerte der Antwort werden geschrieben."""
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]
    vorher = list(env.ha.writes)

    with caplog.at_level(logging.ERROR):
        if ort == "room_setpoint":
            _answer(env, bridge, seq, shift=-10**400)
        else:
            _mqtt(env).answer_raw(seq, learned={"curve": 1.0, "heat_limit": -10**400})
            bridge.worker.run_pending()

    assert [r.getMessage() for r in caplog.records if r.name == "smartheat_runtime.worker"] == []
    if ort == "room_setpoint":
        _ist_serverfehler_ohne_schreiben(env, bridge, vorher)
    else:
        assert bridge.store.state.learned == {}
        assert _delivery(bridge).datenfehler is None
        assert env.ha.writes[len(vorher):] == [
            ("number.curve_current", 0.95), ("number.shift_current", 23.0), ("number.heat_limit", 16.0)]


@pytest.mark.parametrize("hebel", ["curve", "room_setpoint", "heat_limit"])
def test_minus_infinity_as_lever_value_is_a_server_fault(env, hebel):
    """-Inf als Hebelwert (nur +Inf war belegt): Serverfehler, nichts geschrieben."""
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]
    vorher = list(env.ha.writes)
    levers = {"curve": 0.95, "room_setpoint": 23.0, "heat_limit": 16.0}

    _answer(env, bridge, seq, levers={**levers, hebel: float("-inf")})

    _ist_serverfehler_ohne_schreiben(env, bridge, vorher)


def test_snapshot_payload_is_schema_4(env):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)

    snapshot = _mqtt(env).snapshots[-1]
    assert snapshot["schema"] == 4
    assert snapshot["readonly"] == []
    assert set(snapshot["levers"]) == {"curve", "room_setpoint", "heat_limit"}
    assert snapshot["room_target"] == 20.5


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

    monkeypatch.setattr("smartheat_runtime.backup_store.save_backup", _save)
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
    boost_writes = [("number.curve_current", 1.5), ("number.shift_current", 25.0), ("number.heat_limit", 23.0)]
    assert _regulation_writes(env) == boost_writes

    _answer(env, bridge, _mqtt(env).snapshots[0]["seq"], curve=0.95, shift=23.0, heat_limit=17.0)

    assert _regulation_writes(env) == boost_writes
    assert (_backup(env)["restore_point"]["curve"], _backup(env)["restore_point"]["room_setpoint"]) == (0.95, 23.0)
    assert _backup(env)["restore_point"]["heat_limit"] == 17.0


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

    assert _regulation_writes(env) == [
        ("number.curve_current", 0.95), ("number.shift_current", 23.0), ("number.heat_limit", 16.0),
    ]


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
    _override_comfort_boost(bridge, curve=1.0, room_setpoint=24.0)

    _set_room_target(env, bridge, 22.0)  # Comfort-Boost startet, Notfall-Boost haelt die Anlage

    assert _regulation_writes(env) == [
        ("number.curve_current", 1.5), ("number.shift_current", 25.0), ("number.heat_limit", 23.0),
    ]

    _answer(env, bridge, _mqtt(env).snapshots[-1]["seq"], curve=0.95, shift=23.0)

    assert (env.ha.states["number.curve_current"], env.ha.states["number.shift_current"]) == (1.0, 24.0)
    assert _backup(env)["boost_active"] is True
    assert _backup(env)["restore_point"]["curve"] == 0.95

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

    _ha_verbunden(env, bridge)

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


def test_local_check_keeps_cached_target_when_room_target_unreadable(env):
    # Spec 5c 1.3: die Regelung liest den Raum-Soll aus dem Soll-Speicher, nie aus der Entity; ein Bericht ohne Zahl
    # (unavailable) ist keine Eingabe. Bis 0.36 las der lokale Check die Entity und loggte "room_target nicht lesbar".
    _quiet_backup(env)
    bridge = _start(env)
    env.ha.states["sensor.room_target"] = ValueError("unavailable")

    _trigger(env, bridge, "sensor.room_target")

    assert _stable_target(bridge) == 21.0
    assert _delivery(bridge).datenfehler is None


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
    env.ha.states.update({"sensor.wz_battery": 15, "sensor.wz_battery::device_class": "battery"})
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
    assert len(_links(env)) == 1


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
    assert len(_links(env)) == 1


def test_inactive_status_at_start_runs_locally_without_mqtt(env):
    _quiet_backup(env)
    env.abo["status"] = entitlement.INACTIVE
    env.ha.states["sensor.room_actual"] = 19.5  # Notfall-Boost muss auch ohne MQTT greifen

    bridge = _start(env)

    assert _links(env) == []
    assert bridge.mqtt_client is None
    assert _failsafe_file(env)["failsafe_active"] is True
    assert (env.ha.states["number.curve_current"], env.ha.states["number.shift_current"]) == (1.5, 25.0)
    assert len(env.ha.persistent) == 1
    assert "Abo inaktiv" in env.ha.pushes[0]


def test_inactive_after_grace_idles_as_abo_beendet_without_writes(env, monkeypatch):
    _quiet_backup(env)
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    monkeypatch.setattr("smartheat_runtime.entitlement.grace_expired", lambda since, now: True)

    bridge = _start_bridge(env)

    assert isinstance(bridge, app.IdleBridge) and bridge.reason == "abo_beendet"
    assert _links(env) == [] and env.trigger_clients == []
    assert env.ha.writes == [] and env.ha.pushes == []
    assert _status_states(env) == ["startet", "abo_beendet"]


def test_closing_start_retries_a_failed_restore_and_reports_it_once(env, monkeypatch):
    # T2-7 im Startpfad; die Wiederholung laeuft im Worker, das Lebenszeichen weiter.
    _quiet_backup(env, emergency_boost_active=True)
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    monkeypatch.setattr("smartheat_runtime.entitlement.grace_expired", lambda since, now: True)
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


def test_unexpected_entitlement_query_error_counts_as_unknown(env):
    # T10b: eine unerwartet werfende Abo-Pruefung nach dem zweiten Timeout laeuft ueber _fallback_follow_up als
    # "unbekannt" -- Notbetrieb mit Retry, kein Abo-inaktiv-Modus. Im Geraete-Pfad wirft die Abo-Quelle des Dokuments.
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]

    env.abo["status"] = RuntimeError("unerwartet")
    _advance(env, bridge, 30)
    _advance(env, bridge, 30)

    assert _delivery(bridge).notbetrieb is True
    assert _abo_inactive_since(bridge) is None
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
    monkeypatch.setattr("smartheat_runtime.entitlement.grace_expired", lambda since, now: next(answers, True))

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
    monkeypatch.setattr("smartheat_runtime.entitlement.grace_expired", lambda since, now: next(answers, True))
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


def _restore_logs(caplog, levelno):
    """Logzeilen der gescheiterten Wiederherstellung am Fristende (abo.log_restore_failure: Betrieb und Start nach
    Fristende)."""
    return [r for r in caplog.records if r.name == "smartheat_runtime.abo" and r.levelno == levelno
            and "Abo-inaktiv-Frist abgelaufen" in r.getMessage()]


def _grace_end_during_runtime_with_failing_restore(env, monkeypatch, **backup):
    _quiet_backup(env, emergency_boost_active=True, **backup)
    env.ha.states.update({"number.curve_current": 1.5, "number.shift_current": 25.0})  # Notfall-Zeile steht
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    answers = iter([False])  # Startpruefung, danach ist die Frist abgelaufen
    monkeypatch.setattr("smartheat_runtime.entitlement.grace_expired", lambda since, now: next(answers, True))
    bridge = _start_bridge(env)
    env.ha.write_error = RuntimeError("HA nicht erreichbar")
    return bridge


def _closing_start_with_failing_restore(env, monkeypatch, **backup):
    _quiet_backup(env, emergency_boost_active=True, **backup)
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    monkeypatch.setattr("smartheat_runtime.entitlement.grace_expired", lambda since, now: True)
    env.ha.states.update({"number.curve_current": 1.5, "number.shift_current": 25.0})  # Notfall-Zeile steht
    env.ha.write_error = RuntimeError("HA nicht erreichbar")
    return _start_bridge(env)


_RESTORE_FAILED_BEFORE = {"notify_states": {"wiederherstellung": "fehlgeschlagen"},
                          "notify_messages": {"wiederherstellung": abo.RESTORE_FAILED_MESSAGE}}


def test_grace_end_with_a_deferred_restore_logs_one_error_not_one_per_check(env, monkeypatch, caplog):
    """TP12b (4): scheitert die Wiederherstellung am Fristende, stellt das Schreibbudget (Rueckkehr-Staffel 300, 900,
    1800 s) die folgenden Versuche zurueck; der Takt (300 s) loggt das nicht jedes Mal als ERROR, sondern einmal je
    gescheiterter Wiederherstellung (wie die Meldung)."""
    with caplog.at_level(logging.DEBUG):
        bridge = _grace_end_during_runtime_with_failing_restore(env, monkeypatch)
        bridge.worker.run_pending()
        assert len(_restore_logs(caplog, logging.ERROR)) == 1
        for _ in range(12):  # eine Stunde im Takt 300 s, darin zurueckgestellte und gescheiterte Versuche
            _advance(env, bridge, 300)

    assert bridge.idle is False
    assert (len(_restore_logs(caplog, logging.ERROR)), len(_restore_logs(caplog, logging.WARNING))) == (1, 0)
    assert env.ha.pushes.count(abo.RESTORE_FAILED_MESSAGE) == 1

    env.ha.write_error = None
    _advance(env, bridge, 1800)  # spaetestens nach der letzten Stufe der Rueckkehr-Staffel
    assert bridge.idle is True
    assert env.ha.pushes[-1] == abo.RESTORE_OK_MESSAGE


def test_grace_end_with_a_deferred_restore_after_a_restart_logs_one_warning(env, monkeypatch, caplog):
    # TP12b (4), Fix-Runde 1: nach einem Neustart steht der Meldezustand schon; die Episode bleibt einmal sichtbar
    # (WARNING, im INFO-Log), ohne neuen Push und ohne ERROR je Takt.
    with caplog.at_level(logging.DEBUG):
        bridge = _grace_end_during_runtime_with_failing_restore(env, monkeypatch, **_RESTORE_FAILED_BEFORE)
        for _ in range(12):
            _advance(env, bridge, 300)

    assert bridge.idle is False
    assert (len(_restore_logs(caplog, logging.ERROR)), len(_restore_logs(caplog, logging.WARNING))) == (0, 1)
    assert abo.RESTORE_FAILED_MESSAGE not in env.ha.pushes


def test_closing_start_with_a_deferred_restore_logs_one_error_not_one_per_retry(env, monkeypatch, caplog):
    # TP12b (4) im Startpfad (_finish_at_start): Takt 300 s, Rueckkehr-Staffel stellt zurueck.
    with caplog.at_level(logging.DEBUG):
        bridge = _closing_start_with_failing_restore(env, monkeypatch)
        for _ in range(12):
            _advance(env, bridge, 300)

    assert bridge.reason == "abo_beendet"
    assert (len(_restore_logs(caplog, logging.ERROR)), len(_restore_logs(caplog, logging.WARNING))) == (1, 0)
    assert env.ha.pushes.count(abo.RESTORE_FAILED_MESSAGE) == 1


def test_closing_start_with_a_deferred_restore_after_a_restart_logs_one_warning(env, monkeypatch, caplog):
    # TP12b (4), Fix-Runde 1, Startpfad.
    with caplog.at_level(logging.DEBUG):
        bridge = _closing_start_with_failing_restore(env, monkeypatch, **_RESTORE_FAILED_BEFORE)
        for _ in range(12):
            _advance(env, bridge, 300)

    assert bridge.reason == "abo_beendet"
    assert (len(_restore_logs(caplog, logging.ERROR)), len(_restore_logs(caplog, logging.WARNING))) == (0, 1)
    assert abo.RESTORE_FAILED_MESSAGE not in env.ha.pushes


def test_inactive_restart_within_grace_does_not_notify_again(env):
    _quiet_backup(env)
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())

    _start(env)

    assert _links(env) == []
    assert env.ha.pushes == []
    assert env.ha.persistent == []


def test_inactive_after_grace_restores_leftover_boost_once(env, monkeypatch):
    _quiet_backup(env, emergency_boost_active=True)
    env.ha.states.update({"number.curve_current": 1.5, "number.shift_current": 25.0})
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    monkeypatch.setattr("smartheat_runtime.entitlement.grace_expired", lambda since, now: True)

    assert _start_bridge(env).reason == "abo_beendet"

    assert (env.ha.states["number.curve_current"], env.ha.states["number.shift_current"]) == (0.9, 22.0)
    assert _backup(env)["emergency_boost_active"] is False


def test_the_end_of_the_subscription_restores_the_server_original(env, monkeypatch):
    """Spec Stufe 3 S3 5.2: auch die normale Laufzeit-Pipeline (Abschluss-Start, Fristende) kennt die Ursprungswerte."""
    _quiet_backup(env, emergency_boost_active=True, restore_point={"curve": 0.9, "room_setpoint": 22.0, "heat_limit": 18.0})
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    monkeypatch.setattr("smartheat_runtime.entitlement.grace_expired", lambda since, now: True)

    assert _start_bridge(env, ursprungswerte={"heat_limit": 16.0}).reason == "abo_beendet"

    assert env.ha.states["number.heat_limit"] == 16.0


def _start_just_before_grace_end(env, monkeypatch):
    _quiet_backup(env, emergency_boost_active=True)
    env.ha.states.update({"number.curve_current": 1.5, "number.shift_current": 25.0})
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    answers = iter([False])  # Startpruefung, danach ist die Frist abgelaufen
    monkeypatch.setattr("smartheat_runtime.entitlement.grace_expired", lambda since, now: next(answers, True))
    neustarts = []
    bridge = _start_bridge(env, neustart=lambda: neustarts.append(1))
    return bridge, neustarts


def test_grace_end_with_reactivated_abo_restarts_instead_of_restoring(env, monkeypatch, caplog):
    # T2-5: kein faelschliches Zuruecksetzen am Fristende, wenn das Abo wieder aktiv ist.
    bridge, neustarts = _start_just_before_grace_end(env, monkeypatch)
    env.abo["status"] = entitlement.ACTIVE

    with caplog.at_level(logging.INFO):
        bridge.worker.run_pending()

    assert neustarts == [1]  # der Rueckruf neustart des Hosts (Spec Stufe 3 S6 4.2)
    assert "Abo wieder aktiv, Neustart im Normalbetrieb" in caplog.text  # der Anlass je Ursache im Log
    assert not env.paths["ENTITLEMENT_PATH"].exists()
    assert (env.ha.states["number.curve_current"], env.ha.states["number.shift_current"]) == (1.5, 25.0)
    assert all(message != ABO_ENDED for _, message in env.ha.persistent)


@pytest.mark.parametrize("status", [entitlement.INACTIVE, entitlement.UNKNOWN])
def test_grace_end_finishes_when_abo_not_active(env, monkeypatch, status):
    bridge, neustarts = _start_just_before_grace_end(env, monkeypatch)
    env.abo["status"] = status

    bridge.worker.run_pending()
    assert bridge.idle is True
    assert neustarts == []
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
    _override_comfort_boost(bridge, curve=1.0, room_setpoint=24.0)

    _set_room_target(env, bridge, 22.0)  # Comfort-Boost + Tick
    _advance(env, bridge, 30)
    _advance(env, bridge, 30)  # zweiter Ack-Timeout -> Notbetrieb

    assert _delivery(bridge).notbetrieb is True

    _trigger(env, bridge, "sensor.room_actual")  # 20.0 bei Soll 22.0: > 1 K darunter

    # Heizgrenze: beide Zeilen setzen heat_limit_max (23,0); der Notfall-Boost ueberspringt sie, weil
    # die Anlage schon darauf steht (Quota-Check mit dem eigenen letzten Schreibwert).
    assert _regulation_writes(env) == [
        ("number.curve_current", 1.0), ("number.shift_current", 24.0), ("number.heat_limit", 23.0),
        ("number.curve_current", 1.5), ("number.shift_current", 25.0),
    ]

    env.ha.states["sensor.room_actual"] = 21.6  # beide Schwellen erreicht
    _trigger(env, bridge, "sensor.room_actual")

    # Boost-Ende: Wiederherstellungspunkt (Heizgrenze beim Boost-Start von der Anlage gelesen), nicht 23,0
    assert _regulation_writes(env)[-3:] == [
        ("number.curve_current", 0.9), ("number.shift_current", 22.0), ("number.heat_limit", 16.0),
    ]
    assert (_backup(env)["boost_active"], _backup(env)["emergency_boost_active"]) == (False, False)


def test_answer_during_emergency_boost_is_written_once_when_notbetrieb_ends(env):
    bridge = _start_in_notbetrieb_with_emergency_boost(env)

    _answer(env, bridge, "alt-1", curve=0.95, shift=23.0)

    assert env.ha.writes == [
        ("number.curve_current", 1.5), ("number.shift_current", 25.0), ("number.heat_limit", 23.0),
        ("number.curve_current", 0.95), ("number.shift_current", 23.0), ("number.heat_limit", 16.0),
    ]


def test_comfort_boost_end_restores_values_answered_during_the_boost(env):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 22.0)
    _answer(env, bridge, _mqtt(env).snapshots[0]["seq"], curve=0.95, shift=23.0)

    env.ha.states["sensor.room_actual"] = 21.6
    _trigger(env, bridge, "sensor.room_actual")

    assert _regulation_writes(env) == [
        ("number.curve_current", 1.5), ("number.shift_current", 25.0), ("number.heat_limit", 23.0),
        ("number.curve_current", 0.95), ("number.shift_current", 23.0), ("number.heat_limit", 16.0),
    ]
    assert _backup(env)["boost_active"] is False


def test_grace_end_without_boost_restores_learned_values(env, monkeypatch):
    _quiet_backup(env)
    env.ha.states.update({"number.curve_current": 1.2, "number.shift_current": 26.0})
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    answers = iter([False])  # Startpruefung, danach ist die Frist abgelaufen
    monkeypatch.setattr("smartheat_runtime.entitlement.grace_expired", lambda since, now: next(answers, True))

    bridge = _start_bridge(env)

    bridge.worker.run_pending()
    assert bridge.idle is True
    # Heizgrenze: Ursprungswert (beim ersten Check von der Anlage gemerkt)
    assert env.ha.writes == [
        ("number.curve_current", 0.9), ("number.shift_current", 22.0), ("number.heat_limit", 16.0),
    ]
    assert env.ha.persistent[-1] == ("smartheat_abo", ABO_ENDED)


def test_grace_end_during_comfort_boost_restores_learned_values(env, monkeypatch):
    _quiet_backup(env)
    env.ha.states["sensor.room_actual"] = 21.2  # nah genug am Soll: kein Notfall-Boost
    env.abo["status"] = entitlement.INACTIVE
    entitlement.mark_inactive(env.paths["ENTITLEMENT_PATH"], datetime.now().astimezone())
    answers = iter([False, False])  # Startpruefung und erster grace_check
    monkeypatch.setattr("smartheat_runtime.entitlement.grace_expired", lambda since, now: next(answers, True))
    bridge = _start(env)

    _set_room_target(env, bridge, 22.0)

    assert _regulation_writes(env) == [
        ("number.curve_current", 1.5), ("number.shift_current", 25.0), ("number.heat_limit", 23.0),
    ]

    env.clock.advance(300)

    bridge.worker.run_pending()
    assert bridge.idle is True
    assert _regulation_writes(env)[-3:] == [
        ("number.curve_current", 0.9), ("number.shift_current", 22.0), ("number.heat_limit", 16.0),
    ]
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
    assert backup["restore_point"]["curve"] == 0.95


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
    env.clock.advance(300)  # Rueckkehr-Staffel: erster Retry nach 300 s (TP12b: gestaffelt)
    _trigger(env, bridge, "sensor.room_actual")

    assert (env.ha.states["number.curve_current"], env.ha.states["number.shift_current"]) == (0.95, 23.0)
    assert _backup(env)["emergency_boost_active"] is False


def test_deferred_comfort_start_is_started_by_a_later_check(env):
    # Review Focus 4: B5 haelt das alte Soll, der naechste erlaubte Check startet den Boost.
    _quiet_backup(env)
    bridge = _start(env)
    env.ha.write_error = RuntimeError("403 Quota Exceeded")
    _set_room_target(env, bridge, 22.0)  # Comfort-Start scheitert
    env.ha.write_error = None
    # Tick beantworten, sonst laeuft er in den Notbetrieb und der Notfall-Boost verfaelscht den Test.
    _answer(env, bridge, _mqtt(env).snapshots[-1]["seq"], curve=0.9, shift=22.0)

    _trigger(env, bridge, "sensor.room_actual")
    assert _boost_active(bridge) is False  # innerhalb von 30 min zurueckgestellt

    env.clock.advance(1800)
    _trigger(env, bridge, "sensor.room_actual")
    assert _boost_active(bridge) is True


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

# Datenfehler nennen Hebel (Controller-Ruling Task 10, Spec P2-3); bis 0.29.0 "curve_current (...)".
WRITE_DETAIL = "curve (number.curve_current): myVAILLANT-Cloud nicht erreichbar"
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
    assert env.ha.writes[-3:] == [
        ("number.curve_current", 0.95), ("number.shift_current", 23.0), ("number.heat_limit", 16.0),
    ]
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
    _mqtt(env).verbunden()  # aus dem paho-Thread
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

    _mqtt(env).verbunden()
    bridge.worker.run_pending()

    assert len(_mqtt(env).snapshots) == 1


def test_open_tick_at_start_is_sent_on_connect_without_waiting_for_the_ack_timeout(env, monkeypatch):
    # N3: 0.17.0-0.19.0 warteten hier 30 s und zaehlten einen Serverausfall.
    _quiet_backup(env)
    save_backup(env.paths["FAILSAFE_PATH"], {"failsafe_active": False, "pending": {"seq": "alt-1", "trigger": "daily"}})
    link = {"up": False}
    monkeypatch.setattr(RuntimeChannel, "is_connected", verbindung_aus(link))

    bridge = _start(env)
    assert _mqtt(env).snapshots == []

    link["up"] = True
    _mqtt(env).verbunden()
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

    assert isinstance(bridge, app.IdleBridge) and bridge.reason == "abgemeldet"
    assert env.ha.writes == [("number.curve_current", 0.9), ("number.shift_current", 22.0)]
    assert _backup(env)["emergency_boost_active"] is False
    assert {"smartheat_notbetrieb", "smartheat_batterie_sensor_x"} <= set(env.ha.dismissed)
    assert "notify_states" not in _backup(env)
    assert env.ha.pushes == []
    assert _links(env) == [] and env.trigger_clients == []
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
    assert _last_event(env)["grund"] == app.SIGN_OFF_RESTORE_FAILED.format(seconds=300)

    env.ha.write_error = None
    _advance(env, bridge, 300)

    assert env.ha.writes == [("number.curve_current", 0.9), ("number.shift_current", 22.0)]
    assert _last_event(env)["grund"] is None


def test_sign_off_with_invalid_configuration_does_not_write(env):
    _quiet_backup(env, boost_active=True)

    bridge = _start_bridge(env, abgemeldet=True, verteilsystem="Unbekannt")

    assert bridge.reason == "abgemeldet"
    assert env.ha.writes == []
    assert _last_event(env)["grund"] == app.SIGN_OFF_INVALID_CONFIG


def test_sign_off_during_a_comfort_boost_restores_the_learned_values(env):
    """Abmelden mit laufendem Comfort-Boost (bis 0.36 zusaetzlich ohne MQTT-Zugangsdaten, die die Integration beim
    Entfernen leerte; im Geraete-Pfad haelt der Geraetekern den Zugang): die zuletzt gelernten Werte gehen auf die
    Anlage."""
    _quiet_backup(env, boost_active=True)

    bridge = _start_bridge(env, abgemeldet=True)

    assert bridge.reason == "abgemeldet"
    assert env.ha.writes == [("number.curve_current", 0.9), ("number.shift_current", 22.0)]


def test_sign_off_failed_restore_notifies_with_the_restore_values(env):
    _quiet_backup(env, boost_active=True, notify_states={"notbetrieb": "aktiv"})
    env.ha.write_error = RuntimeError("Cloud weg")

    _start_bridge(env, abgemeldet=True)

    message = app.SIGN_OFF_RESTORE_FAILED_MESSAGE.format(werte="Kurve 0,9, Parallelverschiebung 22")
    assert env.ha.pushes == [message]
    assert ("smartheat_wiederherstellung", message) in env.ha.persistent
    assert "smartheat_notbetrieb" in env.ha.dismissed
    assert _backup(env)["notify_states"] == {"wiederherstellung": "fehlgeschlagen"}


def test_sign_off_failed_restore_after_a_restart_renews_the_notification_without_push(env):
    message = app.SIGN_OFF_RESTORE_FAILED_MESSAGE.format(werte="Kurve 0,9, Parallelverschiebung 22")
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

    message = app.SIGN_OFF_NOT_RESTORED_MESSAGE.format(werte="Kurve 0,9, Parallelverschiebung 22")
    assert env.ha.pushes == [message]
    assert ("smartheat_wiederherstellung", message) in env.ha.persistent


def test_sign_off_with_invalid_configuration_without_boost_stays_silent(env):
    _quiet_backup(env)

    _start_bridge(env, abgemeldet=True, verteilsystem="Unbekannt")

    assert env.ha.pushes == []


# --- Ursprungswerte des Servers (Spec Stufe 3 S3 5.2, 5b-W-4) ---


def test_sign_off_restores_the_heat_limit_to_the_server_original(env):
    """Der Ursprungswert vom Server gilt vor dem lokal gemerkten (Ersetzen des Geraets)."""
    _quiet_backup(env, boost_active=True, originals={"heat_limit": 17.0},
                  restore_point={"curve": 0.9, "room_setpoint": 22.0, "heat_limit": 18.0})

    bridge = _start_bridge(env, abgemeldet=True, ursprungswerte={"heat_limit": 16.0})

    assert bridge.reason == "abgemeldet"
    assert ("number.heat_limit", 16.0) in env.ha.writes


def test_a_restart_after_the_sign_off_does_not_write_the_server_originals_again(env):
    """Schluss-Review: die gespiegelten Ursprungswerte bleiben in der Konfiguration; der naechste Start des abgemeldeten
    Geraets schreibt nichts mehr, auch wenn der Kunde die Heizgrenze inzwischen verstellt hat."""
    _quiet_backup(env, boost_active=True, restore_point={"curve": 0.9, "room_setpoint": 22.0, "heat_limit": 18.0})
    _start_bridge(env, abgemeldet=True, ursprungswerte={"heat_limit": 16.0})
    assert ("number.heat_limit", 16.0) in env.ha.writes
    env.ha.states["number.heat_limit"] = 20.0
    writes = list(env.ha.writes)

    bridge = _start_bridge(env, abgemeldet=True, ursprungswerte={"heat_limit": 16.0})

    assert bridge.reason == "abgemeldet" and _last_event(env)["grund"] is None
    assert env.ha.writes == writes


def test_sign_off_without_server_values_uses_the_local_original(env):
    _quiet_backup(env, boost_active=True, originals={"heat_limit": 17.0},
                  restore_point={"curve": 0.9, "room_setpoint": 22.0, "heat_limit": 18.0})

    _start_bridge(env, abgemeldet=True)

    assert ("number.heat_limit", 17.0) in env.ha.writes


def test_reset_after_an_empty_configuration_uses_the_server_originals_of_the_marker(env, monkeypatch):
    """Die Markierung haelt die alte Konfiguration samt ursprungswerte; die leere neue hat keine."""
    _quiet_backup(env, boost_active=True, restore_point={"curve": 0.9, "room_setpoint": 22.0, "heat_limit": 18.0})

    _start_bridge(env, leer_mit_markierung=True, ursprungswerte={"heat_limit": 15.0})

    assert ("number.heat_limit", 15.0) in env.ha.writes


def test_a_huge_server_integer_does_not_break_the_sign_off_restore(env):
    """Bedrohungsmodell: 10**400 in ursprungswerte darf das Zuruecksetzen nicht scheitern lassen; es gilt der lokale Wert."""
    _quiet_backup(env, boost_active=True, originals={"heat_limit": 17.0},
                  restore_point={"curve": 0.9, "room_setpoint": 22.0, "heat_limit": 18.0})

    bridge = _start_bridge(env, abgemeldet=True, ursprungswerte={"heat_limit": 10**400})

    assert bridge.reason == "abgemeldet" and _last_event(env)["grund"] is None
    assert ("number.heat_limit", 17.0) in env.ha.writes


def test_the_server_originals_are_clamped_to_the_local_limits_at_sign_off(env):
    _quiet_backup(env, boost_active=True, restore_point={"curve": 0.9, "room_setpoint": 22.0, "heat_limit": 18.0})

    _start_bridge(env, abgemeldet=True, ursprungswerte={"heat_limit": 99.0})

    assert ("number.heat_limit", 23.0) in env.ha.writes  # Vaillant x Heizkoerper: Heizgrenze bis 23


# --- Leer heisst abmelden (Spec Stufe 3 S3 Abschnitt 4, A5-23) ---


def test_empty_configuration_without_sign_off_restores_then_restarts_once(env, monkeypatch):
    """A5-23: leer ohne sign_off bei laufendem Comfort-Boost: zuruecksetzen mit der Konfiguration aus der Markierung,
    danach Markierung erledigt und genau ein Neustart (der Start davor loescht den Zustand)."""
    _quiet_backup(env, boost_active=True)
    neustarts = []

    bridge = _start_bridge(env, neustart=lambda: neustarts.append(1), leer_mit_markierung=True)

    assert isinstance(bridge, app.IdleBridge) and bridge.reason == "abgemeldet"
    assert env.ha.writes == [("number.curve_current", 0.9), ("number.shift_current", 22.0)]
    assert _last_event(env)["status"] == "abgemeldet" and _last_event(env)["grund"] is None
    bridge.worker.run_pending()
    assert neustarts == [1]
    pfad = env.paths["AUFRAEUMEN_PATH"]
    assert aufraeumen.lesen(pfad).zurueckgesetzt
    assert aufraeumen.entscheiden(pfad, {"anlage": None}) == aufraeumen.LOESCHEN


def test_reset_fails_keeps_state_across_restart(env, monkeypatch):
    """Review Focus 2: scheitert das Zuruecksetzen, bleibt die Markierung offen, kein Neustart, kritische Meldung; der
    naechste Start (Neustart des Add-ons) versucht es mit demselben Zustand erneut."""
    _quiet_backup(env, boost_active=True)
    env.ha.write_error = RuntimeError("Cloud weg")
    neustarts = []

    bridge = _start_bridge(env, neustart=lambda: neustarts.append(1), leer_mit_markierung=True)
    bridge.worker.run_pending()

    pfad = env.paths["AUFRAEUMEN_PATH"]
    assert neustarts == [] and aufraeumen.entscheiden(pfad, {"anlage": None}) == aufraeumen.ZURUECKSETZEN
    assert _backup(env)["boost_active"] is True
    assert _last_event(env)["status"] == "abgemeldet"
    assert _last_event(env)["grund"] == app.SIGN_OFF_RESTORE_FAILED.format(seconds=300)
    message = app.SIGN_OFF_RESTORE_FAILED_MESSAGE.format(werte="Kurve 0,9, Parallelverschiebung 22")
    assert ("smartheat_wiederherstellung", message) in env.ha.persistent

    env.ha.write_error = None
    # Neustart: dieselbe offene Markierung
    bridge = _start_bridge(env, neustart=lambda: neustarts.append(1), leer_mit_markierung=True)
    bridge.worker.run_pending()
    assert env.ha.writes == [("number.curve_current", 0.9), ("number.shift_current", 22.0)] and neustarts == [1]


def test_retry_success_after_empty_configuration_also_restarts(env, monkeypatch):
    _quiet_backup(env, boost_active=True)
    env.ha.write_error = RuntimeError("Cloud weg")
    neustarts = []
    bridge = _start_bridge(env, neustart=lambda: neustarts.append(1), leer_mit_markierung=True)

    env.ha.write_error = None
    _advance(env, bridge, 300)
    bridge.worker.run_pending()

    assert neustarts == [1] and aufraeumen.lesen(env.paths["AUFRAEUMEN_PATH"]).zurueckgesetzt


def test_an_unwritable_marker_after_the_reset_keeps_the_state_and_does_not_restart(env, monkeypatch, caplog):
    """Laesst sich die Markierung nach dem Zuruecksetzen nicht schreiben (Datentraeger), bleibt die Laufzeit im
    Ruhezustand abgemeldet ohne Neustart: kein schneller Neustart-Kreis, der Zustand bleibt, der naechste Start versucht
    es erneut."""
    _quiet_backup(env, boost_active=True)
    aufraeumen.markieren(env.paths["AUFRAEUMEN_PATH"], geraet_helpers.konfiguration_aus_optionen(OPTIONS))
    neustarts = []
    monkeypatch.setattr("smartheat_runtime.aufraeumen.dateien.schreiben_json", _raise_oserror)

    with caplog.at_level(logging.ERROR):
        bridge = _start_bridge(env, neustart=lambda: neustarts.append(1), leer_mit_markierung=True)
        bridge.worker.run_pending()

    assert bridge.reason == "abgemeldet" and neustarts == []
    assert env.ha.writes == [("number.curve_current", 0.9), ("number.shift_current", 22.0)]
    assert aufraeumen.entscheiden(env.paths["AUFRAEUMEN_PATH"], {"anlage": None}) == aufraeumen.ZURUECKSETZEN
    assert "OSError" in caplog.text


def test_normal_sign_off_without_marker_does_not_restart(env, monkeypatch):
    _quiet_backup(env, boost_active=True)
    neustarts = []
    bridge = _start_bridge(env, neustart=lambda: neustarts.append(1), abgemeldet=True)
    bridge.worker.run_pending()
    assert neustarts == []


# --- Durchsetzung: zurueckgesetzter Eingriff reist als KPI mit (TP11, Spec 5.3) ---

OVERRIDE = {"levers": {"curve": 1.3, "room_setpoint": 24.5, "heat_limit": 16.0}, "erkannt": "2026-10-01T08:00:00+02:00"}


def _settle(env, bridge):
    """Die Durchsetzung wertet erst nach settle_seconds Laufzeit bzw. seit dem letzten
    eigenen Schreiben aus (LeverPipeline.settled)."""
    _advance(env, bridge, SETTLE + 1)


def test_manual_override_travels_with_the_next_snapshot_and_is_cleared_after_the_answer(env):
    _quiet_backup(env)
    bridge = _start(env)
    _settle(env, bridge)
    env.ha.states.update({"number.curve_current": 1.3, "number.shift_current": 24.5})
    _advance(env, bridge, 300)
    _advance(env, bridge, 300)  # zwei Runden mit Abweichung

    # Heizgrenze ohne Wiederherstellungspunkt: Live-Wert der Anlage (Rueckfallkette, Plan-Praezisierung 6).
    assert _backup(env)["manual_override_pending"]["levers"] == {"curve": 1.3, "room_setpoint": 24.5, "heat_limit": 16.0}
    assert _last_event(env)["hinweise"]["manueller_eingriff"]["hebel"]["curve"] == 1.3
    assert _regulation_writes(env) == [("number.curve_current", 0.9), ("number.shift_current", 22.0)]  # zurueckgesetzt

    _set_room_target(env, bridge, 20.5)
    snapshot = _mqtt(env).snapshots[-1]
    assert set(snapshot["manual_override"]) == {"levers", "erkannt"}

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


def test_manual_override_detected_during_an_open_tick_survives_a_same_seq_retry(env, monkeypatch):
    """Fix Runde 1, Befund 2: der Server verarbeitet eine bereits gesehene seq idempotent aus
    dem Cache (regelung/tick.py). Ein zwischen dem ersten Publish und einem Retry derselben seq
    neu erkannter Eingriff darf nicht mit dem Retry reisen -- der Server saehe ihn dann nie,
    waehrend die (verspaetete) Antwort ihn beim Client trotzdem als erledigt loeschen wuerde
    (KPI-Verlust). Er muss bis zur naechsten seq warten."""
    _quiet_backup(env)
    _pruefintervall(monkeypatch, 5)
    bridge = _start(env)
    _settle(env, bridge)
    _set_room_target(env, bridge, 20.5)  # erster Tick, seq X, noch ohne Eingriff
    seq = _mqtt(env).snapshots[0]["seq"]
    assert "manual_override" not in _mqtt(env).snapshots[0]

    env.ha.states.update({"number.curve_current": 1.3, "number.shift_current": 24.5})
    _advance(env, bridge, 5)
    _advance(env, bridge, 5)  # zwei Runden mit Abweichung, seq X wartet noch auf Antwort
    assert _backup(env)["manual_override_pending"]["levers"]["curve"] == 1.3

    _advance(env, bridge, 20)  # Ack-Timeout (30 s seit dem Publish) loest den Retry derselben seq aus
    assert [s["seq"] for s in _mqtt(env).snapshots] == [seq, seq]
    assert "manual_override" not in _mqtt(env).snapshots[-1]  # gepinnt: wie beim ersten Publish

    _answer(env, bridge, seq, curve=0.95, shift=23.0)  # verspaetete Antwort auf den Retry trifft ein

    assert _backup(env)["manual_override_pending"]["levers"]["curve"] == 1.3  # ueberlebt, der Server hat ihn nie gesehen


def test_manual_override_detected_while_a_tick_is_open_survives_its_answer(env, monkeypatch):
    """Fix Runde 1, Befund 3c: ein zwischen Publish und Antwort neu erkannter Eingriff (noch ohne
    Retry) darf die Antwort auf den urspruenglichen, eingriffslosen Versuch nicht loeschen
    (sent=None schuetzt ihn ueber die Praezisierung-4-Pruefung in _clear_sent_manual_override)."""
    _quiet_backup(env)
    _pruefintervall(monkeypatch, 5)
    bridge = _start(env)
    _settle(env, bridge)
    _set_room_target(env, bridge, 20.5)  # seq X, noch ohne Eingriff
    seq = _mqtt(env).snapshots[-1]["seq"]

    env.ha.states.update({"number.curve_current": 1.3, "number.shift_current": 24.5})
    _advance(env, bridge, 5)
    _advance(env, bridge, 5)  # zwei Runden: Eingriff erkannt, seq X ist noch offen

    _answer(env, bridge, seq, curve=0.95, shift=23.0)  # Antwort auf den urspruenglichen Versuch

    assert _backup(env)["manual_override_pending"]["levers"]["curve"] == 1.3


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


# --- TP12b: Datentraeger nicht beschreibbar ---

def test_unwritable_disk_is_reported_once_and_cleared_after_repair(env, monkeypatch):
    _quiet_backup(env)
    bridge = _start(env)
    _connect(env, bridge)
    with monkeypatch.context() as patch:
        _break_backup_writes(patch)
        _set_room_target(env, bridge, 20.5)
        _set_room_target(env, bridge, 20.0)

        assert _last_event(env)["status"] == "datenfehler"
        assert _last_event(env)["datenfehler"] == {"art": "lokal", "rollen": ["datentraeger"]}
        assert env.ha.pushes.count(HA_TEXTS.storage_failed) == 1
        assert _mqtt(env).snapshots == []  # N5: ohne gespeicherte Buchung kein Tick

    _advance(env, bridge, 300)  # EV_HEALTH: flush gelingt, ohne dass sich Zustand aendert

    assert env.ha.pushes[-1] == datentraeger.OK_MESSAGE
    assert _last_event(env)["status"] == "regelt"


def test_server_fault_is_dismissed_after_a_disk_fault_cycle_and_the_ack(env, monkeypatch):
    """Review F1: eine gemeldete Server-Stoerung darf nicht offen bleiben, wenn die naechste
    Antwort an einem Datentraegerfehler scheitert und erst die spaetere Antwort sie beendet."""
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]
    _answer(env, bridge, seq, status="rejected", curve=None, shift=None, reason="unplausibel")
    assert "smartheat_datenfehler" in [entry[0] for entry in env.ha.persistent]
    assert bridge.notifier.state("datenfehler") == "server:unplausibel"
    _advance(env, bridge, 30)

    with monkeypatch.context() as patch:
        _break_backup_writes(patch)
        _answer(env, bridge, seq)
        assert _delivery(bridge).datenfehler == DataFault("local", ("datentraeger",))
        assert env.ha.pushes.count(HA_TEXTS.storage_failed) == 1

    _advance(env, bridge, 300)  # EV_HEALTH: flush gelingt, Datentraeger entwarnt
    _advance(env, bridge, 30)
    _answer(env, bridge, seq)

    assert _delivery(bridge).datenfehler is None
    assert bridge.notifier.state("datenfehler") == "ok"
    assert "smartheat_datenfehler" in env.ha.dismissed
    assert env.ha.pushes.count(HA_TEXTS.storage_failed) == 1


def test_pure_disk_fault_cycle_adds_no_data_fault_push(env, monkeypatch):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]
    _break_backup_writes(monkeypatch)
    _answer(env, bridge, seq)
    monkeypatch.undo()
    _advance(env, bridge, 300)
    _advance(env, bridge, 30)
    pushes_before = list(env.ha.pushes)

    _answer(env, bridge, seq)

    assert env.ha.pushes == pushes_before
    assert pushes_before == [HA_TEXTS.storage_failed, datentraeger.OK_MESSAGE]
    assert _delivery(bridge).datenfehler is None


def test_repaired_disk_reaches_the_delivery_in_the_next_health_tick(env, monkeypatch):
    """TP12b (2): die Antwort scheitert vier Mal am Datentraeger (Daten-Wiederholungen 30, 300, 900 s, danach wartet
    der Tick 3600 s). Nach der Reparatur holt der naechste Takt EV_HEALTH (300 s, Pruefintervall) die Antwort, nicht
    erst der Daten-Retry bis zu 60 min spaeter."""
    _quiet_backup(env)
    bridge = _start(env)
    _connect(env, bridge)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]
    with monkeypatch.context() as patch:
        _break_backup_writes(patch)
        _answer(env, bridge, seq)
        for wait in (30, 300, 900):
            _advance(env, bridge, wait)
            assert _mqtt(env).snapshots[-1]["seq"] == seq
            _answer(env, bridge, seq)
        assert _delivery(bridge).pending.stage == 4  # naechste Wartezeit 3600 s
        assert _delivery(bridge).datenfehler == DataFault("local", ("datentraeger",))
    sent = len(_mqtt(env).snapshots)

    # Der letzte Takt EV_HEALTH lief beim Vorspulen um 900 s mit, der naechste folgt 300 s danach (Pruefintervall,
    # dokument_config.DEFAULT_LOCAL_CHECK_INTERVAL_SECONDS). Repariert, aber 1 s vor dem Takt: die Zustellung wartet.
    _advance(env, bridge, 299)
    assert len(_mqtt(env).snapshots) == sent

    _advance(env, bridge, 1)  # Takt EV_HEALTH: flush gelingt, der Tick geht sofort

    assert [s["seq"] for s in _mqtt(env).snapshots[sent:]] == [seq]
    _answer(env, bridge, seq)
    assert _delivery(bridge).pending is None
    assert _delivery(bridge).datenfehler is None
    assert _last_event(env)["status"] == "regelt"


def test_permanently_missing_outdoor_temperature_is_no_data_fault_and_telemetry_omits_it(env):
    """Heutiges Verhalten, Restpunkt R3-1: faellt eine Pflicht-Rolle dauerhaft aus (client1 2026-10-07: Aussentemperatur
    37 min unavailable), entsteht ohne Tick kein Datenfehler, und die Telemetrie laesst das Feld still weg."""
    _quiet_backup(env)
    bridge = _start(env)
    _connect(env, bridge)
    assert _mqtt(env).telemetry[-1]["outdoor_temp"] == 5.0  # vorher gemeldet

    env.ha.states["sensor.outdoor_temp"] = ValueError("could not convert string to float: 'unavailable'")
    sent = len(_mqtt(env).telemetry)
    for _ in range(8):  # 40 min im Takt 300 s
        _advance(env, bridge, 300)

    payloads = _mqtt(env).telemetry[sent:]
    assert len(payloads) >= 7
    assert all("outdoor_temp" not in p and "datenfehler" not in p for p in payloads)
    assert _delivery(bridge).datenfehler is None
    assert (_last_event(env)["status"], _last_event(env)["datenfehler"]) == ("regelt", None)


def test_telemetry_reports_the_unwritable_disk_as_local_data_fault(env, monkeypatch):
    _quiet_backup(env)
    bridge = _start(env)
    _break_backup_writes(monkeypatch)
    _set_room_target(env, bridge, 20.5)

    _advance(env, bridge, 300)  # Telemetrie-Takt

    assert _mqtt(env).telemetry[-1]["datenfehler"] == {"source": "local", "detail": ["datentraeger"]}


def test_unwritable_disk_on_answer_reports_one_message_and_writes_nothing(env, monkeypatch):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]
    writes_before = list(env.ha.writes)
    _break_backup_writes(monkeypatch)

    _answer(env, bridge, seq)

    assert _delivery(bridge).datenfehler == DataFault("local", ("datentraeger",))
    assert _delivery(bridge).notbetrieb is False
    assert env.ha.pushes == [HA_TEXTS.storage_failed]
    assert env.ha.writes == writes_before


# --- TP12b: Pruef-Tick und Soll beim Start ---

def test_probe_tick_sends_target_change_without_booking_the_target(env):
    _quiet_backup(env)
    bridge = _start(env)

    ticks.start_probe_tick(bridge, "Test")
    bridge.worker.run_pending()

    assert [s["trigger"] for s in _mqtt(env).snapshots] == ["target_change"]
    assert _backup(env)["last_published_target_rt"] == 21.0


def test_unreadable_room_target_entity_at_start_does_not_hold_the_due_daily_tick(env):
    # Spec 5c 1.3: der Raum-Soll kommt aus dem Soll-Speicher (Start aus der Bedienung des Servers), eine beim Start
    # unlesbare Soll-Entity ist kein Datenfehler mehr (bis 0.36: lokaler Datenfehler room_target, Tick gehalten).
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    _quiet_backup(env, last_daily_trigger_date=yesterday)
    env.ha.states["sensor.room_target"] = ValueError("could not convert string to float: 'unavailable'")
    bridge = _start(env, daily_trigger_time="00:00", bedienung={"raum_soll": 21.0})

    _trigger(env, bridge, "sensor.room_actual")

    assert _delivery(bridge).pending.trigger == "daily"
    assert _delivery(bridge).datenfehler is None
    assert env.ha.pushes == []
    assert [(s["trigger"], s["room_target"]) for s in _mqtt(env).snapshots] == [("daily", 21.0)]


# --- TP12b: Verbindungswaechter (AU-033) ---

NOTBETRIEB_OFF = "Heizungsbrücke: Serververbindung wiederhergestellt, Notbetrieb beendet."


def _start_without_broker(env):
    _quiet_backup(env)
    bridge = _start_bridge(env)
    _mqtt(env).connected = False
    bridge.worker.run_pending()
    return bridge


def _into_notbetrieb_without_broker(env, bridge):
    for _ in range(3):
        _advance(env, bridge, 300)  # 900 s ohne Verbindung -> Pruef-Tick
    _advance(env, bridge, 30)  # 1. Ack-Timeout (Versuch ungesendet) -> Sofort-Retry
    _advance(env, bridge, 30)  # 2. Ack-Timeout -> Abo aktiv -> Notbetrieb


def test_missing_broker_connection_leads_to_notbetrieb_after_15_minutes(env):
    bridge = _start_without_broker(env)
    for _ in range(2):
        _advance(env, bridge, 300)
    assert _delivery(bridge).pending is None

    _into_notbetrieb_without_broker(env, bridge)

    assert _delivery(bridge).notbetrieb is True
    assert env.ha.pushes == [NOTBETRIEB_ON]
    assert _last_event(env)["status"] == "notbetrieb"
    assert _mqtt(env).snapshots == []


def test_reconnect_answers_the_probe_and_ends_notbetrieb(env):
    bridge = _start_without_broker(env)
    _into_notbetrieb_without_broker(env, bridge)

    _mqtt(env).connected = True
    _connect(env, bridge)
    probe = _mqtt(env).snapshots[-1]
    _answer(env, bridge, probe["seq"], curve=0.9, shift=22.0)

    assert probe["trigger"] == "target_change"
    assert _delivery(bridge).notbetrieb is False
    assert env.ha.pushes[-1] == NOTBETRIEB_OFF
    assert _last_event(env)["status"] == "regelt"


def test_interrupted_outages_do_not_add_up(env):
    _quiet_backup(env)
    bridge = _start(env)
    for connected in (False, False, True, False, False, True):
        _mqtt(env).connected = connected
        _advance(env, bridge, 300)

    assert _delivery(bridge).pending is None


def test_open_tick_during_an_outage_is_not_replaced_by_a_probe(env):
    _quiet_backup(env)
    bridge = _start(env)
    _mqtt(env).connected = False
    _set_room_target(env, bridge, 20.5)
    seq = _delivery(bridge).pending.seq

    for _ in range(4):
        _advance(env, bridge, 300)

    assert _delivery(bridge).pending.seq == seq


def test_telemetry_is_not_published_without_connection(env):
    _quiet_backup(env)
    bridge = _start(env)
    sent = len(_mqtt(env).telemetry)
    _mqtt(env).connected = False

    _advance(env, bridge, 300)

    assert len(_mqtt(env).telemetry) == sent


def test_vaillant_daily_energy_reaches_the_server_as_a_growing_sum(env):
    """Der Tageszaehler der Bindung (Vaillant: daily) geht als wachsende Summe hinaus (A4-49); die Waerme-Erkennung
    liegt beim Server (Audit 4 P-B)."""
    env.ha.states.update({"sensor.waerme_heute": 8.0, "sensor.waerme_heute::unit_of_measurement": "kWh",
                          "sensor.waerme_heute::device_class": "energy"})
    bridge = _start(env, entity_energy_thermal_heating="sensor.waerme_heute")
    assert _mqtt(env).telemetry[-1]["energy"] == {"thermal_heating": 8.0}

    env.ha.states["sensor.waerme_heute"] = 0.5  # Mitternacht
    _advance(env, bridge, 300)

    assert _mqtt(env).telemetry[-1]["energy"] == {"thermal_heating": 8.5}
    assert "waerme" not in _mqtt(env).telemetry[-1]


def test_vaillant_energy_in_a_foreign_unit_is_left_out(env, caplog):
    """Es gilt die erwartete Einheit der Bindung (Vaillant: kWh, Spec Stufe 3 S4b 5): ein Wh-Tageszaehler fehlt."""
    env.ha.states.update({"sensor.waerme_heute": 8000.0, "sensor.waerme_heute::unit_of_measurement": "Wh",
                          "sensor.waerme_heute::device_class": "energy"})
    with caplog.at_level(logging.WARNING):
        _start(env, entity_energy_thermal_heating="sensor.waerme_heute")

    assert _mqtt(env).telemetry and "energy" not in _mqtt(env).telemetry[-1]
    assert "Energiekanal 'thermal_heating' meldet 'Wh' statt kWh und wird weggelassen" in caplog.text


def _ausfall_mit_fehlversuchen(env, failures):
    """Laufende Laufzeit; dann faellt der Link aus und `failures` Verbindungsversuche scheitern. Der erste Lauf des
    Verbindungswaechters (300 s) merkt den Beginn des Ausfalls."""
    _quiet_backup(env)
    bridge = _start(env)
    _mqtt(env).connected = False
    _mqtt(env).connect_failures = failures
    _advance(env, bridge, 300)
    return bridge


@pytest.mark.parametrize(("failures", "down_seconds", "queried"), [
    (3, 900, True), (2, 900, False), (3, 899, False), (0, 2000, False),
], ids=["3-fehlversuche-900s", "2-fehlversuche", "899s", "ohne-fehlversuche"])
def test_a_long_outage_with_repeated_connect_failures_reads_the_abo_status_once(env, failures, down_seconds, queried):
    bridge = _ausfall_mit_fehlversuchen(env, failures)
    before = env.abo["queries"]

    _advance(env, bridge, down_seconds)

    assert env.abo["queries"] - before == (1 if queried else 0)  # Abfragen der Abo-Quelle des Dokuments


def test_an_inactive_abo_found_during_an_outage_starts_no_probe_tick(env):
    bridge = _ausfall_mit_fehlversuchen(env, 3)
    env.abo["status"] = entitlement.INACTIVE
    assert _abo_inactive_since(bridge) is None

    _advance(env, bridge, 900)

    assert _abo_inactive_since(bridge) is not None
    assert _delivery(bridge).pending is None


@pytest.mark.parametrize("status", [entitlement.ACTIVE, entitlement.UNKNOWN])
def test_an_active_or_unclear_abo_during_an_outage_still_starts_the_probe_tick(env, status):
    """Regression: ein normaler Ausfall (Abo aktiv oder Abfrage unklar) darf den Notbetrieb nicht verhindern."""
    bridge = _ausfall_mit_fehlversuchen(env, 3)
    env.abo["status"] = status
    pushes = list(env.ha.pushes)
    assert _delivery(bridge).pending is None

    _advance(env, bridge, 900)

    assert _delivery(bridge).pending is not None and _delivery(bridge).pending.trigger == "target_change"  # Pruef-Tick
    assert _abo_inactive_since(bridge) is None
    assert env.ha.pushes == pushes  # keine Meldung an den Kunden


def test_a_reconnect_resets_the_throttle_of_the_abo_query(env):
    bridge = _ausfall_mit_fehlversuchen(env, 3)
    _advance(env, bridge, 900)
    assert bridge.connection_failing_queried_at is not None

    _mqtt(env).connected = True
    _connect(env, bridge)

    assert bridge.connection_failing_queried_at is None


def test_successful_zone_preparation_costs_no_quota_across_restarts(env):
    _quiet_backup(env)
    for _ in range(8):  # mehr Neustarts als das Tageslimit
        _start(env)
    assert "write_budget" not in _backup(env)


def test_min_flow_is_not_written_to_an_unavailable_entity(env):
    _quiet_backup(env)
    bridge = _start(env)
    env.ha.states["number.min_flow"] = ValueError("could not convert string to float: 'unavailable'")

    _set_room_target(env, bridge, 20.5)

    assert [write for write in env.ha.writes if write[0] == "number.min_flow"] == []
    assert bridge.override.last_written("min_flow") is None


# --- Testoffensive T8 (Mutationsprobe K2, ticks.py) ---

def test_an_answer_with_a_foreign_seq_is_ignored_and_the_log_names_the_expected_seq(env, caplog):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]

    with caplog.at_level(logging.INFO, logger="smartheat_runtime.ticks"):
        _answer(env, bridge, "fremd")

    assert f"Setpoints-Antwort mit seq='fremd' ignoriert (erwartet: {seq!r})" in [r.getMessage() for r in caplog.records]
    assert _delivery(bridge).pending.seq == seq


def test_a_second_delivery_of_the_answer_without_an_open_tick_is_ignored(env, caplog):
    """QoS 1 stellt eine Antwort auch doppelt zu; ohne offenen Tick ist sie bedeutungslos, kein Fehler."""
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]
    _answer(env, bridge, seq)
    assert _delivery(bridge).pending is None

    with caplog.at_level(logging.INFO):
        _answer(env, bridge, seq)

    assert f"Setpoints-Antwort mit seq={seq!r} ignoriert (erwartet: None)" in [r.getMessage() for r in caplog.records]
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert _delivery(bridge).pending is None


@pytest.mark.parametrize("reason", [5, {"grund": "x"}, ""], ids=["zahl", "objekt", "leer"])
def test_a_rejection_whose_reason_is_no_text_is_reported_without_reason(env, reason):
    _quiet_backup(env)
    bridge = _start(env)
    _set_room_target(env, bridge, 20.5)
    seq = _mqtt(env).snapshots[0]["seq"]

    _answer(env, bridge, seq, status="rejected", curve=None, shift=None, reason=reason)

    assert env.ha.pushes == ["Heizungsbrücke: Server hat die Messwerte abgelehnt (ohne Begründung). Die Heizkurve bleibt "
                             "unverändert."]
    assert _delivery(bridge).datenfehler == DataFault("server", ("ohne Begründung",))


def test_a_manual_override_changed_after_the_publish_survives_the_answer_to_the_previous_one(env, monkeypatch):
    """Der Snapshot trug den gespeicherten Eingriff OVERRIDE; danach erkennt die Durchsetzung einen neuen Wert. Die
    Antwort loescht nur genau den gesendeten Eintrag, der neue reist mit dem naechsten Tick."""
    _quiet_backup(env, manual_override_pending=OVERRIDE)
    _pruefintervall(monkeypatch, 5)
    bridge = _start(env)
    _settle(env, bridge)
    _set_room_target(env, bridge, 20.5)
    snapshot = _mqtt(env).snapshots[-1]
    assert snapshot["manual_override"] == OVERRIDE

    env.ha.states.update({"number.curve_current": 1.2, "number.shift_current": 24.5})
    _advance(env, bridge, 5)
    _advance(env, bridge, 5)  # zwei Runden mit Abweichung, der Tick ist noch offen
    assert _backup(env)["manual_override_pending"]["levers"]["curve"] == 1.2

    _answer(env, bridge, snapshot["seq"], curve=0.95, shift=23.0)

    assert _backup(env)["manual_override_pending"]["levers"]["curve"] == 1.2

import math
import time
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from configs import SENSOR, THERMOSTAT
from fake_z2m import FakeZigbee2Mqtt
from fakes import FakeBus

from smartheat_gateway.triggers import EV_TARGET_INPUT, BusTriggerSource, local_zone, seconds_until
from smartheat_gateway.zigbee import ZigbeeMirror
from smartheat_runtime.debounce import ROOM_TARGET_DEBOUNCE_SECONDS
from smartheat_runtime.runtime import EV_LOCAL_CHECK, EV_SOURCE_CONNECTED
from smartheat_runtime.soll_eingang import FROSTSCHUTZ_BIS
from smartheat_runtime.soll_speicher import TargetStore
from smartheat_runtime.worker import Event, RegulationWorker

BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture
def world(tmp_path, clock):
    bus = FakeBus()
    z2m = FakeZigbee2Mqtt(bus)
    mirror = ZigbeeMirror(bus, clock)
    mirror.start()
    z2m.bridge(online=True)
    z2m.add_sensor(SENSOR)
    z2m.add_thermostat(THERMOSTAT)
    worker = RegulationWorker(clock=clock)
    checks = []
    worker.register(EV_LOCAL_CHECK, lambda event: checks.append(("check", event.data.get("room_target_fired", False))))
    worker.register(EV_SOURCE_CONNECTED, lambda event: checks.append(("connected", None)))
    store = TargetStore(tmp_path / "room_target.json", 20.0, clock, lambda: "ts")
    trigger = BusTriggerSource(
        bus, worker, mirror, store, thermostat=THERMOSTAT, sensor_ieees=(SENSOR,), daily_trigger_time="12:00",
        now=lambda: datetime(2026, 1, 15, 11, 59, 0, tzinfo=BERLIN),
    )
    trigger.start()
    worker.run_pending()
    checks.clear()
    return bus, z2m, worker, store, checks


def test_connect_posts_the_contract(world):
    bus, _, worker, _, checks = world
    bus.disconnect()
    bus.connect()
    worker.run_pending()
    assert ("check", True) in checks and ("connected", None) in checks


def _quelle_am_bus(tmp_path, clock, *, verbunden):
    bus = FakeBus()
    if not verbunden:
        bus.disconnect()
    mirror = ZigbeeMirror(bus, clock)
    worker = RegulationWorker(clock=clock)
    checks = []
    worker.register(EV_LOCAL_CHECK, lambda event: checks.append(("check", event.data.get("room_target_fired"))))
    worker.register(EV_SOURCE_CONNECTED, lambda event: checks.append(("connected", None)))
    store = TargetStore(tmp_path / "room_target.json", 20.0, clock, lambda: "ts")
    trigger = BusTriggerSource(bus, worker, mirror, store, thermostat=None, sensor_ieees=(), daily_trigger_time=None)
    return bus, trigger, worker, checks


def test_start_on_a_connected_bus_posts_the_contract_at_once(tmp_path, clock):
    _, trigger, worker, checks = _quelle_am_bus(tmp_path, clock, verbunden=True)
    trigger.start()
    worker.run_pending()
    assert ("check", True) in checks and ("connected", None) in checks


def test_start_on_a_disconnected_bus_posts_nothing_until_it_connects(tmp_path, clock):
    bus, trigger, worker, checks = _quelle_am_bus(tmp_path, clock, verbunden=False)
    trigger.start()
    worker.run_pending()
    assert checks == []
    bus.connect()
    worker.run_pending()
    assert ("check", True) in checks and ("connected", None) in checks


def test_connected_follows_the_bus(tmp_path, clock):
    bus, trigger, _, _ = _quelle_am_bus(tmp_path, clock, verbunden=True)
    assert trigger.connected is True
    bus.disconnect()
    assert trigger.connected is False


def test_sensor_change_posts_an_undebounced_check(world):
    _, z2m, worker, _, checks = world
    z2m.report(SENSOR, temperature=20.4)
    worker.run_pending()
    assert checks == [("check", False)]


def test_portal_target_is_debounced_and_written_to_the_thermostat(world, clock):
    bus, _, worker, store, checks = world
    worker.post(Event(EV_TARGET_INPUT, {"value": 22.0, "source": "portal"}))
    worker.run_pending()
    assert store.value == 22.0 and checks == []
    assert (f"zigbee2mqtt/{THERMOSTAT}/set", {"occupied_heating_setpoint": 22.0}) in bus.decoded()
    clock.advance(10)
    worker.run_pending()
    assert ("check", True) in checks
    assert store.source == "portal"


def test_portal_target_after_a_thermostat_change_takes_over_the_source(world, clock):
    bus, z2m, worker, store, _ = world
    z2m.report(THERMOSTAT, occupied_heating_setpoint=19.0, local_temperature=20.0)
    worker.run_pending()
    assert (store.value, store.source) == (19.0, "thermostat")
    worker.post(Event(EV_TARGET_INPUT, {"value": 22.0, "source": "portal"}))
    worker.run_pending()
    assert (store.value, store.source) == (22.0, "portal")


def test_portal_value_outside_the_range_is_dropped_by_the_runtime(world, clock):
    bus, _, worker, store, checks = world
    worker.post(Event(EV_TARGET_INPUT, {"value": 30.0, "source": "portal"}))
    worker.run_pending()
    clock.advance(10)
    worker.run_pending()
    assert store.value == 20.0 and checks == []


def test_thermostat_change_is_taken_after_the_echo_window(world, clock):
    _, z2m, worker, store, checks = world
    z2m.report(THERMOSTAT, occupied_heating_setpoint=19.0, local_temperature=20.0)
    worker.run_pending()
    assert store.value == 19.0
    clock.advance(10)
    worker.run_pending()
    assert ("check", True) in checks


def test_thermostat_change_inside_the_echo_window_is_taken_from_a_later_identical_report(world, clock):
    bus, z2m, worker, store, checks = world
    worker.post(Event(EV_TARGET_INPUT, {"value": 22.0, "source": "portal"}))
    worker.run_pending()  # schreibt 22 ans Thermostat, Echo-Fenster 60 s
    clock.advance(30)
    z2m.report(THERMOSTAT, occupied_heating_setpoint=23.0, local_temperature=20.0)  # Nutzer dreht im Fenster
    worker.run_pending()
    assert store.value == 22.0  # im Fenster als Echo verworfen
    clock.advance(31)
    z2m.report(THERMOSTAT, occupied_heating_setpoint=23.0, local_temperature=20.1)  # naechste Meldung, gleicher Wert
    worker.run_pending()
    assert (store.value, store.source) == (23.0, "thermostat")
    clock.advance(10)
    worker.run_pending()
    assert ("check", True) in checks


def test_daily_trigger_time_posts_a_check(world, clock):
    _, _, worker, _, checks = world
    clock.advance(61)
    worker.run_pending()
    assert ("check", False) in checks


def test_daily_tick_across_the_autumn_change():
    # 2026-10-25 03:00 MESZ -> 02:00 MEZ: der Tag hat 25 Stunden. now = 24.10. 12:12 MESZ (fester Offset wie wallclock).
    now = datetime(2026, 10, 24, 12, 12, tzinfo=timezone(timedelta(hours=2)))
    assert seconds_until("12:12", now, BERLIN) == 25 * 3600


def test_daily_tick_across_the_spring_change():
    # 2026-03-29 02:00 MEZ -> 03:00 MESZ: 23 Stunden.
    now = datetime(2026, 3, 28, 12, 12, tzinfo=timezone(timedelta(hours=1)))
    assert seconds_until("12:12", now, BERLIN) == 23 * 3600


def test_daily_tick_same_day_and_default_zone(monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    now = datetime(2026, 7, 1, 10, 0, tzinfo=timezone(timedelta(hours=2)))
    assert seconds_until("12:12", now, BERLIN) == 2 * 3600 + 12 * 60
    assert seconds_until("12:12", now) == 2 * 3600 + 12 * 60  # ohne Zone: Offset von now


def test_daily_tick_zone_comes_from_tz(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    now = datetime(2026, 10, 24, 12, 12, tzinfo=timezone(timedelta(hours=2)))
    assert seconds_until("12:12", now) == 25 * 3600


def test_the_zone_of_the_daily_tick_comes_from_tz_not_from_the_machine(monkeypatch):
    # Asia/Tokyo hat nie Sommerzeit und liegt weit von jeder Zeitzone des Rechners: nur die Zone aus TZ gibt 3600
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    now = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)  # in Tokyo 19:00
    assert local_zone(now) == ZoneInfo("Asia/Tokyo")
    assert seconds_until("20:00", now) == 3600


def test_without_tz_the_zone_is_the_offset_of_now_else_utc(monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    indien = timezone(timedelta(hours=5, minutes=30))
    now = datetime(2026, 7, 1, 10, 0, tzinfo=indien)
    assert local_zone(now) is indien
    assert seconds_until("12:00", now) == 2 * 3600  # Ortszeit des Offsets, nicht die des Rechners
    assert local_zone() is UTC
    assert local_zone(datetime(2026, 7, 1, 10, 0)) is UTC  # naive Zeit ohne Offset


@pytest.mark.parametrize("name", ["Nicht/Vorhanden", "../etc/passwd"])
def test_an_unknown_tz_name_falls_back_to_the_offset_of_now(monkeypatch, name):
    monkeypatch.setenv("TZ", name)
    indien = timezone(timedelta(hours=5, minutes=30))
    assert local_zone(datetime(2026, 7, 1, 10, 0, tzinfo=indien)) is indien


@pytest.mark.parametrize(("tageszeit", "jetzt", "erwartet_s"), [
    ("00:00", datetime(2026, 1, 15, 12, 0, tzinfo=BERLIN), 12 * 3600),
    ("00:00", datetime(2026, 1, 15, 23, 59, tzinfo=BERLIN), 60),
    ("00:00", datetime(2026, 1, 15, 0, 0, tzinfo=BERLIN), 24 * 3600),      # genau jetzt: erst morgen
    ("00:00", datetime(2026, 1, 15, 0, 1, tzinfo=BERLIN), 24 * 3600 - 60),
    ("23:59", datetime(2026, 1, 15, 12, 0, tzinfo=BERLIN), 11 * 3600 + 59 * 60),
    ("23:59", datetime(2026, 1, 15, 23, 58, tzinfo=BERLIN), 60),
    ("23:59", datetime(2026, 1, 15, 23, 59, tzinfo=BERLIN), 24 * 3600),    # genau jetzt: erst morgen
    ("23:59", datetime(2026, 1, 15, 23, 59, 30, tzinfo=BERLIN), 24 * 3600 - 30),
])
def test_the_daily_tick_around_midnight(tageszeit, jetzt, erwartet_s):
    assert seconds_until(tageszeit, jetzt, BERLIN) == erwartet_s


def _berlin(tag, stunde, minute, fold=0, monat=10):
    return datetime(2026, monat, tag, stunde, minute, tzinfo=BERLIN, fold=fold)


# 2026-10-25: um 03:00 MESZ springt die Uhr auf 02:00 MEZ zurueck, die Stunde 02:00-02:59 gibt es zweimal (fold 0, fold 1).
# Der Tagestick feuert zur ersten Ortszeit der Tageszeit, also genau einmal je Tag; danach ist der naechste in einem Tag.
@pytest.mark.parametrize(("tageszeit", "jetzt", "erwartet_s"), [
    ("02:30", _berlin(25, 1, 30), 3600),
    ("02:30", _berlin(25, 2, 10), 1200),
    ("02:30", _berlin(25, 2, 10, fold=1), 87600),   # zweiter Durchlauf der Stunde: der Tick von 02:30 MESZ ist schon vorbei
    ("02:30", _berlin(25, 2, 30), 90000),            # genau auf der Tageszeit: erst morgen, der Tag hat 25 Stunden
    ("02:30", _berlin(25, 2, 40), 89400),
    ("02:30", _berlin(25, 2, 40, fold=1), 85800),
    ("02:30", _berlin(25, 3, 0), 84600),
    ("02:00", _berlin(25, 2, 10), 89400),
    ("02:00", _berlin(25, 2, 30, fold=1), 84600),
    ("03:00", _berlin(25, 2, 10, fold=1), 3000),      # Gegenprobe: eine Zeit ausserhalb der doppelten Stunde bleibt heute
])
def test_the_daily_tick_on_the_day_the_clock_is_set_back(tageszeit, jetzt, erwartet_s):
    assert seconds_until(tageszeit, jetzt, BERLIN) == erwartet_s


def test_the_daily_tick_is_never_zero_or_negative_around_the_clock_changes():
    """Rasterprobe ueber beide Zeitumstellungen und die Nachbartage (Minutenraster): nie <= 0, nie mehr als 25 Stunden."""
    for start in (datetime(2026, 10, 24, 12, 0, tzinfo=UTC), datetime(2026, 3, 28, 12, 0, tzinfo=UTC)):
        for minute in range(0, 3 * 24 * 60, 7):
            jetzt = start + timedelta(minutes=minute)
            for tageszeit in ("00:00", "01:59", "02:00", "02:30", "02:59", "03:00", "23:59"):
                wert = seconds_until(tageszeit, jetzt, BERLIN)
                assert 0 < wert <= 25 * 3600, (tageszeit, jetzt, wert)


def test_the_daily_tick_is_planned_again_after_it_fired(tmp_path, clock):
    """Die Uhr steht genau auf der Tageszeit: seconds_until liefert dann einen vollen Tag, nie 0 (kein Dauerfeuer);
    nach dem Feuern ist der naechste Tick wieder in 24 Stunden geplant, und so weiter."""
    bus = FakeBus()
    mirror = ZigbeeMirror(bus, clock)
    mirror.start()
    worker = RegulationWorker(clock=clock)
    checks = []
    worker.register(EV_LOCAL_CHECK, lambda event: checks.append(event.data.get("room_target_fired")))
    worker.register(EV_SOURCE_CONNECTED, lambda event: None)
    store = TargetStore(tmp_path / "room_target.json", 20.0, clock, lambda: "ts")
    BusTriggerSource(bus, worker, mirror, store, thermostat=None, sensor_ieees=(), daily_trigger_time="12:00",
                     now=lambda: datetime(2026, 1, 15, 12, 0, tzinfo=BERLIN)).start()
    worker.run_pending()
    checks.clear()  # der Verbindungs-Check beim Start
    clock.advance(24 * 3600 - 1)
    worker.run_pending()
    assert checks == []  # eine Sekunde vor dem ersten Tick
    clock.advance(1)
    worker.run_pending()
    assert checks == [False]
    clock.advance(24 * 3600 - 1)
    worker.run_pending()
    assert checks == [False]  # der zweite Tick ist erst in einem vollen Tag faellig, kein Dauerfeuer
    clock.advance(1)
    worker.run_pending()
    assert checks == [False, False]


def test_sleepy_thermostat_does_not_overwrite_the_portal_target(world, clock):
    bus, z2m, worker, store, _ = world
    z2m.report(THERMOSTAT, occupied_heating_setpoint=20.0, local_temperature=20.0)  # Ausgangszustand = Soll 20
    worker.run_pending()
    z2m.make_sleepy(THERMOSTAT)
    worker.post(Event(EV_TARGET_INPUT, {"value": 22.0, "source": "portal"}))
    worker.run_pending()
    clock.advance(300)
    z2m.wake(THERMOSTAT, stale_setpoint=20.0)  # erst der alte Stand, dann der geschriebene Wert
    worker.run_pending()
    assert (store.value, store.source) == (22.0, "portal")
    z2m.report(THERMOSTAT, occupied_heating_setpoint=19.0, local_temperature=20.0)  # Nutzer dreht danach
    worker.run_pending()
    assert (store.value, store.source) == (19.0, "thermostat")


@pytest.fixture
def mit_geraet(tmp_path, clock):
    bus = FakeBus()
    z2m = FakeZigbee2Mqtt(bus)
    mirror = ZigbeeMirror(bus, clock)
    mirror.start()
    z2m.bridge(online=True)
    z2m.add_sensor(SENSOR)
    z2m.add_thermostat(THERMOSTAT)
    worker = RegulationWorker(clock=clock)
    welt = SimpleNamespace(bus=bus, z2m=z2m, worker=worker, checks=[], wuensche=[], aus=[])
    worker.register(EV_LOCAL_CHECK, lambda event: welt.checks.append(event.data.get("room_target_fired", False)))
    worker.register(EV_SOURCE_CONNECTED, lambda event: None)

    def wunsch(value):
        welt.wuensche.append(value)
        return min(25.0, max(15.0, math.floor(value * 2 + 0.5) / 2))

    welt.store = TargetStore(tmp_path / "room_target.json", 20.0, clock, lambda: "ts")
    BusTriggerSource(bus, worker, mirror, welt.store, thermostat=THERMOSTAT, sensor_ieees=(SENSOR,),
                     daily_trigger_time=None, wunsch=wunsch,
                     soll_quelle_aus=lambda aus, wirksam: welt.aus.append((aus, wirksam))).start()
    worker.run_pending()
    welt.checks.clear()
    return welt


def _setpoint_writes(welt):
    return [body["occupied_heating_setpoint"] for topic, body in welt.bus.decoded()
            if topic == f"zigbee2mqtt/{THERMOSTAT}/set"]


def test_turning_the_knob_sends_one_limited_wish_after_the_debounce(mit_geraet, clock):
    welt = mit_geraet
    for value in (18.0, 16.0, 14.0):  # Nutzer dreht nachts herunter, das Thermostat meldet Zwischenwerte
        welt.z2m.report(THERMOSTAT, occupied_heating_setpoint=value, local_temperature=20.0)
        welt.worker.run_pending()
        clock.advance(3)
    assert welt.wuensche == [] and welt.checks == []
    clock.advance(10)
    welt.worker.run_pending()
    assert welt.wuensche == [14.0]
    assert (welt.store.value, welt.store.source) == (15.0, "thermostat")
    assert _setpoint_writes(welt) == [15.0] and welt.checks == [True]
    # das Thermostat zeigt 15 (Echo) und meldet danach noch einen veralteten Zwischenwert: kein neuer Wunsch, kein Schreiben
    welt.z2m.report(THERMOSTAT, occupied_heating_setpoint=15.0, local_temperature=20.0)
    welt.worker.run_pending()
    welt.z2m.report(THERMOSTAT, occupied_heating_setpoint=14.0, local_temperature=20.0)
    welt.worker.run_pending()
    clock.advance(10)
    welt.worker.run_pending()
    assert welt.wuensche == [14.0] and _setpoint_writes(welt) == [15.0]
    assert (welt.store.value, welt.store.source) == (15.0, "thermostat")


def test_a_wish_inside_the_range_is_not_written_back(mit_geraet, clock):
    welt = mit_geraet
    welt.z2m.report(THERMOSTAT, occupied_heating_setpoint=21.5, local_temperature=20.0)
    welt.worker.run_pending()
    clock.advance(10)
    welt.worker.run_pending()
    assert welt.wuensche == [21.5] and _setpoint_writes(welt) == [] and welt.store.value == 21.5


def test_thermostat_off_is_no_wish_and_keeps_new_operations_off_the_thermostat(mit_geraet, clock):
    welt = mit_geraet
    welt.z2m.report(THERMOSTAT, system_mode="off")
    welt.worker.run_pending()
    assert welt.aus == [(True, 20.0)]
    welt.z2m.report(THERMOSTAT, occupied_heating_setpoint=18.0, local_temperature=20.0)
    welt.worker.post(Event(EV_TARGET_INPUT, {"value": 22.0, "source": "portal"}))
    welt.worker.run_pending()
    clock.advance(10)
    welt.worker.run_pending()
    assert welt.wuensche == [] and welt.store.value == 22.0 and _setpoint_writes(welt) == []
    welt.z2m.report(THERMOSTAT, system_mode="heat")
    welt.worker.run_pending()
    assert welt.aus == [(True, 20.0), (False, None)]


def test_the_frost_position_is_no_wish(mit_geraet, clock):
    welt = mit_geraet
    welt.z2m.report(THERMOSTAT, occupied_heating_setpoint=FROSTSCHUTZ_BIS, local_temperature=20.0)
    welt.worker.run_pending()
    clock.advance(10)
    welt.worker.run_pending()
    assert welt.aus == [(True, 20.0)] and welt.wuensche == [] and welt.store.value == 20.0
    welt.z2m.report(THERMOSTAT, occupied_heating_setpoint=21.0, local_temperature=20.0)
    welt.worker.run_pending()
    clock.advance(10)
    welt.worker.run_pending()
    assert welt.aus[-1] == (False, None) and welt.wuensche == [21.0]


def test_an_operation_cancels_a_pending_thermostat_wish(mit_geraet, clock):
    welt = mit_geraet
    welt.z2m.report(THERMOSTAT, occupied_heating_setpoint=19.0, local_temperature=20.0)
    welt.worker.run_pending()
    welt.worker.post(Event(EV_TARGET_INPUT, {"value": 22.0, "source": "portal"}))
    welt.worker.run_pending()
    clock.advance(10)
    welt.worker.run_pending()
    assert welt.wuensche == [] and (welt.store.value, welt.store.source) == (22.0, "portal")
    assert _setpoint_writes(welt) == [22.0]


def test_an_operation_with_the_current_value_writes_nothing(mit_geraet):
    welt = mit_geraet
    welt.worker.post(Event(EV_TARGET_INPUT, {"value": 20.0, "source": "portal"}))
    welt.worker.run_pending()
    assert _setpoint_writes(welt) == []


def test_dialing_down_to_frost_cancels_the_pending_wish(mit_geraet, clock):
    welt = mit_geraet
    for value in (12.0, 9.0, 8.0, 7.0, 5.0):  # Nutzer dreht auf Frostschutz, 8 war kurz ein Wunsch
        welt.z2m.report(THERMOSTAT, occupied_heating_setpoint=value, local_temperature=20.0)
        welt.worker.run_pending()
        clock.advance(1)
    clock.advance(10)
    welt.worker.run_pending()
    assert welt.wuensche == [] and _setpoint_writes(welt) == []
    assert welt.aus == [(True, 20.0)]  # wirksam ist der Wert vor dem verworfenen Wunsch, nicht 8.0
    assert (welt.store.value, welt.store.source) == (20.0, "portal")
    welt.z2m.report(THERMOSTAT, occupied_heating_setpoint=21.0, local_temperature=20.0)
    welt.worker.run_pending()
    assert welt.aus[-1] == (False, None)


def test_thermostat_off_cancels_a_pending_wish(mit_geraet, clock):
    welt = mit_geraet
    welt.z2m.report(THERMOSTAT, occupied_heating_setpoint=17.0, local_temperature=20.0)
    welt.worker.run_pending()
    assert welt.store.value == 17.0
    clock.advance(3)
    welt.z2m.report(THERMOSTAT, system_mode="off")
    welt.worker.run_pending()
    clock.advance(10)
    welt.worker.run_pending()
    assert welt.wuensche == [] and _setpoint_writes(welt) == []
    assert welt.aus == [(True, 20.0)] and welt.store.value == 20.0


# --- Wiedereinschalten ---
# Zigbee2MQTT laeuft mit cache_state: true, jede Meldung traegt den vollen Zustand, auch das alte Soll.


def _melden(welt, mode: str, soll: float) -> None:
    welt.z2m.report(THERMOSTAT, system_mode=mode, occupied_heating_setpoint=soll, local_temperature=20.0)
    welt.worker.run_pending()


@pytest.mark.parametrize("huge", [10**400, -10**400], ids=["gross-positiv", "gross-negativ"])
def test_back_on_with_a_setpoint_beyond_float_range_still_writes_the_stored_target(mit_geraet, clock, caplog, huge):
    """Ein Thermostat-Soll als riesige JSON-Ganzzahl ist ein unbrauchbarer Wert wie Text: die Quelle zeigt nichts, das
    geltende Soll geht trotzdem an das Thermostat (kein OverflowError im Worker, der das Zurueckschreiben verschluckt)."""
    welt = mit_geraet
    _melden(welt, "heat", 20.0)
    _melden(welt, "off", 20.0)
    welt.worker.post(Event(EV_TARGET_INPUT, {"value": 21.0, "source": "portal"}))
    welt.worker.run_pending()
    with caplog.at_level("ERROR"):
        _melden(welt, "heat", huge)
    assert _setpoint_writes(welt) == [21.0]
    assert [r.getMessage() for r in caplog.records if r.name == "smartheat_runtime.worker"] == []


def test_back_on_writes_the_stored_target_so_the_stale_setpoint_is_no_wish(mit_geraet, clock):
    """(a) Aus -> Bedienung 21 vom Server -> wieder an: das Thermostat bekommt 21, die Routinemeldung mit dem alten
    Soll 20 ist kein Wunsch und macht die Bedienung nicht rueckgaengig."""
    welt = mit_geraet
    welt.z2m.make_sleepy(THERMOSTAT)  # batteriebetrieben: der Schreibbefehl wartet bis zum Aufwachen
    _melden(welt, "heat", 20.0)
    _melden(welt, "off", 20.0)
    welt.worker.post(Event(EV_TARGET_INPUT, {"value": 21.0, "source": "portal"}))
    welt.worker.run_pending()
    clock.advance(10)
    welt.worker.run_pending()
    assert welt.store.value == 21.0 and _setpoint_writes(welt) == []  # aus: nicht ans Thermostat
    _melden(welt, "heat", 20.0)  # wieder an, die Meldung traegt noch das alte Soll
    assert _setpoint_writes(welt) == [21.0]
    clock.advance(120)  # nach dem Echo-Fenster: Routinemeldung, das Thermostat hat noch nicht uebernommen
    _melden(welt, "heat", 20.0)
    clock.advance(10)
    welt.worker.run_pending()
    assert welt.wuensche == [] and (welt.store.value, welt.store.source) == (21.0, "portal")
    assert _setpoint_writes(welt) == [21.0] and welt.aus[-1] == (False, None)
    welt.z2m.wake(THERMOSTAT)  # bestaetigt 21; danach ist Drehen wieder ein Wunsch
    welt.worker.run_pending()
    assert welt.wuensche == [] and welt.store.value == 21.0
    _melden(welt, "heat", 19.0)
    clock.advance(10)
    welt.worker.run_pending()
    assert welt.wuensche == [19.0]


def test_back_on_resyncs_the_display_after_a_discarded_wish(mit_geraet, clock):
    """Ein Drehwert kurz vor dem Ausschalten wird verworfen (kein Wunsch); beim Wiedereinschalten zeigt das Thermostat
    wieder das geltende Soll."""
    welt = mit_geraet
    _melden(welt, "heat", 17.0)  # Drehen, noch in der Entprellung
    clock.advance(3)
    _melden(welt, "off", 17.0)
    clock.advance(10)
    welt.worker.run_pending()
    assert welt.wuensche == [] and welt.store.value == 20.0
    _melden(welt, "heat", 17.0)
    clock.advance(10)
    welt.worker.run_pending()
    assert _setpoint_writes(welt) == [20.0] and welt.wuensche == [] and welt.store.value == 20.0


def test_leaving_the_frost_position_by_turning_the_knob_stays_a_wish(mit_geraet, clock):
    """(b) Frostschutz verlassen ist eine echte Aenderung am Thermostat: ein Wunsch, kein Zurueckschreiben."""
    welt = mit_geraet
    _melden(welt, "heat", 6.0)
    welt.worker.post(Event(EV_TARGET_INPUT, {"value": 21.0, "source": "portal"}))
    welt.worker.run_pending()
    _melden(welt, "heat", 19.0)
    clock.advance(10)
    welt.worker.run_pending()
    assert welt.wuensche == [19.0] and _setpoint_writes(welt) == []
    assert (welt.store.value, welt.store.source) == (19.0, "thermostat")


def test_back_on_while_still_in_the_frost_position_writes_nothing(mit_geraet, clock):
    welt = mit_geraet
    _melden(welt, "off", 6.0)
    _melden(welt, "heat", 6.0)  # an, aber Frostschutz bleibt: Soll-Quelle weiter aus
    assert _setpoint_writes(welt) == [] and welt.aus == [(True, 20.0)]
    _melden(welt, "heat", 19.0)  # Drehen aus der Frostschutz-Stellung
    clock.advance(10)
    welt.worker.run_pending()
    assert welt.wuensche == [19.0] and _setpoint_writes(welt) == []


def test_back_on_with_the_thermostat_already_showing_the_target_writes_nothing(mit_geraet, clock):
    """(c) Zeigt das Thermostat schon das geltende Soll, schreibt das Wiedereinschalten nichts (kein doppelter Befehl
    an ein schlafendes Thermostat)."""
    welt = mit_geraet
    _melden(welt, "heat", 20.0)
    _melden(welt, "off", 20.0)
    _melden(welt, "heat", 20.0)
    clock.advance(10)
    welt.worker.run_pending()
    assert _setpoint_writes(welt) == [] and welt.wuensche == [] and welt.aus == [(True, 20.0), (False, None)]


def test_routine_reports_while_on_write_nothing(mit_geraet, clock):
    """Jede Meldung traegt system_mode heat: nur der Wechsel von aus auf an schreibt."""
    welt = mit_geraet
    for _ in range(3):
        _melden(welt, "heat", 20.0)
        clock.advance(60)
    assert _setpoint_writes(welt) == [] and welt.aus == []


def test_back_on_after_two_operations_while_off_treats_the_shown_setpoint_as_stale(mit_geraet, clock):
    """Zwei Bedienungen waehrend "aus" (20 -> 21 -> 22): das Thermostat zeigt noch 20, nicht den vorigen Speicherwert
    21; auch 20 gilt bis zur Bestaetigung als alter Wert."""
    welt = mit_geraet
    welt.z2m.make_sleepy(THERMOSTAT)
    _melden(welt, "off", 20.0)
    for wert in (21.0, 22.0):
        welt.worker.post(Event(EV_TARGET_INPUT, {"value": wert, "source": "portal"}))
        welt.worker.run_pending()
    _melden(welt, "heat", 20.0)
    clock.advance(120)
    _melden(welt, "heat", 20.0)
    clock.advance(10)
    welt.worker.run_pending()
    assert welt.wuensche == [] and welt.store.value == 22.0 and _setpoint_writes(welt) == [22.0]


# --- Aqara W100 als gebundenes Thermostat ---

W100 = "0x54ef44100140eb34"  # Testwert in der IEEE-Form von Zigbee2MQTT
MODUS_W100 = [{"thermostat_mode": "ON"}, {"auto_hide_middle_line": False}]


@pytest.fixture
def mit_w100(tmp_path, clock):
    bus = FakeBus()
    z2m = FakeZigbee2Mqtt(bus)
    mirror = ZigbeeMirror(bus, clock)
    mirror.start()
    z2m.bridge(online=True)
    z2m.add_sensor(SENSOR)
    z2m.add_w100(W100)
    worker = RegulationWorker(clock=clock)
    welt = SimpleNamespace(bus=bus, z2m=z2m, mirror=mirror, worker=worker, wuensche=[], aus=[])
    worker.register(EV_LOCAL_CHECK, lambda event: None)
    worker.register(EV_SOURCE_CONNECTED, lambda event: None)

    def wunsch(value):
        welt.wuensche.append(value)
        return min(25.0, max(15.0, math.floor(value * 2 + 0.5) / 2))

    welt.store = TargetStore(tmp_path / "room_target.json", 20.5, clock, lambda: "ts")
    welt.trigger = BusTriggerSource(bus, worker, mirror, welt.store, thermostat=W100, sensor_ieees=(SENSOR,),
                                    daily_trigger_time=None, wunsch=wunsch, clock=clock,
                                    soll_quelle_aus=lambda aus, wirksam: welt.aus.append((aus, wirksam)))
    welt.trigger.start()
    worker.run_pending()
    return welt


def _w100_sets(welt):
    return [body for ieee, body in welt.z2m.set_requests if ieee == W100]


def _w100_melden(welt, **werte):
    welt.z2m.report(W100, **werte)
    welt.worker.run_pending()


def test_the_start_puts_the_w100_into_thermostat_mode_without_heat_or_a_setpoint(mit_w100):
    # heat nur nach dem Koppeln: ein am Geraet ausgeschaltetes W100 bleibt beim Start aus; ohne bekannten Zustand
    # gleicht der Start nicht an (erst nach gemeldetem ON)
    assert _w100_sets(mit_w100) == MODUS_W100


def test_a_report_of_the_rounded_target_is_no_wish(mit_w100, clock):
    welt = mit_w100
    _w100_melden(welt, occupied_heating_setpoint=21, system_mode="heat", local_temperature=20.0)
    clock.advance(10)
    welt.worker.run_pending()
    assert welt.wuensche == [] and (welt.store.value, welt.store.source) == (20.5, "portal")
    _w100_melden(welt, occupied_heating_setpoint=22, system_mode="heat", local_temperature=20.0)  # Gegenprobe: ein Grad mehr
    clock.advance(10)
    welt.worker.run_pending()
    assert welt.wuensche == [22.0] and welt.store.value == 22.0


def test_a_report_below_the_rounded_target_is_a_wish(mit_w100, clock):
    welt = mit_w100
    _w100_melden(welt, occupied_heating_setpoint=20, system_mode="heat", local_temperature=20.0)
    clock.advance(10)
    welt.worker.run_pending()
    assert welt.wuensche == [20.0] and welt.store.value == 20.0


def test_writing_to_the_w100_rounds_half_up(mit_w100):
    welt = mit_w100
    welt.worker.post(Event(EV_TARGET_INPUT, {"value": 22.5, "source": "portal"}))
    welt.worker.run_pending()
    assert welt.store.value == 22.5 and _w100_sets(welt)[len(MODUS_W100):] == [{"occupied_heating_setpoint": 23.0}]


def test_back_on_with_the_w100_showing_the_rounded_target_writes_nothing(mit_w100):
    welt = mit_w100
    _w100_melden(welt, system_mode="off", occupied_heating_setpoint=21)
    _w100_melden(welt, system_mode="heat", occupied_heating_setpoint=21)
    assert welt.aus == [(True, 20.5), (False, None)]
    assert _w100_sets(welt) == MODUS_W100  # ohne Abbildung schriebe das Wiedereinschalten 21 erneut


@pytest.mark.parametrize("fremd", ["cool", "auto"])
def test_cool_or_auto_on_the_w100_is_switched_back_to_heat_at_most_once_a_minute(mit_w100, clock, fremd):
    welt = mit_w100
    _w100_melden(welt, system_mode=fremd)
    assert _w100_sets(welt) == MODUS_W100 + [{"system_mode": "heat"}]
    clock.advance(59)
    _w100_melden(welt, system_mode=fremd)
    assert _w100_sets(welt) == MODUS_W100 + [{"system_mode": "heat"}]
    clock.advance(1)
    _w100_melden(welt, system_mode=fremd)
    assert _w100_sets(welt) == MODUS_W100 + [{"system_mode": "heat"}] * 2


def test_off_on_the_w100_stays_off(mit_w100):
    welt = mit_w100
    _w100_melden(welt, system_mode="off")
    assert welt.aus == [(True, 20.5)] and _w100_sets(welt) == MODUS_W100


def test_a_button_action_means_the_w100_left_thermostat_mode(mit_w100, clock):
    # Labor 2026-10-21: nach dem Neukoppeln senden die Tasten Aktionen, Z2M meldet weiter thermostat_mode ON
    welt = mit_w100
    _w100_melden(welt, action="W100_PMTSD_request")  # gehoert zum Thermostatmodus
    assert _w100_sets(welt) == MODUS_W100
    _w100_melden(welt, action="single_plus")
    assert _w100_sets(welt) == MODUS_W100 * 2
    clock.advance(30)
    _w100_melden(welt, action="single_minus")
    assert _w100_sets(welt) == MODUS_W100 * 2
    clock.advance(30)
    _w100_melden(welt, action="single_minus")
    assert _w100_sets(welt) == MODUS_W100 * 3


def test_pairing_the_bound_w100_again_sets_it_up_from_the_target_store(mit_w100, clock):
    welt = mit_w100
    welt.z2m.interview(W100)
    welt.worker.run_pending()
    # Labor 2026-10-22: zuerst nur der Modus; Soll und heat erst, wenn das Geraet thermostat_mode ON meldet
    assert _w100_sets(welt) == MODUS_W100 + MODUS_W100 and welt.z2m.verworfen == []
    _w100_melden(welt, thermostat_mode="ON")
    assert _w100_sets(welt) == MODUS_W100 + MODUS_W100 + [{"occupied_heating_setpoint": 21.0}, {"system_mode": "heat"}]
    assert welt.z2m.verworfen == []
    # im Echo-Fenster: das alte Soll des Geraets (verspaetete Meldung), dann das Echo; ohne "off" dazwischen, das
    # verwuerfe einen offenen Wunsch und verdeckte ein fehlendes Echo-Fenster
    _w100_melden(welt, occupied_heating_setpoint=19, system_mode="heat")
    _w100_melden(welt, occupied_heating_setpoint=21, system_mode="heat")
    clock.advance(10)
    welt.worker.run_pending()
    assert welt.wuensche == [] and welt.store.value == 20.5


def test_pairing_the_bound_w100_again_with_the_mode_already_on_sets_it_up_at_once(mit_w100):
    welt = mit_w100
    _w100_melden(welt, thermostat_mode="ON")
    welt.z2m.interview(W100)
    welt.worker.run_pending()
    assert _w100_sets(welt) == MODUS_W100 * 2 + [{"occupied_heating_setpoint": 21.0}, {"system_mode": "heat"}]
    assert welt.z2m.verworfen == []


def test_the_bound_w100_gets_the_target_of_the_moment_the_mode_is_reported(mit_w100):
    welt = mit_w100
    welt.z2m.interview(W100)
    welt.worker.run_pending()
    welt.worker.post(Event(EV_TARGET_INPUT, {"value": 22.5, "source": "portal"}))  # Wunsch, bevor ON gemeldet ist
    welt.worker.run_pending()
    assert welt.store.value == 22.5
    _w100_melden(welt, thermostat_mode="ON")
    assert _w100_sets(welt)[-2:] == [{"occupied_heating_setpoint": 23.0}, {"system_mode": "heat"}]
    assert welt.z2m.verworfen == [(W100, "occupied_heating_setpoint")]  # nur das Soll von vor ON, nichts danach


def test_the_bound_w100_gets_setpoint_and_heat_only_after_a_report_with_the_mode_on_and_only_once(mit_w100):
    welt = mit_w100
    welt.z2m.interview(W100)
    welt.worker.run_pending()
    _w100_melden(welt, thermostat_mode="OFF", system_mode="off")
    assert _w100_sets(welt) == MODUS_W100 * 2
    _w100_melden(welt, thermostat_mode="ON")
    nach_on = _w100_sets(welt)
    assert nach_on == MODUS_W100 * 2 + [{"occupied_heating_setpoint": 21.0}, {"system_mode": "heat"}]
    _w100_melden(welt, thermostat_mode="ON", battery=80)
    _w100_melden(welt, system_mode="off")
    assert _w100_sets(welt) == nach_on


def test_a_thermostat_without_a_profile_gets_no_thermostat_mode_and_exact_values(mit_geraet):
    welt = mit_geraet
    welt.z2m.report(THERMOSTAT, action="single_plus", system_mode="cool")
    welt.worker.run_pending()
    welt.worker.post(Event(EV_TARGET_INPUT, {"value": 22.5, "source": "portal"}))
    welt.worker.run_pending()
    assert [body for ieee, body in welt.z2m.set_requests] == [{"occupied_heating_setpoint": 22.5}]


def test_a_retained_w100_report_after_a_restart_is_no_wish_and_no_switching_off(mit_w100, clock):
    # Retained Meldungen mit frischem last_seen gelten als Wert, bleiben aber ohne Hook (Broker-Replay)
    welt = mit_w100
    melden = {"occupied_heating_setpoint": 22, "system_mode": "off", "local_temperature": 20.0,
              "last_seen": datetime.now(UTC).isoformat()}
    welt.bus.deliver(f"zigbee2mqtt/{W100}", melden, retain=True)
    welt.worker.run_pending()
    clock.advance(ROOM_TARGET_DEBOUNCE_SECONDS + 1)
    welt.worker.run_pending()
    assert welt.mirror.value(W100, "system_mode") == "off"  # der Wert gilt (Spiegel), ...
    assert welt.wuensche == [] and welt.aus == [] and welt.store.value == 20.5  # ... loest aber nichts aus
    assert _w100_sets(welt) == MODUS_W100
    # Gegenprobe: live ist das Soll 22 ein Wunsch, und dieselbe Meldung mit off schaltet aus
    welt.bus.deliver(f"zigbee2mqtt/{W100}", {**melden, "system_mode": "heat"})
    welt.worker.run_pending()
    clock.advance(ROOM_TARGET_DEBOUNCE_SECONDS + 1)
    welt.worker.run_pending()
    assert welt.wuensche == [22]
    welt.bus.deliver(f"zigbee2mqtt/{W100}", melden)
    welt.worker.run_pending()
    assert welt.aus == [(True, 22.0)]


def test_a_stopped_trigger_source_posts_nothing(tmp_path, clock):
    """Zwei Quellen nacheinander am selben Spiegel (Neustart der Laufzeit im Prozess): nach stop der ersten landet eine
    Zigbee-Meldung nur noch im Worker der zweiten, der Haken der ersten ist abgemeldet."""
    bus = FakeBus()
    z2m = FakeZigbee2Mqtt(bus)
    mirror = ZigbeeMirror(bus, clock)
    mirror.start()
    z2m.bridge(online=True)
    z2m.add_sensor(SENSOR)

    def quelle(name):
        worker = RegulationWorker(clock=clock)
        checks = []
        worker.register(EV_LOCAL_CHECK, lambda event: checks.append(name))
        worker.register(EV_SOURCE_CONNECTED, lambda event: None)
        store = TargetStore(tmp_path / f"room_target_{name}.json", 20.0, clock, lambda: "ts")
        source = BusTriggerSource(bus, worker, mirror, store, thermostat=None, sensor_ieees=(SENSOR,),
                                  daily_trigger_time=None)
        source.start()
        worker.run_pending()
        checks.clear()
        return source, worker, checks

    erste, erster_worker, erste_checks = quelle("erste")
    z2m.report(SENSOR, temperature=20.1)
    erster_worker.run_pending()
    assert erste_checks == ["erste"]  # Gegenprobe: solange sie laeuft, bekommt die erste Quelle die Meldung
    erste.stop()
    zweite, zweiter_worker, zweite_checks = quelle("zweite")
    z2m.report(SENSOR, temperature=20.4)
    erster_worker.run_pending()
    zweiter_worker.run_pending()
    assert erste_checks == ["erste"] and zweite_checks == ["zweite"]


def test_a_stopped_trigger_source_lets_go_of_the_bound_w100(mit_w100):
    """stop meldet das Thermostat bei der Pflege ab: ein erneutes Koppeln danach richtet es nicht mehr mit dem
    Soll-Speicher der gestoppten Quelle ein."""
    welt = mit_w100
    welt.store.apply_portal(23.0)  # ein Soll im Speicher, das von der Vorgabe (20) unterscheidbar ist
    welt.trigger.stop()
    welt.z2m.interview(W100)
    _w100_melden(welt, thermostat_mode="ON")
    # nicht mehr gebunden: die Vorgabe der Pflege (20), nicht das Soll aus dem Speicher der gestoppten Quelle
    assert welt.store.value == 23.0
    assert _w100_sets(welt) == MODUS_W100 * 2 + [{"occupied_heating_setpoint": 20.0}, {"system_mode": "heat"}]


def test_a_w100_showing_the_rounded_target_lifts_the_frost_position(mit_w100, caplog):
    """Die Anzeige des gerundeten Solls gilt als das geltende Soll (nicht als unbrauchbarer Wert): sie hebt die
    Frostschutz-Stellung auf, wie jede brauchbare Meldung."""
    welt = mit_w100
    _w100_melden(welt, occupied_heating_setpoint=5, system_mode="heat")
    assert welt.aus == [(True, 20.5)]
    with caplog.at_level("DEBUG"):
        _w100_melden(welt, occupied_heating_setpoint=21, system_mode="heat")  # 21 ist die Anzeige von 20.5
    assert welt.aus == [(True, 20.5), (False, None)]
    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]


# --- Start: letzter bekannter Zustand, "aus" und Frostschutz (Nutzer-Entscheidung) ---

ALTER = {"frisch": 0.0, "drei Stunden alt": 3 * 3600.0, "ohne last_seen": None}


def _neustart(tmp_path, clock, *, zustand=None, alter=0.0, w100=False, soll=22.0, schlaeft=False):
    """Prozessstart wie nach einem Neustart: Zigbee2MQTT hat den Zustand des Thermostats vorher retained gemeldet
    (Broker-Replay beim Abonnieren, keine Hooks); alter None = Meldung ohne last_seen. Danach die Laufzeit mit
    Angleichen beim Start."""
    bus = FakeBus()
    z2m = FakeZigbee2Mqtt(bus)
    z2m.bridge(online=True)
    z2m.add_sensor(SENSOR)
    ieee = W100 if w100 else THERMOSTAT
    (z2m.add_w100 if w100 else z2m.add_thermostat)(ieee)
    if schlaeft:
        z2m.make_sleepy(ieee)
    if zustand is not None:
        z2m.report(ieee, wall=None if alter is None else time.time() - alter, **zustand)
    mirror = ZigbeeMirror(bus, clock)
    mirror.start()  # FakeBus spielt die retained Meldungen mit retain=True zu
    worker = RegulationWorker(clock=clock)
    welt = SimpleNamespace(bus=bus, z2m=z2m, mirror=mirror, worker=worker, ieee=ieee, wuensche=[], aus=[])
    worker.register(EV_LOCAL_CHECK, lambda event: None)
    worker.register(EV_SOURCE_CONNECTED, lambda event: None)

    def wunsch(value):
        welt.wuensche.append(value)
        return min(25.0, max(15.0, value))

    welt.store = TargetStore(tmp_path / "room_target.json", soll, clock, lambda: "ts")
    welt.trigger = BusTriggerSource(bus, worker, mirror, welt.store, thermostat=ieee, sensor_ieees=(SENSOR,),
                                    daily_trigger_time=None, wunsch=wunsch, clock=clock, angleichen=True,
                                    soll_quelle_aus=lambda aus, wirksam: welt.aus.append((aus, wirksam)))
    welt.trigger.start()
    worker.run_pending()
    return welt


def _sollwerte(welt):
    return [body["occupied_heating_setpoint"] for ieee, body in welt.z2m.set_requests
            if ieee == welt.ieee and "occupied_heating_setpoint" in body]


@pytest.mark.parametrize("alter", ALTER.values(), ids=ALTER.keys())
@pytest.mark.parametrize("zustand", [{"system_mode": "off", "occupied_heating_setpoint": 20.0},
                                     {"system_mode": "heat", "occupied_heating_setpoint": 5.0},
                                     {"system_mode": "heat", "occupied_heating_setpoint": FROSTSCHUTZ_BIS}],
                         ids=["aus", "Frostschutz 5", "Frostschutz 7"])
def test_start_does_not_align_a_thermostat_that_is_off(tmp_path, clock, zustand, alter):
    """Der letzte bekannte Zustand gilt beim Start fuer "aus" (auch retained, jedes Alter), nie als Wunsch."""
    welt = _neustart(tmp_path, clock, zustand={**zustand, "local_temperature": 20.0}, alter=alter)
    clock.advance(ROOM_TARGET_DEBOUNCE_SECONDS + 1)
    welt.worker.run_pending()
    assert _sollwerte(welt) == [] and welt.aus == [(True, 22.0)]
    assert welt.wuensche == [] and (welt.store.value, welt.store.source) == (22.0, "portal")


@pytest.mark.parametrize("alter", ALTER.values(), ids=ALTER.keys())
def test_start_aligns_a_thermostat_whose_last_known_state_is_on(tmp_path, clock, alter):
    # Gegenprobe: bekannter Zustand an (auch Soll knapp ueber FROSTSCHUTZ_BIS) -> Angleichen beim Start
    welt = _neustart(tmp_path, clock, zustand={"system_mode": "heat", "occupied_heating_setpoint": 7.5,
                                                "local_temperature": 20.0}, alter=alter)
    assert _sollwerte(welt) == [22.0] and welt.aus == []
    clock.advance(ROOM_TARGET_DEBOUNCE_SECONDS + 1)
    welt.worker.run_pending()
    assert welt.wuensche == [] and welt.store.value == 22.0


def test_start_without_a_known_state_aligns_with_the_first_report(tmp_path, clock):
    """Ohne bekannten Zustand gleicht der Start erst mit der ersten Live-Meldung an, vor deren Ereignissen: das
    gemeldete Soll 19 faellt ins Echo-Fenster und ist kein Wunsch."""
    welt = _neustart(tmp_path, clock, schlaeft=True)
    assert _sollwerte(welt) == []
    welt.z2m.report(THERMOSTAT, system_mode="heat", occupied_heating_setpoint=19.0, local_temperature=20.0)
    welt.worker.run_pending()
    clock.advance(ROOM_TARGET_DEBOUNCE_SECONDS + 1)
    welt.worker.run_pending()
    assert _sollwerte(welt) == [22.0] and welt.wuensche == [] and welt.store.value == 22.0
    clock.advance(120)  # nach dem Echo-Fenster: Routinemeldung, das schlafende Thermostat hat noch nicht uebernommen
    welt.z2m.report(THERMOSTAT, system_mode="heat", occupied_heating_setpoint=19.0, local_temperature=20.0)
    welt.worker.run_pending()
    clock.advance(ROOM_TARGET_DEBOUNCE_SECONDS + 1)
    welt.worker.run_pending()
    assert _sollwerte(welt) == [22.0]  # nur einmal je Start
    assert welt.wuensche == [] and welt.store.value == 22.0  # das angezeigte 19 gilt als alter Wert (veraltet)


@pytest.mark.parametrize("meldung", [{"system_mode": "off", "occupied_heating_setpoint": 19.0},
                                     {"system_mode": "heat", "occupied_heating_setpoint": 5.0}],
                         ids=["aus", "Frostschutz"])
def test_start_without_a_known_state_writes_nothing_if_the_first_report_is_off(tmp_path, clock, meldung):
    welt = _neustart(tmp_path, clock)
    welt.z2m.report(THERMOSTAT, local_temperature=20.0, **meldung)
    welt.worker.run_pending()
    clock.advance(ROOM_TARGET_DEBOUNCE_SECONDS + 1)
    welt.worker.run_pending()
    assert _sollwerte(welt) == [] and welt.aus == [(True, 22.0)] and welt.wuensche == []
    welt.z2m.report(THERMOSTAT, system_mode="heat", occupied_heating_setpoint=19.0, local_temperature=20.0)
    welt.worker.run_pending()
    # Gegenprobe: wieder an (bzw. Frostschutz durch Drehen verlassen) wie im laufenden Betrieb
    assert _sollwerte(welt) == ([22.0] if meldung["system_mode"] == "off" else [])


def test_an_operation_after_a_restart_with_the_thermostat_off_waits_for_it_to_be_on(tmp_path, clock):
    """Eine Bedienung waehrend "aus" (bekannt aus dem retained Zustand) wird gespeichert und mit "wieder an"
    geschrieben."""
    welt = _neustart(tmp_path, clock, zustand={"system_mode": "off", "occupied_heating_setpoint": 20.0,
                                                "local_temperature": 20.0}, alter=None)
    welt.worker.post(Event(EV_TARGET_INPUT, {"value": 23.0, "source": "portal"}))
    welt.worker.run_pending()
    assert welt.store.value == 23.0 and _sollwerte(welt) == []
    welt.z2m.report(THERMOSTAT, system_mode="heat", occupied_heating_setpoint=20.0, local_temperature=20.0)
    welt.worker.run_pending()
    clock.advance(ROOM_TARGET_DEBOUNCE_SECONDS + 1)
    welt.worker.run_pending()
    assert _sollwerte(welt) == [23.0] and welt.wuensche == [] and welt.store.value == 23.0


@pytest.mark.parametrize("zustand", [{"system_mode": "off", "occupied_heating_setpoint": 20.0},
                                     {"system_mode": "heat", "occupied_heating_setpoint": FROSTSCHUTZ_BIS}],
                         ids=["aus", "Frostschutz 7"])
def test_nothing_reaches_a_thermostat_whose_last_known_state_is_off_even_unseen_by_the_input(mit_geraet, clock,
                                                                                             zustand):
    """Pruefung vor jedem Schreiben: der retained Zustand "aus" kam erst nach dem Start (Broker-Replay ohne
    Hook, der Soll-Eingang weiss nichts davon); die Bedienung des Servers wird gespeichert, aber nicht geschrieben, und
    oeffnet kein Echo-Fenster (schreiben gibt False)."""
    welt = mit_geraet
    welt.bus.deliver(f"zigbee2mqtt/{THERMOSTAT}", zustand, retain=True)
    welt.worker.post(Event(EV_TARGET_INPUT, {"value": 23.0, "source": "portal"}))
    welt.worker.run_pending()
    assert welt.store.value == 23.0 and _setpoint_writes(welt) == [] and welt.store.echo_rest() == 0.0
    welt.z2m.report(THERMOSTAT, system_mode="heat", occupied_heating_setpoint=20.0, local_temperature=20.0)
    welt.worker.post(Event(EV_TARGET_INPUT, {"value": 22.0, "source": "portal"}))
    welt.worker.run_pending()
    assert _setpoint_writes(welt) == [22.0]  # Gegenprobe: an -> geschrieben


def test_start_aligns_a_w100_only_after_it_reports_on(tmp_path, clock):
    """Der Konverter lumi verwirft ein Soll, solange er thermostat_mode nicht als ON kennt; das Angleichen
    beim Start wartet auf die Meldung ON und schreibt dann genau ein Soll (gerundet), ohne heat."""
    welt = _neustart(tmp_path, clock, w100=True, soll=22.5,
                     zustand={"thermostat_mode": "OFF", "system_mode": "heat", "occupied_heating_setpoint": 20})
    assert _w100_sets(welt) == MODUS_W100
    welt.z2m.report(W100, thermostat_mode="ON")
    welt.worker.run_pending()
    welt.z2m.report(W100, thermostat_mode="ON", battery=80)
    welt.worker.run_pending()
    clock.advance(ROOM_TARGET_DEBOUNCE_SECONDS + 1)
    welt.worker.run_pending()
    assert _w100_sets(welt) == MODUS_W100 + [{"occupied_heating_setpoint": 23.0}]
    assert welt.z2m.verworfen == [] and welt.wuensche == [] and welt.store.value == 22.5


def test_start_aligns_a_w100_at_once_if_its_known_state_is_on(tmp_path, clock):
    welt = _neustart(tmp_path, clock, w100=True, soll=22.5,
                     zustand={"thermostat_mode": "ON", "system_mode": "heat", "occupied_heating_setpoint": 20})
    assert _w100_sets(welt) == MODUS_W100 + [{"occupied_heating_setpoint": 23.0}] and welt.z2m.verworfen == []


def test_start_does_not_align_a_w100_that_is_off_even_after_it_reports_on(tmp_path, clock):
    welt = _neustart(tmp_path, clock, w100=True,
                     zustand={"thermostat_mode": "OFF", "system_mode": "off", "occupied_heating_setpoint": 20})
    welt.z2m.report(W100, thermostat_mode="ON")
    welt.worker.run_pending()
    assert _w100_sets(welt) == MODUS_W100 and welt.aus == [(True, 22.0)]


def test_start_does_not_align_when_only_the_input_still_knows_frost(tmp_path, clock):
    """Der Start kennt die Frostschutz-Stellung aus dem retained Zustand; die ON-Meldung traegt ein
    unbrauchbares Soll (null). Der Spiegel zeigt dann nicht mehr Frost, der Soll-Eingang haelt sie (ein unbrauchbarer Wert
    beendet sie nicht): das Angleichen folgt dem Soll-Eingang und schreibt nichts."""
    welt = _neustart(tmp_path, clock, w100=True, soll=22.5,
                     zustand={"thermostat_mode": "OFF", "system_mode": "heat", "occupied_heating_setpoint": 5})
    welt.z2m.report(W100, thermostat_mode="ON", occupied_heating_setpoint=None)
    welt.worker.run_pending()
    clock.advance(ROOM_TARGET_DEBOUNCE_SECONDS + 1)
    welt.worker.run_pending()
    assert _w100_sets(welt) == MODUS_W100 and welt.aus == [(True, 22.5)]


@pytest.mark.parametrize("unbrauchbar", [{"occupied_heating_setpoint": None}, {"occupied_heating_setpoint": "x"},
                                         {"system_mode": None}, {"system_mode": 0}],
                         ids=["Soll null", "Soll Text", "Modus null", "Modus Zahl"])
def test_a_state_without_a_usable_mode_or_setpoint_is_not_known(tmp_path, clock, unbrauchbar):
    """Bekannt ist ein Zustand nur mit einem Modus (Text) oder einem endlichen Soll; sonst wartet das
    Angleichen auf die erste brauchbare Meldung (fail closed)."""
    welt = _neustart(tmp_path, clock, zustand={**unbrauchbar, "local_temperature": 20.0})
    assert _sollwerte(welt) == []
    welt.z2m.report(THERMOSTAT, local_temperature=20.0, **unbrauchbar)
    welt.worker.run_pending()
    assert _sollwerte(welt) == []
    welt.z2m.report(THERMOSTAT, system_mode="heat", occupied_heating_setpoint=20.0, local_temperature=20.0)
    welt.worker.run_pending()
    assert _sollwerte(welt) == [22.0]


@pytest.mark.parametrize("zustand", [{"system_mode": "auto", "occupied_heating_setpoint": 20.0},
                                     {"system_mode": "cool", "occupied_heating_setpoint": 20.0},
                                     {"system_mode": "heat", "occupied_heating_setpoint": None},
                                     {"system_mode": "heat", "occupied_heating_setpoint": "x"}],
                         ids=["auto", "cool", "heat mit Soll null", "heat mit Soll Text"])
def test_the_write_check_blocks_only_off_and_frost(tmp_path, clock, zustand):
    # Nur system_mode "off" oder ein endliches Soll bis 7 °C blockt; jeder andere Modus und ein
    # unbrauchbares Soll nicht
    welt = _neustart(tmp_path, clock, zustand={**zustand, "local_temperature": 20.0})
    assert _sollwerte(welt) == [22.0] and welt.aus == []

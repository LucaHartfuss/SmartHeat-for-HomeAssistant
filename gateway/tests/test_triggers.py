import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from configs import SENSOR, THERMOSTAT
from fake_z2m import FakeZigbee2Mqtt
from fakes import FakeBus

from smartheat_gateway.triggers import EV_TARGET_INPUT, BusTriggerSource, seconds_until
from smartheat_gateway.zigbee import ZigbeeMirror
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


# --- Wiedereinschalten (Review D2, Important 2; Restpunkt 5b-D2-11) ---
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

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from configs import SENSOR, THERMOSTAT
from fake_z2m import FakeZigbee2Mqtt
from fakes import FakeBus

from smartheat_gateway import topics
from smartheat_gateway.target_store import TargetStore
from smartheat_gateway.triggers import BusTriggerSource, seconds_until
from smartheat_gateway.zigbee import ZigbeeMirror
from smartheat_runtime.runtime import EV_LOCAL_CHECK, EV_SOURCE_CONNECTED
from smartheat_runtime.worker import RegulationWorker

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
    bus.publish(topics.CMD_ROOM_TARGET, {"value": 22.0, "source": "portal", "ts": "x"})
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
    bus.publish(topics.CMD_ROOM_TARGET, {"value": 22.0, "source": "portal", "ts": "x"})
    worker.run_pending()
    assert (store.value, store.source) == (22.0, "portal")


def test_portal_value_outside_the_range_is_dropped_by_the_runtime(world, clock):
    bus, _, worker, store, checks = world
    bus.publish(topics.CMD_ROOM_TARGET, {"value": 30.0, "source": "portal", "ts": "x"})
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
    bus.publish(topics.CMD_ROOM_TARGET, {"value": 22.0, "source": "portal", "ts": "x"})
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

"""Ereignisvertrag der Gateway-Trigger-Quelle (heizungsbruecke/testhilfen/trigger_konformitaet.py; Spec S6 4.1)."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from configs import SENSOR, THERMOSTAT
from fake_z2m import FakeZigbee2Mqtt
from fakes import FakeBus
from trigger_konformitaet import PRUEFUNGEN, TriggerWelt, beobachten

from smartheat_gateway.triggers import EV_DAILY, BusTriggerSource
from smartheat_gateway.zigbee import ZigbeeMirror
from smartheat_runtime.soll_speicher import TargetStore
from smartheat_runtime.worker import Event, RegulationWorker


@pytest.fixture
def welt(tmp_path, clock):
    bus = FakeBus()
    z2m = FakeZigbee2Mqtt(bus)
    mirror = ZigbeeMirror(bus, clock)
    mirror.start()
    z2m.bridge(online=True)
    z2m.add_sensor(SENSOR)
    z2m.add_thermostat(THERMOSTAT)
    worker = RegulationWorker(clock=clock)
    seen = beobachten(worker)
    store = TargetStore(tmp_path / "room_target.json", 20.0, clock, lambda: "ts")
    BusTriggerSource(bus, worker, mirror, store, thermostat=THERMOSTAT, sensor_ieees=(SENSOR,),
                     daily_trigger_time="12:00",
                     now=lambda: datetime(2026, 1, 15, 11, 59, 0, tzinfo=ZoneInfo("Europe/Berlin"))).start()

    def verbinden():
        bus.disconnect()
        bus.connect()

    return TriggerWelt(run=worker.run_pending, seen=seen, verbinden=verbinden,
                       sensor_meldung=lambda: z2m.report(SENSOR, temperature=20.4),
                       tagestick=lambda: worker.post(Event(EV_DAILY)))


@pytest.mark.parametrize("pruefung", PRUEFUNGEN, ids=lambda p: p.__name__)
def test_ereignisvertrag(pruefung, welt):
    pruefung(welt)

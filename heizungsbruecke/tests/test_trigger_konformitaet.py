"""Ereignisvertrag der HA-Trigger-Quelle (testhilfen/trigger_konformitaet.py)."""
import pytest
from fakes import FakeClock, FakeHa
from trigger_konformitaet import PRUEFUNGEN, TriggerWelt, beobachten

from heizungsbruecke import geraet_trigger
from smartheat_runtime.roles import ChannelManifest
from smartheat_runtime.soll_speicher import TargetStore
from smartheat_runtime.worker import RegulationWorker

CLIMATE = "climate.wohnzimmer_thermostat"
SOLL = f"{CLIMATE}::temperature"


@pytest.fixture
def welt(tmp_path):
    clock = FakeClock()
    worker = RegulationWorker(clock)
    ha = FakeHa()
    ha.states.update({CLIMATE: "heat", SOLL: 21.0})
    store = TargetStore(tmp_path / "soll.json", 21.0, clock, lambda: "t")
    seen = beobachten(worker)
    client = geraet_trigger.build(
        ChannelManifest(refs={"room_target": "smartheat:raum_soll", "room_actual": "sensor.raum"}), ha, worker, store,
        SOLL, tagestick="12:00", wache_s=300, wunsch=lambda v, h, d: v, soll_quelle_aus=lambda aus, wirksam: None)
    return TriggerWelt(
        run=worker.run_pending, seen=seen, verbinden=client._on_connected,
        sensor_meldung=lambda: client._on_trigger_event(
            {"platform": "state", "entity_id": "sensor.raum", "attribute": None}),
        tagestick=lambda: client._on_trigger_event({"platform": "time", "now": "12:00"}))


@pytest.mark.parametrize("pruefung", PRUEFUNGEN, ids=lambda p: p.__name__)
def test_ereignisvertrag(pruefung, welt):
    pruefung(welt)

"""Spec 5c 1.3: Bedienwunsch aus Soll-Entity und Ereignis smartheat_wunsch, Thermostat aus, Angleichen ohne Echo."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from conftest import FakeClock
from fakes import FakeHa

from heizungsbruecke import geraet_trigger
from smartheat_runtime.roles import ChannelManifest
from smartheat_runtime.runtime import EV_LOCAL_CHECK, EV_SOURCE_CONNECTED
from smartheat_runtime.soll_eingang import EV_TARGET_INPUT
from smartheat_runtime.soll_speicher import TargetStore
from smartheat_runtime.worker import Event, RegulationWorker

CLIMATE = "climate.wohnzimmer_thermostat"
SOLL = f"{CLIMATE}::temperature"


class Aufbau:
    def __init__(self, tmp_path: Path, soll=SOLL, room_actual="sensor.raum", ha=None):
        self.clock = FakeClock()
        self.worker = RegulationWorker(self.clock)
        self.ha = ha or FakeHa()
        self.ha.states.update({CLIMATE: "heat", SOLL: 21.0})
        self.store = TargetStore(tmp_path / "soll.json", 21.0, self.clock, lambda: "t")
        self.wuensche, self.aus, self.checks = [], [], []
        self.worker.register(EV_LOCAL_CHECK, lambda event: self.checks.append(event.data))
        manifest = ChannelManifest(refs={"room_target": "smartheat:raum_soll", "room_actual": room_actual})
        self.client = geraet_trigger.build(
            manifest, {"daily_trigger_time": "12:00"}, self.ha, self.worker, self.store, soll,
            wunsch=lambda v, h, d: self.wuensche.append((v, h, d)) or min(max(v, 15.0), 25.0),
            soll_quelle_aus=lambda aus, wirksam: self.aus.append((aus, wirksam)))
        self.fire = self.client._on_trigger_event

    def run(self):
        self.worker.run_pending()

    def soll_aenderung(self, wert, context=None):
        self.fire({"platform": "state", "entity_id": CLIMATE, "attribute": "temperature",
                   "to_state": {"state": "heat", "attributes": {"temperature": wert}, "context": context or {}}})

    def ereignis(self, wert, context):
        self.fire({"platform": "event", "event": {"event_type": "smartheat_wunsch", "data": {"raum_soll": wert},
                                                  "context": context}})


def test_trigger_list(tmp_path):
    triggers = Aufbau(tmp_path).client._triggers
    assert {"platform": "state", "entity_id": CLIMATE, "attribute": "temperature", "for": {"seconds": 10}} in triggers
    assert {"platform": "state", "entity_id": CLIMATE, "to": None} in triggers  # nur Zustandswechsel (HVAC-Modus)
    assert {"platform": "state", "entity_id": "sensor.raum"} in triggers
    assert {"platform": "event", "event_type": "smartheat_wunsch"} in triggers
    assert {"platform": "time", "at": "12:00"} in triggers
    assert len(triggers) == 5


def test_the_trigger_is_on_the_real_entity_not_on_the_manifest_ref(tmp_path):
    triggers = Aufbau(tmp_path).client._triggers
    assert not any(t.get("entity_id", "").startswith("smartheat") for t in triggers)


def test_sensor_target_has_one_state_trigger_without_mode(tmp_path):
    triggers = Aufbau(tmp_path, soll="sensor.soll").client._triggers
    assert [t for t in triggers if t.get("entity_id") == "sensor.soll"] == [
        {"platform": "state", "entity_id": "sensor.soll", "for": {"seconds": 10}}]


def test_the_ha_trigger_window_is_the_shared_debounce_constant():
    from smartheat_runtime.debounce import ROOM_TARGET_DEBOUNCE_SECONDS
    assert geraet_trigger.ROOM_TARGET_DEBOUNCE_SECONDS == ROOM_TARGET_DEBOUNCE_SECONDS == 10


def test_entity_change_by_a_user_is_a_wish(tmp_path):
    a = Aufbau(tmp_path)
    a.soll_aenderung(22.5, {"user_id": "u1", "parent_id": None})
    a.run()
    assert a.wuensche == [(22.5, "soll_entity", "nutzer")] and a.store.value == 22.5


def test_entity_change_by_an_automation_or_unknown(tmp_path):
    a = Aufbau(tmp_path)
    a.soll_aenderung(22.5, {"user_id": None, "parent_id": "p1"})
    a.run()
    a.clock.advance(61)  # Echo-Fenster gibt es hier nicht; der naechste Wert ist ein neuer Wunsch
    a.soll_aenderung(23.0, {})
    a.run()
    assert a.wuensche == [(22.5, "soll_entity", "automation"), (23.0, "soll_entity", None)]


@pytest.mark.parametrize("context,durch", [
    ({"user_id": "u", "parent_id": "p"}, "nutzer"), ({"user_id": None, "parent_id": "p"}, "automation"),
    ({"user_id": None, "parent_id": None}, None), ({}, None), (None, None), ("x", None)])
def test_durch_from_context(context, durch):
    assert geraet_trigger.durch_aus_kontext(context) == durch


def test_event_wish_by_an_automation_aligns_the_thermostat(tmp_path):
    a = Aufbau(tmp_path)
    a.ereignis(22.0, {"user_id": None, "parent_id": "p1"})
    a.run()
    assert a.wuensche == [(22.0, "smartheat_entity", "automation")]
    assert (SOLL, 22.0) in a.ha.writes


def test_hvac_off_is_not_a_wish(tmp_path):
    a = Aufbau(tmp_path)
    a.fire({"platform": "state", "entity_id": CLIMATE, "to_state": {"state": "off"}})
    a.run()
    assert a.wuensche == [] and a.aus == [(True, 21.0)]


def test_hvac_back_on_clears_the_off_state(tmp_path):
    a = Aufbau(tmp_path)
    a.fire({"platform": "state", "entity_id": CLIMATE, "to_state": {"state": "off"}})
    a.fire({"platform": "state", "entity_id": CLIMATE, "to_state": {"state": "heat"}})
    a.run()
    assert a.aus == [(True, 21.0), (False, None)]


def test_room_actual_on_the_same_climate_entity_is_a_local_check_not_a_mode(tmp_path):
    a = Aufbau(tmp_path, room_actual=f"{CLIMATE}::current_temperature")
    a.fire({"platform": "state", "entity_id": CLIMATE, "attribute": "current_temperature",
            "to_state": {"state": "off", "attributes": {"current_temperature": 20.0}}})
    a.run()
    assert a.aus == [] and a.wuensche == [] and a.checks[-1] == {"room_target_fired": False}


def test_sensor_target_is_never_written(tmp_path):
    a = Aufbau(tmp_path, soll="sensor.soll")
    a.ereignis(22.0, {})
    a.run()
    assert a.ha.writes == [] and a.store.value == 22.0


def test_sensor_target_state_change_is_a_wish(tmp_path):
    a = Aufbau(tmp_path, soll="input_number.soll")
    a.fire({"platform": "state", "entity_id": "input_number.soll", "to_state": {"state": "22.5", "context": {}}})
    a.run()
    assert a.wuensche == [(22.5, "soll_entity", None)] and a.ha.writes == []


@pytest.mark.parametrize("state", ["unavailable", "unknown", None, "abc", "nan", "inf"])
def test_unusable_target_states_are_ignored(tmp_path, state):
    a = Aufbau(tmp_path, soll="input_number.soll")
    a.fire({"platform": "state", "entity_id": "input_number.soll", "to_state": {"state": state}})
    a.run()
    assert a.wuensche == [] and a.store.value == 21.0


@pytest.mark.parametrize("data", [{}, {"raum_soll": "22"}, {"raum_soll": True}, {"raum_soll": None}])
def test_event_without_a_number_is_ignored(tmp_path, data):
    a = Aufbau(tmp_path)
    a.fire({"platform": "event", "event": {"event_type": "smartheat_wunsch", "data": data, "context": {}}})
    a.run()
    assert a.wuensche == [] and a.store.value == 21.0


def test_portal_operation_aligns_the_entity_without_a_wish(tmp_path):
    a = Aufbau(tmp_path)
    a.worker.post(Event(EV_TARGET_INPUT, {"value": 22.5, "source": "portal"}))
    a.run()
    assert (SOLL, 22.5) in a.ha.writes
    a.ha.states[SOLL] = 22.5
    a.soll_aenderung(22.5)
    a.run()
    assert a.wuensche == [] and a.store.value == 22.5 and a.store.source == "portal"


def test_a_stale_entity_report_after_a_portal_operation_is_no_wish(tmp_path):
    a = Aufbau(tmp_path)
    a.worker.post(Event(EV_TARGET_INPUT, {"value": 22.5, "source": "portal"}))
    a.run()
    a.soll_aenderung(21.0)  # die Entity meldet noch den alten Wert (cache_state)
    a.run()
    assert a.wuensche == [] and a.store.value == 22.5


def test_event_wish_acts_at_once_and_goes_to_the_device(tmp_path):
    a = Aufbau(tmp_path)
    a.ereignis(23.0, {"user_id": "u1"})
    a.run()
    assert a.store.value == 23.0 and a.checks[-1] == {"room_target_fired": True}
    assert a.wuensche == [(23.0, "smartheat_entity", "nutzer")]


def test_event_wish_reaches_the_device_core_buffer_without_a_connection(tmp_path):
    """Der Wunsch geht ueber den Geraetekern (begrenzen, Puffer), auch wenn keine Verbindung besteht."""
    from smartheat_device import wish
    puffer = wish.WunschPuffer(tmp_path / "puffer")

    def wunsch(wert, herkunft, durch):
        begrenzt, roh = wish.begrenzen(wert)
        puffer.setzen(wish.neu(begrenzt, roh, basis_version=1, uhr_synchron=True, herkunft=herkunft, durch=durch,
                               wall=1_800_000_000.0))
        return begrenzt

    a = Aufbau(tmp_path)
    manifest = ChannelManifest(refs={"room_actual": "sensor.raum"})
    client = geraet_trigger.build(manifest, {}, a.ha, a.worker, a.store, SOLL, wunsch=wunsch,
                                  soll_quelle_aus=lambda aus, wirksam: None)
    client._on_trigger_event({"platform": "event", "event": {
        "event_type": "smartheat_wunsch", "data": {"raum_soll": 23.0}, "context": {"user_id": "u1"}}})
    a.run()
    assert puffer.offen()
    payload = puffer.wunsch.payload()
    assert payload["werte"] == {"raum_soll": 23.0} and payload["herkunft"] == "smartheat_entity"
    assert payload["durch"] == "nutzer"


def test_event_wish_is_clamped_by_the_device_and_the_entity_shows_the_clamp(tmp_path):
    a = Aufbau(tmp_path)
    a.ereignis(30.0, {"user_id": "u1"})
    a.run()
    assert a.store.value == 25.0 and (SOLL, 25.0) in a.ha.writes
    assert a.wuensche == [(30.0, "smartheat_entity", "nutzer")]


def test_room_actual_change_is_a_local_check(tmp_path):
    a = Aufbau(tmp_path)
    a.fire({"platform": "state", "entity_id": "sensor.raum", "attribute": None})
    a.run()
    assert a.checks[-1] == {"room_target_fired": False}


def test_a_flood_of_sensor_changes_is_one_local_check(tmp_path):
    a = Aufbau(tmp_path)
    for _ in range(10):
        a.fire({"platform": "state", "entity_id": "sensor.raum", "attribute": None})
    a.run()
    assert a.checks == [{"room_target_fired": False}]


def test_daily_tick_is_a_local_check(tmp_path):
    a = Aufbau(tmp_path)
    a.fire({"platform": "time", "now": "12:00"})
    a.run()
    assert a.checks[-1] == {"room_target_fired": False}


def test_on_connected_queues_a_fresh_local_check_and_a_status_refresh(tmp_path):
    a = Aufbau(tmp_path)
    seen = []
    a.worker.register(EV_SOURCE_CONNECTED, lambda event: seen.append(event))
    a.client._on_connected()
    a.run()
    assert a.checks[-1] == {"room_target_fired": True} and len(seen) == 1


def test_a_failing_write_does_not_break_the_wish(tmp_path):
    ha = FakeHa()
    a = Aufbau(tmp_path, ha=ha)
    ha.write_error = RuntimeError("HA down")
    a.ereignis(22.0, {"user_id": "u1"})
    a.run()
    assert a.store.value == 22.0 and a.wuensche == [(22.0, "smartheat_entity", "nutzer")]


def test_ha_soll_quelle_writes_only_climate_and_reads_the_shown_value():
    ha = FakeHa()
    ha.states.update({SOLL: 21.0, "sensor.s": 20.0, "input_number.x": "abc"})
    assert geraet_trigger.HaSollQuelle(ha, SOLL).angezeigt() == 21.0
    assert geraet_trigger.HaSollQuelle(ha, SOLL).schreiben(22.0) is True and ha.writes == [(SOLL, 22.0)]
    assert geraet_trigger.HaSollQuelle(ha, "sensor.s").schreiben(22.0) is False
    assert geraet_trigger.HaSollQuelle(ha, "sensor.s").angezeigt() == 20.0
    assert geraet_trigger.HaSollQuelle(ha, "input_number.x").angezeigt() is None
    assert geraet_trigger.HaSollQuelle(ha, "sensor.missing").angezeigt() is None


def test_build_passes_the_ws_parameters_to_the_client(monkeypatch, tmp_path):
    created = []
    monkeypatch.setattr("heizungsbruecke.geraet_trigger.HaTriggerClient",
                        lambda **kwargs: created.append(kwargs) or SimpleNamespace(**kwargs))
    ha = MagicMock(token="tok")
    ha.websocket_url.return_value = "ws://x/api/websocket"
    geraet_trigger.build(ChannelManifest(refs={"room_actual": "sensor.raum"}), {}, ha,
                         RegulationWorker(FakeClock()), TargetStore(tmp_path / "s.json", 21.0, FakeClock(), lambda: "t"),
                         SOLL, wunsch=lambda v, h, d: v, soll_quelle_aus=lambda a, w: None)
    assert created[0]["ws_url"] == "ws://x/api/websocket" and created[0]["token"] == "tok"
    assert {"platform": "time", "at": "12:00"} not in created[0]["triggers"]  # ohne Tagestick keiner

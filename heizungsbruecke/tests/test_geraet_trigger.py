"""Spec 5c 1.3: Bedienwunsch aus Soll-Entity und Ereignis smartheat_wunsch, Thermostat aus, Angleichen ohne Echo."""
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from conftest import FakeClock
from fakes import FakeHa

from heizungsbruecke import geraet_trigger
from heizungsbruecke.config import ConfigError
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


@pytest.mark.parametrize("data", [{}, {"raum_soll": "abc"}, {"raum_soll": "nan"}, {"raum_soll": "inf"},
                                  {"raum_soll": True}, {"raum_soll": None}, {"raum_soll": [22]}])
def test_event_without_a_finite_number_is_ignored_and_logged(tmp_path, caplog, data):
    a = Aufbau(tmp_path)
    with caplog.at_level("WARNING"):
        a.fire({"platform": "event", "event": {"event_type": "smartheat_wunsch", "data": data, "context": {}}})
    a.run()
    assert a.wuensche == [] and a.store.value == 21.0
    assert "smartheat_wunsch ohne gueltige raum_soll" in caplog.text
    assert "abc" not in caplog.text  # nur der Typ, nie der Inhalt


@pytest.mark.parametrize("raw,value", [("22.5", 22.5), (" 23 ", 23.0), (22, 22.0)])
def test_event_wish_accepts_a_number_given_as_text(tmp_path, raw, value):
    a = Aufbau(tmp_path)
    a.ereignis(raw, {"user_id": "u1"})
    a.run()
    assert a.wuensche == [(value, "smartheat_entity", "nutzer")]


def test_portal_operation_aligns_the_entity_without_a_wish(tmp_path):
    a = Aufbau(tmp_path)
    a.worker.post(Event(EV_TARGET_INPUT, {"value": 22.5, "source": "portal"}))
    a.run()
    assert (SOLL, 22.5) in a.ha.writes
    a.ha.states[SOLL] = 22.5
    a.soll_aenderung(22.5)
    a.run()
    assert a.wuensche == [] and a.store.value == 22.5 and a.store.source == "portal"


def test_the_write_back_after_an_event_wish_is_no_second_wish(tmp_path):
    a = Aufbau(tmp_path)
    a.ereignis(22.0, {"user_id": "u1"})
    a.run()
    assert (SOLL, 22.0) in a.ha.writes
    a.ha.states[SOLL] = 22.0
    a.soll_aenderung(22.0)  # Echo der eigenen Angleichung
    a.soll_aenderung(21.0)  # und der alte Wert aus dem Cache
    a.run()
    a.clock.advance(70)
    a.run()  # auch das Nachlesen nach dem Fenster ergibt nichts
    assert a.wuensche == [(22.0, "smartheat_entity", "nutzer")] and a.store.value == 22.0


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


# --- Echo-Fenster: eine Bedienung an der Entity im Fenster geht nicht verloren ---

def test_a_user_change_inside_the_echo_window_is_picked_up_after_the_window(tmp_path):
    a = Aufbau(tmp_path)
    a.worker.post(Event(EV_TARGET_INPUT, {"value": 22.5, "source": "portal"}))
    a.run()
    a.clock.advance(11)
    a.ha.states[SOLL] = 22.5
    a.soll_aenderung(22.5)  # Bestaetigung des Thermostats
    a.run()
    a.clock.advance(30)
    a.ha.states[SOLL] = 23.0
    a.soll_aenderung(23.0, {"user_id": "u1"})  # Nutzer im Fenster (+41 s): der Speicher verwirft ihn
    a.run()
    assert a.wuensche == [] and a.store.value == 22.5
    a.clock.advance(21)  # +62 s: Fenster vorbei, Nachlesen
    a.run()
    assert a.wuensche == [(23.0, "soll_entity", "nutzer")] and a.store.value == 23.0


def test_a_pure_echo_inside_the_window_gives_no_wish_after_it(tmp_path):
    a = Aufbau(tmp_path)
    a.worker.post(Event(EV_TARGET_INPUT, {"value": 22.5, "source": "portal"}))
    a.run()
    a.clock.advance(5)
    a.soll_aenderung(21.0)  # alter Wert aus dem Cache
    a.clock.advance(5)
    a.ha.states[SOLL] = 22.5
    a.soll_aenderung(22.5)  # geschriebener Wert
    a.run()
    a.clock.advance(61)
    a.run()
    assert a.wuensche == [] and a.store.value == 22.5 and a.store.source == "portal"


def test_a_thermostat_still_showing_the_old_value_after_the_window_is_no_wish(tmp_path):
    a = Aufbau(tmp_path)
    a.worker.post(Event(EV_TARGET_INPUT, {"value": 22.5, "source": "portal"}))
    a.run()
    a.clock.advance(5)
    a.ha.states[SOLL] = 21.0  # der Schreibbefehl ist noch nicht angekommen
    a.soll_aenderung(21.0)
    a.run()
    a.clock.advance(61)
    a.run()
    assert a.wuensche == [] and a.store.value == 22.5


def test_one_read_per_window_however_many_reports(tmp_path):
    a = Aufbau(tmp_path)
    a.worker.post(Event(EV_TARGET_INPUT, {"value": 22.5, "source": "portal"}))
    a.run()
    reads = []
    original = a.ha.get_state
    a.ha.get_state = lambda ref: reads.append(ref) or original(ref)
    for _ in range(5):
        a.clock.advance(3)
        a.soll_aenderung(22.5)
        a.run()
    a.clock.advance(61)
    a.run()
    assert reads == [SOLL]


# --- (Wieder-)Verbindung: Aenderungen waehrend der Trennung ---

def test_a_value_changed_during_the_outage_becomes_one_wish_after_reconnect(tmp_path):
    a = Aufbau(tmp_path)
    a.ha.states[SOLL] = 23.5
    a.client._on_connected()
    a.run()
    a.client._on_connected()  # zweite Verbindung: nichts Neues
    a.run()
    assert a.wuensche == [(23.5, "soll_entity", None)] and a.store.value == 23.5


def test_an_unchanged_value_gives_no_wish_after_reconnect(tmp_path):
    a = Aufbau(tmp_path)
    a.client._on_connected()
    a.run()
    assert a.wuensche == [] and a.aus == [] and a.checks[-1] == {"room_target_fired": True}


def test_a_thermostat_already_off_at_the_first_connect_reports_the_source_off(tmp_path):
    a = Aufbau(tmp_path)
    a.ha.states[CLIMATE] = "off"
    a.client._on_connected()
    a.run()
    assert a.aus == [(True, 21.0)] and a.wuensche == []


def test_hvac_back_on_during_the_outage_is_taken_over_at_reconnect(tmp_path):
    a = Aufbau(tmp_path)
    a.fire({"platform": "state", "entity_id": CLIMATE, "to_state": {"state": "off"}})
    a.run()
    a.client._on_connected()  # Thermostat ist inzwischen wieder an (Zustand heat)
    a.run()
    assert a.aus[0] == (True, 21.0) and a.aus[-1] == (False, None)


def test_unreadable_entity_state_at_reconnect_changes_nothing(tmp_path):
    a = Aufbau(tmp_path)
    a.ha.states[CLIMATE] = "unavailable"
    a.ha.states[SOLL] = RuntimeError("down")
    a.client._on_connected()
    a.run()
    assert a.wuensche == [] and a.aus == [] and a.store.value == 21.0


def test_the_reconnect_read_after_an_own_write_is_no_wish(tmp_path):
    a = Aufbau(tmp_path)
    a.worker.post(Event(EV_TARGET_INPUT, {"value": 22.5, "source": "portal"}))
    a.run()
    a.client._on_connected()  # die Entity zeigt noch den alten Wert (21.0): Echo-Fenster
    a.run()
    assert a.wuensche == [] and a.store.value == 22.5


# --- HVAC-Modi ohne Aussage, Soll-Ref ohne Attribut ---

def test_unavailable_hvac_mode_is_neither_off_nor_back_on(tmp_path):
    a = Aufbau(tmp_path)
    for mode in ("off", "unavailable", "unknown"):
        a.fire({"platform": "state", "entity_id": CLIMATE, "to_state": {"state": mode}})
    a.run()
    assert a.aus == [(True, 21.0)]
    a.fire({"platform": "state", "entity_id": CLIMATE, "to_state": {"state": "heat"}})
    a.run()
    assert a.aus == [(True, 21.0), (False, None)]


def test_a_climate_target_without_attribute_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="climate.wohnzimmer_thermostat::temperature"):
        Aufbau(tmp_path, soll=CLIMATE)


def test_while_the_websocket_is_down_the_entity_is_read_every_local_check_interval(tmp_path):
    """Bis 0.36 las der Watchdog der Laufzeit den Raum-Soll bei getrenntem WebSocket direkt aus HA; im Geraete-Pfad
    liest die Wache die Soll-Entity im Takt des lokalen Checks und gibt den Wert durch den Soll-Eingang."""
    a = Aufbau(tmp_path)  # der Client hat nie verbunden
    a.ha.states[SOLL] = 22.5
    a.clock.advance(299)
    a.run()
    assert a.wuensche == []
    a.clock.advance(1)
    a.run()
    assert a.wuensche == [(22.5, "soll_entity", None)] and a.store.value == 22.5


def test_while_the_websocket_is_up_the_watch_reads_nothing(tmp_path, monkeypatch):
    a = Aufbau(tmp_path)
    monkeypatch.setattr(type(a.client), "connected", property(lambda self: True))
    a.ha.states[SOLL] = 22.5
    a.clock.advance(600)
    a.run()
    assert a.wuensche == [] and a.store.value == 21.0


def test_an_unavailable_target_entity_is_logged_once_without_its_content(tmp_path, monkeypatch, caplog):
    jetzt = [1000.0]
    monkeypatch.setattr(geraet_trigger.time, "monotonic", lambda: jetzt[0])
    a = Aufbau(tmp_path)
    with caplog.at_level(logging.INFO, logger="heizungsbruecke.geraet_trigger"):
        a.soll_aenderung("unavailable")
        a.soll_aenderung(None)  # weiter ohne Zahl: keine zweite Zeile
        a.soll_aenderung(22.0)
        a.soll_aenderung("unavailable")  # wieder ohne Zahl, aber innerhalb der Stunde
        jetzt[0] += geraet_trigger.OHNE_ZAHL_LOG_SECONDS
        a.soll_aenderung(22.5)
        a.soll_aenderung("unknown")  # nach einer Zahl und nach der Stunde: neue Zeile
    zeilen = [r.getMessage() for r in caplog.records if "keinen Zahlenwert" in r.getMessage()]
    assert len(zeilen) == 2 and all(CLIMATE in z for z in zeilen)
    assert not any("unavailable" in z or "unknown" in z for z in zeilen)

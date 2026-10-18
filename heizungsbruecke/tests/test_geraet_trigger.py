"""Spec 5c 1.3: Bedienwunsch aus Soll-Entity und Ereignis smartheat_wunsch, Thermostat aus, Angleichen ohne Echo."""
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import websocket
from conftest import FakeClock
from fakes import FakeHa

from heizungsbruecke import geraet_trigger, ha_trigger_client
from heizungsbruecke.ha_api import HomeAssistantApi
from smartheat_runtime.dokument_config import ConfigError
from smartheat_runtime.roles import ChannelManifest
from smartheat_runtime.runtime import EV_LOCAL_CHECK, EV_SOURCE_CONNECTED
from smartheat_runtime.soll_eingang import EV_TARGET_INPUT
from smartheat_runtime.soll_speicher import TargetStore
from smartheat_runtime.worker import Event, RegulationWorker

CLIMATE = "climate.wohnzimmer_thermostat"
# Die echte Klasse fuer spec: waehrend des patch ist websocket.WebSocketApp selbst die Attrappe.
WebSocketApp = websocket.WebSocketApp
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
            manifest, self.ha, self.worker, self.store, soll, tagestick="12:00", wache_s=300,
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


@pytest.mark.parametrize("huge", [10**400, -10**400], ids=["gross-positiv", "gross-negativ"])
def test_target_attribute_beyond_float_range_is_ignored(tmp_path, huge):
    a = Aufbau(tmp_path)
    a.soll_aenderung(huge)
    a.run()
    assert a.wuensche == [] and a.store.value == 21.0


@pytest.mark.parametrize("data", [{}, {"raum_soll": "abc"}, {"raum_soll": "nan"}, {"raum_soll": "inf"},
                                  {"raum_soll": True}, {"raum_soll": None}, {"raum_soll": [22]},
                                  pytest.param({"raum_soll": 10**400}, id="gross-positiv"),
                                  pytest.param({"raum_soll": -10**400}, id="gross-negativ")])
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
    client = geraet_trigger.build(manifest, a.ha, a.worker, a.store, SOLL, tagestick=None, wache_s=300,
                                  wunsch=wunsch, soll_quelle_aus=lambda aus, wirksam: None)
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
    """Adresse und Token der HA-API kommen am WebSocket an (Fake-WebSocketApp an der Bibliotheksgrenze): die
    Verbindung geht an die WebSocket-URL der API, die Anmeldung traegt deren Token, das Abonnement die Trigger."""
    urls, gesendet = [], []

    def websocket_app(url, on_message=None, on_close=None, on_error=None):
        urls.append(url)
        app = MagicMock(spec=WebSocketApp)
        app.send.side_effect = lambda text: gesendet.append(json.loads(text))

        def run_forever(**_kwargs):
            on_message(app, json.dumps({"type": "auth_required"}))
            on_message(app, json.dumps({"type": "auth_ok"}))

        app.run_forever.side_effect = run_forever
        return app

    ha = HomeAssistantApi(base_url="http://supervisor", token="tok-der-api")
    client = geraet_trigger.build(
        ChannelManifest(refs={"room_actual": "sensor.raum"}), ha, RegulationWorker(FakeClock()),
        TargetStore(tmp_path / "s.json", 21.0, FakeClock(), lambda: "t"), SOLL, tagestick=None, wache_s=300,
        wunsch=lambda v, h, d: v, soll_quelle_aus=lambda a, w: None)
    # Nach dem Ende der Verbindung beendet der erste Backoff-Schlaf den Client (kein Wiederverbinden im Test).
    monkeypatch.setattr(ha_trigger_client, "time", SimpleNamespace(sleep=lambda _s: client.stop()))
    with patch("heizungsbruecke.ha_trigger_client.websocket.WebSocketApp", autospec=True, side_effect=websocket_app):
        client.start()
        client._thread.join(timeout=5)
    assert not client._thread.is_alive()

    assert urls == ["ws://supervisor/core/api/websocket"]
    assert gesendet[0] == {"type": "auth", "access_token": "tok-der-api"}
    assert gesendet[1]["type"] == "subscribe_trigger"
    assert {"platform": "state", "entity_id": "sensor.raum"} in gesendet[1]["trigger"]
    assert {"platform": "time", "at": "12:00"} not in gesendet[1]["trigger"]  # ohne Tagestick keiner


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


# --- Gescheiterte Angleichung (Final-Review I1): nachholen statt den alten Wert als Wunsch zu nehmen ---

def _portal_waehrend_ha_down(a, wert=22.5):
    a.ha.write_error = RuntimeError("HA Core startet")
    a.worker.post(Event(EV_TARGET_INPUT, {"value": wert, "source": "portal"}))
    a.run()
    assert a.store.value == wert and a.ha.writes == []


def test_a_portal_operation_whose_alignment_failed_survives_the_reconnect(tmp_path):
    """Reproduktion des Final-Reviews: Speicher 21, Portal 22.5 waehrend HA neu startet, das Lesen beim Verbinden sieht
    21. Vorher: Speicher wieder 21 und ein Wunsch (21.0, soll_entity). Jetzt: die Angleichung wird nachgeholt."""
    a = Aufbau(tmp_path)
    _portal_waehrend_ha_down(a)
    a.ha.write_error = None
    a.client._on_connected()
    a.run()
    assert a.ha.writes == [(SOLL, 22.5)] and a.ha.states[SOLL] == 22.5
    assert a.store.value == 22.5 and a.store.source == "portal" and a.wuensche == []


def test_after_the_owed_write_a_stale_report_is_no_wish_but_a_real_user_change_is(tmp_path):
    a = Aufbau(tmp_path)
    _portal_waehrend_ha_down(a)
    a.ha.write_error = None
    a.ha.states[SOLL] = 21.0
    a.client._on_connected()
    a.run()
    a.ha.states[SOLL] = 21.0  # das Thermostat zeigt kurz noch den alten Wert
    a.soll_aenderung(21.0)
    a.run()
    a.clock.advance(70)  # nach dem Echo-Fenster: das Nachlesen sieht noch 21 (Schreibbefehl offen, veraltet)
    a.run()
    assert a.wuensche == [] and a.store.value == 22.5
    a.ha.states[SOLL] = 23.0
    a.soll_aenderung(23.0, {"user_id": "u1"})  # echte Bedienung nach der gelungenen Angleichung
    a.run()
    assert a.wuensche == [(23.0, "soll_entity", "nutzer")] and a.store.value == 23.0


def test_the_watch_retries_the_owed_alignment_until_it_succeeds(tmp_path):
    a = Aufbau(tmp_path)  # WebSocket nie verbunden: die Wache liest im Takt des lokalen Checks
    _portal_waehrend_ha_down(a)
    a.clock.advance(300)
    a.run()  # HA weiter nicht beschreibbar: kein Wunsch, Speicher bleibt
    assert a.ha.writes == [] and a.wuensche == [] and a.store.value == 22.5
    a.ha.write_error = None
    a.clock.advance(300)
    a.run()
    assert a.ha.writes == [(SOLL, 22.5)] and a.wuensche == [] and a.store.value == 22.5
    a.clock.advance(300)
    a.run()  # angeglichen: die Wache schreibt nicht erneut
    assert a.ha.writes == [(SOLL, 22.5)]


def test_the_watch_retries_an_owed_alignment_also_while_connected(tmp_path, monkeypatch):
    a = Aufbau(tmp_path)
    monkeypatch.setattr(type(a.client), "connected", property(lambda self: True))
    _portal_waehrend_ha_down(a)
    a.ha.write_error = None
    a.clock.advance(300)
    a.run()
    assert a.ha.writes == [(SOLL, 22.5)] and a.wuensche == [] and a.store.value == 22.5


def test_a_report_of_the_old_value_while_the_alignment_is_owed_writes_again(tmp_path):
    """Nach einem HA-Neustart meldet der Trigger den wiederhergestellten alten Wert (unavailable -> 21.0)."""
    a = Aufbau(tmp_path)
    _portal_waehrend_ha_down(a)
    a.ha.write_error = None
    a.soll_aenderung(21.0)
    a.run()
    assert a.ha.writes == [(SOLL, 22.5)] and a.wuensche == [] and a.store.value == 22.5


def test_a_failed_write_back_of_a_clamped_wish_is_written_again_not_wished_back(tmp_path):
    a = Aufbau(tmp_path)
    a.ha.write_error = RuntimeError("HA Core startet")
    a.ereignis(30.0, {"user_id": "u1"})  # lokal 30 -> begrenzt 25; das Zurueckschreiben scheitert
    a.run()
    assert a.store.value == 25.0 and a.wuensche == [(30.0, "smartheat_entity", "nutzer")]
    a.ha.write_error = None
    a.client._on_connected()  # die Entity zeigt noch 21
    a.run()
    assert a.ha.writes == [(SOLL, 25.0)] and a.store.value == 25.0
    assert a.wuensche == [(30.0, "smartheat_entity", "nutzer")]


def test_an_owed_alignment_is_not_written_to_a_thermostat_that_is_off_and_follows_when_it_is_on_again(tmp_path):
    a = Aufbau(tmp_path)
    _portal_waehrend_ha_down(a)
    a.ha.write_error = None
    a.ha.states[CLIMATE] = "off"
    a.client._on_connected()
    a.run()
    assert a.ha.writes == [] and a.wuensche == [] and a.aus == [(True, 22.5)]
    a.fire({"platform": "state", "entity_id": CLIMATE, "to_state": {"state": "heat"}})
    a.run()  # wieder an: der Soll-Eingang gleicht an (wie bisher)
    assert a.ha.writes == [(SOLL, 22.5)] and a.wuensche == [] and a.store.value == 22.5


def test_an_owed_alignment_leaves_a_thermostat_in_frost_protection_alone(tmp_path):
    """Frostschutz-Stellung (bis 7 °C) ist Soll-Quelle aus (Spec 5b 3.2): auch eine geschuldete Angleichung dreht den
    bewusst heruntergestellten Heizkoerper nicht wieder hoch."""
    a = Aufbau(tmp_path)
    _portal_waehrend_ha_down(a)
    a.ha.write_error = None
    a.ha.states[SOLL] = 5.0
    a.client._on_connected()
    a.run()
    assert a.ha.writes == [] and a.wuensche == [] and a.aus == [(True, 22.5)] and a.store.value == 22.5


def test_a_sensor_target_never_owes_an_alignment(tmp_path):
    a = Aufbau(tmp_path, soll="input_number.soll")
    a.ha.states["input_number.soll"] = 21.0
    a.worker.post(Event(EV_TARGET_INPUT, {"value": 22.5, "source": "portal"}))
    a.run()
    a.client._on_connected()  # nicht schreibbar: der gezeigte Wert ist eine Eingabe wie bisher
    a.run()
    assert a.ha.writes == [] and a.wuensche == [(21.0, "soll_entity", None)]

import pytest
from configs import THERMOSTAT
from fake_z2m import FakeZigbee2Mqtt
from fakes import FakeBus

from smartheat_gateway.thermostat_profil import profil
from smartheat_gateway.zigbee import ZigbeeMirror

W100 = "0x54ef44100140eb34"  # Testwert in der IEEE-Form von Zigbee2MQTT
EINRICHTUNG_W100 = [
    (W100, {"thermostat_mode": "ON"}),
    (W100, {"auto_hide_middle_line": False}),
    (W100, {"occupied_heating_setpoint": 20.0}),
    (W100, {"system_mode": "heat"}),
]


@pytest.fixture
def welt(clock):
    bus = FakeBus()
    z2m = FakeZigbee2Mqtt(bus)
    mirror = ZigbeeMirror(bus, clock)
    mirror.start()
    z2m.bridge(online=True)
    z2m.add_w100(W100)
    z2m.add_thermostat(THERMOSTAT)
    return bus, z2m, mirror


def test_a_paired_w100_becomes_a_thermostat_with_one_key_per_message(welt):
    # Labor 2026-10-21: Z2M setzt beim Koppeln thermostat_mode OFF; mehrere Schluessel in einer Nachricht ueberschreiben
    # sich im Konverter, also je Schluessel eine Nachricht, das Soll vor heat (sonst gilt das Werks-Soll 15 als Wunsch)
    _, z2m, _ = welt
    z2m.interview(W100)
    assert z2m.set_requests == EINRICHTUNG_W100


def test_a_thermostat_without_a_profile_is_left_alone(welt):
    _, z2m, _ = welt
    z2m.interview(THERMOSTAT)
    assert z2m.set_requests == []


@pytest.mark.parametrize("status", ["started", "failed"])
def test_an_unfinished_interview_sets_nothing(welt, status):
    _, z2m, _ = welt
    z2m.interview(W100, status=status)
    assert z2m.set_requests == []
    z2m.interview(W100)  # Gegenprobe: dasselbe Geraet nach erfolgreichem Interview
    assert z2m.set_requests == EINRICHTUNG_W100


@pytest.mark.parametrize("ereignis", [
    None, [], "text", {"type": "device_joined", "data": {"ieee_address": W100}},
    {"type": "device_interview"}, {"type": "device_interview", "data": "x"},
    {"type": "device_interview", "data": {"ieee_address": W100, "status": "successful"}},
    {"type": "device_interview", "data": {"ieee_address": W100, "status": "successful", "definition": "TH-S04D"}},
    {"type": "device_interview", "data": {"ieee_address": W100, "status": "successful", "definition": {}}},
    {"type": "device_interview", "data": {"ieee_address": 7, "status": "successful",
                                          "definition": {"model": "TH-S04D"}}},
    {"type": "device_interview", "data": {"status": "successful", "definition": {"model": "TH-S04D"}}},
    {"type": "device_interview", "data": {"ieee_address": W100, "status": "failed", "definition": {"model": "TH-S04D"}}},
])
def test_junk_on_bridge_event_sets_nothing_and_raises_nothing(welt, ereignis):
    bus, z2m, _ = welt
    bus.publish("zigbee2mqtt/bridge/event", ereignis)
    assert z2m.set_requests == []


def test_the_bound_thermostat_is_set_up_by_its_binding_not_with_the_default(welt):
    _, z2m, mirror = welt
    aufrufe = []
    mirror.pflege.binden(W100, lambda: aufrufe.append("laufzeit"))
    z2m.interview(W100)
    assert aufrufe == ["laufzeit"] and z2m.set_requests == []
    mirror.pflege.loesen(W100)
    z2m.interview(W100)
    assert aufrufe == ["laufzeit"] and z2m.set_requests == EINRICHTUNG_W100


def test_a_new_binding_replaces_the_previous_one(welt):
    # die Laufzeit startet nach jeder Konfigurationsaenderung neu (L4-17): es gilt nur die juengste Bindung
    _, z2m, mirror = welt
    aufrufe = []
    mirror.pflege.binden(W100, lambda: aufrufe.append("alt"))
    mirror.pflege.binden(W100, lambda: aufrufe.append("neu"))
    z2m.interview(W100)
    assert aufrufe == ["neu"]
    mirror.pflege.binden(THERMOSTAT, lambda: aufrufe.append("anderes"))
    z2m.interview(W100)  # nicht mehr gebunden: Vorgabe
    assert aufrufe == ["neu"] and z2m.set_requests == EINRICHTUNG_W100


def test_releasing_another_device_keeps_the_binding(welt):
    _, z2m, mirror = welt
    aufrufe = []
    mirror.pflege.binden(W100, lambda: aufrufe.append("laufzeit"))
    mirror.pflege.loesen(THERMOSTAT)
    z2m.interview(W100)
    assert aufrufe == ["laufzeit"] and z2m.set_requests == []


def test_profiles_exist_only_for_known_models():
    assert profil("TH-S04D") is not None and profil("TH-S04D").ganzzahlig
    assert profil("TRV-Test") is None and profil("") is None and profil(None) is None


@pytest.mark.parametrize(("soll", "angezeigt"), [
    (20.5, 21.0), (21.5, 22.0), (15.0, 15.0), (21.0, 21.0), (24.5, 25.0), (25.0, 25.0),
])
def test_the_w100_shows_whole_degrees_rounded_half_up(soll, angezeigt):
    # kaufmaennisch (Nutzer 2026-10-21); round() rundete 20,5 auf 20 und 21,5 auf 22
    assert profil("TH-S04D").anzeige(soll) == angezeigt

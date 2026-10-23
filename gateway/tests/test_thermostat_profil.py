import pytest
from configs import THERMOSTAT
from fake_z2m import FakeZigbee2Mqtt
from fakes import FakeBus

from smartheat_gateway.thermostat_profil import profil, soll_und_heizen
from smartheat_gateway.zigbee import ZigbeeMirror

W100 = "0x54ef44100140eb34"  # Testwert in der IEEE-Form von Zigbee2MQTT
W101 = "0x54ef44100140eb35"
MODUS_W100 = [
    (W100, {"thermostat_mode": "ON"}),
    (W100, {"auto_hide_middle_line": False}),
]
SOLL_UND_HEIZEN_W100 = [
    (W100, {"occupied_heating_setpoint": 20.0}),
    (W100, {"system_mode": "heat"}),
]
EINRICHTUNG_W100 = MODUS_W100 + SOLL_UND_HEIZEN_W100


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


def test_a_paired_w100_gets_its_mode_at_once_and_setpoint_and_heat_once_it_reports_the_mode_on(welt):
    # Labor 2026-10-22: der Konverter lumi verwirft Soll und system_mode, solange der zwischengespeicherte
    # thermostat_mode nicht ON ist, und ON steht dort erst, wenn das Geraet es gemeldet hat. Je Schluessel eine
    # Nachricht (Labor 2026-10-21), das Soll vor heat (sonst gilt das Werks-Soll 15 als Wunsch)
    _, z2m, _ = welt
    z2m.interview(W100)
    assert z2m.set_requests == MODUS_W100  # kein Soll, kein system_mode, ...
    z2m.report(W100, thermostat_mode="ON", system_mode="off", occupied_heating_setpoint=15)
    assert z2m.set_requests == EINRICHTUNG_W100  # ... erst nach der Meldung, genau einmal, Soll vor heat
    assert z2m.verworfen == []


def test_a_w100_whose_state_already_knows_the_mode_on_is_set_up_at_once(welt):
    # Gegenprobe zum Labor-Befund: beim Neukoppeln kannte der Zustand ON schon, alles wurde uebernommen
    _, z2m, _ = welt
    z2m.report(W100, thermostat_mode="ON")
    z2m.interview(W100)
    assert z2m.set_requests == EINRICHTUNG_W100 and z2m.verworfen == []


def test_a_report_without_the_mode_on_sends_nothing_more_and_a_later_one_with_on_does(welt):
    _, z2m, _ = welt
    z2m.interview(W100)
    z2m.report(W100, thermostat_mode="OFF", system_mode="off")
    z2m.report(W100, battery=90)  # eine Meldung ganz ohne thermostat_mode (Zigbee2MQTT haelt den alten Zustand)
    z2m.report(THERMOSTAT, thermostat_mode="ON")  # ein anderes Geraet zaehlt nicht
    assert z2m.set_requests == MODUS_W100
    z2m.report(W100, thermostat_mode="ON")
    assert z2m.set_requests == EINRICHTUNG_W100


def test_setpoint_and_heat_are_sent_only_once(welt):
    _, z2m, _ = welt
    z2m.interview(W100)
    z2m.report(W100, thermostat_mode="ON")
    z2m.report(W100, thermostat_mode="ON", battery=80)
    z2m.report(W100, system_mode="off")  # am Geraet ausgeschaltet: bleibt aus
    assert z2m.set_requests == EINRICHTUNG_W100


def test_pairing_again_before_the_mode_is_reported_keeps_one_open_rest(welt):
    _, z2m, _ = welt
    z2m.interview(W100)
    z2m.interview(W100)
    assert z2m.set_requests == MODUS_W100 * 2
    z2m.report(W100, thermostat_mode="ON")
    assert z2m.set_requests == MODUS_W100 * 2 + SOLL_UND_HEIZEN_W100


def test_each_device_has_its_own_open_rest(welt):
    # je IEEE ein offener Rest: der eines Geraets hindert das andere nicht
    _, z2m, _ = welt
    z2m.add_w100(W101)
    z2m.interview(W100)
    z2m.interview(W101)
    z2m.report(W101, thermostat_mode="ON")
    assert z2m.set_requests == MODUS_W100 + [(W101, {"thermostat_mode": "ON"}),
                                              (W101, {"auto_hide_middle_line": False}),
                                              (W101, {"occupied_heating_setpoint": 20.0}),
                                              (W101, {"system_mode": "heat"})]
    z2m.report(W100, thermostat_mode="ON")
    assert z2m.set_requests[-2:] == SOLL_UND_HEIZEN_W100


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
    z2m.report(W100, thermostat_mode="ON")
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
    z2m.report(W100, thermostat_mode="ON")
    assert aufrufe == ["laufzeit"] and z2m.set_requests == EINRICHTUNG_W100


def test_a_new_binding_replaces_the_previous_one(welt):
    # die Laufzeit startet nach jeder Konfigurationsaenderung neu: es gilt nur die juengste Bindung
    _, z2m, mirror = welt
    aufrufe = []
    mirror.pflege.binden(W100, lambda: aufrufe.append("alt"))
    mirror.pflege.binden(W100, lambda: aufrufe.append("neu"))
    z2m.interview(W100)
    assert aufrufe == ["neu"]
    mirror.pflege.binden(THERMOSTAT, lambda: aufrufe.append("anderes"))
    z2m.interview(W100)  # nicht mehr gebunden: Vorgabe
    z2m.report(W100, thermostat_mode="ON")
    assert aufrufe == ["neu"] and z2m.set_requests == EINRICHTUNG_W100


def test_releasing_another_device_keeps_the_binding(welt):
    _, z2m, mirror = welt
    aufrufe = []
    mirror.pflege.binden(W100, lambda: aufrufe.append("laufzeit"))
    mirror.pflege.loesen(THERMOSTAT)
    z2m.interview(W100)
    assert aufrufe == ["laufzeit"] and z2m.set_requests == []


def test_releasing_the_device_drops_its_open_rest(welt):
    _, z2m, mirror = welt
    z2m.interview(W100)
    mirror.pflege.loesen(W100)
    z2m.report(W100, thermostat_mode="ON")
    assert z2m.set_requests == MODUS_W100


def test_binding_before_the_on_report_gives_the_wish_not_20(welt):
    """Ungebunden gekoppelt (Rest mit DEFAULT_RAUM_SOLL), dann gebunden, bevor das Geraet ON meldet: die
    Bindung uebernimmt die Einrichtung, das Geraet bekommt das Soll aus dem Soll-Speicher (22,5 -> 23), nicht 20."""
    _, z2m, mirror = welt
    p = profil("TH-S04D")
    z2m.interview(W100)
    aufrufe = []

    def einrichten_lassen():  # wie die Laufzeit: Einrichtung im Worker mit dem Soll aus dem Speicher
        aufrufe.append("laufzeit")
        mirror.pflege.einrichten(W100, p, lambda: soll_und_heizen(mirror, W100, p, 22.5))

    mirror.pflege.binden(W100, einrichten_lassen)
    z2m.report(W100, thermostat_mode="ON")
    assert [body for _, body in z2m.set_requests if "occupied_heating_setpoint" in body] == [
        {"occupied_heating_setpoint": 23.0}]
    assert z2m.set_requests == MODUS_W100 * 2 + [(W100, {"occupied_heating_setpoint": 23.0}),
                                                  (W100, {"system_mode": "heat"})]
    assert aufrufe == ["laufzeit"] and z2m.verworfen == []
    mirror.pflege.binden(W100, einrichten_lassen)  # Gegenprobe: ohne offenen Rest richtet das Binden nichts ein
    assert aufrufe == ["laufzeit"]


def test_after_on_runs_at_once_if_the_mode_on_is_known_and_otherwise_with_the_report(welt):
    _, z2m, mirror = welt
    aufrufe = []
    mirror.pflege.nach_an(W100, lambda: aufrufe.append("an"))
    z2m.report(W100, thermostat_mode="OFF")
    mirror.pflege.binden(W100, lambda: aufrufe.append("einrichten"))  # kein Rest einer Kopplung: Binden richtet nicht ein
    assert aufrufe == [] and z2m.set_requests == []  # nur warten, nichts senden
    z2m.report(W100, thermostat_mode="ON")
    z2m.report(W100, thermostat_mode="ON")
    assert aufrufe == ["an"]
    mirror.pflege.nach_an(W100, lambda: aufrufe.append("sofort"))
    assert aufrufe == ["an", "sofort"]


def test_profiles_exist_only_for_known_models():
    assert profil("TH-S04D") is not None and profil("TH-S04D").ganzzahlig
    assert profil("TRV-Test") is None and profil("") is None and profil(None) is None


@pytest.mark.parametrize(("soll", "angezeigt"), [
    (20.5, 21.0), (21.5, 22.0), (15.0, 15.0), (21.0, 21.0), (24.5, 25.0), (25.0, 25.0),
])
def test_the_w100_shows_whole_degrees_rounded_half_up(soll, angezeigt):
    # kaufmaennisch (Nutzer 2026-10-21); round() rundete 20,5 auf 20 und 21,5 auf 22
    assert profil("TH-S04D").anzeige(soll) == angezeigt

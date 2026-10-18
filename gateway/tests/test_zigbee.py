import pytest
from fake_z2m import FakeZigbee2Mqtt
from fakes import FakeBus

from smartheat_gateway.zigbee import (
    CAP_BATTERY,
    CAP_BATTERY_LOW,
    CAP_SETPOINT_WRITABLE,
    CAP_TEMPERATURE,
    ZigbeeMirror,
    capabilities,
)

SENSOR, THERMOSTAT = "0x00124b0000000001", "0x00124b0000000002"


@pytest.fixture
def world(clock):
    bus = FakeBus()
    z2m = FakeZigbee2Mqtt(bus)
    mirror = ZigbeeMirror(bus, clock)
    mirror.start()
    z2m.bridge(online=True)
    z2m.add_sensor(SENSOR, battery="prozent")
    z2m.add_thermostat(THERMOSTAT)
    return bus, z2m, mirror


def test_capabilities_from_exposes(world):
    _, _, mirror = world
    sensor, thermostat = mirror.device(SENSOR), mirror.device(THERMOSTAT)
    assert {CAP_TEMPERATURE, CAP_BATTERY} <= sensor.faehigkeiten and sensor.art == "fuehler"
    assert CAP_SETPOINT_WRITABLE in thermostat.faehigkeiten and thermostat.art == "thermostat"
    assert CAP_BATTERY_LOW in thermostat.faehigkeiten


def test_nested_climate_features_and_read_only_setpoint():
    exposes = [{"type": "climate", "features": [
        {"name": "occupied_heating_setpoint", "property": "occupied_heating_setpoint", "access": 1},
        {"name": "local_temperature", "property": "local_temperature", "access": 1},
    ]}]
    caps = capabilities(exposes)
    assert CAP_SETPOINT_WRITABLE not in caps and "local_temperature" in caps


def test_values_age_with_the_monotonic_clock(world, clock):
    _, z2m, mirror = world
    z2m.report(SENSOR, temperature=20.5, battery=80)
    assert mirror.value(SENSOR, "temperature") == 20.5
    clock.advance(7199)
    assert mirror.value(SENSOR, "temperature") == 20.5
    clock.advance(2)
    with pytest.raises(ValueError, match="veraltet"):
        mirror.value(SENSOR, "temperature")


def test_bridge_offline_invalidates_all_values(world):
    _, z2m, mirror = world
    z2m.report(SENSOR, temperature=20.5)
    z2m.bridge(online=False)
    with pytest.raises(ValueError, match="offline"):
        mirror.value(SENSOR, "temperature")


def test_unknown_device_or_field(world):
    _, z2m, mirror = world
    with pytest.raises(ValueError):
        mirror.value("0x00124b00000000ff", "temperature")
    z2m.report(SENSOR, temperature=20.5)
    with pytest.raises(ValueError):
        mirror.value(SENSOR, "humidity")


def test_write_setpoint_and_permit_join(world):
    bus, z2m, mirror = world
    mirror.write_setpoint(THERMOSTAT, 21.5)
    assert ("zigbee2mqtt/0x00124b0000000002/set", {"occupied_heating_setpoint": 21.5}) in bus.decoded()
    mirror.permit_join(120)
    assert z2m.permit_join_requests == [120]


def test_device_message_hook_ignores_bridge_and_subtopics(world):
    bus, z2m, mirror = world
    seen = []
    mirror.on_device_message(lambda ieee, payload: seen.append((ieee, payload)))
    z2m.report(SENSOR, temperature=21.0)
    bus.publish(f"zigbee2mqtt/{SENSOR}/availability", {"state": "online"})
    assert seen == [(SENSOR, {"temperature": 21.0})]


def test_retained_device_payload_is_kept_but_not_fresh(clock):
    bus = FakeBus()
    mirror = ZigbeeMirror(bus, clock)
    mirror.start()
    seen = []
    mirror.on_device_message(lambda ieee, payload: seen.append(ieee))
    bus.deliver("zigbee2mqtt/0xabc", {"temperature": 21.5}, retain=True)
    assert mirror.payload("0xabc") == {"temperature": 21.5}
    assert mirror.last_seen("0xabc") is None
    with pytest.raises(ValueError, match="noch nicht live"):
        mirror.value("0xabc", "temperature")
    assert seen == []  # ein replaytes Thermostat-Soll ist keine Eingabe


def test_live_message_after_retained_counts(clock):
    bus = FakeBus()
    mirror = ZigbeeMirror(bus, clock)
    mirror.start()
    bus.deliver("zigbee2mqtt/0xabc", {"temperature": 21.5}, retain=True)
    bus.deliver("zigbee2mqtt/0xabc", {"temperature": 21.7})
    assert mirror.value("0xabc", "temperature") == 21.7
    assert mirror.last_seen("0xabc") == clock()


def _state_of(mirror):
    return (mirror.online, mirror.devices(), mirror.payload(SENSOR), mirror.payload(THERMOSTAT),
            mirror.last_seen(SENSOR))


JUNK_ON_A_DEVICE_TOPIC = [
    ("kein JSON", "kein json{"), ("Zahl", "42"), ("Text als JSON", '"online"'), ("Liste", "[1, 2]"), ("null", "null"),
    ("leer", ""),
]


@pytest.mark.parametrize("junk", [roh for _, roh in JUNK_ON_A_DEVICE_TOPIC], ids=[n for n, _ in JUNK_ON_A_DEVICE_TOPIC])
def test_junk_on_a_device_topic_changes_nothing_and_raises_nothing(world, junk):
    bus, z2m, mirror = world
    z2m.report(SENSOR, temperature=20.5, battery=80)
    seen = []
    mirror.on_device_message(lambda ieee, payload: seen.append(ieee))
    before = _state_of(mirror)
    bus.publish(f"zigbee2mqtt/{SENSOR}", junk)  # roher Text; FakeBus ruft den Rueckruf des Spiegels ohne Fangnetz
    bus.publish(f"zigbee2mqtt/{THERMOSTAT}", junk)
    assert _state_of(mirror) == before and seen == []
    assert mirror.value(SENSOR, "temperature") == 20.5
    # Gegenprobe: eine gueltige Meldung auf demselben Topic aendert den Zustand und ruft den Hook
    z2m.report(SENSOR, temperature=19.0, battery=80)
    assert _state_of(mirror) != before and mirror.value(SENSOR, "temperature") == 19.0 and seen == [SENSOR]


def test_bytes_that_are_no_utf8_on_a_device_topic_change_nothing(world):
    bus, z2m, mirror = world
    z2m.report(SENSOR, temperature=20.5)
    before = _state_of(mirror)
    [(_, callback)] = [(topic, cb) for topic, cb in bus.subscriptions if topic == "zigbee2mqtt/#"]
    callback(f"zigbee2mqtt/{SENSOR}", b"\xff\xfe", False)
    assert _state_of(mirror) == before


def test_bridge_devices_skips_what_is_no_object_and_the_coordinator(world):
    bus, _, mirror = world
    sensor = {"ieee_address": SENSOR, "type": "EndDevice", "definition": {
        "model": "S", "vendor": "V", "exposes": [{"name": "temperature", "property": "temperature", "access": 1}]}}
    coordinator = {"ieee_address": "0x00124b00000000aa", "type": "Coordinator", "definition": None}
    bus.publish("zigbee2mqtt/bridge/devices", [5, "text", None, [sensor], coordinator, sensor, 1.5])
    assert [device.ieee for device in mirror.devices()] == [SENSOR]
    assert mirror.device("0x00124b00000000aa") is None
    # Gegenprobe: ohne den Koordinator-Eintrag zaehlt der Eintrag mit anderem Typ als Geraet
    bus.publish("zigbee2mqtt/bridge/devices", [{**coordinator, "type": "Router"}, 5])
    assert [device.ieee for device in mirror.devices()] == ["0x00124b00000000aa"]


@pytest.mark.parametrize("body", ["kein json{", "{}", '{"ieee_address": "x"}', "42", '"liste"', "null"],
                         ids=["kein JSON", "leeres Objekt", "Objekt", "Zahl", "Text", "null"])
def test_a_bridge_devices_message_that_is_no_list_leaves_the_device_list_alone(world, body):
    bus, _, mirror = world
    before = mirror.devices()
    assert [device.ieee for device in before] == [SENSOR, THERMOSTAT]
    bus.publish("zigbee2mqtt/bridge/devices", body)
    assert mirror.devices() == before


@pytest.mark.parametrize(("roh", "online"), [
    ('{"state": "online"}', True), ('{"state": "offline"}', False), ("online", True), ("offline", False),
    ("kaputt{", False), ("[1]", False), ('{"x": 1}', None), ("null", None), ("", None)])
def test_what_the_bridge_state_makes_of_a_message(world, roh, online):
    """Festgehalten: anders als auf Geraetetopics zaehlt Muell auf bridge/state als offline (nur ein fehlender Zustand
    laesst `online` unbekannt); offline macht alle Werte ungueltig, bis die naechste Meldung online sagt."""
    bus, z2m, mirror = world
    z2m.report(SENSOR, temperature=20.5)
    assert mirror.online is True and mirror.value(SENSOR, "temperature") == 20.5
    bus.publish("zigbee2mqtt/bridge/state", roh)
    assert mirror.online is online
    if online is False:
        with pytest.raises(ValueError, match="offline"):
            mirror.value(SENSOR, "temperature")
    z2m.bridge(online=True)  # Gegenprobe: ein gueltiges online stellt den Spiegel wieder her
    assert mirror.online is True and mirror.value(SENSOR, "temperature") == 20.5

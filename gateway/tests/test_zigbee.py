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

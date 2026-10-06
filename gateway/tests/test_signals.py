import pytest
from configs import SENSOR, THERMOSTAT
from fake_z2m import FakeZigbee2Mqtt
from fakes import FakeBus

from smartheat_gateway.drivers.registry import create
from smartheat_gateway.paths import Paths
from smartheat_gateway.signals import REF_ROOM_MEAN, REF_ROOM_TARGET, GatewaySignalSource
from smartheat_gateway.target_store import TargetStore
from smartheat_gateway.zigbee import ZigbeeMirror
from smartheat_runtime import battery
from smartheat_runtime.ports import SignalNotFound, SourceUnavailable
from smartheat_runtime.runtime_config import BATTERY_LOW_FLAG

SECOND = "0x00124b0000000003"


@pytest.fixture
def world(data_dir, clock):
    bus = FakeBus()
    z2m = FakeZigbee2Mqtt(bus)
    mirror = ZigbeeMirror(bus, clock)
    mirror.start()
    z2m.bridge(online=True)
    z2m.add_sensor(SENSOR)
    z2m.add_sensor(SECOND, battery="flag")
    z2m.add_thermostat(THERMOSTAT)
    driver = create("simulation", {}, Paths(data_dir), clock=clock, writer=True)
    driver.poll_once()
    store = TargetStore(data_dir / "runtime" / "room_target.json", 21.0, clock, lambda: "ts")
    source = GatewaySignalSource(bus, mirror)
    source.bind(driver, store, (f"zigbee:{SENSOR}:temperature", f"zigbee:{SECOND}:temperature"))
    return bus, z2m, source


def test_room_mean_target_and_driver(world):
    _, z2m, source = world
    z2m.report(SENSOR, temperature=20.0)
    z2m.report(SECOND, temperature=21.0)
    assert source.get_state(REF_ROOM_MEAN) == 20.5
    assert source.get_state(REF_ROOM_TARGET) == 21.0
    assert isinstance(source.get_state("treiber:outdoor_temp"), float)


def test_one_dead_sensor_keeps_the_mean_none_is_invalid(world):
    _, z2m, source = world
    z2m.report(SENSOR, temperature=20.0)
    assert source.get_state(REF_ROOM_MEAN) == 20.0
    z2m.bridge(online=False)
    with pytest.raises(ValueError):
        source.get_state(REF_ROOM_MEAN)


def test_bool_battery_flag_reads_like_ha(world):
    _, z2m, source = world
    z2m.report(SECOND, battery_low=True)
    assert source.get_raw_state(f"zigbee:{SECOND}:battery_low") == "on"
    state = battery.next_state("ok", BATTERY_LOW_FLAG, source.get_raw_state(f"zigbee:{SECOND}:battery_low"))
    assert state == "niedrig"
    z2m.report(SECOND, battery_low=False)
    assert source.get_raw_state(f"zigbee:{SECOND}:battery_low") == "off"


def test_bus_down_is_unavailable_unknown_ref_not_found(world):
    bus, _, source = world
    with pytest.raises(SignalNotFound):
        source.get_state("sensor.wohnzimmer")
    bus.disconnect()
    with pytest.raises(SourceUnavailable):
        source.get_state(f"zigbee:{SENSOR}:temperature")


def test_device_keys(world):
    _, _, source = world
    assert source.device_key(f"zigbee:{SENSOR}:temperature") == SENSOR
    assert source.device_key("treiber:room_temperature") == "treiber:room_temperature"

import math
from types import SimpleNamespace

import pytest
from bindung_beispiele import VIESSMANN_VICARE_BINDING
from configs import SENSOR, THERMOSTAT
from fake_z2m import FakeZigbee2Mqtt
from fakes import FakeBus

from smartheat_gateway.drivers.registry import create
from smartheat_gateway.paths import Paths
from smartheat_gateway.signals import REF_ROOM_MEAN, REF_ROOM_TARGET, GatewaySignalSource
from smartheat_gateway.texts import SHG_TEXTS
from smartheat_gateway.zigbee import ZigbeeMirror
from smartheat_runtime import battery
from smartheat_runtime.notifier import Notifier
from smartheat_runtime.ports import SignalNotFound, SignalUngueltig, SourceUnavailable
from smartheat_runtime.runtime_config import BATTERY_LOW_FLAG, BatteryRef
from smartheat_runtime.soll_speicher import TargetStore
from smartheat_runtime.state import StateStore

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
    driver = create("simulation", {}, Paths(data_dir), clock=clock, writer=True,
                    description=VIESSMANN_VICARE_BINDING)
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
    with pytest.raises(SignalUngueltig):  # erwartet ungueltig, kein Programmfehler
        source.get_state(REF_ROOM_MEAN)


@pytest.mark.parametrize("wert", ["abc", True, None, [1]], ids=["text", "bool", "null", "liste"])
def test_a_reading_that_is_no_number_is_signal_ungueltig(world, wert):
    _, z2m, source = world
    z2m.report(SENSOR, temperature=wert)
    with pytest.raises(SignalUngueltig, match="liefert keine Zahl"):
        source.get_state(f"zigbee:{SENSOR}:temperature")


@pytest.mark.parametrize("huge", [10**400, -10**400], ids=["gross-positiv", "gross-negativ"])
def test_integer_beyond_float_range_is_no_number_and_leaves_the_mean_to_the_others(world, huge):
    # Zigbee2MQTT liefert JSON; eine Ganzzahl ausserhalb des float-Bereichs ist ein ungueltiger Wert (ValueError)
    _, z2m, source = world
    z2m.report(SENSOR, temperature=huge)
    z2m.report(SECOND, temperature=21.0)
    with pytest.raises(SignalUngueltig, match="Zahlenbereich"):
        source.get_state(f"zigbee:{SENSOR}:temperature")
    assert source.get_state(REF_ROOM_MEAN) == 21.0


ROH_NICHT_PLAUSIBEL = [("NaN", math.isnan), ("Infinity", lambda v: v == math.inf), ("-Infinity", lambda v: v == -math.inf),
                       ("1e999", lambda v: v == math.inf), ("1e308", lambda v: v == 1e308)]


@pytest.mark.parametrize(("roh", "erwartet"), ROH_NICHT_PLAUSIBEL, ids=[roh for roh, _ in ROH_NICHT_PLAUSIBEL])
def test_a_non_finite_or_implausible_sensor_value_is_passed_on_like_the_ha_host_and_dropped_by_the_room_mean(
        world, roh, erwartet):
    """F-gateway-host-26, festgehalten: Zigbee2MQTT-JSON mit NaN, Infinity, 1e999 oder 1e308 (Bus-Weg: `bus.decode` mit
    json.loads) kommt als float aus `get_state`, genau wie `float(state)` im HA-Host (ha_api.get_state). Die Pruefung auf
    Endlichkeit und Plausibilitaet (5 bis 35 Grad) liegt hinter der Signalquelle: das Raummittel (room_mean, in beiden Hosts
    dieselbe Funktion) zaehlt solche Werte nicht. Der zweite Fuehler bestimmt das Mittel allein."""
    bus, z2m, source = world
    bus.publish(f"zigbee2mqtt/{SENSOR}", '{"temperature": ' + roh + "}")  # roher Text wie vom Broker
    z2m.report(SECOND, temperature=21.0)
    value = source.get_state(f"zigbee:{SENSOR}:temperature")
    assert isinstance(value, float) and erwartet(value)
    assert source.get_state(REF_ROOM_MEAN) == 21.0  # Gegenprobe: ein plausibler Wert zaehlt (21,0 allein)
    z2m.report(SECOND, temperature=float("nan"))
    with pytest.raises(SignalUngueltig, match="kein Raumf"):
        source.get_state(REF_ROOM_MEAN)  # beide Fuehler unbrauchbar: kein Mittel, kein erfundener Wert


def test_the_gateway_has_no_unit(world):
    _, _, source = world
    with pytest.raises(ValueError) as caught:
        source.get_unit(f"zigbee:{SENSOR}:temperature")
    assert not isinstance(caught.value, SignalUngueltig)  # Programmfehler, kein ungueltiger Wert


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


class RecordingSink:
    def __init__(self) -> None:
        self.pushed: list[tuple[str, str]] = []

    def push(self, key: str, message: str) -> None:
        self.pushed.append((key, message))

    def show(self, key: str, message: str) -> None:
        pass

    def withdraw(self, key: str) -> None:
        pass


def test_battery_low_flag_ends_up_as_one_hint(world, data_dir):
    """Batterie-Hinweis durchgaengig: Zigbee-Meldung -> Signalquelle -> battery.check_batteries -> Notifier."""
    _, z2m, source = world
    ref = f"zigbee:{SECOND}:battery_low"
    sink = RecordingSink()
    rt = SimpleNamespace(
        config=SimpleNamespace(battery_refs=(BatteryRef(ref, BATTERY_LOW_FLAG),)), signals=source,
        notifier=Notifier(StateStore(data_dir / "backup.json", data_dir / "failsafe_state.json"), sink),
        texts=SHG_TEXTS,
    )
    z2m.report(SECOND, battery_low=True)
    battery.check_batteries(rt)
    battery.check_batteries(rt)  # unveraendert: keine zweite Meldung
    assert len(sink.pushed) == 1
    key, text = sink.pushed[0]
    assert key == f"batterie:{ref}" and SECOND in text and "schwach" in text

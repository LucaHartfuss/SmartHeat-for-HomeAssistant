"""V2-Konformitaet des Gateway-Hosts (heizungsbruecke/testhilfen/host_konformitaet.py)."""
import pytest
from configs import SENSOR, konfiguration
from fake_z2m import FakeZigbee2Mqtt
from fakes import FakeBus
from host_konformitaet import Welt, parameter

from smartheat_device.geraet_host_basis import EV_RELOAD
from smartheat_gateway import geraet_host as gh
from smartheat_gateway.paths import Paths
from smartheat_gateway.signals import GatewaySignalSource
from smartheat_gateway.zigbee import ZigbeeMirror

ABWEICHUNGEN: dict[str, str] = {}


@pytest.fixture
def welt(tmp_path, clock):
    paths = Paths(tmp_path / "data")
    bus = FakeBus()
    mirror = ZigbeeMirror(bus, clock)
    mirror.start()
    z2m = FakeZigbee2Mqtt(bus)
    z2m.bridge(online=True)
    z2m.add_sensor(SENSOR)
    signale = GatewaySignalSource(bus, mirror)

    def fuehler_lesen(roh):  # der Fuehler meldet `roh` ueber Zigbee2MQTT, gelesen ueber die echte Signalquelle
        z2m.report(SENSOR, temperature=roh)
        return signale.get_state(f"zigbee:{SENSOR}:temperature")

    host = gh.GatewayDeviceHost(paths, mirror, host_dir=tmp_path / "host", clock=clock)
    return Welt(host=host, clock=clock, reload_kind=EV_RELOAD, aufraeumen_pfad=paths.aufraeumen,
                abgemeldet_pfad=paths.abgemeldet, konfiguration=konfiguration(), wunsch_herkunft="thermostat",
                fuehler_lesen=fuehler_lesen)


@pytest.mark.parametrize("pruefung", parameter(ABWEICHUNGEN))
def test_konformitaet(pruefung, welt):
    pruefung(welt)

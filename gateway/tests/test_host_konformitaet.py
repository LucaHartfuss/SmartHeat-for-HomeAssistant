"""V2-Konformitaet des Gateway-Hosts (heizungsbruecke/testhilfen/host_konformitaet.py; Spec Stufe 3 S6 4.1)."""
import pytest
from configs import konfiguration
from fakes import FakeBus
from host_konformitaet import Welt, parameter

from smartheat_device.geraet_host_basis import EV_RELOAD
from smartheat_gateway import geraet_host as gh
from smartheat_gateway.paths import Paths
from smartheat_gateway.zigbee import ZigbeeMirror

ABWEICHUNGEN: dict[str, str] = {}


@pytest.fixture
def welt(tmp_path, clock):
    paths = Paths(tmp_path / "data")
    host = gh.GatewayDeviceHost(paths, ZigbeeMirror(FakeBus(), clock), host_dir=tmp_path / "host", clock=clock)
    return Welt(host=host, clock=clock, reload_kind=EV_RELOAD, aufraeumen_pfad=paths.aufraeumen,
                konfiguration=konfiguration(), wunsch_herkunft="thermostat")


@pytest.mark.parametrize("pruefung", parameter(ABWEICHUNGEN))
def test_konformitaet(pruefung, welt):
    pruefung(welt)

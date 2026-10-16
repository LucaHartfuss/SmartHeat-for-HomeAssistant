"""V2-Konformitaet des HA-Hosts (testhilfen/host_konformitaet.py; Spec Stufe 3 S6 4.1)."""
from types import SimpleNamespace

import pytest
from conftest import FakeClock
from fakes import FakeHa
from geraet_helpers import konfiguration_aus_optionen
from host_konformitaet import Welt, parameter
from test_runtime import OPTIONS

from heizungsbruecke import config, geraet_host
from smartheat_device.geraet_host_basis import EV_RELOAD

ABWEICHUNGEN: dict[str, str] = {}


@pytest.fixture
def welt(tmp_path, monkeypatch):
    for name in ("RUNTIME_DIR", "ABGEMELDET_PATH", "AUFRAEUMEN_PATH", "ROOM_TARGET_PATH", "BACKUP_PATH",
                 "RASTUNG_PATH"):
        monkeypatch.setattr(config, name, tmp_path / getattr(config, name).name)
    clock = FakeClock()
    host = geraet_host.HaDeviceHost(FakeHa(), SimpleNamespace(host_info=lambda: {"dt_synchronized": True}),
                                    clock=clock)
    return Welt(host=host, clock=clock, reload_kind=EV_RELOAD, aufraeumen_pfad=config.AUFRAEUMEN_PATH,
                konfiguration=konfiguration_aus_optionen(OPTIONS), wunsch_herkunft="soll_entity")


@pytest.mark.parametrize("pruefung", parameter(ABWEICHUNGEN))
def test_konformitaet(pruefung, welt):
    pruefung(welt)

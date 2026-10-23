"""V2-Konformitaet des HA-Hosts (testhilfen/host_konformitaet.py; Spec Stufe 3 S6 4.1)."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fakes import FakeClock, FakeHa, ha_antwort
from geraet_helpers import konfiguration_aus_optionen
from host_konformitaet import Welt, parameter
from runtime_helpers import OPTIONS

from heizungsbruecke import config, geraet_host
from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.ha_signals import HaSignalSource
from smartheat_device.geraet_host_basis import EV_RELOAD

ABWEICHUNGEN: dict[str, str] = {}


def _fuehler_lesen(roh):
    """Der Fuehler meldet `roh` als HA-Zustand (Text) ueber die echte REST-Kette HomeAssistantApi -> HaSignalSource."""
    api = HomeAssistantApi(base_url="http://supervisor", token="test-token")
    with patch("heizungsbruecke.ha_api.requests.get", autospec=True, return_value=ha_antwort({"state": str(roh)})):
        return HaSignalSource(api).get_state("sensor.room_actual")


@pytest.fixture
def welt(tmp_path, monkeypatch):
    for name in ("RUNTIME_DIR", "ABGEMELDET_PATH", "AUFRAEUMEN_PATH", "ROOM_TARGET_PATH", "BACKUP_PATH",
                 "RASTUNG_PATH"):
        monkeypatch.setattr(config, name, tmp_path / getattr(config, name).name)
    clock = FakeClock()
    host = geraet_host.HaDeviceHost(FakeHa(), SimpleNamespace(host_info=lambda: {"dt_synchronized": True}),
                                    clock=clock)
    return Welt(host=host, clock=clock, reload_kind=EV_RELOAD, aufraeumen_pfad=config.AUFRAEUMEN_PATH,
                abgemeldet_pfad=config.ABGEMELDET_PATH, konfiguration=konfiguration_aus_optionen(OPTIONS),
                wunsch_herkunft="soll_entity", fuehler_lesen=_fuehler_lesen)


@pytest.mark.parametrize("pruefung", parameter(ABWEICHUNGEN))
def test_konformitaet(pruefung, welt):
    pruefung(welt)

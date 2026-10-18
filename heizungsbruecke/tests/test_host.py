"""HA-Host der Laufzeit (Plan SHG G1, Praezisierung 6) im Geraete-Pfad: Konfiguration aus dem Konfigurationsdokument."""
from unittest.mock import MagicMock

from fakes import FakeHa
from geraet_helpers import FakeGeraet, konfiguration_aus_optionen
from test_runtime import OPTIONS, _start_bridge, env  # noqa: F401  (Fixture)

from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.host import HaHost
from smartheat_runtime.app import IdleBridge


def test_boot_info_without_a_document_is_not_configured():
    boot = HaHost(FakeGeraet(None), MagicMock(spec=HomeAssistantApi)).boot_info()
    assert not boot.configured and not boot.signed_off


def test_sign_off_with_broken_configuration_restores_nothing_and_reports_why(env):
    bridge = _start_bridge(env, abgemeldet=True, verteilsystem=None)
    assert isinstance(bridge, IdleBridge) and bridge.reason == "abgemeldet"
    assert env.ha.writes == []
    assert bridge.status is not None and bridge.status.flags.grund.startswith("Konfiguration ungültig")


def test_sign_off_parts_are_none_for_a_broken_configuration():
    geraet = FakeGeraet(konfiguration_aus_optionen({**OPTIONS, "verteilsystem": None}))
    assert HaHost(geraet, FakeHa()).sign_off_parts() is None

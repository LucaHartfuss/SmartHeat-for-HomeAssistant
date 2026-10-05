"""HA-Host der Laufzeit (Plan SHG G1, Praezisierung 6)."""
from unittest.mock import MagicMock

from fakes import FakeHa
from test_runtime import OPTIONS, env  # noqa: F401  (Fixture)

from heizungsbruecke.__main__ import _start_bridge
from heizungsbruecke.host import HaHost
from smartheat_runtime.app import IdleBridge


def test_boot_info_of_unconfigured_options_is_not_configured():
    boot = HaHost({}, MagicMock()).boot_info()
    assert not boot.configured and not boot.signed_off


def test_sign_off_with_broken_configuration_restores_nothing_and_reports_why(env):
    broken = {**OPTIONS, "abgemeldet": True, "verteilsystem": None}
    bridge = _start_bridge(broken, env.ha, clock=env.clock)
    assert isinstance(bridge, IdleBridge) and bridge.reason == "abgemeldet"
    assert env.ha.writes == []
    assert bridge.status is not None and bridge.status.flags.grund.startswith("Konfiguration ungültig")


def test_sign_off_parts_are_none_for_a_broken_configuration():
    assert HaHost({**OPTIONS, "abgemeldet": True, "verteilsystem": None}, FakeHa()).sign_off_parts() is None

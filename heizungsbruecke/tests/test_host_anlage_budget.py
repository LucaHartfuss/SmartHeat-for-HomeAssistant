"""Der HA-Host gibt den Pfad der Anlagenzaehler in der BootInfo mit: neben den Kreisordnern unter `runtime/`."""
from unittest.mock import MagicMock

from geraet_helpers import FakeGeraet, konfiguration_aus_optionen
from runtime_helpers import OPTIONS

from heizungsbruecke import config
from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.host import HaHost


def test_die_boot_info_nennt_die_anlagendatei_neben_den_kreisordnern(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "runtime")
    host = HaHost(FakeGeraet(konfiguration_aus_optionen(OPTIONS)), MagicMock(spec=HomeAssistantApi))

    boot = host.boot_info()

    assert boot.anlage_budget_path == tmp_path / "runtime" / "anlage_budget.json"
    assert boot.backup_path.parent.parent == boot.anlage_budget_path.parent

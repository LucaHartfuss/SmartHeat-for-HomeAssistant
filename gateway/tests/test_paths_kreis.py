"""Zustandspfade des Gateways je Heizkreis: `runtime/kreis-<n>/...`."""
from pathlib import Path

from smartheat_core import zustandspfade
from smartheat_device import zustand_migration
from smartheat_gateway.paths import Paths


def test_der_zustand_liegt_je_kreis_in_seinem_ordner(tmp_path):
    paths = Paths(tmp_path)
    assert paths.kreis_dir(2) == tmp_path / "runtime" / "kreis-2"
    assert paths.backup(2) == tmp_path / "runtime" / "kreis-2" / "backup.json"
    assert paths.failsafe(1) == tmp_path / "runtime" / "kreis-1" / "failsafe_state.json"
    assert paths.entitlement(1) == tmp_path / "runtime" / "kreis-1" / "entitlement_state.json"
    assert paths.room_target(1) == tmp_path / "runtime" / "kreis-1" / "room_target.json"
    assert paths.backup(1) != paths.backup(2)


def test_was_die_anlage_betrifft_liegt_neben_den_kreisordnern(tmp_path):
    paths = Paths(Path(tmp_path))
    assert paths.abgemeldet == tmp_path / "runtime" / "abgemeldet.json"
    assert paths.quota_dir == tmp_path / "quota"


def test_die_zustandsmigration_trifft_den_alten_ort_und_den_kreisordner_des_gateways(tmp_path):
    paths = Paths(tmp_path)
    alt = zustand_migration.alte_pfade(tmp_path, "gateway")
    assert alt[zustandspfade.BACKUP] == paths.alter_backup
    assert all(pfad.parent == paths.runtime_dir for pfad in alt.values())
    assert zustand_migration.zielordner(tmp_path) == paths.kreis_dir(1) == paths.backup(1).parent
    assert paths.device_dir.parent == tmp_path

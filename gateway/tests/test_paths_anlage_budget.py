"""Die Anlagenzaehler des Gateways liegen neben den Kreisordnern: `runtime/anlage_budget.json`."""
from smartheat_gateway.paths import Paths


def test_die_anlagendatei_liegt_neben_den_kreisordnern(tmp_path):
    paths = Paths(tmp_path)
    assert paths.anlage_budget == tmp_path / "runtime" / "anlage_budget.json"
    assert paths.anlage_budget.parent == paths.kreis_dir(1).parent



def test_die_namen_des_hosts_gleichen_den_zustandspfaden_des_geraetekerns(tmp_path):
    """`paths.py` steht ohne den Geraetekern im Host-Paket und fuehrt die Namen darum selbst."""
    from smartheat_core import zustandspfade
    paths = Paths(tmp_path)
    assert paths.anlage_budget.name == zustandspfade.ANLAGE_BUDGET
    assert paths.backup(2) == zustandspfade.kreis_dir(paths.runtime_dir, 2) / zustandspfade.BACKUP
    assert paths.failsafe(2).name == zustandspfade.FAILSAFE
    assert paths.entitlement(2).name == zustandspfade.ENTITLEMENT
    assert paths.room_target(2).name == zustandspfade.ROOM_TARGET


def test_eine_unmoegliche_kreisnummer_wirft(tmp_path):
    import pytest
    for kreis in (0, -1, 10000, True, "1"):
        with pytest.raises(ValueError):
            Paths(tmp_path).kreis_dir(kreis)

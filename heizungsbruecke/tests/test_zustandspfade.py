"""Zustandspfade je Heizkreis: `runtime/kreis-<n>/...` fuer beide Hosts, die alten Pfade gibt es nicht mehr."""
from pathlib import Path

import pytest

from heizungsbruecke import config
from smartheat_core import zustandspfade


def test_der_zustand_eines_kreises_liegt_in_seinem_ordner():
    runtime = Path("/r")
    assert zustandspfade.kreis_dir(runtime, 1) == Path("/r/kreis-1")
    assert zustandspfade.kreis_dir(runtime, 12) == Path("/r/kreis-12")


@pytest.mark.parametrize("kreis", [0, -1, 10000, True, "1", 1.0, None])
def test_eine_unbrauchbare_kreisnummer_scheitert(kreis):
    with pytest.raises(ValueError):
        zustandspfade.kreis_dir(Path("/r"), kreis)


def test_kreisordner_findet_nur_echte_ordner_in_der_reihenfolge_der_nummer(tmp_path):
    for name in ("kreis-10", "kreis-2", "kreis-1"):
        (tmp_path / name).mkdir()
    (tmp_path / "kreis-3").write_text("datei")
    (tmp_path / "kreis-04").mkdir()
    (tmp_path / "kreis-x").mkdir()
    (tmp_path / "abgemeldet.json").write_text("{}")
    (tmp_path / "kreis-5").symlink_to(tmp_path / "kreis-1")
    assert [p.name for p in zustandspfade.kreisordner(tmp_path)] == ["kreis-1", "kreis-2", "kreis-10"]
    assert zustandspfade.kreisordner(tmp_path / "fehlt") == []


def test_die_pfade_des_add_ons_haengen_am_kreis(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "runtime")
    assert config.backup_path(2) == tmp_path / "runtime" / "kreis-2" / "backup.json"
    assert config.failsafe_path(2) == tmp_path / "runtime" / "kreis-2" / "failsafe_state.json"
    assert config.entitlement_path(1) == tmp_path / "runtime" / "kreis-1" / "entitlement_state.json"
    assert config.room_target_path(1) == tmp_path / "runtime" / "kreis-1" / "room_target.json"
    assert config.backup_path(1) != config.backup_path(2)


def test_die_alten_pfade_des_add_ons_gibt_es_nicht_mehr():
    for name in ("BACKUP_PATH", "FAILSAFE_PATH", "ENTITLEMENT_PATH", "ROOM_TARGET_PATH"):
        assert not hasattr(config, name), name


def _zustand_anlegen(runtime):
    for kreis in (1, 2):
        ordner = runtime / f"kreis-{kreis}"
        ordner.mkdir(parents=True)
        for name in zustandspfade.DATEIEN:
            (ordner / name).write_text("{}")
    (runtime / "abgemeldet.json").write_text("{}")
    (runtime / "anlage_budget.json").write_text("{}")
    (runtime / "unterordner").mkdir()
    (runtime / "unterordner" / "x.json").write_text("{}")


def test_das_zuruecksetzen_nimmt_alles_ausser_den_wiederherstellungspunkten(tmp_path):
    _zustand_anlegen(tmp_path)

    namen = {str(p.relative_to(tmp_path)) for p in zustandspfade.ohne_wiederherstellungspunkt(tmp_path)}

    assert namen == {"abgemeldet.json", "anlage_budget.json", "unterordner",
                     "kreis-1/failsafe_state.json", "kreis-1/entitlement_state.json", "kreis-1/room_target.json",
                     "kreis-2/failsafe_state.json", "kreis-2/entitlement_state.json", "kreis-2/room_target.json"}


def test_die_wiederherstellungspunkte_sind_die_backups_der_kreisordner(tmp_path):
    _zustand_anlegen(tmp_path)
    (tmp_path / "kreis-2" / "backup.json").unlink()

    assert zustandspfade.wiederherstellungspunkte(tmp_path) == [tmp_path / "kreis-1" / "backup.json"]


def test_ohne_runtime_ordner_gibt_es_nichts_zu_loeschen(tmp_path):
    assert zustandspfade.ohne_wiederherstellungspunkt(tmp_path / "fehlt") == []
    assert zustandspfade.wiederherstellungspunkte(tmp_path / "fehlt") == []

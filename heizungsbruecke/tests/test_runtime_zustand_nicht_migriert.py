"""Liegt der Zustand noch im Format vor der Kreisordnung (backup.json im alten Pfad), startet die Laufzeit nicht: sie
meldet einen Konfigurationsfehler und schreibt nichts in den neuen Kreisordner; die Migration holt den Zustand
nach, danach startet sie."""
import json
from types import SimpleNamespace

from runtime_helpers import _is_running, _start_bridge

from smartheat_runtime import app
from smartheat_runtime.budget_store import BudgetStore
from smartheat_runtime.state import StateStore


def test_ein_unmigrierter_zustand_verhindert_den_start(env):
    env.paths["ALTER_BACKUP_PATH"].write_text(json.dumps({"plant_id": "alt"}))

    bridge = _start_bridge(env)

    assert not _is_running(bridge)
    assert bridge.reason == "konfigurationsfehler"
    assert not (env.paths["RUNTIME_DIR"] / "kreis-1").exists()
    assert env.paths["ALTER_BACKUP_PATH"].exists()


def test_ein_leerer_kreisordner_gilt_nicht_als_migriert(env):
    """Eine zurueckgenommene Migration kann den Ordner zurueckgelassen haben: die Sicherung selbst fehlt noch."""
    kreis = env.paths["RUNTIME_DIR"] / "kreis-1"
    kreis.mkdir(parents=True)
    env.paths["ALTER_BACKUP_PATH"].write_text(json.dumps({"plant_id": "alt"}))

    bridge = _start_bridge(env)

    assert not _is_running(bridge)
    assert bridge.status.flags.grund == app.GRUND_ZUSTAND_NICHT_MIGRIERT
    assert list(kreis.iterdir()) == []  # die Fehlerroute legt nichts an


def test_ein_unvollstaendiger_kreisordner_gilt_nicht_als_migriert(env):
    kreis = env.paths["RUNTIME_DIR"] / "kreis-1"
    kreis.mkdir(parents=True)
    (kreis / "failsafe_state.json").write_text("{}")
    env.paths["ALTER_BACKUP_PATH"].write_text(json.dumps({"plant_id": "alt"}))

    assert not _is_running(_start_bridge(env))
    assert not (kreis / "backup.json").exists()


def test_ein_migrierter_kreisordner_mit_sicherung_startet(env):
    kreis = env.paths["RUNTIME_DIR"] / "kreis-1"
    kreis.mkdir(parents=True)
    (kreis / "backup.json").write_text("{}")
    env.paths["ALTER_BACKUP_PATH"].write_text("{}")  # Rest der Migration, der Betreiber raeumt auf

    assert _is_running(_start_bridge(env))


def test_der_grund_nennt_den_code_und_den_weg(env):
    env.paths["ALTER_BACKUP_PATH"].write_text("{}")

    bridge = _start_bridge(env)

    assert bridge.status.flags.grund == app.GRUND_ZUSTAND_NICHT_MIGRIERT
    assert bridge.status.flags.konfigurationsfehler


def test_ohne_alten_zustand_startet_die_laufzeit(env):
    assert _is_running(_start_bridge(env))


def test_nach_der_migration_startet_die_laufzeit(env):
    env.paths["ALTER_BACKUP_PATH"].write_text("{}")
    assert not _is_running(_start_bridge(env))

    env.paths["ALTER_BACKUP_PATH"].rename(env.paths["ALTER_BACKUP_PATH"].with_name("backup.json.pre-0.9.0"))

    assert _is_running(_start_bridge(env))


def test_der_zustandsspeicher_fuehrt_die_zaehler_der_anlage_nur_mit_anlagendatei(tmp_path):
    boot = SimpleNamespace(backup_path=tmp_path / "kreis-1" / "backup.json",
                           failsafe_path=tmp_path / "kreis-1" / "failsafe_state.json", anlage_budget_path=None)

    assert type(app._zustandsspeicher(boot)) is StateStore

    boot.anlage_budget_path = tmp_path / "anlage_budget.json"
    assert type(app._zustandsspeicher(boot)) is BudgetStore

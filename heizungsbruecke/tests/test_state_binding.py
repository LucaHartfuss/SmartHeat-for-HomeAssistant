"""Audit 4, A4-08 (GK-3, GW-2), Nutzer-Entscheidung E3."""
import pytest

from smartheat_runtime.backup_store import load_backup, save_backup
from smartheat_runtime.delivery import DeliveryState
from smartheat_runtime.state import StateStore, StorageError, bind_to_setup

OLD = {
    "restore_point": {"curve": 1.4, "room_setpoint": 22.0, "heat_limit": 18.0}, "originals": {"heat_limit": 16.0},
    "last_published_target_rt": 21.0, "last_daily_trigger_date": "2026-10-07", "lifetime_writes": 7,
}


def _store(tmp_path, backup):
    save_backup(tmp_path / "backup.json", backup)
    return StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json")


def test_existing_state_without_binding_is_kept(tmp_path):
    # Review Focus 1: Update von client1
    store = _store(tmp_path, OLD)
    assert bind_to_setup(store, "s1", "anlage-a") == "bestand"
    assert store.state.restore_point == OLD["restore_point"] and store.state.last_published_target_rt == 21.0
    assert load_backup(tmp_path / "backup.json")["plant_id"] == "anlage-a"


def test_a_new_setup_of_the_same_plant_keeps_the_originals_and_forces_a_first_tick(tmp_path):
    # Review Focus 2
    store = _store(tmp_path, {**OLD, "setup_id": "s1", "plant_id": "anlage-a"})
    assert bind_to_setup(store, "s2", "anlage-a") == "neue_einrichtung"
    assert store.state.originals == {"heat_limit": 16.0} and store.state.restore_point == OLD["restore_point"]
    assert store.state.last_published_target_rt is None and store.state.last_daily_trigger_date is None


def test_another_plant_discards_the_plant_bound_state(tmp_path):
    store = _store(tmp_path, {**OLD, "setup_id": "s1", "plant_id": "anlage-a", "boost_active": True})
    assert bind_to_setup(store, "s2", "anlage-b") == "andere_anlage"
    state = store.state
    assert (state.restore_point, state.originals, state.lifetime_writes, state.boost_active) == ({}, {}, 0, False)
    assert state.last_published_target_rt is None


def test_the_same_setup_changes_nothing(tmp_path):
    store = _store(tmp_path, {**OLD, "setup_id": "s1", "plant_id": "anlage-a"})
    assert bind_to_setup(store, "s1", "anlage-a") == "unveraendert"
    assert store.state.last_published_target_rt == 21.0


@pytest.mark.parametrize("plant", ["anlage-a", "anlage-b"], ids=["neue_einrichtung", "andere_anlage"])
def test_the_delivery_state_is_reset_even_if_saving_the_binding_fails(tmp_path, monkeypatch, plant):
    # app.py faengt den StorageError ab und startet weiter: die Zustellung darf dann nicht mehr vom alten Setup stammen
    store = _store(tmp_path, {**OLD, "setup_id": "s1", "plant_id": "anlage-a"})
    store.set_delivery(DeliveryState(server_failures=2, notbetrieb=True))

    def failing_update(**changes):
        raise StorageError("Datentraeger voll")

    monkeypatch.setattr(store, "update", failing_update)
    with pytest.raises(StorageError):
        bind_to_setup(store, "s2", plant)
    assert store.state.delivery == DeliveryState()

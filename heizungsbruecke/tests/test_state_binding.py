"""Audit 4, A4-08 (GK-3, GW-2), Nutzer-Entscheidung E3; seit Stufe 3 S7 (A5-63) bindet tenant_id statt setup_id."""
import json

import pytest

from smartheat_runtime.backup_store import load_backup, save_backup
from smartheat_runtime.delivery import DataFault, DeliveryState, PendingTick
from smartheat_runtime.state import StateStore, StorageError, bind_to_tenant

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
    assert bind_to_tenant(store, "s1", "anlage-a") == "bestand"
    assert store.state.restore_point == OLD["restore_point"] and store.state.last_published_target_rt == 21.0
    assert load_backup(tmp_path / "backup.json")["plant_id"] == "anlage-a"


def test_a_new_setup_of_the_same_plant_keeps_the_originals_and_forces_a_first_tick(tmp_path):
    # Review Focus 2
    store = _store(tmp_path, {**OLD, "tenant_id": "s1", "plant_id": "anlage-a"})
    assert bind_to_tenant(store, "s2", "anlage-a") == "neue_einrichtung"
    assert store.state.originals == {"heat_limit": 16.0} and store.state.restore_point == OLD["restore_point"]
    assert store.state.last_published_target_rt is None and store.state.last_daily_trigger_date is None


def test_another_plant_discards_the_plant_bound_state(tmp_path):
    store = _store(tmp_path, {**OLD, "tenant_id": "s1", "plant_id": "anlage-a", "boost_active": True})
    assert bind_to_tenant(store, "s2", "anlage-b") == "andere_anlage"
    state = store.state
    assert (state.restore_point, state.originals, state.lifetime_writes, state.boost_active) == ({}, {}, 0, False)
    assert state.last_published_target_rt is None


def test_the_same_setup_changes_nothing(tmp_path):
    store = _store(tmp_path, {**OLD, "tenant_id": "s1", "plant_id": "anlage-a"})
    assert bind_to_tenant(store, "s1", "anlage-a") == "unveraendert"
    assert store.state.last_published_target_rt == 21.0


@pytest.mark.parametrize("plant", ["anlage-a", "anlage-b"], ids=["neue_einrichtung", "andere_anlage"])
def test_the_delivery_state_is_reset_even_if_saving_the_binding_fails(tmp_path, monkeypatch, plant):
    # app.py faengt den StorageError ab und startet weiter: die Zustellung darf dann nicht mehr vom alten Setup stammen
    store = _store(tmp_path, {**OLD, "tenant_id": "s1", "plant_id": "anlage-a"})
    store.set_delivery(DeliveryState(server_failures=2, notbetrieb=True))

    def failing_update(**changes):
        raise StorageError("Datentraeger voll")

    monkeypatch.setattr(store, "update", failing_update)
    with pytest.raises(StorageError):
        bind_to_tenant(store, "s2", plant)
    assert store.state.delivery == DeliveryState()


def test_ein_alter_zustand_mit_setup_id_laedt_als_tenant_id_ohne_neue_einrichtung(tmp_path):
    # Review Focus 4: bis Stufe 3 S7 hiess das Feld setup_id (immer = tenant_id); Ursprungswerte und Zustellung bleiben
    alt = StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json")
    save_backup(tmp_path / "backup.json",
                {**OLD, "setup_id": "s1", "plant_id": "anlage-a", "restore_point": {"curve": 1.0}})
    delivery = DeliveryState(notbetrieb=True, pending=PendingTick(seq="s-1", trigger="daily"),
                             datenfehler=DataFault(source="local", detail=("ha",)))  # nur die persistierten Felder
    alt.set_delivery(delivery)  # schreibt failsafe_state.json
    store = StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json")  # Neustart: Laden vom Datentraeger
    assert store.state.tenant_id == "s1"
    assert store.state.delivery == delivery and store.state.originals == OLD["originals"]
    assert bind_to_tenant(store, "s1", "anlage-a") == "unveraendert"
    assert store.state.restore_point == {"curve": 1.0}
    store.update(last_ack_at="2026-10-08T12:00:00+02:00")  # eine Aenderung schreibt backup.json neu
    raw = json.loads((tmp_path / "backup.json").read_text())
    assert "setup_id" not in raw and raw["tenant_id"] == "s1"
    assert raw["restore_point"] == {"curve": 1.0} and raw["originals"] == OLD["originals"]
    assert store.state.delivery == delivery and store.state.originals == OLD["originals"]
    neu = StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json")  # und nach dem Schreiben erneut geladen
    assert neu.state.delivery == delivery and neu.state.originals == OLD["originals"]


def test_bei_setup_id_und_tenant_id_gewinnt_die_tenant_id(tmp_path):
    store = _store(tmp_path, {**OLD, "setup_id": "alt", "tenant_id": "s1", "plant_id": "anlage-a"})
    assert store.state.tenant_id == "s1"
    store.update(last_ack_at="2026-10-08T12:00:00+02:00")
    raw = json.loads((tmp_path / "backup.json").read_text())
    assert "setup_id" not in raw and raw["tenant_id"] == "s1"


def test_eine_setup_id_mit_falschem_typ_gilt_wie_ein_anderes_textfeld_als_ungueltig(tmp_path):
    store = _store(tmp_path, {**OLD, "setup_id": 5, "plant_id": "anlage-a"})
    assert store.state.tenant_id is None and store.state.plant_id == "anlage-a"
    assert store.state.restore_point == OLD["restore_point"]
    store.update(last_ack_at="2026-10-08T12:00:00+02:00")
    raw = json.loads((tmp_path / "backup.json").read_text())
    assert "setup_id" not in raw and raw.get("tenant_id") is None


def test_ein_bestand_mit_anlage_aber_ohne_einrichtung_ist_eine_neue_einrichtung(tmp_path):
    # Rueckwegfall: tenant_id fehlt, plant_id ist gesetzt -> nicht "bestand" (sonst bliebe der Erstkontakt-Tick aus)
    store = _store(tmp_path, {**OLD, "plant_id": "anlage-a"})
    assert store.state.tenant_id is None and store.state.plant_id == "anlage-a"
    assert bind_to_tenant(store, "s2", "anlage-a") == "neue_einrichtung"
    assert store.state.tenant_id == "s2" and store.state.originals == {"heat_limit": 16.0}
    assert store.state.last_published_target_rt is None and store.state.last_daily_trigger_date is None

"""Persistenz Plan 3b: neue backup.json-Felder mit Standardwerten, ohne Migration (Review Focus: backup.json von 0.30.0
laedt ohne Wertverlust und bleibt beim Schreiben gleich)."""
import logging

import pytest

from heizungsbruecke.backup_store import load_backup
from heizungsbruecke.state import BridgeState

# backup.json, wie Add-on 0.30.0 sie fuer client1 schreibt (alle Feldarten, ein unbekannter Schluessel).
V030_BACKUP = {
    "restore_point": {"curve": 1.0, "room_setpoint": 17.5, "heat_limit": 16.0},
    "originals": {"heat_limit": 15.0},
    "boost_active": False, "emergency_boost_active": False,
    "last_room_target": 20.5, "last_published_target_rt": 20.5, "last_daily_trigger_date": "2026-10-02",
    "last_ack_at": "2026-10-02T12:00:05+02:00",
    "notify_states": {"manueller_eingriff": "curve=1.2"}, "notify_messages": {"notbetrieb": "x"},
    "manual_override": {"levers": {"curve": 1.2, "room_setpoint": 17.5, "heat_limit": 16.0}, "erkannt": "2026-10-02T09:00:00+02:00",
                        "rollen": {"curve": 1.2}, "signatur": "curve=1.2", "gemeldet": "curve=1.2"},
    "write_budget": {"enforce:curve": {"day": "2026-10-02", "count": 1}},
    "kommt_spaeter": {"a": 1},
}


def test_a_0_30_0_backup_loads_with_defaults_for_the_new_fields(make_store):
    store = make_store(backup=V030_BACKUP)
    state = store.state
    assert (state.lifetime_writes, state.deferred_levers, state.aux_originals, state.energy_state) == (0, (), {}, {})
    assert state.restore_point == V030_BACKUP["restore_point"]
    assert state.write_budget == V030_BACKUP["write_budget"]


def test_a_0_30_0_backup_is_written_back_unchanged(make_store, tmp_path):
    store = make_store(backup=V030_BACKUP)
    store.update(boost_active=True)
    store.update(boost_active=False)
    assert load_backup(tmp_path / "backup.json") == V030_BACKUP


def test_new_fields_round_trip(make_store, tmp_path):
    store = make_store()
    store.update(
        lifetime_writes=12, deferred_levers=("curve", "heat_limit"),
        aux_originals={"mode_select": "Automatik", "setpoint_comfort": 22.0},
        energy_state={"thermal_heating": {"raw": 4.0, "sum": 10.0}},
    )
    backup = load_backup(tmp_path / "backup.json")
    assert backup["lifetime_writes"] == 12 and backup["deferred_levers"] == ["curve", "heat_limit"]
    reloaded = make_store()
    assert reloaded.state.lifetime_writes == 12
    assert reloaded.state.deferred_levers == ("curve", "heat_limit")
    assert reloaded.state.aux_originals == {"mode_select": "Automatik", "setpoint_comfort": 22.0}
    assert reloaded.state.energy_state == {"thermal_heating": {"raw": 4.0, "sum": 10.0}}


@pytest.mark.parametrize(("key", "value"), [
    ("lifetime_writes", -1), ("lifetime_writes", True), ("lifetime_writes", 1.5),
    ("deferred_levers", "curve"), ("deferred_levers", [1]),
    ("aux_originals", {"mode_select": None}), ("aux_originals", ["Normal"]),
    ("energy_state", {"x": {"raw": 1.0}}), ("energy_state", {"x": {"raw": 1.0, "sum": float("nan")}}),
])
def test_invalid_new_fields_fall_back_to_their_default(make_store, caplog, key, value):
    with caplog.at_level(logging.WARNING):
        store = make_store(backup={**V030_BACKUP, key: value})
    assert getattr(store.state, key) == getattr(BridgeState(), key)
    assert key in caplog.text
    assert store.state.restore_point == V030_BACKUP["restore_point"]

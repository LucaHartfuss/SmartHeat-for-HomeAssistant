"""Lokaler Check und "Tick faellig?" (regulation.py) mit echtem StateStore und Override."""
import logging
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from heizungsbruecke import regulation
from heizungsbruecke.backup_store import load_backup
from heizungsbruecke.manifest import ChannelManifest
from heizungsbruecke.override import Override

# Unterscheidbar: Notfall (= Clamp-Maximum) 0.8/5.0, Comfort 0.5/2.0, Wiederherstellungspunkt 0.3/1.0.
OPTIONS = {
    "curve_min": 0.2, "curve_max": 0.8, "offset_min": 0.0, "offset_max": 5.0,
    "boost_threshold_k": 0.5, "boost_curve_value": 0.5, "boost_offset_value": 2.0,
    "daily_trigger_time": "12:00",
}
ROOM_ROLES = {"room_actual": "sensor.room_actual", "room_target": "sensor.room_target"}
ENTITY_IDS = {**ROOM_ROLES, "curve_current": "number.curve", "offset_current": "number.offset"}
RESTORE_POINT = {"curve_current": 0.3, "offset_current": 1.0}
EMERGENCY = [("number.curve", 0.8), ("number.offset", 5.0)]
COMFORT = [("number.curve", 0.5), ("number.offset", 2.0)]
RESTORE = [("number.curve", 0.3), ("number.offset", 1.0)]


def _runtime(store, *, room_actual=20.0, room_target=21.0, entity_ids=ENTITY_IDS, states=None):
    """`room_target` ist der Stable-Target-Cache (None = nicht befuellt); `states` ergaenzt die
    HA-Werte (eine Exception als Wert wird beim Lesen geworfen)."""
    values = {"sensor.room_actual": room_actual, **(states or {})}
    ha_api = MagicMock()

    def _get_state(entity_id):
        value = values[entity_id]
        if isinstance(value, Exception):
            raise value
        return value

    ha_api.get_state.side_effect = _get_state
    manifest = ChannelManifest(entity_ids=entity_ids)
    options = dict(OPTIONS)
    if room_target is not None:
        store.update(stable_target=room_target)
    return SimpleNamespace(
        manifest=manifest, ha_api=ha_api, options=options, store=store, states=values,
        override=Override(store, manifest, ha_api, options),
    )


def _writes(rt):
    return [call.args for call in rt.ha_api.set_number_value.call_args_list]


# --- run_local_check ---

def test_check_reads_only_room_actual_and_uses_cached_target(make_store):
    rt = _runtime(make_store(), room_actual=19.0)

    regulation.run_local_check(rt)

    assert [call.args[0] for call in rt.ha_api.get_state.call_args_list] == ["sensor.room_actual"]
    assert rt.store.state.boost_active is False  # ohne Vorwert keine "Erhoehung"
    assert _writes(rt) == []


def test_check_without_cached_target_is_skipped_visibly(make_store, caplog):
    rt = _runtime(make_store(), room_target=None)

    with caplog.at_level(logging.WARNING):
        regulation.run_local_check(rt)

    rt.ha_api.get_state.assert_not_called()
    assert "room_target=None" in caplog.text


def test_check_without_room_roles_does_nothing(make_store):
    rt = _runtime(make_store(), entity_ids={"curve_current": "number.curve"})

    regulation.run_local_check(rt)

    rt.ha_api.get_state.assert_not_called()


def test_check_propagates_read_errors(make_store):
    rt = _runtime(make_store(), room_actual=RuntimeError("HA nicht erreichbar"))

    with pytest.raises(RuntimeError):
        regulation.run_local_check(rt)


def test_check_records_target_and_history(make_store, tmp_path, monkeypatch):
    monkeypatch.setattr("heizungsbruecke.regulation.time.time", lambda: 1_000_000.0)
    rt = _runtime(make_store(), entity_ids=ROOM_ROLES)

    regulation.run_local_check(rt)

    backup = load_backup(tmp_path / "backup.json")
    assert backup["last_room_target"] == 21.0
    assert backup["target_history"] == [[1_000_000.0, 21.0]]

    monkeypatch.setattr("heizungsbruecke.regulation.time.time", lambda: 1_003_600.0)
    rt.store.update(stable_target=22.0)
    regulation.run_local_check(rt)

    assert load_backup(tmp_path / "backup.json")["target_history"] == [[1_000_000.0, 21.0], [1_003_600.0, 22.0]]


def test_target_raise_starts_comfort_boost(make_store, tmp_path):
    rt = _runtime(make_store(backup={"last_room_target": 20.0, **RESTORE_POINT}), room_actual=19.0)

    regulation.run_local_check(rt)

    assert _writes(rt) == COMFORT
    assert load_backup(tmp_path / "backup.json")["boost_active"] is True


def test_comfort_boost_ends_on_arrival_and_restores(make_store, tmp_path):
    rt = _runtime(
        make_store(backup={"last_room_target": 21.0, "boost_active": True, **RESTORE_POINT}), room_actual=20.8,
    )

    regulation.run_local_check(rt)

    assert _writes(rt) == RESTORE
    assert load_backup(tmp_path / "backup.json")["boost_active"] is False


def test_steady_state_check_does_not_write_backup(make_store, monkeypatch):
    # SD-Karte: ein Check ohne Aenderung schreibt nichts.
    store = make_store(backup={
        "last_room_target": 20.0, "boost_active": False, "last_published_target_rt": 20.0,
        "target_history": [[0, 20.0]],
    })
    rt = _runtime(store, room_actual=20.0, room_target=20.0)
    saves = []
    monkeypatch.setattr("heizungsbruecke.backup_store.save_backup", lambda path, values: saves.append(path))

    regulation.run_local_check(rt)

    assert saves == []


@pytest.mark.parametrize("notbetrieb,expected", [(True, EMERGENCY), (False, [])])
def test_emergency_boost_starts_only_during_notbetrieb(make_store, notbetrieb, expected):
    store = make_store(backup=dict(RESTORE_POINT), failsafe={"failsafe_active": notbetrieb})
    rt = _runtime(store, room_actual=18.5, room_target=20.0)

    regulation.run_local_check(rt)

    assert _writes(rt) == expected
    assert store.state.emergency_boost_active is notbetrieb


def test_emergency_boost_ends_once_notbetrieb_is_over(make_store):
    store = make_store(backup={"emergency_boost_active": True, **RESTORE_POINT})
    rt = _runtime(store, room_actual=18.5, room_target=20.0)

    regulation.run_local_check(rt)

    assert _writes(rt) == RESTORE
    assert store.state.emergency_boost_active is False


def test_comfort_start_during_emergency_boost_does_not_write(make_store):
    store = make_store(
        backup={"emergency_boost_active": True, "last_room_target": 20.0, **RESTORE_POINT},
        failsafe={"failsafe_active": True},
    )
    rt = _runtime(store, room_actual=19.0, room_target=21.0)

    regulation.run_local_check(rt)

    assert _writes(rt) == []
    assert (store.state.boost_active, store.state.emergency_boost_active) == (True, True)


def test_emergency_end_hands_device_to_running_comfort_boost(make_store):
    store = make_store(backup={
        "emergency_boost_active": True, "boost_active": True, "last_room_target": 21.0, **RESTORE_POINT,
    })
    rt = _runtime(store, room_actual=19.0, room_target=21.0)

    regulation.run_local_check(rt)

    assert _writes(rt) == COMFORT
    assert (store.state.boost_active, store.state.emergency_boost_active) == (True, False)


def test_emergency_end_without_comfort_boost_restores(make_store):
    store = make_store(backup={"emergency_boost_active": True, "last_room_target": 21.0, **RESTORE_POINT})
    rt = _runtime(store, room_actual=19.0, room_target=21.0)

    regulation.run_local_check(rt)

    assert _writes(rt) == RESTORE


def test_refused_comfort_start_keeps_target_rise_pending(make_store, tmp_path):
    # B5: ohne Wiederherstellungspunkt bleibt der alte Sollwert gemerkt, der naechste Check
    # sieht die Erhoehung erneut.
    store = make_store(backup={"last_room_target": 20.0})
    rt = _runtime(
        store, room_actual=19.0, room_target=21.0,
        states={"number.curve": RuntimeError("Cloud nicht erreichbar"), "number.offset": 22.0},
    )

    regulation.run_local_check(rt)

    assert _writes(rt) == []
    assert load_backup(tmp_path / "backup.json")["last_room_target"] == 20.0

    rt.states["number.curve"] = 0.9
    regulation.run_local_check(rt)

    assert _writes(rt) == COMFORT
    assert load_backup(tmp_path / "backup.json")["last_room_target"] == 21.0


def test_check_after_abo_finished_writes_nothing(make_store, tmp_path):
    store = make_store(backup=dict(RESTORE_POINT), failsafe={"failsafe_active": True})
    rt = _runtime(store, room_actual=18.0, room_target=21.0)
    store.update(abo_finished=True)
    before = load_backup(tmp_path / "backup.json")

    regulation.run_local_check(rt)

    assert _writes(rt) == []
    assert load_backup(tmp_path / "backup.json") == before


def test_check_in_abo_inactive_mode_runs_emergency_boost(make_store):
    store = make_store(backup=dict(RESTORE_POINT), failsafe={"failsafe_active": True})
    store.update(abo_inactive_since=datetime(2026, 9, 25).astimezone())
    rt = _runtime(store, room_actual=18.0, room_target=21.0)

    regulation.run_local_check(rt)

    assert _writes(rt) == EMERGENCY


# --- claim_due_tick ---

def test_claim_due_tick_claims_target_change_and_books_it(make_store, tmp_path):
    rt = _runtime(make_store(backup={"last_published_target_rt": 21.0}), room_target=20.5)

    assert regulation.claim_due_tick(rt, datetime(2026, 9, 17, 9, 0)) == "target_change"
    assert load_backup(tmp_path / "backup.json")["last_published_target_rt"] == 20.5


def test_claim_due_tick_returns_none_and_writes_nothing_when_nothing_is_due(make_store, tmp_path):
    rt = _runtime(make_store(backup={"last_published_target_rt": 21.0}), room_target=21.0)

    assert regulation.claim_due_tick(rt, datetime(2026, 9, 17, 9, 0)) is None
    assert load_backup(tmp_path / "backup.json") == {"last_published_target_rt": 21.0}


def test_claim_due_tick_claims_daily_once_per_day(make_store, tmp_path):
    rt = _runtime(make_store(backup={"last_published_target_rt": 21.0}), room_target=21.0)

    assert regulation.claim_due_tick(rt, datetime(2026, 9, 17, 12, 5)) == "daily"
    assert load_backup(tmp_path / "backup.json")["last_daily_trigger_date"] == "2026-09-17"
    assert regulation.claim_due_tick(rt, datetime(2026, 9, 17, 15, 0)) is None


def test_claim_due_tick_prefers_target_change_but_also_books_daily(make_store, tmp_path):
    rt = _runtime(make_store(backup={"last_published_target_rt": 21.0}), room_target=22.0)

    assert regulation.claim_due_tick(rt, datetime(2026, 9, 17, 12, 5)) == "target_change"
    assert load_backup(tmp_path / "backup.json")["last_daily_trigger_date"] == "2026-09-17"


def test_claim_due_tick_is_claimed_again_after_a_failed_booking(make_store, tmp_path, monkeypatch):
    # Wie 0.16.0: ohne gespeicherte Buchung kein Tick, der naechste Check beansprucht ihn erneut.
    rt = _runtime(make_store(backup={"last_published_target_rt": 21.0}), room_target=20.5)

    def _broken_save(path, values):
        raise OSError("SD-Karte kaputt")

    monkeypatch.setattr("heizungsbruecke.backup_store.save_backup", _broken_save)
    with pytest.raises(OSError):
        regulation.claim_due_tick(rt, datetime(2026, 9, 17, 12, 5))
    assert (rt.store.state.last_published_target_rt, rt.store.state.last_daily_trigger_date) == (21.0, None)

    monkeypatch.undo()
    assert regulation.claim_due_tick(rt, datetime(2026, 9, 17, 12, 5)) == "target_change"
    backup = load_backup(tmp_path / "backup.json")
    assert (backup["last_published_target_rt"], backup["last_daily_trigger_date"]) == (20.5, "2026-09-17")


@pytest.mark.parametrize("abo_inactive,room_target", [(True, 20.5), (False, None)])
def test_claim_due_tick_needs_cached_target_and_active_abo(make_store, abo_inactive, room_target):
    store = make_store(backup={"last_published_target_rt": 21.0})
    rt = _runtime(store, room_target=room_target)
    if abo_inactive:
        store.update(abo_inactive_since=datetime(2026, 9, 25).astimezone())

    assert regulation.claim_due_tick(rt, datetime(2026, 9, 17, 12, 5)) is None

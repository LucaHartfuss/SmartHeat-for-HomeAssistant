"""Audit 4, A4-09 (GK-1), Nutzer-Entscheidung E2: Comfort-Boost hoechstens 4 h, Ende nach 3 unlesbaren Raumwerten."""
import contextlib
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fakes import runtime_config

from heizungsbruecke.ha_binding import HaPlantBinding
from smartheat_core import wallclock
from smartheat_core.pipeline import LeverPipeline
from smartheat_core.safety import resolve_local_safety
from smartheat_runtime import regulation
from smartheat_runtime.backup_store import save_backup
from smartheat_runtime.roles import ChannelManifest
from smartheat_runtime.state import StateStore, StorageError

SAFETY = resolve_local_safety("vaillant_vrc720", "Heizkoerper")
REFS = {"room_actual": "sensor.room_actual", "room_target": "sensor.room_target",
        "curve_current": "number.curve", "shift_current": "number.shift", "heat_limit": "number.heat_limit"}
START = datetime(2026, 10, 8, 6, 0).astimezone()


def _rt(tmp_path, values, backup=None):
    save_backup(tmp_path / "backup.json", backup or {
        "last_room_target": 20.0, "restore_point": {"curve": 1.0, "room_setpoint": 20.0, "heat_limit": 16.0}})
    store = StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json")
    ha_api = MagicMock()
    ha_api.get_attribute.side_effect = ValueError("kein Wertebereich")

    def _get_state(entity_id):
        value = values[entity_id]
        if isinstance(value, Exception):
            raise value
        return value

    ha_api.get_state.side_effect = _get_state
    manifest = ChannelManifest(refs=REFS)
    rt = SimpleNamespace(manifest=manifest, signals=ha_api, ha_api=ha_api, config=runtime_config(), store=store,
                         override=LeverPipeline(store, HaPlantBinding(ha_api, manifest), SAFETY))
    store.update(stable_target=21.0)
    return rt


@pytest.fixture
def clock(monkeypatch):
    now = {"t": START}
    monkeypatch.setattr(wallclock, "_now", lambda: now["t"])
    return now


def test_comfort_boost_ends_after_four_hours(tmp_path, clock):
    values = {"sensor.room_actual": 19.0, "number.curve": 1.0, "number.shift": 20.0, "number.heat_limit": 16.0}
    rt = _rt(tmp_path, values)
    regulation.run_local_check(rt)
    assert rt.store.state.boost_active and rt.store.state.boost_since == START.isoformat()

    clock["t"] = START + timedelta(hours=3, minutes=59)
    regulation.run_local_check(rt)
    assert rt.store.state.boost_active

    clock["t"] = START + timedelta(hours=4)
    regulation.run_local_check(rt)
    assert not rt.store.state.boost_active and rt.store.state.boost_since is None
    assert ("number.curve", 1.0) in [call.args for call in rt.ha_api.set_number_value.call_args_list]


def test_comfort_boost_ends_after_three_unreadable_room_values(tmp_path, clock):
    values = {"sensor.room_actual": 19.0, "number.curve": 1.0, "number.shift": 20.0, "number.heat_limit": 16.0}
    rt = _rt(tmp_path, values)
    regulation.run_local_check(rt)
    values["sensor.room_actual"] = ValueError("could not convert string to float: 'unknown'")
    for _ in range(2):  # Review Focus 4: ein- oder zweimal unlesbar beendet nichts
        with pytest.raises(ValueError):
            regulation.run_local_check(rt)
    assert rt.store.state.boost_active
    with pytest.raises(ValueError):
        regulation.run_local_check(rt)
    assert not rt.store.state.boost_active


def test_a_boost_persisted_before_the_update_runs_four_hours_from_the_first_check(tmp_path, clock):
    # Review Focus 3: boost_active ohne boost_since (Stand vor dem Update)
    values = {"sensor.room_actual": 19.0, "number.curve": 1.5, "number.shift": 25.0, "number.heat_limit": 23.0}
    rt = _rt(tmp_path, values, {"last_room_target": 21.0, "boost_active": True,
                                "restore_point": {"curve": 1.0, "room_setpoint": 20.0, "heat_limit": 16.0}})
    regulation.run_local_check(rt)
    assert rt.store.state.boost_active and rt.store.state.boost_since == START.isoformat()


@pytest.mark.parametrize("raw", ["2026-10-08T06:00:00", "kaputt"])
def test_a_naive_or_corrupt_boost_since_in_memory_does_not_kill_the_local_check(tmp_path, clock, raw):
    # Audit 4 P-C2 Endpruefung: ein nicht lesbarer Zeitstempel gilt als "unbekannt" (wie ein Boost vor dem Update)
    values = {"sensor.room_actual": 19.0, "number.curve": 1.5, "number.shift": 25.0, "number.heat_limit": 23.0}
    rt = _rt(tmp_path, values, {"last_room_target": 21.0, "boost_active": True,
                                "restore_point": {"curve": 1.0, "room_setpoint": 20.0, "heat_limit": 16.0}})
    rt.store.update(boost_since=raw)
    regulation.run_local_check(rt)
    assert rt.store.state.boost_active and rt.store.state.boost_since == START.isoformat()
    clock["t"] = START + timedelta(hours=4)
    regulation.run_local_check(rt)
    assert not rt.store.state.boost_active and rt.store.state.boost_since is None


def test_a_stale_boost_since_does_not_shorten_a_new_comfort_boost(tmp_path, clock):
    # Audit 4 P-C2 Endpruefung: boost_since blieb nach einem Ende ausserhalb des lokalen Checks stehen
    values = {"sensor.room_actual": 19.0, "number.curve": 1.0, "number.shift": 20.0, "number.heat_limit": 16.0}
    stale = (START - timedelta(hours=10)).isoformat()
    rt = _rt(tmp_path, values, {"last_room_target": 20.0, "boost_active": False, "boost_since": stale,
                                "restore_point": {"curve": 1.0, "room_setpoint": 20.0, "heat_limit": 16.0}})
    regulation.run_local_check(rt)
    assert rt.store.state.boost_active and rt.store.state.boost_since == START.isoformat()

    clock["t"] = START + timedelta(minutes=5)
    regulation.run_local_check(rt)
    assert rt.store.state.boost_active and rt.store.state.boost_since == START.isoformat()

    clock["t"] = START + timedelta(hours=4)
    regulation.run_local_check(rt)
    assert not rt.store.state.boost_active


def test_boost_since_stays_in_memory_when_the_backup_cannot_be_written(tmp_path, clock, monkeypatch):
    # Audit 4 P-C2 Endpruefung: voller/schreibgeschuetzter Datentraeger darf die 4-h-Grenze nicht aushebeln
    values = {"sensor.room_actual": 19.0, "number.curve": 1.5, "number.shift": 25.0, "number.heat_limit": 23.0}
    rt = _rt(tmp_path, values, {"last_room_target": 21.0, "boost_active": True,
                                "restore_point": {"curve": 1.0, "room_setpoint": 20.0, "heat_limit": 16.0}})

    def _failing_save(path, content):
        raise StorageError("Datentraeger voll")

    monkeypatch.setattr(rt.store, "_save", _failing_save)
    with contextlib.suppress(StorageError):
        regulation.run_local_check(rt)
    assert rt.store.state.boost_since == START.isoformat()
    assert rt.store.storage_failed

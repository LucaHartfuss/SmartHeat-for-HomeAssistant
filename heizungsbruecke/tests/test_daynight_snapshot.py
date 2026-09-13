from datetime import datetime
from unittest.mock import MagicMock

from heizungsbruecke.backup_store import load_backup, save_backup
from heizungsbruecke.daynight_snapshot import maybe_snapshot


def test_maybe_snapshot_writes_day_avg_after_20h_when_not_yet_done_today(tmp_path):
    ha_api = MagicMock()
    ha_api.get_state.return_value = 22.3
    state_path = tmp_path / "daynight_snapshot_state.json"

    maybe_snapshot(ha_api, "sensor.room_12h_avg", "input_number.day_avg", "input_number.night_avg",
                   state_path, now=datetime(2026, 9, 13, 20, 5))

    ha_api.set_input_number_value.assert_called_once_with("input_number.day_avg", 22.3)
    assert load_backup(state_path)["last_day_snapshot_date"] == "2026-09-13"


def test_maybe_snapshot_writes_night_avg_after_8h_when_not_yet_done_today(tmp_path):
    ha_api = MagicMock()
    ha_api.get_state.return_value = 19.8
    state_path = tmp_path / "daynight_snapshot_state.json"

    maybe_snapshot(ha_api, "sensor.room_12h_avg", "input_number.day_avg", "input_number.night_avg",
                   state_path, now=datetime(2026, 9, 13, 8, 15))

    ha_api.set_input_number_value.assert_called_once_with("input_number.night_avg", 19.8)
    assert load_backup(state_path)["last_night_snapshot_date"] == "2026-09-13"


def test_maybe_snapshot_does_not_repeat_day_avg_same_day(tmp_path):
    ha_api = MagicMock()
    ha_api.get_state.return_value = 22.3
    state_path = tmp_path / "daynight_snapshot_state.json"
    save_backup(state_path, {"last_day_snapshot_date": "2026-09-13"})

    maybe_snapshot(ha_api, "sensor.room_12h_avg", "input_number.day_avg", "input_number.night_avg",
                   state_path, now=datetime(2026, 9, 13, 23, 0))

    ha_api.set_input_number_value.assert_not_called()


def test_maybe_snapshot_does_nothing_before_8h(tmp_path):
    ha_api = MagicMock()
    state_path = tmp_path / "daynight_snapshot_state.json"

    maybe_snapshot(ha_api, "sensor.room_12h_avg", "input_number.day_avg", "input_number.night_avg",
                   state_path, now=datetime(2026, 9, 13, 7, 59))

    ha_api.set_input_number_value.assert_not_called()
    ha_api.get_state.assert_not_called()


def test_maybe_snapshot_writes_both_when_first_run_is_late_in_the_day(tmp_path):
    # Erster Start des Add-ons z.B. um 21 Uhr: beide Fenster (>=8 und >=20) sind
    # fuer heute noch nicht dokumentiert -> beide werden im selben Tick nachgeholt.
    ha_api = MagicMock()
    ha_api.get_state.return_value = 21.0
    state_path = tmp_path / "daynight_snapshot_state.json"

    maybe_snapshot(ha_api, "sensor.room_12h_avg", "input_number.day_avg", "input_number.night_avg",
                   state_path, now=datetime(2026, 9, 13, 21, 0))

    assert ha_api.set_input_number_value.call_count == 2

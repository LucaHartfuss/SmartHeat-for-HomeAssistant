from unittest.mock import MagicMock

from heizungsbruecke.backup_store import load_backup, save_backup
from heizungsbruecke.derived_sensors import ensure_all


def test_ensure_all_creates_all_five_helpers_when_none_exist(tmp_path):
    ha_api = MagicMock()
    ha_api.entity_exists.return_value = False
    ha_api.create_statistics_sensor.side_effect = [
        "sensor.smartheat_client1_room_12h_avg",
        "sensor.smartheat_client1_dart",
        "sensor.smartheat_client1_dat",
    ]
    ha_api.create_input_number.side_effect = [
        "input_number.smartheat_client1_room_day_avg",
        "input_number.smartheat_client1_room_night_avg",
    ]
    state_path = tmp_path / "derived_sensors.json"

    result = ensure_all(ha_api, "client1", "sensor.rt", "sensor.aussentemperatur", 3.0, state_path)

    assert result == {
        "dat": "sensor.smartheat_client1_dat",
        "dart": "sensor.smartheat_client1_dart",
        "room_day_avg": "input_number.smartheat_client1_room_day_avg",
        "room_night_avg": "input_number.smartheat_client1_room_night_avg",
        "_room_12h_avg": "sensor.smartheat_client1_room_12h_avg",
    }
    ha_api.create_statistics_sensor.assert_any_call(
        name="SmartHeat client1 Raumtemp. 3h-Mittel", source_entity_id="sensor.rt", max_age_hours=3.0,
    )
    assert ha_api.create_statistics_sensor.call_count == 3
    assert ha_api.create_input_number.call_count == 2


def test_ensure_all_reuses_existing_entities_without_recreating(tmp_path):
    state_path = tmp_path / "derived_sensors.json"
    save_backup(state_path, {
        "room_12h_avg": {"entity_id": "sensor.smartheat_client1_room_12h_avg"},
        "dart": {"entity_id": "sensor.smartheat_client1_dart"},
        "dat": {"entity_id": "sensor.smartheat_client1_dat"},
        "room_day_avg": {"entity_id": "input_number.smartheat_client1_room_day_avg"},
        "room_night_avg": {"entity_id": "input_number.smartheat_client1_room_night_avg"},
    })
    ha_api = MagicMock()
    ha_api.entity_exists.return_value = True

    result = ensure_all(ha_api, "client1", "sensor.rt", "sensor.aussentemperatur", 3.0, state_path)

    assert result["dat"] == "sensor.smartheat_client1_dat"
    ha_api.create_statistics_sensor.assert_not_called()
    ha_api.create_input_number.assert_not_called()


def test_ensure_all_recreates_only_the_missing_entry(tmp_path):
    state_path = tmp_path / "derived_sensors.json"
    save_backup(state_path, {
        "room_12h_avg": {"entity_id": "sensor.smartheat_client1_room_12h_avg"},
        "dart": {"entity_id": "sensor.smartheat_client1_dart"},
        # "dat" fehlt -- z.B. nach einem frueheren Teilfehlschlag
        "room_day_avg": {"entity_id": "input_number.smartheat_client1_room_day_avg"},
        "room_night_avg": {"entity_id": "input_number.smartheat_client1_room_night_avg"},
    })
    ha_api = MagicMock()
    ha_api.entity_exists.return_value = True
    ha_api.create_statistics_sensor.return_value = "sensor.smartheat_client1_dat"

    result = ensure_all(ha_api, "client1", "sensor.rt", "sensor.aussentemperatur", 3.0, state_path)

    assert result["dat"] == "sensor.smartheat_client1_dat"
    ha_api.create_statistics_sensor.assert_called_once_with(
        name="SmartHeat client1 DAT", source_entity_id="sensor.aussentemperatur", max_age_hours=24,
    )
    ha_api.create_input_number.assert_not_called()


def test_ensure_all_recreates_entity_deleted_out_of_band(tmp_path):
    state_path = tmp_path / "derived_sensors.json"
    save_backup(state_path, {"dat": {"entity_id": "sensor.smartheat_client1_dat"}})
    ha_api = MagicMock()
    ha_api.entity_exists.return_value = False  # Kunde hat den Helfer geloescht
    ha_api.create_statistics_sensor.return_value = "sensor.smartheat_client1_dat_neu"
    ha_api.create_input_number.side_effect = [
        "input_number.smartheat_client1_room_day_avg",
        "input_number.smartheat_client1_room_night_avg",
    ]

    result = ensure_all(ha_api, "client1", "sensor.rt", "sensor.aussentemperatur", 3.0, state_path)

    assert result["dat"] == "sensor.smartheat_client1_dat_neu"
    assert load_backup(state_path)["dat"]["entity_id"] == "sensor.smartheat_client1_dat_neu"


def test_ensure_all_uses_avg_window_hours_in_display_name_and_max_age(tmp_path):
    ha_api = MagicMock()
    ha_api.entity_exists.return_value = False
    ha_api.create_statistics_sensor.side_effect = [
        "sensor.smartheat_client1_room_avg", "sensor.smartheat_client1_dart", "sensor.smartheat_client1_dat",
    ]
    ha_api.create_input_number.side_effect = [
        "input_number.smartheat_client1_room_day_avg", "input_number.smartheat_client1_room_night_avg",
    ]
    state_path = tmp_path / "derived_sensors.json"

    ensure_all(ha_api, "client1", "sensor.rt", "sensor.aussentemperatur", 4.5, state_path)

    ha_api.create_statistics_sensor.assert_any_call(
        name="SmartHeat client1 Raumtemp. 4.5h-Mittel", source_entity_id="sensor.rt", max_age_hours=4.5,
    )

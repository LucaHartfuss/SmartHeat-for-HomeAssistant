"""Hilfs-Entities (Spec TP6 3.2/3.3): Template-Sensoren, Statistik-Helfer auf deren Basis und
Neuanlage bei Quellwechsel."""
import logging
from unittest.mock import MagicMock

import pytest

from heizungsbruecke.backup_store import load_backup, save_backup
from heizungsbruecke.derived_sensors import ensure_all
from heizungsbruecke.helper_templates import outdoor_temperature_template, room_temperature_template

ROOMS = ["sensor.wz", "climate.kz::current_temperature"]
ROOM_TEMPLATE = room_temperature_template(ROOMS)
ROOM_ID = "sensor.smartheat_t1_raumtemperatur"
OUTDOOR_ID = "sensor.smartheat_t1_aussentemperatur"


def _ha_api(existing=()):
    ha_api = MagicMock()
    ha_api.entity_exists.side_effect = lambda entity_id: entity_id in existing
    ha_api.create_template_sensor.side_effect = lambda name, template: (
        OUTDOOR_ID if "Außentemperatur" in name else ROOM_ID
    )
    ha_api.create_statistics_sensor.side_effect = lambda name, source_entity_id, max_age_hours, **kw: (
        "sensor." + name.lower().replace(" ", "_").replace(".", "").replace("-", "_")
    )
    ha_api.create_input_number.side_effect = lambda name, **kw: "input_number." + name.lower().replace(" ", "_").replace(".", "")
    return ha_api


def _run(ha_api, state_path, room_sensors=ROOMS, outdoor="sensor.aussen"):
    return ensure_all(ha_api, "t1", room_sensors, outdoor, 3.0, state_path)


def test_fresh_install_creates_room_template_and_bases_statistics_on_it(tmp_path):
    ha_api = _ha_api()

    result = _run(ha_api, tmp_path / "d.json")

    ha_api.create_template_sensor.assert_called_once_with(name="SmartHeat t1 Raumtemperatur", template=ROOM_TEMPLATE)
    ha_api.create_statistics_sensor.assert_any_call(
        name="SmartHeat t1 DART", source_entity_id=ROOM_ID, max_age_hours=24,
    )
    ha_api.create_statistics_sensor.assert_any_call(
        name="SmartHeat t1 Raumtemp. 3h-Mittel", source_entity_id=ROOM_ID, max_age_hours=3.0,
    )
    ha_api.create_statistics_sensor.assert_any_call(
        name="SmartHeat t1 DAT", source_entity_id="sensor.aussen", max_age_hours=24,
    )
    ha_api.create_statistics_sensor.assert_any_call(
        name="SmartHeat t1 Aussentemp. 24h-Minimum", source_entity_id="sensor.aussen",
        max_age_hours=24, state_characteristic="value_min",
    )
    assert result.entity_ids["room_actual"] == ROOM_ID
    assert "outdoor_temp" not in result.entity_ids
    assert set(result.entity_ids) == {
        "room_actual", "dat", "dart", "room_day_avg", "room_night_avg", "_room_12h_avg", "outdoor_min_24h",
    }
    assert result.replaced == ()
    ha_api.delete_helper.assert_not_called()
    tracking = load_backup(tmp_path / "d.json")
    assert tracking["room_temperature"] == {"entity_id": ROOM_ID, "source": ROOM_TEMPLATE}
    assert tracking["dart"]["source"] == ROOM_ID
    assert tracking["dat"]["source"] == "sensor.aussen"
    assert "source" not in tracking["room_day_avg"]


def test_weather_source_gets_an_outdoor_template_that_feeds_dat(tmp_path):
    ha_api = _ha_api()

    result = _run(ha_api, tmp_path / "d.json", outdoor="weather.forecast_home")

    ha_api.create_template_sensor.assert_any_call(
        name="SmartHeat t1 Außentemperatur", template=outdoor_temperature_template("weather.forecast_home"),
    )
    ha_api.create_statistics_sensor.assert_any_call(name="SmartHeat t1 DAT", source_entity_id=OUTDOOR_ID, max_age_hours=24)
    assert result.entity_ids["outdoor_temp"] == OUTDOOR_ID


def _tracking_after_fresh_run(tmp_path, **kwargs):
    ha_api = _ha_api()
    _run(ha_api, tmp_path / "d.json", **kwargs)
    tracking = load_backup(tmp_path / "d.json")
    return tracking, {entry["entity_id"] for entry in tracking.values()}


def test_same_sources_reuse_every_helper(tmp_path):
    _, existing = _tracking_after_fresh_run(tmp_path)
    ha_api = _ha_api(existing)

    result = _run(ha_api, tmp_path / "d.json")

    ha_api.create_template_sensor.assert_not_called()
    ha_api.create_statistics_sensor.assert_not_called()
    ha_api.create_input_number.assert_not_called()
    ha_api.delete_helper.assert_not_called()
    assert result.replaced == ()


def test_changed_room_sensors_recreate_only_the_room_template(tmp_path):
    _, existing = _tracking_after_fresh_run(tmp_path)
    ha_api = _ha_api(existing)

    result = _run(ha_api, tmp_path / "d.json", room_sensors=["sensor.wz"])

    ha_api.delete_helper.assert_called_once_with(ROOM_ID)
    ha_api.create_template_sensor.assert_called_once_with(
        name="SmartHeat t1 Raumtemperatur", template=room_temperature_template(["sensor.wz"]),
    )
    ha_api.create_statistics_sensor.assert_not_called()  # gleiche Quell-Entity-ID
    assert result.replaced == ("room_temperature",)


def test_changed_outdoor_sensor_recreates_dat_and_minimum(tmp_path):
    _, existing = _tracking_after_fresh_run(tmp_path)
    ha_api = _ha_api(existing)

    result = _run(ha_api, tmp_path / "d.json", outdoor="sensor.aussen_neu")

    assert set(result.replaced) == {"dat", "outdoor_min_24h"}
    assert ha_api.delete_helper.call_count == 2


def test_helpers_without_recorded_source_are_recreated_once(tmp_path):
    """Bestand vor TP6 (client1): Statistik-Helfer ohne `source` werden einmal neu angelegt,
    die input_number-Helfer bleiben."""
    state_path = tmp_path / "d.json"
    save_backup(state_path, {
        "room_12h_avg": {"entity_id": "sensor.alt_12h"},
        "dart": {"entity_id": "sensor.alt_dart"},
        "dat": {"entity_id": "sensor.alt_dat"},
        "room_day_avg": {"entity_id": "input_number.alt_tag"},
        "room_night_avg": {"entity_id": "input_number.alt_nacht"},
        "outdoor_min_24h": {"entity_id": "sensor.alt_min"},
    })
    ha_api = _ha_api({
        "sensor.alt_12h", "sensor.alt_dart", "sensor.alt_dat", "input_number.alt_tag",
        "input_number.alt_nacht", "sensor.alt_min",
    })

    result = _run(ha_api, state_path)

    assert set(result.replaced) == {"room_12h_avg", "dart", "dat", "outdoor_min_24h"}
    assert {c.args[0] for c in ha_api.delete_helper.call_args_list} == {
        "sensor.alt_12h", "sensor.alt_dart", "sensor.alt_dat", "sensor.alt_min",
    }
    ha_api.create_input_number.assert_not_called()
    assert result.entity_ids["room_day_avg"] == "input_number.alt_tag"


def test_tracked_helper_deleted_by_the_user_is_recreated_without_counting_as_replaced(tmp_path):
    tracking, existing = _tracking_after_fresh_run(tmp_path)
    ha_api = _ha_api(existing - {tracking["dart"]["entity_id"]})

    result = _run(ha_api, tmp_path / "d.json")

    ha_api.delete_helper.assert_not_called()
    assert ha_api.create_statistics_sensor.call_count == 1
    assert result.replaced == ()


def test_switch_from_weather_to_sensor_deletes_the_outdoor_template(tmp_path):
    _, existing = _tracking_after_fresh_run(tmp_path, outdoor="weather.forecast_home")
    ha_api = _ha_api(existing)

    result = _run(ha_api, tmp_path / "d.json", outdoor="sensor.aussen")

    ha_api.delete_helper.assert_any_call(OUTDOOR_ID)
    assert "outdoor_temperature" not in load_backup(tmp_path / "d.json")
    assert "outdoor_temp" not in result.entity_ids


def test_optional_outdoor_minimum_failure_is_only_a_warning(tmp_path, caplog):
    ha_api = _ha_api()
    original = ha_api.create_statistics_sensor.side_effect

    def _no_minimum(name, source_entity_id, max_age_hours, **kw):
        if "Minimum" in name:
            raise RuntimeError("kaputt")
        return original(name, source_entity_id, max_age_hours, **kw)

    ha_api.create_statistics_sensor.side_effect = _no_minimum
    with caplog.at_level(logging.WARNING):
        result = _run(ha_api, tmp_path / "d.json")

    assert "outdoor_min_24h" not in result.entity_ids
    assert "Sommersperre" in caplog.text


def test_fingerprint_is_stable_and_follows_the_sources(tmp_path):
    first = _run(_ha_api(), tmp_path / "a.json")
    second = _run(_ha_api(), tmp_path / "b.json")
    other = _run(_ha_api(), tmp_path / "c.json", room_sensors=["sensor.wz"])

    assert first.sources_fingerprint == second.sources_fingerprint
    assert first.sources_fingerprint != other.sources_fingerprint
    assert len(first.sources_fingerprint) == 12


def test_failing_template_creation_propagates(tmp_path):
    ha_api = _ha_api()
    ha_api.create_template_sensor.side_effect = RuntimeError("HA lehnt ab")

    with pytest.raises(RuntimeError):
        _run(ha_api, tmp_path / "d.json")


def test_source_change_is_still_reported_when_create_failed_after_delete(tmp_path):
    """Loeschen gelang, Anlegen scheiterte (Start-Retry): der naechste Versuch findet den alten
    Helfer nicht mehr, muss den Quellwechsel aber trotzdem melden (einmaliger Hinweis)."""
    _, existing = _tracking_after_fresh_run(tmp_path)
    existing = set(existing)
    ha_api = _ha_api(existing)
    ha_api.delete_helper.side_effect = existing.discard
    original = ha_api.create_template_sensor.side_effect
    ha_api.create_template_sensor.side_effect = [RuntimeError("HA lehnt ab"), original(
        name="SmartHeat t1 Raumtemperatur", template="x",
    )]

    with pytest.raises(RuntimeError):
        _run(ha_api, tmp_path / "d.json", room_sensors=["sensor.wz"])
    result = _run(ha_api, tmp_path / "d.json", room_sensors=["sensor.wz"])

    ha_api.delete_helper.assert_called_once_with(ROOM_ID)
    assert result.replaced == ("room_temperature",)
    assert load_backup(tmp_path / "d.json")["room_temperature"] == {
        "entity_id": ROOM_ID, "source": room_temperature_template(["sensor.wz"]),
    }

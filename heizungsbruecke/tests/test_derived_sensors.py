"""Hilfs-Entities (Spec TP6 3.2/3.3): Template-Sensoren, Neuanlage bei Quellwechsel und (TP11)
das Wegraeumen der frueheren Statistik- und Tag-/Nacht-Helfer."""
import logging
from unittest.mock import MagicMock

import pytest

from heizungsbruecke.backup_store import load_backup, save_backup
from heizungsbruecke.derived_sensors import ensure_all
from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.helper_templates import outdoor_temperature_template, room_temperature_template

ROOMS = ["sensor.wz", "climate.kz::current_temperature"]
ROOM_TEMPLATE = room_temperature_template(ROOMS)
ROOM_ID = "sensor.smartheat_t1_raumtemperatur"
OUTDOOR_ID = "sensor.smartheat_t1_aussentemperatur"


def _ha_api(existing=()):
    ha_api = MagicMock(spec=HomeAssistantApi)  # nur Methoden, die es wirklich gibt
    ha_api.entity_exists.side_effect = lambda entity_id: entity_id in existing
    ha_api.create_template_sensor.side_effect = lambda name, template: (
        OUTDOOR_ID if "Außentemperatur" in name else ROOM_ID
    )
    return ha_api


def _run(ha_api, state_path, room_sensors=ROOMS, outdoor="sensor.aussen"):
    return ensure_all(ha_api, "t1", room_sensors, outdoor, state_path)


def test_fresh_install_creates_only_the_room_template(tmp_path):
    ha_api = _ha_api()

    result = _run(ha_api, tmp_path / "d.json")

    ha_api.create_template_sensor.assert_called_once_with(name="SmartHeat t1 Raumtemperatur", template=ROOM_TEMPLATE)
    assert result.entity_ids == {"room_actual": ROOM_ID}
    assert result.replaced == ()
    ha_api.delete_helper.assert_not_called()
    ha_api.delete_input_number.assert_not_called()
    assert load_backup(tmp_path / "d.json") == {"room_temperature": {"entity_id": ROOM_ID, "source": ROOM_TEMPLATE}}


def test_weather_source_gets_an_outdoor_template(tmp_path):
    ha_api = _ha_api()

    result = _run(ha_api, tmp_path / "d.json", outdoor="weather.forecast_home")

    ha_api.create_template_sensor.assert_any_call(
        name="SmartHeat t1 Außentemperatur", template=outdoor_temperature_template("weather.forecast_home"),
    )
    assert result.entity_ids == {"room_actual": ROOM_ID, "outdoor_temp": OUTDOOR_ID}


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
    ha_api.delete_helper.assert_not_called()
    assert result.replaced == ()


def test_changed_room_sensors_recreate_only_the_room_template(tmp_path):
    _, existing = _tracking_after_fresh_run(tmp_path)
    ha_api = _ha_api(existing)

    result = _run(ha_api, tmp_path / "d.json", room_sensors=["sensor.wz"])

    ha_api.delete_helper.assert_called_once_with(ROOM_ID, tenant_id="t1")
    ha_api.create_template_sensor.assert_called_once_with(
        name="SmartHeat t1 Raumtemperatur", template=room_temperature_template(["sensor.wz"]),
    )
    assert result.replaced == ("room_temperature",)


def test_changed_weather_source_recreates_the_outdoor_template(tmp_path):
    _, existing = _tracking_after_fresh_run(tmp_path, outdoor="weather.forecast_home")
    ha_api = _ha_api(existing)

    result = _run(ha_api, tmp_path / "d.json", outdoor="weather.other")

    ha_api.delete_helper.assert_called_once_with(OUTDOOR_ID, tenant_id="t1")
    assert result.replaced == ("outdoor_temperature",)


def test_tracked_helper_deleted_by_the_user_is_recreated_without_counting_as_replaced(tmp_path):
    _, existing = _tracking_after_fresh_run(tmp_path)
    ha_api = _ha_api(existing - {ROOM_ID})

    result = _run(ha_api, tmp_path / "d.json")

    ha_api.delete_helper.assert_not_called()
    assert ha_api.create_template_sensor.call_count == 1
    assert result.replaced == ()


def test_switch_from_weather_to_sensor_deletes_the_outdoor_template(tmp_path):
    _, existing = _tracking_after_fresh_run(tmp_path, outdoor="weather.forecast_home")
    ha_api = _ha_api(existing)

    result = _run(ha_api, tmp_path / "d.json", outdoor="sensor.aussen")

    ha_api.delete_helper.assert_any_call(OUTDOOR_ID, tenant_id="t1")
    assert "outdoor_temperature" not in load_backup(tmp_path / "d.json")
    assert "outdoor_temp" not in result.entity_ids


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
    ha_api.delete_helper.side_effect = lambda entity_id, tenant_id=None: existing.discard(entity_id)
    original = ha_api.create_template_sensor.side_effect
    ha_api.create_template_sensor.side_effect = [RuntimeError("HA lehnt ab"), original(
        name="SmartHeat t1 Raumtemperatur", template="x",
    )]

    with pytest.raises(RuntimeError):
        _run(ha_api, tmp_path / "d.json", room_sensors=["sensor.wz"])
    result = _run(ha_api, tmp_path / "d.json", room_sensors=["sensor.wz"])

    ha_api.delete_helper.assert_called_once_with(ROOM_ID, tenant_id="t1")
    assert result.replaced == ("room_temperature",)
    assert load_backup(tmp_path / "d.json")["room_temperature"] == {
        "entity_id": ROOM_ID, "source": room_temperature_template(["sensor.wz"]),
    }


# --- TP11: fruehere Statistik- und Tag-/Nacht-Helfer werden weggeraeumt ---

OBSOLETE_TRACKING = {
    "room_temperature": {"entity_id": ROOM_ID, "source": ROOM_TEMPLATE},
    "room_12h_avg": {"entity_id": "sensor.smartheat_t1_raumtemp_3h_mittel", "source": ROOM_ID},
    "dart": {"entity_id": "sensor.smartheat_t1_dart", "source": ROOM_ID},
    "dat": {"entity_id": "sensor.smartheat_t1_dat", "source": "sensor.aussen"},
    "outdoor_min_24h": {"entity_id": "sensor.smartheat_t1_aussentemp_24h_minimum", "source": "sensor.aussen"},
    "room_day_avg": {"entity_id": "input_number.smartheat_t1_raumtemp_tagesmittel"},
    "room_night_avg": {"entity_id": "input_number.smartheat_t1_raumtemp_nachtmittel"},
}


def test_obsolete_helpers_are_removed(tmp_path):
    state_path = tmp_path / "d.json"
    save_backup(state_path, OBSOLETE_TRACKING)
    ha_api = _ha_api({entry["entity_id"] for entry in OBSOLETE_TRACKING.values()})

    result = _run(ha_api, state_path)

    assert {c.args[0] for c in ha_api.delete_helper.call_args_list} == {
        "sensor.smartheat_t1_raumtemp_3h_mittel", "sensor.smartheat_t1_dart", "sensor.smartheat_t1_dat",
        "sensor.smartheat_t1_aussentemp_24h_minimum",
    }
    assert {c.args[0] for c in ha_api.delete_input_number.call_args_list} == {
        "input_number.smartheat_t1_raumtemp_tagesmittel", "input_number.smartheat_t1_raumtemp_nachtmittel",
    }
    assert load_backup(state_path) == {"room_temperature": OBSOLETE_TRACKING["room_temperature"]}
    assert result.entity_ids == {"room_actual": ROOM_ID}
    assert result.replaced == ()


def test_obsolete_helper_already_gone_is_only_dropped_from_tracking(tmp_path):
    state_path = tmp_path / "d.json"
    save_backup(state_path, {"dart": OBSOLETE_TRACKING["dart"], "room_day_avg": OBSOLETE_TRACKING["room_day_avg"]})
    ha_api = _ha_api()

    _run(ha_api, state_path)

    ha_api.delete_helper.assert_not_called()
    ha_api.delete_input_number.assert_not_called()
    assert "dart" not in load_backup(state_path) and "room_day_avg" not in load_backup(state_path)


def test_failing_cleanup_is_not_a_start_error(tmp_path, caplog):
    state_path = tmp_path / "d.json"
    save_backup(state_path, {"dat": OBSOLETE_TRACKING["dat"], "room_night_avg": OBSOLETE_TRACKING["room_night_avg"]})
    ha_api = _ha_api({"sensor.smartheat_t1_dat", "input_number.smartheat_t1_raumtemp_nachtmittel"})
    ha_api.delete_helper.side_effect = RuntimeError("kein Config-Entry")

    with caplog.at_level(logging.WARNING):
        result = _run(ha_api, state_path)

    assert "room_actual" in result.entity_ids
    ha_api.delete_input_number.assert_called_once_with("input_number.smartheat_t1_raumtemp_nachtmittel")
    tracking = load_backup(state_path)
    assert tracking["dat"] == OBSOLETE_TRACKING["dat"]  # naechster Start versucht es erneut
    assert "room_night_avg" not in tracking
    assert "sensor.smartheat_t1_dat" in caplog.text


def test_broken_tracking_entry_of_an_obsolete_helper_is_not_a_start_error(tmp_path, caplog):
    state_path = tmp_path / "d.json"
    save_backup(state_path, {"dart": "kaputt", "room_day_avg": OBSOLETE_TRACKING["room_day_avg"]})
    ha_api = _ha_api({"input_number.smartheat_t1_raumtemp_tagesmittel"})

    with caplog.at_level(logging.WARNING):
        result = _run(ha_api, state_path)

    assert result.entity_ids == {"room_actual": ROOM_ID}
    ha_api.delete_input_number.assert_called_once_with("input_number.smartheat_t1_raumtemp_tagesmittel")
    assert "dart" in caplog.text


def test_obsolete_helpers_are_deleted_with_the_tenant_for_the_title_check(tmp_path):
    state_path = tmp_path / "d.json"
    renamed = "sensor.heizraum_zuhause_smartheat_t1_dat"
    save_backup(state_path, {"dat": {"entity_id": renamed, "source": "sensor.aussen"}})
    ha_api = _ha_api({renamed})

    _run(ha_api, state_path)

    ha_api.delete_helper.assert_called_once_with(renamed, tenant_id="t1")

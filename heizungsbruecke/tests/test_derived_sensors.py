"""Hilfs-Entities: Template-Sensoren, Neuanlage bei Quellwechsel und Entfernen der Anlage."""
import logging
from unittest.mock import MagicMock

import pytest

from heizungsbruecke.derived_sensors import ensure_all, entfernen
from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.helper_templates import outdoor_temperature_template, room_temperature_template
from smartheat_runtime.backup_store import load_backup, save_backup

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


def test_a_helper_without_a_remembered_source_is_recreated_and_reported(tmp_path):
    """Ein Helfer, dessen Eintrag keine Quelle kennt, gilt als Quellwechsel: er wird geloescht und neu angelegt, die
    Quelle danach gemerkt, und der Wechsel zaehlt als ersetzt (Meldung „quellwechsel“). Gegenprobe: mit gemerkter
    gleicher Quelle bleibt er."""
    save_backup(tmp_path / "d.json", {"room_temperature": {"entity_id": ROOM_ID}})
    ha_api = _ha_api(existing={ROOM_ID})

    result = _run(ha_api, tmp_path / "d.json")

    ha_api.delete_helper.assert_called_once_with(ROOM_ID, tenant_id="t1")
    ha_api.create_template_sensor.assert_called_once_with(name="SmartHeat t1 Raumtemperatur", template=ROOM_TEMPLATE)
    assert result.replaced == ("room_temperature",)
    assert load_backup(tmp_path / "d.json") == {"room_temperature": {"entity_id": ROOM_ID, "source": ROOM_TEMPLATE}}
    ha_api = _ha_api(existing={ROOM_ID})
    assert _run(ha_api, tmp_path / "d.json").replaced == ()
    ha_api.delete_helper.assert_not_called()


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


# --- Entfernen der Anlage: alle Helfer weg, nicht loeschbare bleiben fuer den naechsten Versuch ---


def _tracking_with_old_helpers(tmp_path):
    tracking, _ = _tracking_after_fresh_run(tmp_path, outdoor="weather.forecast_home")
    tracking["room_day_avg"] = {"entity_id": "input_number.smartheat_t1_tagesmittel"}  # Altrest einer frueheren Version
    save_backup(tmp_path / "d.json", tracking)
    return tracking


def test_removing_deletes_every_helper_and_the_list(tmp_path):
    tracking = _tracking_with_old_helpers(tmp_path)
    ha_api = _ha_api({entry["entity_id"] for entry in tracking.values()})

    assert entfernen(ha_api, "t1", tmp_path / "d.json") is True

    assert sorted(call.args[0] for call in ha_api.delete_helper.call_args_list) == [OUTDOOR_ID, ROOM_ID]
    assert all(call.kwargs == {"tenant_id": "t1"} for call in ha_api.delete_helper.call_args_list)
    assert not (tmp_path / "d.json").exists()


def test_entfernen_verwirft_unbekannte_eintraege_ohne_loeschversuch(tmp_path):
    """Ein Altrest unter einem fremden Schluessel (frueherer Helfer) wird nicht in HA geloescht, auch wenn die Entity
    existiert; er haelt die Aufraeum-Markierung nicht fest, selbst wenn ein Loeschversuch scheitern wuerde."""
    tracking = _tracking_with_old_helpers(tmp_path)
    altrest = "sensor.smartheat_t1_dart"
    tracking["dart"] = {"entity_id": altrest, "source": ROOM_ID}
    save_backup(tmp_path / "d.json", tracking)
    ha_api = _ha_api({entry["entity_id"] for entry in tracking.values()})

    def delete_helper(entity_id, tenant_id):
        if entity_id == altrest:
            raise RuntimeError("kein Config-Entry")

    ha_api.delete_helper.side_effect = delete_helper

    assert entfernen(ha_api, "t1", tmp_path / "d.json") is True

    assert altrest not in [call.args[0] for call in ha_api.delete_helper.call_args_list]
    assert not (tmp_path / "d.json").exists()


def test_removing_keeps_a_helper_that_could_not_be_deleted(tmp_path, caplog):
    tracking = _tracking_with_old_helpers(tmp_path)
    ha_api = _ha_api({entry["entity_id"] for entry in tracking.values()})

    def delete_helper(entity_id, tenant_id):
        if entity_id == ROOM_ID:
            raise RuntimeError("geheim")

    ha_api.delete_helper.side_effect = delete_helper

    with caplog.at_level(logging.WARNING):
        assert entfernen(ha_api, "t1", tmp_path / "d.json") is False

    assert load_backup(tmp_path / "d.json") == {"room_temperature": tracking["room_temperature"]}
    assert "RuntimeError" in caplog.text and "geheim" not in caplog.text


def test_removing_a_helper_that_is_already_gone_only_forgets_it(tmp_path):
    _tracking_with_old_helpers(tmp_path)
    ha_api = _ha_api()  # keine der Entities existiert noch

    assert entfernen(ha_api, "t1", tmp_path / "d.json") is True

    ha_api.delete_helper.assert_not_called()
    assert not (tmp_path / "d.json").exists()


@pytest.mark.parametrize("content", ["{kaputt", "[]", '{"room_temperature": "sensor.alt", "x": {"source": "s"}}'])
def test_removing_with_a_broken_list_never_raises(tmp_path, content):
    (tmp_path / "d.json").write_text(content)
    ha_api = _ha_api({"sensor.alt"})

    assert entfernen(ha_api, "t1", tmp_path / "d.json") is True

    ha_api.delete_helper.assert_not_called()
    assert not (tmp_path / "d.json").exists()


def test_removing_without_a_list_is_done(tmp_path):
    assert entfernen(_ha_api(), "t1", tmp_path / "d.json") is True

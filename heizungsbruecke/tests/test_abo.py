"""Abo-inaktiv-Modus und Fristende (abo.py)."""
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from heizungsbruecke import abo, entitlement
from heizungsbruecke.backup_store import load_backup
from heizungsbruecke.manifest import ChannelManifest
from heizungsbruecke.override import Override

ABO_NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone(timedelta(hours=2)))
OPTIONS = {
    "tenant_id": "t1", "curve_min": 0.2, "curve_max": 0.8, "offset_min": 0.0, "offset_max": 5.0,
    "boost_curve_value": 0.5, "boost_offset_value": 2.0,
}
BOTH_ROLES = {"curve_current": "number.curve", "offset_current": "number.offset"}


@pytest.fixture(autouse=True)
def _entitlement_path(tmp_path, monkeypatch):
    monkeypatch.setattr("heizungsbruecke.config.ENTITLEMENT_PATH", tmp_path / "entitlement_state.json")


def _runtime(store, entity_ids=BOTH_ROLES, notify_service="notify.handy"):
    manifest = ChannelManifest(entity_ids=entity_ids)
    ha_api = MagicMock()
    options = {**OPTIONS, "notify_service": notify_service}
    return SimpleNamespace(
        manifest=manifest, ha_api=ha_api, options=options, store=store, mqtt_client=MagicMock(),
        override=Override(store, manifest, ha_api, options),
    )


def test_inactive_message_contains_grace_end_date():
    assert abo.inactive_message(ABO_NOW) == (
        "SmartHeat: Abo inaktiv. Die Heizung läuft noch bis 25.10.2026 im Notbetrieb weiter, "
        "danach bleiben die zuletzt gelernten Werte fest eingestellt."
    )


def test_enter_inactive_activates_notbetrieb_stops_mqtt_and_notifies_all_channels(make_store, tmp_path):
    rt = _runtime(make_store(failsafe={"failsafe_active": False, "pending": {"seq": "s1", "trigger": "daily"}}))

    abo.enter_inactive(rt, ABO_NOW)

    assert rt.store.state.abo_inactive_since == ABO_NOW
    assert (rt.store.state.delivery.notbetrieb, rt.store.state.delivery.pending) == (True, None)
    assert load_backup(tmp_path / "failsafe_state.json") == {"failsafe_active": True, "datenfehler": None, "pending": None}
    rt.mqtt_client.stop.assert_called_once()
    expected = abo.inactive_message(ABO_NOW)
    rt.ha_api.send_notification.assert_called_once_with("notify.handy", expected)
    rt.ha_api.create_persistent_notification.assert_called_once_with("SmartHeat", expected, "smartheat_abo_inaktiv")


def test_enter_inactive_does_not_repeat_notification_when_already_marked(make_store, tmp_path, caplog):
    entitlement.mark_inactive(tmp_path / "entitlement_state.json", ABO_NOW - timedelta(days=5))
    rt = _runtime(make_store())

    with caplog.at_level(logging.WARNING):
        abo.enter_inactive(rt, ABO_NOW)

    assert rt.store.state.abo_inactive_since == ABO_NOW - timedelta(days=5)
    rt.ha_api.send_notification.assert_not_called()
    rt.ha_api.create_persistent_notification.assert_not_called()
    assert "20.10.2026" in caplog.text  # Fristende weiter im Log sichtbar


def test_enter_inactive_is_noop_when_already_in_mode(make_store):
    rt = _runtime(make_store())
    rt.store.update(abo_inactive_since=ABO_NOW)

    abo.enter_inactive(rt, ABO_NOW)

    rt.mqtt_client.stop.assert_not_called()


def test_enter_inactive_survives_failing_channels(make_store):
    rt = _runtime(make_store())
    rt.ha_api.send_notification.side_effect = RuntimeError("push kaputt")
    rt.ha_api.create_persistent_notification.side_effect = RuntimeError("ha kaputt")
    rt.mqtt_client.stop.side_effect = RuntimeError("paho kaputt")

    abo.enter_inactive(rt, ABO_NOW)  # darf nicht werfen

    assert rt.store.state.delivery.notbetrieb is True


def test_enter_inactive_with_failing_entitlement_persist_still_enters_mode(make_store, tmp_path, monkeypatch, caplog):
    def _failing(path, now):
        raise OSError("SD-Karte kaputt")

    monkeypatch.setattr("heizungsbruecke.entitlement.mark_inactive", _failing)
    rt = _runtime(make_store())

    with caplog.at_level(logging.ERROR):
        abo.enter_inactive(rt, ABO_NOW)

    assert rt.store.state.abo_inactive_since == ABO_NOW
    assert load_backup(tmp_path / "failsafe_state.json")["failsafe_active"] is True
    rt.mqtt_client.stop.assert_called_once()
    rt.ha_api.create_persistent_notification.assert_called_once_with(
        "SmartHeat", abo.inactive_message(ABO_NOW), "smartheat_abo_inaktiv",
    )
    assert "SD-Karte kaputt" in caplog.text


def test_finish_grace_mid_boost_restores_learned_values_clamped(make_store, tmp_path):
    store = make_store(backup={
        "curve_current": 0.4, "offset_current": 9.0,  # ueber offset_max=5.0 -> geclampt
        "emergency_boost_active": True, "boost_active": True,
    })
    rt = _runtime(store)

    assert abo.finish_grace(rt, always_restore=True, final_notice=True) is True

    rt.ha_api.set_number_value.assert_any_call("number.curve", 0.4)
    rt.ha_api.set_number_value.assert_any_call("number.offset", 5.0)
    backup = load_backup(tmp_path / "backup.json")
    assert (backup["boost_active"], backup["emergency_boost_active"]) == (False, False)
    assert store.state.abo_finished is True
    rt.ha_api.send_notification.assert_called_once_with("notify.handy", abo.ABO_ENDED_MESSAGE)
    rt.ha_api.create_persistent_notification.assert_called_once_with(
        "SmartHeat", abo.ABO_ENDED_MESSAGE, "smartheat_abo_inaktiv",
    )


def test_finish_grace_counts_restore_as_done_when_saving_flags_fails(make_store, monkeypatch, caplog):
    store = make_store(backup={"curve_current": 0.4, "offset_current": 2.0, "emergency_boost_active": True})
    rt = _runtime(store)

    def _broken_save(path, values):
        raise OSError("SD-Karte kaputt")

    monkeypatch.setattr("heizungsbruecke.backup_store.save_backup", _broken_save)

    with caplog.at_level(logging.ERROR):
        assert abo.finish_grace(rt, always_restore=True, final_notice=True) is True

    assert store.state.abo_finished is True
    assert store.state.emergency_boost_active is False
    rt.ha_api.set_number_value.assert_any_call("number.curve", 0.4)
    assert "SD-Karte kaputt" in caplog.text


def test_finish_grace_keeps_flags_when_restore_write_fails(make_store, tmp_path):
    store = make_store(backup={"curve_current": 0.4, "emergency_boost_active": True})
    rt = _runtime(store, entity_ids={"curve_current": "number.curve"})
    rt.ha_api.set_number_value.side_effect = RuntimeError("HA nicht erreichbar")

    assert abo.finish_grace(rt, always_restore=True, final_notice=True) is False

    assert load_backup(tmp_path / "backup.json")["emergency_boost_active"] is True
    assert store.state.abo_finished is False
    rt.ha_api.send_notification.assert_not_called()
    rt.ha_api.create_persistent_notification.assert_not_called()


def test_finish_grace_without_forced_restore_and_without_flags_writes_nothing(make_store, caplog):
    rt = _runtime(make_store(backup={"curve_current": 0.4, "boost_active": False}), entity_ids={"curve_current": "number.curve"})

    with caplog.at_level(logging.INFO):
        assert abo.finish_grace(rt, always_restore=False, final_notice=False) is True

    rt.ha_api.set_number_value.assert_not_called()
    rt.ha_api.send_notification.assert_not_called()
    rt.ha_api.create_persistent_notification.assert_not_called()
    assert "Frist" in caplog.text

"""Abo-inaktiv-Modus und Fristende (abo.py)."""
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from conftest import FakeClock
from fakes import runtime_config

from heizungsbruecke.ha_binding import HaPlantBinding
from heizungsbruecke.ha_sinks import HaNotifySink, HaStatusSink
from heizungsbruecke.version import ADDON_VERSION
from smartheat_core.levers import LEVER_SETS
from smartheat_core.pipeline import LeverPipeline
from smartheat_core.safety import LocalSafety
from smartheat_runtime import abo, entitlement
from smartheat_runtime.backup_store import load_backup
from smartheat_runtime.notifier import Notifier
from smartheat_runtime.roles import ChannelManifest
from smartheat_runtime.status import StatusReporter
from smartheat_runtime.texts import HA_TEXTS

ABO_NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone(timedelta(hours=2)))
OPTIONS = {"tenant_id": "t1"}
SAFETY = LocalSafety(
    ranges={"curve": (0.2, 0.8), "room_setpoint": (0.0, 5.0), "heat_limit": (5.0, 20.0), "min_flow": (20.0, 30.0)},
    comfort_boost={"curve": 0.5, "room_setpoint": 2.0, "heat_limit": 20.0},
    emergency_boost_levers=("curve", "room_setpoint", "heat_limit"),
    arrival_threshold_k=0.5,
)
BOTH_ROLES = {"curve_current": "number.curve", "shift_current": "number.shift"}


def _runtime(tmp_path, store, entity_ids=BOTH_ROLES, notify_services=("notify.handy",), clock=None):
    manifest = ChannelManifest(refs=entity_ids)
    ha_api = MagicMock()
    return SimpleNamespace(
        manifest=manifest, signals=ha_api, ha_api=ha_api, store=store, mqtt_client=MagicMock(),
        config=runtime_config(entitlement_path=tmp_path / "entitlement_state.json"),
        override=LeverPipeline(store, HaPlantBinding(ha_api, manifest), SAFETY),
        notifier=Notifier(store, HaNotifySink(ha_api, list(notify_services))),
        status=StatusReporter(
            HaStatusSink(ha_api), OPTIONS["tenant_id"], None, store, LEVER_SETS["vaillant_vrc720"], ADDON_VERSION,
        ),
        clock=clock or FakeClock(), auth_rejected_queried_at=None, auth_rejected_last_status=None,
        connection_failing_queried_at=None, texts=HA_TEXTS,
    )


def test_inactive_message_contains_grace_end_date():
    assert abo.inactive_message(ABO_NOW) == (
        "SmartHeat: Abo inaktiv. Die Heizung läuft noch bis 25.10.2026 im Notbetrieb weiter, "
        "danach bleiben die zuletzt gelernten Werte fest eingestellt."
    )


def test_enter_inactive_activates_notbetrieb_stops_mqtt_and_notifies_all_channels(make_store, tmp_path):
    rt = _runtime(tmp_path, make_store(failsafe={"failsafe_active": False, "pending": {"seq": "s1", "trigger": "daily"}}))

    abo.enter_inactive(rt, ABO_NOW)

    assert rt.store.state.abo_inactive_since == ABO_NOW
    assert (rt.store.state.delivery.notbetrieb, rt.store.state.delivery.pending) == (True, None)
    assert load_backup(tmp_path / "failsafe_state.json") == {"failsafe_active": True, "datenfehler": None, "pending": None}
    rt.mqtt_client.stop.assert_called_once()
    expected = abo.inactive_message(ABO_NOW)
    rt.ha_api.send_notification.assert_called_once_with("notify.handy", expected)
    rt.ha_api.create_persistent_notification.assert_called_once_with("SmartHeat", expected, "smartheat_abo")


def test_enter_inactive_does_not_repeat_notification_when_already_marked(make_store, tmp_path, caplog):
    entitlement.mark_inactive(tmp_path / "entitlement_state.json", ABO_NOW - timedelta(days=5))
    rt = _runtime(tmp_path, make_store())

    with caplog.at_level(logging.WARNING):
        abo.enter_inactive(rt, ABO_NOW)

    assert rt.store.state.abo_inactive_since == ABO_NOW - timedelta(days=5)
    rt.ha_api.send_notification.assert_not_called()
    rt.ha_api.create_persistent_notification.assert_not_called()
    assert "20.10.2026" in caplog.text  # Fristende weiter im Log sichtbar


def test_enter_inactive_is_noop_when_already_in_mode(make_store, tmp_path):
    rt = _runtime(tmp_path, make_store())
    rt.store.update(abo_inactive_since=ABO_NOW)

    abo.enter_inactive(rt, ABO_NOW)

    rt.mqtt_client.stop.assert_not_called()


def test_enter_inactive_survives_failing_channels(make_store, tmp_path):
    rt = _runtime(tmp_path, make_store())
    rt.ha_api.send_notification.side_effect = RuntimeError("push kaputt")
    rt.ha_api.create_persistent_notification.side_effect = RuntimeError("ha kaputt")
    rt.mqtt_client.stop.side_effect = RuntimeError("paho kaputt")

    abo.enter_inactive(rt, ABO_NOW)  # darf nicht werfen

    assert rt.store.state.delivery.notbetrieb is True


def test_enter_inactive_with_failing_entitlement_persist_still_enters_mode(make_store, tmp_path, monkeypatch, caplog):
    def _failing(path, now):
        raise OSError("Datentraeger kaputt")

    monkeypatch.setattr("smartheat_runtime.entitlement.mark_inactive", _failing)
    rt = _runtime(tmp_path, make_store())

    with caplog.at_level(logging.ERROR):
        abo.enter_inactive(rt, ABO_NOW)

    assert rt.store.state.abo_inactive_since == ABO_NOW
    assert load_backup(tmp_path / "failsafe_state.json")["failsafe_active"] is True
    rt.mqtt_client.stop.assert_called_once()
    rt.ha_api.create_persistent_notification.assert_called_once_with(
        "SmartHeat", abo.inactive_message(ABO_NOW), "smartheat_abo",
    )
    assert "Datentraeger kaputt" in caplog.text


def test_finish_grace_mid_boost_restores_learned_values_clamped(make_store, tmp_path):
    store = make_store(backup={
        "restore_point": {"curve": 0.4, "room_setpoint": 9.0},  # ueber dem Maximum 5.0 -> geclampt
        "emergency_boost_active": True, "boost_active": True,
    })
    rt = _runtime(tmp_path, store)

    assert abo.finish_grace(rt, always_restore=True, final_notice=True) is True

    rt.ha_api.set_number_value.assert_any_call("number.curve", 0.4)
    rt.ha_api.set_number_value.assert_any_call("number.shift", 5.0)
    backup = load_backup(tmp_path / "backup.json")
    assert (backup["boost_active"], backup["emergency_boost_active"]) == (False, False)
    assert store.state.abo_finished is True
    rt.ha_api.send_notification.assert_called_once_with("notify.handy", abo.ABO_ENDED_MESSAGE)
    rt.ha_api.create_persistent_notification.assert_called_once_with(
        "SmartHeat", abo.ABO_ENDED_MESSAGE, "smartheat_abo",
    )


def test_finish_grace_counts_restore_as_done_when_saving_flags_fails(make_store, tmp_path, monkeypatch, caplog):
    store = make_store(backup={"restore_point": {"curve": 0.4, "room_setpoint": 2.0}, "emergency_boost_active": True})
    rt = _runtime(tmp_path, store)

    def _broken_save(path, values):
        raise OSError("Datentraeger kaputt")

    monkeypatch.setattr("smartheat_runtime.backup_store.save_backup", _broken_save)

    with caplog.at_level(logging.ERROR):
        assert abo.finish_grace(rt, always_restore=True, final_notice=True) is True

    assert store.state.abo_finished is True
    assert store.state.emergency_boost_active is False
    rt.ha_api.set_number_value.assert_any_call("number.curve", 0.4)
    assert "Datentraeger kaputt" in caplog.text


def test_finish_grace_keeps_flags_when_restore_write_fails(make_store, tmp_path):
    store = make_store(backup={"restore_point": {"curve": 0.4}, "emergency_boost_active": True})
    rt = _runtime(tmp_path, store, entity_ids={"curve_current": "number.curve"})
    rt.ha_api.set_number_value.side_effect = RuntimeError("HA nicht erreichbar")

    assert abo.finish_grace(rt, always_restore=True, final_notice=True) is False

    assert load_backup(tmp_path / "backup.json")["emergency_boost_active"] is True
    assert store.state.abo_finished is False
    rt.ha_api.send_notification.assert_not_called()
    rt.ha_api.create_persistent_notification.assert_not_called()


def test_finish_grace_without_forced_restore_and_without_flags_writes_nothing(make_store, tmp_path, caplog):
    rt = _runtime(tmp_path,
        make_store(backup={"restore_point": {"curve": 0.4}, "boost_active": False}),
        entity_ids={"curve_current": "number.curve"},
    )

    with caplog.at_level(logging.INFO):
        assert abo.finish_grace(rt, always_restore=False, final_notice=False) is True

    rt.ha_api.set_number_value.assert_not_called()
    rt.ha_api.send_notification.assert_not_called()
    rt.ha_api.create_persistent_notification.assert_not_called()
    assert "Frist" in caplog.text


@pytest.mark.parametrize("status, expect_inactive, expect_rejected", [
    (entitlement.INACTIVE, True, False),
    (entitlement.REJECTED, False, True),
    (entitlement.ACTIVE, False, False),
    (entitlement.UNKNOWN, False, False),
])
def test_connection_failing_acts_only_on_a_clear_answer(make_store, tmp_path, monkeypatch, status, expect_inactive, expect_rejected):
    rt = _runtime(tmp_path, make_store())
    monkeypatch.setattr(abo.entitlement, "query", lambda config: status)
    abo.handle_connection_failing(rt)
    assert (rt.store.state.abo_inactive_since is not None) is expect_inactive
    assert rt.status.flags.zugang_abgelehnt is expect_rejected
    if status in (entitlement.ACTIVE, entitlement.UNKNOWN):  # normaler Ausfall: keine Meldung an den Kunden
        rt.ha_api.send_notification.assert_not_called()
        rt.ha_api.create_persistent_notification.assert_not_called()


def test_connection_failing_rejected_notifies_like_an_auth_rejection(make_store, tmp_path, monkeypatch):
    rt = _runtime(tmp_path, make_store())
    monkeypatch.setattr(abo.entitlement, "query", lambda config: entitlement.REJECTED)
    abo.handle_connection_failing(rt)
    rt.ha_api.send_notification.assert_called_once_with("notify.handy", abo.ACCESS_DENIED_MESSAGE)
    assert rt.auth_rejected_queried_at == rt.clock()


def test_connection_failing_is_throttled_like_auth_rejection(make_store, tmp_path, monkeypatch):
    rt = _runtime(tmp_path, make_store())
    queries = []
    monkeypatch.setattr(abo.entitlement, "query", lambda config: queries.append(1) or entitlement.UNKNOWN)
    abo.handle_connection_failing(rt)
    abo.handle_connection_failing(rt)
    rt.clock.advance(abo.AUTH_REJECTED_QUERY_INTERVAL_SECONDS)
    abo.handle_connection_failing(rt)
    assert len(queries) == 2


def test_connection_failing_is_silent_in_the_abo_inactive_mode(make_store, tmp_path, monkeypatch):
    rt = _runtime(tmp_path, make_store())
    rt.store.update(abo_inactive_since=ABO_NOW)
    monkeypatch.setattr(abo.entitlement, "query", lambda config: pytest.fail("keine Abfrage"))
    abo.handle_connection_failing(rt)


def test_restart_process_reexecs_the_original_command(monkeypatch):
    calls = []
    monkeypatch.setattr(abo.os, "execv", lambda executable, argv: calls.append((executable, argv)))
    monkeypatch.setattr(abo.sys, "orig_argv", ["python", "-m", "heizungsbruecke"])
    abo.restart_process()
    assert calls == [(abo.sys.executable, [abo.sys.executable, "-m", "heizungsbruecke"])]

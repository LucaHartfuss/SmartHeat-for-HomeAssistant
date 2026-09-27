"""Meldungen mit Zustandsentprellung (notifier.py, Spec TP6 3.4)."""
import logging
from unittest.mock import MagicMock

import pytest

from heizungsbruecke.notifier import HINT_CATEGORIES, STATE_OK, Notifier, category, notification_id
from heizungsbruecke.state import StateStore

SERVICES = ["notify.mobile_app_a", "notify.mobile_app_b"]


@pytest.fixture
def ha_api():
    return MagicMock()


def _notifier(store, ha_api, services=SERVICES):
    return Notifier(store, ha_api, services)


def test_notification_id_is_a_slug():
    assert notification_id("raumfuehler:sensor.wz-1") == "smartheat_raumfuehler_sensor_wz_1"
    assert notification_id("notbetrieb") == "smartheat_notbetrieb"


def test_unknown_previous_state_counts_as_ok(make_store, ha_api):
    notifier = _notifier(make_store(), ha_api)

    assert notifier.state("notbetrieb") == STATE_OK
    assert notifier.notify("notbetrieb", STATE_OK, "alles gut", critical=True) is False
    ha_api.send_notification.assert_not_called()
    ha_api.dismiss_persistent_notification.assert_not_called()


def test_critical_state_change_pushes_every_service_and_creates_persistent(make_store, ha_api):
    notifier = _notifier(make_store(), ha_api)

    assert notifier.notify("notbetrieb", "aktiv", "Notbetrieb aktiv", critical=True) is True

    assert [c.args for c in ha_api.send_notification.call_args_list] == [
        ("notify.mobile_app_a", "Notbetrieb aktiv"), ("notify.mobile_app_b", "Notbetrieb aktiv"),
    ]
    ha_api.create_persistent_notification.assert_called_once_with(
        "SmartHeat", "Notbetrieb aktiv", "smartheat_notbetrieb",
    )


def test_same_state_is_silent(make_store, ha_api):
    notifier = _notifier(make_store(), ha_api)
    notifier.notify("notbetrieb", "aktiv", "Notbetrieb aktiv", critical=True)
    ha_api.reset_mock()

    assert notifier.notify("notbetrieb", "aktiv", "Notbetrieb aktiv", critical=True) is False

    ha_api.send_notification.assert_not_called()
    ha_api.create_persistent_notification.assert_not_called()


def test_notify_is_silent_for_same_state_after_restart(make_store, ha_api, tmp_path):
    _notifier(make_store(), ha_api).notify("konfiguration", "fehler:x", "Fehler x", critical=True)
    ha_api.reset_mock()
    restarted = _notifier(StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json"), ha_api)

    assert restarted.notify("konfiguration", "fehler:x", "Fehler x", critical=True) is False
    ha_api.send_notification.assert_not_called()


def test_back_to_ok_pushes_dismisses_and_forgets_the_key(make_store, ha_api):
    store = make_store()
    notifier = _notifier(store, ha_api)
    notifier.notify("notbetrieb", "aktiv", "Notbetrieb aktiv", critical=True)
    ha_api.reset_mock()

    assert notifier.notify("notbetrieb", STATE_OK, "Notbetrieb beendet", critical=True) is True

    assert ha_api.send_notification.call_count == 2
    ha_api.dismiss_persistent_notification.assert_called_once_with("smartheat_notbetrieb")
    assert "notbetrieb" not in store.state.notify_states


def test_non_critical_never_touches_persistent_notifications(make_store, ha_api):
    notifier = _notifier(make_store(), ha_api)

    notifier.notify("batterie:sensor.x", "niedrig", "Batterie niedrig", critical=False)
    notifier.notify("batterie:sensor.x", STATE_OK, "Batterie ok", critical=False)

    ha_api.create_persistent_notification.assert_not_called()
    ha_api.dismiss_persistent_notification.assert_not_called()
    assert ha_api.send_notification.call_count == 4


def test_failing_service_does_not_block_the_others(make_store, ha_api, caplog):
    ha_api.send_notification.side_effect = [RuntimeError("weg"), None]
    notifier = _notifier(make_store(), ha_api)

    with caplog.at_level(logging.WARNING):
        notifier.notify("abo", "inaktiv", "Abo inaktiv", critical=True)

    assert ha_api.send_notification.call_count == 2
    ha_api.create_persistent_notification.assert_called_once()
    assert "notify.mobile_app_a" in caplog.text


def test_failing_persistent_notification_is_only_logged(make_store, ha_api, caplog):
    ha_api.create_persistent_notification.side_effect = RuntimeError("HA weg")
    notifier = _notifier(make_store(), ha_api)

    with caplog.at_level(logging.WARNING):
        assert notifier.notify("abo", "inaktiv", "Abo inaktiv", critical=True) is True

    assert "abo" in caplog.text


def test_state_write_failure_still_notifies(make_store, ha_api, monkeypatch):
    store = make_store()
    notifier = _notifier(store, ha_api)

    def _broken(*args, **kwargs):
        raise OSError("Datentraeger kaputt")

    monkeypatch.setattr("heizungsbruecke.backup_store.save_backup", _broken)
    assert notifier.notify("notbetrieb", "aktiv", "Notbetrieb aktiv", critical=True) is True

    assert store.state.notify_states == {"notbetrieb": "aktiv"}
    assert ha_api.send_notification.call_count == 2


def test_seed_sets_a_missing_state_silently_so_the_recovery_is_reported(make_store, ha_api):
    notifier = _notifier(make_store(), ha_api)

    notifier.seed("notbetrieb", "aktiv")
    ha_api.send_notification.assert_not_called()

    assert notifier.notify("notbetrieb", STATE_OK, "Notbetrieb beendet", critical=True) is True


def test_seed_never_overrides_a_known_state(make_store, ha_api):
    notifier = _notifier(make_store(), ha_api)
    notifier.notify("datenfehler", "lokal:dat", "Datenfehler dat", critical=True)

    notifier.seed("datenfehler", "anlage")

    assert notifier.state("datenfehler") == "lokal:dat"


def test_without_services_only_persistent_notification_is_created(make_store, ha_api):
    notifier = _notifier(make_store(), ha_api, services=[])

    notifier.notify("konfiguration", "fehler:veraltet", "Konfiguration veraltet", critical=True)

    ha_api.send_notification.assert_not_called()
    ha_api.create_persistent_notification.assert_called_once()


def test_critical_message_is_kept_until_ok(make_store, ha_api):
    store = make_store()
    notifier = _notifier(store, ha_api)

    notifier.notify("notbetrieb", "aktiv", "Notbetrieb aktiv", critical=True)
    notifier.notify("batterie:sensor.x", "niedrig", "Batterie niedrig", critical=False)
    assert store.state.notify_messages == {"notbetrieb": "Notbetrieb aktiv"}

    notifier.notify("notbetrieb", STATE_OK, "Notbetrieb beendet", critical=True)
    assert store.state.notify_messages == {}


def test_republish_persistent_recreates_open_critical_notifications_without_push(make_store, ha_api, tmp_path):
    """Review I3: HA haelt persistent_notifications nur im Speicher. Nach einem HA-Neustart legt
    das Add-on sie fuer jede noch offene kritische Stoerung neu an, auch nach eigenem Neustart."""
    notifier = _notifier(make_store(), ha_api)
    notifier.notify("notbetrieb", "aktiv", "Notbetrieb aktiv", critical=True)
    notifier.notify("datenfehler", "lokal:dat", "Datenfehler dat", critical=True)
    notifier.notify("datenfehler", STATE_OK, "Datenfehler behoben", critical=True)
    notifier.notify("batterie:sensor.x", "niedrig", "Batterie niedrig", critical=False)
    ha_api.reset_mock()
    restarted = _notifier(StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json"), ha_api)

    restarted.republish_persistent()

    ha_api.create_persistent_notification.assert_called_once_with(
        "SmartHeat", "Notbetrieb aktiv", "smartheat_notbetrieb",
    )
    ha_api.send_notification.assert_not_called()
    ha_api.dismiss_persistent_notification.assert_not_called()


def test_republish_persistent_skips_seeded_state_without_message(make_store, ha_api):
    notifier = _notifier(make_store(), ha_api)
    notifier.seed("notbetrieb", "aktiv")

    notifier.republish_persistent()

    ha_api.create_persistent_notification.assert_not_called()


def test_republish_persistent_failure_is_only_logged(make_store, ha_api, caplog):
    notifier = _notifier(make_store(), ha_api)
    notifier.notify("notbetrieb", "aktiv", "Notbetrieb aktiv", critical=True)
    notifier.notify("abo", "inaktiv", "Abo inaktiv", critical=True)
    ha_api.create_persistent_notification.side_effect = RuntimeError("HA weg")
    ha_api.create_persistent_notification.reset_mock()

    with caplog.at_level(logging.WARNING):
        notifier.republish_persistent()  # darf nicht werfen

    assert ha_api.create_persistent_notification.call_count == 2
    assert "notbetrieb" in caplog.text


def test_critical_event_while_ha_unreachable_is_restored_by_republish(make_store, ha_api):
    """Push und Benachrichtigung scheitern (HA weg): der Push wird nicht wiederholt, die
    Benachrichtigung kommt beim Wiederverbinden (republish_persistent) doch noch an."""
    ha_api.send_notification.side_effect = RuntimeError("HA weg")
    ha_api.create_persistent_notification.side_effect = RuntimeError("HA weg")
    notifier = _notifier(make_store(), ha_api)
    notifier.notify("notbetrieb", "aktiv", "Notbetrieb aktiv", critical=True)
    ha_api.reset_mock()
    ha_api.send_notification.side_effect = None
    ha_api.create_persistent_notification.side_effect = None

    notifier.republish_persistent()

    ha_api.create_persistent_notification.assert_called_once_with(
        "SmartHeat", "Notbetrieb aktiv", "smartheat_notbetrieb",
    )
    ha_api.send_notification.assert_not_called()


def test_refresh_persistent_reasserts_an_unchanged_critical_state_without_push(make_store, ha_api):
    store = make_store()
    notifier = _notifier(store, ha_api)
    notifier.notify("konfiguration", "fehler:hilfs_entities", "Fehler (Flow a)", critical=True)
    ha_api.reset_mock()

    notifier.refresh_persistent("konfiguration", "Fehler (Flow b)")

    ha_api.create_persistent_notification.assert_called_once_with(
        "SmartHeat", "Fehler (Flow b)", "smartheat_konfiguration",
    )
    ha_api.send_notification.assert_not_called()
    assert store.state.notify_messages == {"konfiguration": "Fehler (Flow b)"}


def test_refresh_persistent_does_nothing_for_an_ok_key(make_store, ha_api):
    store = make_store()
    notifier = _notifier(store, ha_api)

    notifier.refresh_persistent("konfiguration", "Fehler")

    ha_api.create_persistent_notification.assert_not_called()
    assert store.state.notify_messages == {}


def test_category_is_the_key_prefix():
    assert category("raumfuehler:sensor.wz") == "raumfuehler"
    assert category("quellwechsel") == "quellwechsel"
    assert HINT_CATEGORIES == ("raumfuehler", "batterie", "manueller_eingriff", "quellwechsel")


def test_switched_off_hint_category_is_tracked_and_logged_but_not_pushed(make_store, ha_api, caplog):
    notifier = Notifier(make_store(), ha_api, SERVICES, hints_off=["batterie"])

    with caplog.at_level(logging.WARNING):
        assert notifier.notify("batterie:sensor.x", "niedrig", "Batterie schwach", critical=False) is True

    ha_api.send_notification.assert_not_called()
    assert notifier.state("batterie:sensor.x") == "niedrig"
    assert "Batterie schwach" in caplog.text


def test_other_hint_categories_are_still_pushed(make_store, ha_api):
    notifier = Notifier(make_store(), ha_api, SERVICES, hints_off=["batterie"])

    notifier.notify("raumfuehler:sensor.a", "ausgefallen", "Fuehler weg", critical=False)

    assert ha_api.send_notification.call_count == len(SERVICES)


def test_critical_messages_ignore_the_hint_switches(make_store, ha_api):
    notifier = Notifier(make_store(), ha_api, SERVICES, hints_off=list(HINT_CATEGORIES))

    notifier.notify("batterie:sensor.x", "niedrig", "Batterie kritisch niedrig", critical=True)

    assert ha_api.send_notification.call_count == len(SERVICES)


def test_silent_ok_logs_the_return_without_push(make_store, ha_api, caplog):
    notifier = _notifier(make_store(), ha_api)
    notifier.notify("manueller_eingriff", "1.3/24.5", "manuell verstellt", critical=False)
    ha_api.reset_mock()

    with caplog.at_level(logging.INFO):
        assert notifier.notify("manueller_eingriff", STATE_OK, "wieder gelernt", critical=False, silent_ok=True) is True

    ha_api.send_notification.assert_not_called()
    assert notifier.state("manueller_eingriff") == STATE_OK
    assert "wieder gelernt" in caplog.text


def test_clear_all_dismisses_open_notifications_and_forgets_states_without_push(make_store, ha_api):
    store = make_store()
    notifier = _notifier(store, ha_api)
    notifier.notify("notbetrieb", "aktiv", "Notbetrieb aktiv", critical=True)
    notifier.notify("batterie:sensor.x", "niedrig", "Batterie", critical=False)
    ha_api.reset_mock()

    notifier.clear_all()

    dismissed = {c.args[0] for c in ha_api.dismiss_persistent_notification.call_args_list}
    assert dismissed == {"smartheat_notbetrieb", "smartheat_batterie_sensor_x"}
    ha_api.send_notification.assert_not_called()
    assert (store.state.notify_states, store.state.notify_messages) == ({}, {})

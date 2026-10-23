"""Meldungen mit Zustandsentprellung (notifier.py)."""
import logging
from unittest.mock import MagicMock

import pytest
from fakes import FREMDTEXT, FREMDTEXT_IM_LOG, FailingServiceHa, logzeilen

from heizungsbruecke.ha_api import HomeAssistantApi
from heizungsbruecke.ha_sinks import HaNotifySink, notification_id
from smartheat_core import pipeline
from smartheat_core.config_check import HA_HINWEISE
from smartheat_runtime.notifier import STATE_OK, Notifier, category
from smartheat_runtime.state import StateStore

SERVICES = ["notify.mobile_app_a", "notify.mobile_app_b"]


@pytest.fixture
def ha_api():
    return MagicMock(spec=HomeAssistantApi)


def _notifier(store, ha_api, services=SERVICES):
    return Notifier(store, HaNotifySink(ha_api, services))


def test_notification_id_is_a_slug():
    assert notification_id("raumfuehler:sensor.wz-1") == "smartheat_raumfuehler_sensor_wz_1"
    assert notification_id("notbetrieb") == "smartheat_notbetrieb"


def test_a_failing_notify_service_does_not_stop_the_persistent_notification_or_the_state(make_store):
    store = make_store()
    ha = FailingServiceHa()
    notifier = Notifier(store, HaNotifySink(ha, ["notify.kaputt", "notify.handy"]))
    assert notifier.notify("abo", "inaktiv", "Text", critical=True)
    assert ha.pushes == ["Text"]
    assert ha.persistent == [("smartheat_abo", "Text")]
    assert store.state.notify_states == {"abo": "inaktiv"}


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


class _RaisingPushSink:
    """NotifySink-Fake, dessen Push scheitert (ein Host-Sink ausser HA darf den Notifier nicht abbrechen)."""

    def __init__(self) -> None:
        self.shown: list[tuple[str, str]] = []

    def push(self, key: str, message: str) -> None:
        raise RuntimeError("Push kaputt")

    def show(self, key: str, message: str) -> None:
        self.shown.append((key, message))

    def withdraw(self, key: str) -> None:
        pass


def test_failing_sink_push_does_not_stop_the_persistent_notification_or_the_state(make_store, caplog):
    store = make_store()
    sink = _RaisingPushSink()
    notifier = Notifier(store, sink)

    with caplog.at_level(logging.WARNING):
        assert notifier.notify("abo", "inaktiv", "Abo inaktiv", critical=True) is True

    assert sink.shown == [("abo", "Abo inaktiv")]
    assert store.state.notify_states == {"abo": "inaktiv"}
    assert store.state.notify_messages == {"abo": "Abo inaktiv"}
    assert "Push-Benachrichtigung 'abo' konnte nicht gesendet werden" in caplog.text


def test_state_write_failure_still_notifies(make_store, ha_api, monkeypatch):
    store = make_store()
    notifier = _notifier(store, ha_api)

    def _broken(*args, **kwargs):
        raise OSError("Datentraeger kaputt")

    monkeypatch.setattr("smartheat_runtime.backup_store.save_backup", _broken)
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


def test_switched_off_hint_category_is_tracked_and_logged_but_not_pushed(make_store, ha_api, caplog):
    notifier = Notifier(make_store(), HaNotifySink(ha_api, SERVICES), hints_off=["batterie"])

    with caplog.at_level(logging.WARNING):
        assert notifier.notify("batterie:sensor.x", "niedrig", "Batterie schwach", critical=False) is True

    ha_api.send_notification.assert_not_called()
    assert notifier.state("batterie:sensor.x") == "niedrig"
    assert "Batterie schwach" in caplog.text


def test_write_budget_hints_are_switchable():
    assert HA_HINWEISE[-2:] == ("schreibbudget", "schreibzaehler")
    assert category(pipeline.BUDGET_KEY) == "schreibbudget"
    assert category(pipeline.LIFETIME_KEY) == "schreibzaehler"


def test_switched_off_write_budget_hint_is_tracked_but_not_pushed(make_store, ha_api):
    notifier = Notifier(make_store(), HaNotifySink(ha_api, SERVICES), hints_off=["schreibbudget"])

    assert notifier.notify(pipeline.BUDGET_KEY, "erreicht", "Tageslimit erreicht", critical=False) is True
    notifier.notify(pipeline.LIFETIME_KEY, "10000", "Zaehler", critical=False)

    assert notifier.state(pipeline.BUDGET_KEY) == "erreicht"
    assert ha_api.send_notification.call_count == len(SERVICES)  # nur der Zaehler-Hinweis


def test_other_hint_categories_are_still_pushed(make_store, ha_api):
    notifier = Notifier(make_store(), HaNotifySink(ha_api, SERVICES), hints_off=["batterie"])

    notifier.notify("raumfuehler:sensor.a", "ausgefallen", "Fuehler weg", critical=False)

    assert ha_api.send_notification.call_count == len(SERVICES)


def test_critical_messages_ignore_the_hint_switches(make_store, ha_api):
    notifier = Notifier(make_store(), HaNotifySink(ha_api, SERVICES), hints_off=list(HA_HINWEISE))

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


@pytest.mark.parametrize(("state", "stufe"), [("inaktiv", logging.WARNING), (STATE_OK, logging.INFO)],
                         ids=["warnung", "entwarnung"])
def test_the_log_line_escapes_single_surrogates_from_server_text(make_store, ha_api, caplog, state, stufe):
    """Eine Meldung kann Text vom Server tragen (Grund einer Ablehnung).
    Ein einzelnes Surrogat braeche die UTF-8-Ausgabe des Logs (Logging error, Zeile verloren); die Logzeile schreibt
    es als Escape, Push und gespeicherter Text bleiben unveraendert."""
    store = make_store()
    notifier = _notifier(store, ha_api)
    if state == STATE_OK:
        notifier.seed("server", "abgelehnt")
    message = "Server hat die Messwerte abgelehnt (\ud800). Die Heizkurve bleibt unverändert."

    with caplog.at_level(logging.INFO, logger="smartheat_runtime.notifier"):
        assert notifier.notify("server", state, message, critical=False) is True

    (zeile,) = [r for r in caplog.records if r.levelno == stufe]
    assert zeile.getMessage() == "Server hat die Messwerte abgelehnt (\\ud800). Die Heizkurve bleibt unverändert."
    zeile.getMessage().encode("utf-8")
    assert [c.args for c in ha_api.send_notification.call_args_list] == [(service, message) for service in SERVICES]


def test_the_log_line_escapes_control_characters_from_server_text(make_store, ha_api, caplog):
    """Sicherheitscheck zu a9756e8: Zeilenumbruch, Wagenruecklauf, Tabulator und ANSI-Escape im Servertext taeuschen keine
    eigene Logzeile vor; die Logzeile schreibt sie als Escape, der Push bekommt den Text unveraendert."""
    notifier = _notifier(make_store(), ha_api)
    message = "Server hat die Messwerte abgelehnt (x\r\nWARNING  gefaelscht\t\x1b[31m\x00). Die Heizkurve bleibt unverändert."

    with caplog.at_level(logging.INFO, logger="smartheat_runtime.notifier"):
        assert notifier.notify("server", "abgelehnt", message, critical=False) is True

    (zeile,) = [r.getMessage() for r in caplog.records]
    assert zeile == ("Server hat die Messwerte abgelehnt (x\\r\\nWARNING  gefaelscht\\t\\x1b[31m\\x00). "
                     "Die Heizkurve bleibt unverändert.")
    assert zeile.splitlines() == [zeile]
    assert [c.args for c in ha_api.send_notification.call_args_list] == [(service, message) for service in SERVICES]


class _ScheiternderSink:
    """NotifySink-Fake, dessen Push bzw. Anzeige scheitert (Fehlerpfade des Notifiers)."""

    def __init__(self, *, push_fehler: bool = False, show_fehler: bool = False) -> None:
        self._push_fehler, self._show_fehler = push_fehler, show_fehler

    def push(self, key: str, message: str) -> None:
        if self._push_fehler:
            raise RuntimeError("Push kaputt")

    def show(self, key: str, message: str) -> None:
        if self._show_fehler:
            raise RuntimeError("HA weg")

    def withdraw(self, key: str) -> None:
        pass


FREMDER_SCHLUESSEL = f"fehler:{FREMDTEXT}"  # HaHost.load: StartFailure(str(ConfigError)) traegt Text des Servers
FREMDER_SCHLUESSEL_IM_LOG = f"fehler:{FREMDTEXT_IM_LOG}"


def _kaputter_speicher(monkeypatch):
    monkeypatch.setattr("smartheat_runtime.backup_store.save_backup", _raise_oserror)


def _raise_oserror(*args, **kwargs):
    raise OSError("Datentraeger kaputt")


def _seed_ohne_speicher(store, monkeypatch):
    _kaputter_speicher(monkeypatch)
    Notifier(store, _ScheiternderSink()).seed(FREMDER_SCHLUESSEL, "aktiv")
    return "Meldezustand '{}' konnte nicht gespeichert werden"


def _notify_ohne_speicher(store, monkeypatch):
    _kaputter_speicher(monkeypatch)
    Notifier(store, _ScheiternderSink()).notify(FREMDER_SCHLUESSEL, "aktiv", "Text", critical=True)
    return "Meldezustand '{}' konnte nicht gespeichert werden, Meldung geht trotzdem raus"


def _push_scheitert(store, monkeypatch):
    Notifier(store, _ScheiternderSink(push_fehler=True)).notify(FREMDER_SCHLUESSEL, "aktiv", "Text", critical=False)
    return "Push-Benachrichtigung '{}' konnte nicht gesendet werden"


def _meldetext_ohne_speicher(store, monkeypatch):
    notifier = Notifier(store, _ScheiternderSink())
    notifier.notify(FREMDER_SCHLUESSEL, "aktiv", "alt", critical=True)
    _kaputter_speicher(monkeypatch)
    notifier.refresh_persistent(FREMDER_SCHLUESSEL, "neu")
    return "Meldetext '{}' konnte nicht gespeichert werden"


def _anzeige_scheitert(store, monkeypatch):
    Notifier(store, _ScheiternderSink(show_fehler=True)).notify(FREMDER_SCHLUESSEL, "aktiv", "Text", critical=True)
    return "Offene Meldung '{}' konnte nicht aktualisiert werden"


@pytest.mark.parametrize("pfad", [_seed_ohne_speicher, _notify_ohne_speicher, _push_scheitert,
                                  _meldetext_ohne_speicher, _anzeige_scheitert],
                         ids=["seed", "notify_speicher", "push", "refresh_speicher", "anzeige"])
def test_ein_meldeschluessel_mit_fremdem_text_steht_in_den_fehlerpfaden_nur_escaped_im_log(
    make_store, monkeypatch, caplog, pfad,
):
    """Der Schluessel `fehler:<Text>` eines Startfehlers traegt Text des Servers (Konfigurationsdokument).
    Scheitern Speichern, Push oder Anzeige, steht er in der Warnung nur als Escape (Zeilenumbruch, U+202E, U+061C)."""
    store = make_store()
    with caplog.at_level(logging.INFO, logger="smartheat_runtime.notifier"):
        vorlage = pfad(store, monkeypatch)
    assert vorlage.format(FREMDER_SCHLUESSEL_IM_LOG) in logzeilen(caplog)

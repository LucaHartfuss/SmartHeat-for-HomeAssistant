"""Anbindung der Waermelieferungs-Erkennung an Zustand, Meldung und Status (TP12f, Spec 2)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from heizungsbruecke import waerme_hint
from heizungsbruecke.notifier import STATE_OK
from heizungsbruecke.state import StateStore, StorageError

CEST = timezone(timedelta(hours=2))
T0 = datetime(2026, 9, 30, 2, 11, tzinfo=CEST)
TICK = timedelta(minutes=5)


def _rt(store):
    return SimpleNamespace(store=store, notifier=MagicMock(), status=MagicMock())


def _feed(rt, start, end, setpoint=38.0, flow=26.0, room=21.0):
    result, ts = None, start
    while ts < end:
        result = waerme_hint.apply_tick(rt, room, {"flow_temperature": flow}, {"flow_setpoint": setpoint}, now=ts)
        ts += TICK
    return result


def test_setting_the_flag_notifies_once_persists_and_publishes_the_status(make_store):
    rt = _rt(make_store())

    assert _feed(rt, T0, T0 + timedelta(hours=3)) is False
    assert _feed(rt, T0 + timedelta(hours=3), T0 + timedelta(hours=6)) is True

    assert rt.store.state.waerme_fehlt_seit == "2026-09-30T05:11:00+02:00"
    rt.notifier.notify.assert_called_once()
    args, kwargs = rt.notifier.notify.call_args
    assert args[0] == "therme" and args[1] == "fehlt" and kwargs == {"critical": False}
    assert "seit 05:11" in args[2] and "S.031" in args[2]
    rt.status.publish_if_changed.assert_called_once()


def test_a_share_at_the_threshold_for_the_hold_clears_the_flag_with_a_silent_all_clear(make_store):
    rt = _rt(make_store())
    _feed(rt, T0, T0 + timedelta(hours=4))
    rt.notifier.notify.reset_mock()
    rt.status.publish_if_changed.reset_mock()

    # Eine Stunde guter Vorlauf (Anteil 0,71): das Flag bleibt, bis CLEAR_HOLD erreicht ist.
    assert _feed(rt, T0 + timedelta(hours=4), T0 + timedelta(hours=5), flow=33.0) is True
    rt.notifier.notify.assert_not_called()
    assert _feed(rt, T0 + timedelta(hours=5), T0 + timedelta(hours=5, minutes=5), flow=33.0) is False

    assert rt.store.state.waerme_fehlt_seit is None
    args, kwargs = rt.notifier.notify.call_args
    assert (args[0], args[1], kwargs) == ("therme", STATE_OK, {"critical": False, "silent_ok": True})
    rt.status.publish_if_changed.assert_called_once()


def test_a_restart_keeps_the_flag_without_a_second_notification(make_store, tmp_path):
    rt = _rt(make_store())
    _feed(rt, T0, T0 + timedelta(hours=4))

    restarted = _rt(StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json"))
    assert restarted.store.state.waerme is None
    assert _feed(restarted, T0 + timedelta(hours=4), T0 + timedelta(hours=5)) is True

    restarted.notifier.notify.assert_not_called()
    restarted.status.publish_if_changed.assert_not_called()


def test_an_invalid_stored_value_counts_as_no_flag(make_store):
    rt = _rt(make_store({"waerme_fehlt_seit": "gestern"}))
    assert _feed(rt, T0, T0 + TICK, flow=33.0) is False
    rt.notifier.notify.assert_not_called()


def test_missing_readings_never_set_the_flag(make_store):
    rt = _rt(make_store())
    assert waerme_hint.apply_tick(rt, 21.0, {}, {}, now=T0) is False
    assert waerme_hint.apply_tick(rt, 21.0, {"flow_temperature": 26.0}, {}, now=T0 + TICK) is False
    assert waerme_hint.apply_tick(rt, 21.0, {}, {"flow_setpoint": 38.0}, now=T0 + 2 * TICK) is False
    rt.notifier.notify.assert_not_called()


def test_a_storage_error_does_not_stop_the_detection(make_store, monkeypatch):
    rt = _rt(make_store())

    def broken(*args, **kwargs):
        raise StorageError("Datentraeger voll")

    # StateStore.update aendert den Zustand im Speicher zuerst und reicht erst dann den Schreibfehler weiter.
    monkeypatch.setattr(rt.store, "_save", broken)
    assert _feed(rt, T0, T0 + timedelta(hours=4)) is True     # Flag im Speicher gesetzt, nichts wirft
    assert rt.store.state.waerme_fehlt_seit == "2026-09-30T05:11:00+02:00"


def test_a_failing_notifier_does_not_stop_the_detection(make_store):
    rt = _rt(make_store())
    rt.notifier.notify.side_effect = RuntimeError("HA nicht erreichbar")
    assert _feed(rt, T0, T0 + timedelta(hours=4)) is True     # Flag gesetzt, Fehler nur geloggt

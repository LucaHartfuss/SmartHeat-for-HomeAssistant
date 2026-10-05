"""Einzelfuehler-Ausfall (Spec TP6 3.5, Praezisierung 5)."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import requests
from fakes import runtime_config

from heizungsbruecke.ha_signals import HaSignalSource
from heizungsbruecke.ha_sinks import HaNotifySink
from heizungsbruecke.notifier import Notifier
from heizungsbruecke.room_sensors import check_room_sensors


def _http_error(status_code):
    response = MagicMock(status_code=status_code)
    error = requests.HTTPError(f"{status_code}")
    error.response = response
    return error


def _rt(make_store, values, room_sensors, store=None):
    ha_api = MagicMock()
    store = store if store is not None else make_store()

    def _get_state(ref):
        value = values[ref]
        if isinstance(value, Exception):
            raise value
        return value

    ha_api.get_state.side_effect = _get_state
    return SimpleNamespace(
        config=runtime_config(room_sensor_refs=tuple(room_sensors)), signals=HaSignalSource(ha_api), ha_api=ha_api,
        store=store,
        notifier=Notifier(store, HaNotifySink(ha_api, ["notify.mobile_app_a"])),
    )


def _texts(rt):
    return [c.args[1] for c in rt.ha_api.send_notification.call_args_list]


def test_single_failed_sensor_is_reported_with_remaining_count(make_store):
    """Review Focus 5."""
    values = {"sensor.a": 21.0, "climate.b::current_temperature": ValueError("unavailable"), "sensor.c": 20.0}
    rt = _rt(make_store, values, values.keys())

    check_room_sensors(rt)
    check_room_sensors(rt)

    assert _texts(rt) == ["SmartHeat: Raumfühler climate.b liefert keine Werte, Mittelwert aus 2 Fühlern."]
    rt.ha_api.create_persistent_notification.assert_not_called()


def test_implausible_value_counts_as_failed_and_recovery_is_reported(make_store):
    values = {"sensor.a": 21.0, "sensor.b": 0.0}
    rt = _rt(make_store, values, values.keys())
    check_room_sensors(rt)
    check_room_sensors(rt)

    values["sensor.b"] = 20.5
    check_room_sensors(rt)

    assert _texts(rt)[-1] == "SmartHeat: Raumfühler sensor.b liefert wieder Werte."


def test_all_failed_says_so_instead_of_mean_of_zero(make_store):
    values = {"sensor.a": ValueError("x"), "sensor.b": KeyError("current_temperature")}
    rt = _rt(make_store, values, values.keys())

    check_room_sensors(rt)
    check_room_sensors(rt)

    texts = _texts(rt)
    assert len(texts) == 2
    assert all("Kein Raumfühler liefert mehr gültige Werte" in text for text in texts)


def test_single_configured_sensor_is_left_to_the_data_error(make_store):
    rt = _rt(make_store, {"sensor.a": ValueError("x")}, ["sensor.a"])

    check_room_sensors(rt)

    rt.ha_api.get_state.assert_not_called()
    rt.ha_api.send_notification.assert_not_called()


def test_unreachable_ha_changes_nothing(make_store):
    values = {"sensor.a": requests.ConnectionError("weg"), "sensor.b": ValueError("x")}
    rt = _rt(make_store, values, values.keys())

    check_room_sensors(rt)

    rt.ha_api.send_notification.assert_not_called()


def test_deleted_room_sensor_404_is_reported_like_any_failed_sensor(make_store):
    """P17: eine geloeschte Raumfuehler-Entity (HTTP 404) zaehlt wie jeder andere Ausfall,
    ist also kein 'HA nicht erreichbar', das die Runde ohne Aenderung abbricht."""
    values = {"sensor.a": 21.0, "sensor.b": _http_error(404)}
    rt = _rt(make_store, values, values.keys())

    check_room_sensors(rt)
    check_room_sensors(rt)

    assert _texts(rt) == ["SmartHeat: Raumfühler sensor.b liefert keine Werte, Mittelwert aus 1 Fühler."]


def test_non_404_http_error_stops_the_round_without_changes(make_store):
    values = {"sensor.a": 21.0, "sensor.b": _http_error(500)}
    rt = _rt(make_store, values, values.keys())

    check_room_sensors(rt)

    rt.ha_api.send_notification.assert_not_called()


def test_one_failed_round_is_not_reported(make_store):
    """M1: nach einem Host-Neustart (Zigbee laedt noch) oder bei einer kurzen Funkstoerung
    faellt ein Fuehler eine Runde aus -- das allein ist noch keine Meldung."""
    values = {"sensor.a": 21.0, "sensor.b": ValueError("unavailable")}
    rt = _rt(make_store, values, values.keys())

    check_room_sensors(rt)
    values["sensor.b"] = 20.5
    check_room_sensors(rt)
    values["sensor.b"] = ValueError("unavailable")
    check_room_sensors(rt)

    rt.ha_api.send_notification.assert_not_called()


def test_second_consecutive_failed_round_is_reported(make_store):
    values = {"sensor.a": 21.0, "sensor.b": ValueError("unavailable")}
    rt = _rt(make_store, values, values.keys())

    check_room_sensors(rt)
    rt.ha_api.send_notification.assert_not_called()
    check_room_sensors(rt)
    check_room_sensors(rt)

    assert _texts(rt) == ["SmartHeat: Raumfühler sensor.b liefert keine Werte, Mittelwert aus 1 Fühler."]


def test_recovery_is_reported_in_the_first_good_round(make_store):
    values = {"sensor.a": 21.0, "sensor.b": ValueError("unavailable")}
    rt = _rt(make_store, values, values.keys())
    check_room_sensors(rt)
    check_room_sensors(rt)

    values["sensor.b"] = 20.5
    check_room_sensors(rt)

    assert _texts(rt)[-1] == "SmartHeat: Raumfühler sensor.b liefert wieder Werte."
    assert len(_texts(rt)) == 2


def test_reported_failure_is_kept_through_the_first_round_after_restart(make_store):
    """Nach einem Neustart zaehlt die Entprellung neu; ein schon gemeldeter Ausfall darf dabei
    nicht als 'wieder Werte' enden, solange der Fuehler weiter ausfaellt."""
    store = make_store()
    values = {"sensor.a": 21.0, "sensor.b": ValueError("unavailable")}
    rt = _rt(make_store, values, values.keys(), store=store)
    check_room_sensors(rt)
    check_room_sensors(rt)
    store.update(room_sensor_misses={})  # Laufzeitfeld, nach Neustart leer

    rt.ha_api.reset_mock()
    check_room_sensors(rt)
    check_room_sensors(rt)

    rt.ha_api.send_notification.assert_not_called()


def test_unreachable_ha_keeps_the_failed_round_count(make_store):
    values = {"sensor.a": 21.0, "sensor.b": ValueError("unavailable")}
    rt = _rt(make_store, values, values.keys())
    check_room_sensors(rt)

    values["sensor.a"] = requests.ConnectionError("weg")
    check_room_sensors(rt)
    values["sensor.a"] = 21.0
    check_room_sensors(rt)

    assert len(_texts(rt)) == 1


def test_plural_text_with_two_remaining_sensors(make_store):
    values = {"sensor.a": 21.0, "sensor.b": ValueError("x"), "sensor.c": 20.0}
    rt = _rt(make_store, values, values.keys())

    check_room_sensors(rt)
    check_room_sensors(rt)

    assert _texts(rt) == ["SmartHeat: Raumfühler sensor.b liefert keine Werte, Mittelwert aus 2 Fühlern."]


def test_a_deleted_room_sensor_counts_as_failed_and_an_unreachable_ha_ends_the_round(make_store):
    values = {"sensor.a": 21.0, "sensor.weg": _http_error(404)}
    rt = _rt(make_store, values, values.keys())
    check_room_sensors(rt)
    check_room_sensors(rt)
    assert _texts(rt) == ["SmartHeat: Raumfühler sensor.weg liefert keine Werte, Mittelwert aus 1 Fühler."]

    values["sensor.a"] = requests.ConnectionError("HA weg")
    misses = dict(rt.store.state.room_sensor_misses)
    check_room_sensors(rt)

    assert len(_texts(rt)) == 1
    assert rt.store.state.room_sensor_misses == misses

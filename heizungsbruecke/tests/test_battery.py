"""Batterieueberwachung (Spec TP6 3.5): Hysterese 20/25 %, binary_sensor, unavailable ohne Wechsel."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
from fakes import runtime_config

from heizungsbruecke.battery import STATE_LOW, check_batteries, next_state
from heizungsbruecke.ha_signals import HaSignalSource
from heizungsbruecke.notifier import Notifier


@pytest.mark.parametrize("previous,raw,expected", [
    ("ok", "19.9", STATE_LOW), ("ok", "20", "ok"), ("ok", "24", "ok"),
    (STATE_LOW, "24.9", STATE_LOW), (STATE_LOW, "25", "ok"), ("ok", "nan", "ok"), ("ok", "leer", "ok"),
])
def test_percent_sensor_hysteresis(previous, raw, expected):
    assert next_state(previous, "sensor.wz_battery", raw) == expected


@pytest.mark.parametrize("previous,raw,expected", [
    ("ok", "on", STATE_LOW), (STATE_LOW, "off", "ok"), (STATE_LOW, "komisch", STATE_LOW),
])
def test_binary_sensor(previous, raw, expected):
    assert next_state(previous, "binary_sensor.wz_battery_low", raw) == expected


def _http_error(status_code):
    response = MagicMock(status_code=status_code)
    error = requests.HTTPError(f"{status_code}")
    error.response = response
    return error


def _rt(make_store, raw_states, battery_entities=("sensor.wz_battery",)):
    ha_api = MagicMock()

    def _raw(entity_id):
        value = raw_states[entity_id]
        if isinstance(value, Exception):
            raise value
        return value

    ha_api.get_raw_state.side_effect = _raw
    store = make_store()
    return SimpleNamespace(
        config=runtime_config(battery_refs=tuple(battery_entities)), signals=HaSignalSource(ha_api), ha_api=ha_api,
        notifier=Notifier(store, ha_api, ["notify.mobile_app_a"]),
    )


def test_low_battery_is_pushed_once_and_recovery_once(make_store):
    states = {"sensor.wz_battery": "15"}
    rt = _rt(make_store, states)

    check_batteries(rt)
    check_batteries(rt)
    states["sensor.wz_battery"] = "22"
    check_batteries(rt)
    states["sensor.wz_battery"] = "80"
    check_batteries(rt)

    texts = [c.args[1] for c in rt.ha_api.send_notification.call_args_list]
    assert len(texts) == 2
    assert "sensor.wz_battery" in texts[0] and "15 %" in texts[0]
    assert "wieder in Ordnung" in texts[1]
    rt.ha_api.create_persistent_notification.assert_not_called()


def test_unavailable_battery_changes_nothing(make_store):
    rt = _rt(make_store, {"sensor.wz_battery": ValueError("unavailable")})

    check_batteries(rt)

    rt.ha_api.send_notification.assert_not_called()
    assert rt.notifier.state("batterie:sensor.wz_battery") == "ok"


def test_unreachable_ha_stops_the_round_without_changes(make_store):
    rt = _rt(make_store, {"sensor.a": requests.ConnectionError("weg"), "sensor.b": "5"},
             battery_entities=("sensor.a", "sensor.b"))

    check_batteries(rt)

    rt.ha_api.send_notification.assert_not_called()


def test_deleted_battery_entity_404_is_skipped_without_stopping_the_round(make_store):
    """P17: eine geloeschte Batterie-Entity (HTTP 404) ist kein 'HA nicht erreichbar' -- nur
    dieser Batterie-Check wird uebersprungen, die uebrigen laufen weiter."""
    rt = _rt(make_store, {"sensor.a": _http_error(404), "sensor.b": "15"},
             battery_entities=("sensor.a", "sensor.b"))

    check_batteries(rt)

    texts = [c.args[1] for c in rt.ha_api.send_notification.call_args_list]
    assert len(texts) == 1
    assert "sensor.b" in texts[0]
    assert rt.notifier.state("batterie:sensor.a") == "ok"


def test_non_404_http_error_stops_the_round_without_changes(make_store):
    rt = _rt(make_store, {"sensor.a": _http_error(500), "sensor.b": "15"},
             battery_entities=("sensor.a", "sensor.b"))

    check_batteries(rt)

    rt.ha_api.send_notification.assert_not_called()


def test_a_deleted_battery_entity_is_skipped_and_an_unreachable_ha_ends_the_round(make_store):
    states = {"sensor.weg_battery": _http_error(404), "sensor.wz_battery": "10"}
    rt = _rt(make_store, states, battery_entities=("sensor.weg_battery", "sensor.wz_battery"))

    check_batteries(rt)
    assert rt.notifier.state("batterie:sensor.wz_battery") == STATE_LOW

    states["sensor.weg_battery"] = requests.ConnectionError("HA weg")
    states["sensor.wz_battery"] = "80"
    check_batteries(rt)

    assert rt.notifier.state("batterie:sensor.wz_battery") == STATE_LOW  # Runde abgebrochen, keine Entwarnung
    assert len(rt.ha_api.send_notification.call_args_list) == 1

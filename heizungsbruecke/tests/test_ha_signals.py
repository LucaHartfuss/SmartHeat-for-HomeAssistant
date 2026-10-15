"""HaSignalSource uebersetzt die Fehler der HA-REST-API in die Fehler des Ports (Plan SHG G1, Praezisierung 2)."""
import pytest
import requests

from heizungsbruecke.ha_signals import HaSignalSource
from smartheat_runtime.ports import SignalNotFound, SourceUnavailable


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(response=response)


class _Ha:
    def __init__(self, error=None, value=21.5, raw="heat"):
        self.error, self.value, self.raw = error, value, raw
        self.attribute_calls = []

    def get_attribute(self, entity_id, attribute):
        self.attribute_calls.append((entity_id, attribute))
        if self.error is not None:
            raise self.error
        return "kWh"

    def get_state(self, ref):
        if self.error is not None:
            raise self.error
        return self.value

    def get_raw_state(self, ref):
        if self.error is not None:
            raise self.error
        return self.raw


def test_values_pass_through():
    signals = HaSignalSource(_Ha())
    assert signals.get_state("sensor.x") == 21.5
    assert signals.get_raw_state("sensor.x") == "heat"


def test_get_unit_reads_the_unit_attribute_of_the_entity():
    ha = _Ha()
    assert HaSignalSource(ha).get_unit("sensor.x") == "kWh"
    assert HaSignalSource(ha).get_unit("sensor.x::state") == "kWh"
    assert ha.attribute_calls == [("sensor.x", "unit_of_measurement")] * 2


def test_get_unit_translates_errors_and_keeps_a_missing_attribute_a_value_error():
    with pytest.raises(SignalNotFound):
        HaSignalSource(_Ha(error=_http_error(404))).get_unit("sensor.x")
    with pytest.raises(SourceUnavailable):
        HaSignalSource(_Ha(error=requests.Timeout("langsam"))).get_unit("sensor.x")
    with pytest.raises(ValueError):
        HaSignalSource(_Ha(error=ValueError("kein Attribut"))).get_unit("sensor.x")


def test_the_geraet_signals_have_no_unit_for_the_target_reference():
    from heizungsbruecke.ha_signals import SOLL_REF, GeraetSignale

    signals = GeraetSignale(HaSignalSource(_Ha()), lambda: None)
    assert signals.get_unit("sensor.x") == "kWh"
    with pytest.raises(ValueError):
        signals.get_unit(SOLL_REF)


@pytest.mark.parametrize("read", ["get_state", "get_raw_state"])
def test_404_is_signal_not_found(read):
    with pytest.raises(SignalNotFound):
        getattr(HaSignalSource(_Ha(error=_http_error(404))), read)("sensor.x")


@pytest.mark.parametrize("error", [_http_error(500), requests.ConnectionError("weg"), requests.Timeout("langsam")])
def test_other_request_errors_are_source_unavailable(error):
    with pytest.raises(SourceUnavailable):
        HaSignalSource(_Ha(error=error)).get_state("sensor.x")


@pytest.mark.parametrize("error", [ValueError("unavailable"), KeyError("temperature"), TypeError("None")])
def test_invalid_values_keep_their_error(error):
    with pytest.raises(type(error)):
        HaSignalSource(_Ha(error=error)).get_state("sensor.x")


def test_device_key_is_the_entity_without_attribute():
    source = HaSignalSource(object())
    assert source.device_key("climate.wz::current_temperature") == "climate.wz"
    assert source.device_key("sensor.a") == "sensor.a"

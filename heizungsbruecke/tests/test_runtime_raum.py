"""Raum und Startwert, beide Hosts (Spec Stufe 3 S6 4.2/4.3)."""
import pytest

from smartheat_runtime.raum import RaumPublisher
from smartheat_runtime.soll_speicher import DEFAULT_RAUM_SOLL, bedienung_start


class _Store:
    value, source = 21.5, "portal"


class _Signals:
    def __init__(self, fehler=None):
        self.fehler = fehler

    def get_state(self, ref):
        if self.fehler:
            raise self.fehler
        return 20.4


@pytest.mark.parametrize("fehler", [None, RuntimeError("x"), OSError("y"), KeyError("z")])
def test_der_raum_wird_abgelegt_und_ein_lesefehler_ist_kein_ist(fehler):
    abgelegt = []
    RaumPublisher(abgelegt.append, _Signals(fehler), _Store(), "sensor.raum", lambda: "ts").publish()
    assert abgelegt == [{"ist": None if fehler else 20.4, "soll": 21.5, "soll_quelle": "portal", "ts": "ts"}]


def test_ohne_fuehler_kein_ist():
    abgelegt = []
    RaumPublisher(abgelegt.append, _Signals(), _Store(), None, lambda: "ts").publish()
    assert abgelegt[0]["ist"] is None


@pytest.mark.parametrize(("inhalt", "start"), [
    ({"raum_soll": 22}, 22.0), ({"raum_soll": 22.5}, 22.5), ({"raum_soll": 15.0}, 15.0), ({"raum_soll": 25.0}, 25.0),
    ({"raum_soll": 30.0}, None), ({"raum_soll": 14.5}, None), ({"raum_soll": 22.3}, None), ({"raum_soll": True}, None),
    ({"raum_soll": "22"}, None), ({}, None), (None, None), ("kaputt", None),
])
def test_startwert_nur_aus_einer_gueltigen_bedienung(inhalt, start):
    assert bedienung_start(inhalt) == start
    assert start is None or type(start) is float


def test_der_standard_ist_20_grad():
    assert DEFAULT_RAUM_SOLL == 20.0

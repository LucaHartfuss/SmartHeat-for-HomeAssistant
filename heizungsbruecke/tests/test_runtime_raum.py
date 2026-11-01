"""Raum und Startwert, beide Hosts."""
import pytest

from smartheat_device import wire
from smartheat_runtime.raum import RaumPublisher
from smartheat_runtime.soll_speicher import DEFAULT_RAUM_SOLL, KREIS, TargetStore, bedienung_start, raum_soll_von


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


_NICHT_ENDLICH = [float("nan"), float("inf"), float("-inf"), 10**400]
_IDS = ["nan", "inf", "-inf", "riesig"]


class _FesterStore:
    def __init__(self, value, source="portal"):
        self.value, self.source = value, source


@pytest.mark.parametrize("wert", _NICHT_ENDLICH, ids=_IDS)
def test_ein_nicht_endliches_ist_wird_zu_none_die_uebrigen_felder_bleiben(wert):
    """Ein Fuehler mit NaN/Inf legt den Raum nicht lahm, nur ist faellt weg."""
    class Signals:
        def get_state(self, ref):
            return wert
    abgelegt = []
    RaumPublisher(abgelegt.append, Signals(), _Store(), "sensor.raum", lambda: "ts").publish()
    assert abgelegt == [{"ist": None, "soll": 21.5, "soll_quelle": "portal", "ts": "ts"}]


@pytest.mark.parametrize("wert", _NICHT_ENDLICH, ids=_IDS)
def test_ein_nicht_endliches_soll_wird_zu_none_ist_und_quelle_bleiben(wert):
    abgelegt = []
    RaumPublisher(abgelegt.append, _Signals(), _FesterStore(wert), "sensor.raum", lambda: "ts").publish()
    assert abgelegt == [{"ist": 20.4, "soll": None, "soll_quelle": "portal", "ts": "ts"}]


def _bed(raum_soll, kreis=1):
    return {"kreise": [{"kreis": kreis, "modus": "manuell", "raum_soll": raum_soll}]}


@pytest.mark.parametrize(("inhalt", "start"), [
    (_bed(22), 22.0), (_bed(22.5), 22.5), (_bed(15.0), 15.0), (_bed(25.0), 25.0),
    (_bed(30.0), None), (_bed(14.5), None), (_bed(22.3), None), (_bed(True), None),
    (_bed("22"), None), ({}, None), (None, None), ("kaputt", None),
    ({"kreise": []}, None), ({"kreise": "x"}, None), ({"kreise": [1]}, None),
])
def test_startwert_nur_aus_einer_gueltigen_bedienung(inhalt, start):
    assert bedienung_start(inhalt) == start
    assert start is None or type(start) is float


def test_der_kreis_des_hosts_ist_der_erste_kreis():
    assert KREIS == wire.KREIS_MIN


def test_der_host_liest_nur_den_eintrag_seines_kreises():
    zwei = {"kreise": [{"kreis": 1, "modus": "manuell", "raum_soll": 21.0},
                       {"kreis": 2, "modus": "manuell", "raum_soll": 24.0}]}
    assert raum_soll_von(zwei) == 21.0 and raum_soll_von(zwei, 2) == 24.0
    assert raum_soll_von(zwei, 3) is None
    assert bedienung_start(zwei) == 21.0
    nur_zweiter = {"kreise": [{"kreis": 2, "modus": "manuell", "raum_soll": 24.0}]}
    assert raum_soll_von(nur_zweiter) is None and bedienung_start(nur_zweiter) is None


def test_der_standard_ist_20_grad():
    assert DEFAULT_RAUM_SOLL == 20.0


def test_soll_quelle_im_raum_bleibt_bei_der_bestaetigung_desselben_werts(tmp_path, clock):
    """Der Raum fuer up/status meldet nach einer Bestaetigung des Servers mit demselben
    Wert weiter die Quelle des Wunsches, erst ein anderer Wert des Servers meldet portal."""
    store = TargetStore(tmp_path / "room_target.json", 20.0, clock, lambda: "ts")
    abgelegt = []
    raum = RaumPublisher(abgelegt.append, _Signals(), store, "sensor.raum", lambda: "ts")
    store.apply_thermostat(21.5)
    raum.publish()
    assert abgelegt[-1]["soll_quelle"] == "thermostat"

    store.apply_portal(21.5)
    raum.publish()
    assert (abgelegt[-1]["soll"], abgelegt[-1]["soll_quelle"]) == (21.5, "thermostat")

    store.apply_portal(22.0)
    raum.publish()
    assert (abgelegt[-1]["soll"], abgelegt[-1]["soll_quelle"]) == (22.0, "portal")

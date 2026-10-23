"""Ausfall der Aussentemperatur (R3-1): dauerhaft ohne gueltigen Wert -> eine Kundenmeldung nach derselben Rundenzahl
wie bei Raumfuehlern, Rueckkehr entwarnt sofort."""
import logging
from types import SimpleNamespace

import pytest
from fakes import FREMDTEXT, FREMDTEXT_IM_LOG, FakeNotifySink, logzeilen

from smartheat_runtime import aussentemperatur, room_sensors
from smartheat_runtime.notifier import STATE_OK, Notifier
from smartheat_runtime.ports import SignalNotFound, SignalUngueltig, SourceUnavailable
from smartheat_runtime.roles import ChannelManifest
from smartheat_runtime.texts import HA_TEXTS

REF = "sensor.aussen"


class _Signale:
    """Signalquelle mit einem einzigen, von Hand gesetzten Wert; ein Fehler als Wert wird geworfen."""

    def __init__(self, value) -> None:
        self.value = value
        self.reads: list[str] = []

    def get_state(self, ref):
        self.reads.append(ref)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def _rt(make_store, value, refs=None):
    store = make_store()
    kunde = FakeNotifySink()
    return SimpleNamespace(
        manifest=ChannelManifest(refs={"outdoor_temp": REF} if refs is None else refs), signals=_Signale(value),
        store=store, notifier=Notifier(store, kunde), kunde=kunde, texts=HA_TEXTS,
    )


def _rounds(rt, n):
    for _ in range(n):
        aussentemperatur.check(rt)


def test_dauerhaft_fehlende_aussentemperatur_meldet_einmal(make_store):
    rt = _rt(make_store, SignalUngueltig("unavailable"))

    _rounds(rt, room_sensors.FAILED_ROUNDS - 1)
    assert rt.kunde.pushes == []  # eine kurze Stoerung (Zigbee/Wetterdienst laedt noch) bleibt still
    _rounds(rt, 1)
    assert [key for key, _text in rt.kunde.pushes] == ["aussentemperatur"]
    assert rt.kunde.shown == {}  # nicht kritisch: nur ein Push, keine offene Meldung
    assert rt.notifier.state("aussentemperatur") == aussentemperatur.STATE_FAILED

    _rounds(rt, 5)
    assert len(rt.kunde.pushes) == 1  # weiter ausgefallen: keine Wiederholung

    rt.signals.value = 7.5
    aussentemperatur.check(rt)  # Rueckkehr sofort, schon nach einer Runde
    assert [key for key, _text in rt.kunde.pushes] == ["aussentemperatur"] * 2
    assert "wieder" in rt.kunde.pushes[-1][1]
    assert rt.notifier.state("aussentemperatur") == STATE_OK


@pytest.mark.parametrize("value", [
    SignalNotFound("404"), SignalUngueltig("x"), ValueError("x"), KeyError("temperature"), TypeError("x"),
    -40.1, 45.1, float("nan"), float("inf"), True, None,
], ids=repr)
def test_ungueltiger_wert_zaehlt_als_ausfall(make_store, value):
    rt = _rt(make_store, value)
    _rounds(rt, room_sensors.FAILED_ROUNDS)
    assert [key for key, _text in rt.kunde.pushes] == ["aussentemperatur"]


@pytest.mark.parametrize("value", [-40.0, 0.0, 45.0])
def test_gueltige_aussentemperatur_bleibt_still(make_store, value):
    rt = _rt(make_store, value)
    _rounds(rt, 10)
    assert rt.kunde.pushes == [] and rt.store.state.outdoor_misses == 0


def test_eine_gueltige_runde_setzt_den_zaehler_zurueck(make_store):
    rt = _rt(make_store, SignalUngueltig("x"))
    _rounds(rt, room_sensors.FAILED_ROUNDS - 1)
    rt.signals.value = 3.0
    aussentemperatur.check(rt)
    rt.signals.value = SignalUngueltig("x")
    _rounds(rt, room_sensors.FAILED_ROUNDS - 1)
    assert rt.kunde.pushes == []  # nicht aufeinanderfolgend: kein Ausfall


def test_unerreichbare_quelle_aendert_nichts(make_store, caplog):
    rt = _rt(make_store, SignalUngueltig("x"))
    aussentemperatur.check(rt)
    rt.signals.value = SourceUnavailable(FREMDTEXT)
    with caplog.at_level(logging.WARNING):
        _rounds(rt, 5)
    assert (f"Aussentemperatur-Pruefung abgebrochen, {HA_TEXTS.source_unavailable_log}: {FREMDTEXT_IM_LOG}"
            in logzeilen(caplog))
    assert rt.kunde.pushes == [] and rt.store.state.outdoor_misses == 1  # die Runde bricht ab, zaehlt nicht mit
    rt.signals.value = SignalUngueltig("x")
    aussentemperatur.check(rt)
    assert [key for key, _text in rt.kunde.pushes] == ["aussentemperatur"]


def test_ohne_zugeordnete_aussentemperatur_wird_nichts_gelesen(make_store):
    rt = _rt(make_store, SignalUngueltig("x"), refs={"room_actual": "sensor.raum"})
    _rounds(rt, 5)
    assert rt.signals.reads == [] and rt.kunde.pushes == []


def test_schon_gemeldeter_ausfall_bleibt_nach_dem_neustart_bis_zur_rueckkehr(make_store):
    """Der Zaehler ist Laufzeit (nicht persistiert), der Meldezustand uebersteht den Neustart: derselbe Ausfall wird
    nicht erneut gemeldet, die Rueckkehr entwarnt."""
    rt = _rt(make_store, SignalUngueltig("x"))
    _rounds(rt, room_sensors.FAILED_ROUNDS)
    rt.store.update(outdoor_misses=0)  # Neustart: Zaehler leer
    _rounds(rt, 3)
    assert len(rt.kunde.pushes) == 1
    rt.signals.value = 12.0
    aussentemperatur.check(rt)
    assert len(rt.kunde.pushes) == 2

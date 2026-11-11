"""Port der Uhr in der Laufzeit (smartheat_runtime/uhr.py): nur ein echtes True gilt als synchron, jeder Fehler und
jede Verspaetung ist "nicht synchron", der Zwischenspeicher haelt den Takt frei."""
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fakes import FakeClock

from smartheat_runtime import uhr
from smartheat_runtime.uhr import UhrPort


class Abfrage:
    """Rueckruf des Geraete-Hosts mit Zaehler."""

    def __init__(self, antwort=True) -> None:
        self.antwort = antwort
        self.aufrufe = 0

    def __call__(self):
        self.aufrufe += 1
        if isinstance(self.antwort, BaseException):
            raise self.antwort
        return self.antwort


@pytest.mark.parametrize("antwort,erwartet", [(True, True), (False, False), (None, False), ("ja", False), (1, False),
                                              (0, False), ([], False)])
def test_nur_ein_echtes_true_gilt_als_synchron(antwort, erwartet):
    assert UhrPort(Abfrage(antwort), clock=FakeClock()).uhr_synchron() is erwartet


@pytest.mark.parametrize("fehler", [OSError("weg"), RuntimeError("kaputt"), ValueError("x"), KeyError("k")])
def test_wirft_der_rueckruf_gilt_die_uhr_als_nicht_synchron(fehler, caplog):
    assert UhrPort(Abfrage(fehler), clock=FakeClock()).uhr_synchron() is False
    assert type(fehler).__name__ in caplog.text and str(fehler) not in caplog.text  # nur die Fehlerart ins Log


def test_der_zwischenspeicher_haelt_den_rueckruf_bis_zum_ablauf_fern():
    uhr_zeit, abfrage = FakeClock(), Abfrage(True)
    port = UhrPort(abfrage, clock=uhr_zeit, zwischenspeicher_s=30)
    assert [port.uhr_synchron() for _ in range(3)] == [True] * 3
    uhr_zeit.advance(29)
    assert port.uhr_synchron() is True and abfrage.aufrufe == 1
    abfrage.antwort = False
    uhr_zeit.advance(1)  # genau die Zwischenspeicherzeit: abgelaufen
    assert port.uhr_synchron() is False and abfrage.aufrufe == 2


def test_ein_fehler_wird_bis_zum_ablauf_nicht_wiederholt_und_danach_erholt_sich_die_uhr():
    uhr_zeit, abfrage = FakeClock(), Abfrage(OSError("weg"))
    port = UhrPort(abfrage, clock=uhr_zeit, zwischenspeicher_s=30)
    assert port.uhr_synchron() is False and port.uhr_synchron() is False and abfrage.aufrufe == 1
    abfrage.antwort = True
    uhr_zeit.advance(30)
    assert port.uhr_synchron() is True


class Haengend:
    """Rueckruf, der erst nach freigeben() antwortet."""

    def __init__(self) -> None:
        self.frei = threading.Event()
        self.aufrufe = 0

    def __call__(self):
        self.aufrufe += 1
        self.frei.wait(10)
        return True


def test_kein_aufruf_haelt_den_takt_laenger_als_die_zwischenspeicherzeit():
    abfrage = Haengend()
    port = UhrPort(abfrage, clock=FakeClock(), zwischenspeicher_s=0.3, wartezeit_s=5)  # die Wartezeit begrenzt die Zeit
    try:
        start = time.monotonic()
        assert port.uhr_synchron() is False  # unbekannt, solange die Antwort fehlt
        erster = time.monotonic() - start
        assert erster < 0.3 + 0.2
        start = time.monotonic()
        assert port.uhr_synchron() is False  # die Abfrage haengt noch: kein Warten, keine zweite Abfrage
        assert time.monotonic() - start < 0.1
        assert abfrage.aufrufe == 1
    finally:
        abfrage.frei.set()
    bis = time.monotonic() + 5
    while not port.uhr_synchron() and time.monotonic() < bis:
        time.sleep(0.01)
    assert port.uhr_synchron() is True  # die verspaetete Antwort gilt, sobald sie da ist


def test_die_standardzeiten_halten_den_takt_frei():
    assert 0 < uhr.UHR_WARTEZEIT_S <= uhr.UHR_ZWISCHENSPEICHER_S


def test_eine_schnelle_antwort_kommt_schon_beim_ersten_aufruf():
    start = time.monotonic()
    assert UhrPort(Abfrage(True), clock=FakeClock()).uhr_synchron() is True
    assert time.monotonic() - start < uhr.UHR_WARTEZEIT_S


class Wand:
    """Fake-Wanduhr (setzt wallclock._now)."""

    def __init__(self, monkeypatch, start) -> None:
        self.jetzt = start
        monkeypatch.setattr("smartheat_core.wallclock._now", lambda: self.jetzt)


def _vertrauen(monkeypatch, antwort=True):
    clock = FakeClock()
    wand = Wand(monkeypatch, datetime(2026, 11, 11, 12, 0, tzinfo=UTC))
    abfrage = Abfrage(antwort)
    return uhr.UhrVertrauen(abfrage, clock=clock), clock, wand, abfrage


def test_vertrauen_gibt_es_erst_nach_einer_synchronen_meldung(monkeypatch):
    vertrauen, _, _, _ = _vertrauen(monkeypatch, antwort=False)
    assert vertrauen() is False


def test_vertrauen_haelt_bei_gleichlaufender_wand_und_monotoner_uhr(monkeypatch):
    vertrauen, clock, wand, abfrage = _vertrauen(monkeypatch)
    assert vertrauen() is True
    abfrage.antwort = False  # der Zeitdienst meldet nicht mehr, der Beleg bleibt
    clock.advance(3600)
    wand.jetzt += timedelta(hours=1)
    assert vertrauen() is True


def test_vertrauen_geht_bei_einem_wanduhrsprung_verloren(monkeypatch):
    vertrauen, clock, wand, abfrage = _vertrauen(monkeypatch)
    assert vertrauen() is True
    abfrage.antwort = False
    clock.advance(60)
    wand.jetzt += timedelta(days=40)  # Sprung vor: die 30 Tage waeren sonst "abgelaufen"
    assert vertrauen() is False

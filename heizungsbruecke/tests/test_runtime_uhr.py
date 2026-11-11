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
    """Rueckruf, der erst nach `frei.set()` antwortet (hoechstens 10 s); `beendet` zeigt, dass er zurueckgekehrt ist."""

    def __init__(self) -> None:
        self.frei = threading.Event()
        self.beendet = threading.Event()
        self.aufrufe = 0

    def __call__(self):
        self.aufrufe += 1
        self.frei.wait(10)
        self.beendet.set()
        return True


def test_kein_aufruf_haelt_den_takt_laenger_als_die_zwischenspeicherzeit():
    # Beurteilt wird nicht die Wanduhr, sondern ob der Aufruf zurueckkehrt, waehrend die Abfrage noch haengt.
    abfrage = Haengend()
    port = UhrPort(abfrage, clock=FakeClock(), zwischenspeicher_s=0.05, wartezeit_s=5)  # die Zwischenspeicherzeit begrenzt
    try:
        assert port.uhr_synchron() is False  # unbekannt, solange die Antwort fehlt
        assert not abfrage.beendet.is_set()
        assert port.uhr_synchron() is False  # die Abfrage haengt noch: keine zweite Abfrage
        assert not abfrage.beendet.is_set()
        assert abfrage.aufrufe == 1
    finally:
        abfrage.frei.set()
    assert abfrage.beendet.wait(5)
    bis = time.monotonic() + 5
    while not port.uhr_synchron() and time.monotonic() < bis:
        time.sleep(0.01)
    assert port.uhr_synchron() is True  # die verspaetete Antwort gilt, sobald sie da ist


def test_die_standardzeiten_halten_den_takt_frei():
    # Absolute Werte: leitete der Test die Schranke aus den Konstanten ab, bliebe eine verlaengerte Wartezeit unbemerkt.
    assert uhr.UHR_WARTEZEIT_S == 0.5
    assert uhr.UHR_ZWISCHENSPEICHER_S == 30.0


def test_eine_schnelle_antwort_kommt_schon_beim_ersten_aufruf():
    abfrage = Abfrage(True)
    assert UhrPort(abfrage, clock=FakeClock()).uhr_synchron() is True and abfrage.aufrufe == 1


def test_die_antwort_traegt_wand_und_monotone_zeit_ihres_eintreffens(monkeypatch):
    uhr_zeit = FakeClock()
    wand = Wand(monkeypatch, datetime(2026, 11, 11, 12, 0, tzinfo=UTC))
    port = UhrPort(Abfrage(True), clock=uhr_zeit, zwischenspeicher_s=30)
    erste = port.antwort()
    assert erste == uhr.UhrAntwort(True, datetime(2026, 11, 11, 12, 0, tzinfo=UTC), 1000.0)
    uhr_zeit.advance(10)
    wand.jetzt += timedelta(seconds=10)
    assert port.antwort() == erste  # zwischengespeichert: der Zeitpunkt der Antwort, nicht des Aufrufs
    assert port.uhr_synchron() is True


def test_ohne_synchrone_antwort_hat_die_antwort_keinen_zeitpunkt():
    for abfrage in (Abfrage(False), Abfrage(OSError("weg"))):
        assert UhrPort(abfrage, clock=FakeClock()).antwort() == uhr.UhrAntwort(False, None, None)


class Wand:
    """Fake-Wanduhr (setzt wallclock._now)."""

    def __init__(self, monkeypatch, start) -> None:
        self.jetzt = start
        monkeypatch.setattr("smartheat_core.wallclock._now", lambda: self.jetzt)


def _vertrauen(monkeypatch, antwort=True):
    clock = FakeClock()
    wand = Wand(monkeypatch, datetime(2026, 11, 11, 12, 0, tzinfo=UTC))
    abfrage = Abfrage(antwort)
    port = UhrPort(abfrage, clock=clock, zwischenspeicher_s=30)
    return uhr.UhrVertrauen(port.antwort, clock=clock), clock, wand, abfrage


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


def test_ein_wanduhrsprung_innerhalb_der_zwischenspeicherzeit_nimmt_das_vertrauen(monkeypatch):
    # Der Beleg stammt vom Zeitpunkt der Antwort, nicht vom Aufruf: das zwischengespeicherte "synchron" macht den
    # Sprung nicht zum neuen Beleg.
    vertrauen, clock, wand, abfrage = _vertrauen(monkeypatch)
    assert vertrauen() is True
    clock.advance(10)  # weniger als die Zwischenspeicherzeit: der Port fragt nicht neu
    wand.jetzt += timedelta(days=31)
    assert vertrauen() is False
    assert abfrage.aufrufe == 1


def test_nach_dem_sprung_gibt_die_naechste_synchrone_antwort_das_vertrauen_zurueck(monkeypatch):
    vertrauen, clock, wand, abfrage = _vertrauen(monkeypatch)
    assert vertrauen() is True
    clock.advance(10)
    wand.jetzt += timedelta(days=31)
    assert vertrauen() is False
    clock.advance(20)  # Zwischenspeicher abgelaufen, der Zeitdienst meldet die (nun richtige) Uhr als synchron
    assert vertrauen() is True and abfrage.aufrufe == 2

"""Wanduhr des Clients (Port Clock). Die monotone Uhr wird als Callable uebergeben; die
Wanduhr ist prozessweit, weil auch reine Kernfunktionen (Tagesbudget, Zeitstempel im Zustand) sie brauchen. Beide
Hosts nutzen die Zeitquelle (Systemzeit, in einem Testlauf um einen Versatz verschoben); Tests setzen `_now` per
monkeypatch."""
from collections.abc import Callable
from datetime import date, datetime
from typing import NamedTuple

from smartheat_core import zeitquelle


def _system_now() -> datetime:
    return zeitquelle.fachliche_wanduhr()


_now: Callable[[], datetime] = _system_now


def now() -> datetime:
    """Aktuelle Ortszeit mit Zeitzone (statt `datetime.now().astimezone()` an jeder Stelle)."""
    return _now()


def today() -> date:
    """Heutiges Datum in Ortszeit (statt `date.today()`)."""
    return _now().date()


#: so weit darf die Wanduhr gegen die monotone Uhr seit der letzten synchronen Meldung abweichen (Sekunden)
UHR_SPRUNG_TOLERANZ_S = 300.0


class UhrBeleg(NamedTuple):
    """Wand- und monotone Uhr im Augenblick der letzten Meldung "Zeitdienst synchron"."""

    wand: datetime
    mono: float


def beleg_fortschreiben(beleg: UhrBeleg | None, *, synchron: bool, wand_jetzt: datetime,
                        mono_jetzt: float) -> UhrBeleg | None:
    """Neuer Beleg, wenn der Zeitdienst jetzt synchron meldet, sonst der bisherige (None: seit dem Start nie synchron)."""
    return UhrBeleg(wand_jetzt, mono_jetzt) if synchron else beleg


def vertrauenswuerdig(beleg: UhrBeleg | None, *, wand_jetzt: datetime, mono_jetzt: float,
                      toleranz_s: float = UHR_SPRUNG_TOLERANZ_S) -> bool:
    """Ob die Wanduhr als Grundlage zeitabhaengiger Betriebsarten gilt (reine Funktion, liest keine Uhr).

    Voraussetzung ist eine synchrone Meldung seit dem Start (Beleg vorhanden). Seitdem darf die vergangene Wanduhrzeit
    von der vergangenen monotonen Zeit hoechstens um die Toleranz abweichen, ein Sprung vor oder zurueck nimmt das
    Vertrauen. Eine neue synchrone Meldung erneuert den Beleg und gibt es zurueck."""
    if beleg is None:
        return False
    # Ueber .timestamp(): die Differenz zweier aware datetimes mit demselben tzinfo-Objekt ignoriert die Offsets (Zeitumstellung).
    sprung_s = (wand_jetzt.timestamp() - beleg.wand.timestamp()) - (mono_jetzt - beleg.mono)
    return abs(sprung_s) <= toleranz_s

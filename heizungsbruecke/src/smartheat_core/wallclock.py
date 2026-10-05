"""Wanduhr des Clients (Port Clock, Spec SHG 3.2). Die monotone Uhr wird wie bisher als Callable uebergeben; die
Wanduhr ist prozessweit, weil auch reine Kernfunktionen (Tagesbudget, Zeitstempel im Zustand) sie brauchen. Beide
Hosts nutzen die Systemzeit; Tests setzen `_now` per monkeypatch."""
from collections.abc import Callable
from datetime import date, datetime


def _system_now() -> datetime:
    return datetime.now().astimezone()


_now: Callable[[], datetime] = _system_now


def now() -> datetime:
    """Aktuelle Ortszeit mit Zeitzone (frueher `datetime.now().astimezone()` an jeder Stelle)."""
    return _now()


def today() -> date:
    """Heutiges Datum in Ortszeit (frueher `date.today()`)."""
    return _now().date()

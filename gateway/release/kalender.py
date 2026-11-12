"""Kalenderrechnung der Release-Werkzeuge ohne Abhaengigkeiten (build_bundle.py laeuft auch ohne cryptography)."""
from datetime import datetime


def plus_monate(start: datetime, monate: int) -> datetime:
    """`monate` Kalendermonate spaeter, am Monatsende auf den letzten Tag des Zielmonats gekuerzt (31.1. + 1 = 28.2.)."""
    index = start.year * 12 + start.month - 1 + monate
    jahr, monat = divmod(index, 12)
    monat += 1
    for tag in range(start.day, 0, -1):
        try:
            return start.replace(year=jahr, month=monat, day=tag)
        except ValueError:
            continue
    raise AssertionError("unerreichbar: Tag 1 gibt es in jedem Monat")

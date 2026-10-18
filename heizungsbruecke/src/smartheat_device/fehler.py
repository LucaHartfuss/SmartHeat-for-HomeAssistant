"""Fehler an den Server (Spec Stufe 3 S5 6.1/6.2, G-6): Code und details, keine Texte. details nur mit den Schluesseln
aus wire.DETAIL_FIELDS -- feld aus wire.PRUEF_FELDER, regel aus wire.PRUEF_REGELN, die uebrigen endliche Zahlen. Den
Text bildet der Server (geraete/texte.py); lokale Anzeigen (Meldungen, Diagnoseseite, Log) haben eigene Texte."""
import json
import math
from collections.abc import Iterable

from smartheat_device import wire

ZAHLEN = tuple(key for key in wire.DETAIL_FIELDS if key not in ("feld", "regel"))


def _zahl(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def kodierbar(wert) -> bool:
    """Ein Wert aus dem Netz (json.loads) laesst sich als UTF-8 speichern. json.loads liefert aus "\\ud800" ein einzelnes
    Surrogat, das smartheat_core.dateien.schreiben_json (ensure_ascii=False) nicht kodieren kann: wer solche Texte
    annaehme, scheiterte beim Speichern mit einer Ausnahme statt mit einer Ablehnung (Testoffensive T2, Task 13)."""
    try:
        json.dumps(wert, ensure_ascii=False).encode()
    except UnicodeEncodeError:
        return False
    return True


def gueltig(details) -> bool:
    """details hat die Form des Vertrags; None (keine details) ist gueltig."""
    if details is None:
        return True
    if not isinstance(details, dict) or not set(details) <= set(wire.DETAIL_FIELDS):
        return False
    feld, regel = details.get("feld"), details.get("regel")
    return ((feld is None or (isinstance(feld, str) and feld in wire.PRUEF_FELDER))
            and (regel is None or (isinstance(regel, str) and regel in wire.PRUEF_REGELN))
            and all(_zahl(details[key]) for key in ZAHLEN if key in details))


def details(feld: str | None = None, regel: str | None = None, **zahlen: float) -> dict:
    """details fuer Failed, InvalidPayload und die Pruefungen der Hosts. Ein Verstoss gegen den Vertrag ist ein
    Programmierfehler (ValueError, nur die Schluessel in der Meldung, Regel 6)."""
    werte = {**({"feld": feld} if feld is not None else {}), **({"regel": regel} if regel is not None else {}), **zahlen}
    if not gueltig(werte):
        raise ValueError(f"details passen nicht zum Vertrag ({sorted(werte)})")
    return werte


def fremdes_feld(inhalt: dict, erlaubt: Iterable[str]) -> dict | None:
    """details fuer das erste (alphabetisch) Feld von `inhalt` ausserhalb von `erlaubt`; None, wenn es keines gibt.
    Der Name kommt vom Netz: er steht nur in details, wenn er ein Feld des Vertrags ist (wire.PRUEF_FELDER), sonst
    nur die Regel."""
    fremd = sorted(map(str, set(inhalt) - set(erlaubt)))
    return details(fremd[0] if fremd[0] in wire.PRUEF_FELDER else None, "unbekannt") if fremd else None


def body(grund: str, werte: dict | None) -> dict:
    """error von up/result (wire.ERROR_FIELDS): grund, details nur wenn vorhanden; nie ein Text."""
    return {"grund": grund, "details": dict(werte)} if werte else {"grund": grund}

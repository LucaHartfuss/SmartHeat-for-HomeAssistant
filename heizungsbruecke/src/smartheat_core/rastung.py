"""Rastung des Sicherheitssatzes (G-1; Spec Stufe 3 S3 Abschnitt 3): Hebelsatz x Verteilsystem der ersten
eingerichteten Konfiguration, im Geraeteordner neben der Identitaet (device/sicherheitssatz.json). Kein Dokument des
Servers loest sie, auch keine leere Konfiguration; sie geht nur mit den Geraetedaten (Add-on neu installieren, Gateway
neu aufsetzen). Eine kaputte oder manipulierte Datei (auch ein Symlink) sperrt: UNBEKANNT passt zu keiner Konfiguration
(der Client ist quelloffen; wer weiss, wie man die Datei beschaedigt, darf sie damit nicht loesen). Rein:
Standardbibliothek und smartheat_core."""
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from smartheat_core import dateien
from smartheat_core.safety import resolve_local_safety

logger = logging.getLogger(__name__)

DATEI = "sicherheitssatz.json"
TEXT = ("Hebelsatz und Verteilsystem sind auf diesem Gerät festgelegt. Ändern lassen sie sich nur, wenn das Add-on neu "
        "installiert bzw. das Gateway neu aufgesetzt wird.")
_MAX_BYTES = 4096


@dataclass(frozen=True)
class Rastung:
    hebelsatz: str
    verteilsystem: str


UNBEKANNT = Rastung("", "")


def _gesperrt(grund: str) -> Rastung:
    logger.error("Rastung des Sicherheitssatzes %s: jede Einrichtung wird abgelehnt, bis die Geraetedaten geloescht "
                 "werden", grund)
    return UNBEKANNT


def _json(pfad: Path):
    """Folgt keinem Symlink am letzten Namen (O_NOFOLLOW: ELOOP, auch bei einem ins Leere zeigenden Link)."""
    fd = os.open(pfad, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as datei:
        return json.loads(datei.read(_MAX_BYTES + 1).decode())


def lesen(pfad: Path) -> Rastung | None:
    """None: keine Rastung (Datei fehlt). UNBEKANNT: Datei da, aber Symlink, unlesbar, unvollstaendig oder ohne lokale
    Werte."""
    try:
        roh = _json(pfad)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError) as error:
        return _gesperrt(f"nicht lesbar ({type(error).__name__})")
    if not (isinstance(roh, dict) and isinstance(roh.get("hebelsatz"), str)
            and isinstance(roh.get("verteilsystem"), str)):
        return _gesperrt("unvollstaendig")
    try:
        resolve_local_safety(roh["hebelsatz"], roh["verteilsystem"])
    except ValueError:
        return _gesperrt("ohne lokale Sicherheitswerte")
    return Rastung(roh["hebelsatz"], roh["verteilsystem"])


def passt(rastung: Rastung | None, konfiguration: Mapping) -> bool:
    if rastung is None:
        return True
    paar = (konfiguration.get("hebelsatz"), konfiguration.get("verteilsystem"))
    return rastung != UNBEKANNT and paar == (rastung.hebelsatz, rastung.verteilsystem)


def schreiben(pfad: Path, rastung: Rastung, seit: str) -> None:
    dateien.schreiben_json(pfad, {"hebelsatz": rastung.hebelsatz, "verteilsystem": rastung.verteilsystem,
                                  "seit": seit}, modus=dateien.PRIVAT)

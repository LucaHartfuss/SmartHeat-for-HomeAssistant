"""Leer heisst abmelden (Audit 5, A5-23; Spec Stufe 3 S3 Abschnitt 4), beide Hosts gleich. Beim Wechsel von eingerichtet
auf leer schreibt der Host die Markierung aufraeumen.json mit der alten Konfiguration und startet neu. Der naechste
Start setzt mit ihr zurueck wie sign_off (Laufzeit im Modus abgemeldet); gelingt das, wird die Markierung
`zurueckgesetzt`, der Prozess startet neu, und erst dann loescht der Host den anlagengebundenen Zustand. So kann kein
Server-Pfad (device release, ersetzen, _fail) die Anlage auf SmartHeat-Werten zuruecklassen. Ohne gespeicherte
Konfiguration (Markierung aus 1.0.0, kaputt) wird geloescht wie bisher. Ist die Markierung da, aber nicht lesbar (EIO,
EACCES; auch ihr Vorhandensein ist dann nicht feststellbar), bleibt sie, und nichts wird geloescht (BEHALTEN, S3S3-
Marker): ein Lesefehler ist keine fehlende Konfiguration, der naechste Start versucht es erneut.

Bedrohungsmodell (Spec 2): die Markierung schreibt das Geraet selbst aus seiner gespeicherten, schon geprueften
Konfiguration; der Server liefert hier nichts. Nennt die Konfiguration der Markierung eine andere Anlage als die
Markierung, gilt sie als fehlend (loeschen wie bisher), und die Laufzeit prueft sie vor dem Zuruecksetzen ohnehin gegen
die Rastung (sign_off_parts)."""
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from smartheat_core import dateien
from smartheat_runtime.dokument_config import eingerichtet

logger = logging.getLogger(__name__)

KEINE, VERWERFEN, ZURUECKSETZEN, LOESCHEN, BEHALTEN = "keine", "verwerfen", "zuruecksetzen", "loeschen", "behalten"


@dataclass(frozen=True)
class Markierung:
    anlage: str
    konfiguration: dict | None
    zurueckgesetzt: bool


def markieren(pfad: Path, alt: Mapping) -> None:
    dateien.schreiben_json(pfad, {"anlage": alt["anlage"], "konfiguration": dict(alt), "zurueckgesetzt": False},
                           modus=dateien.PRIVAT)


def lesen(pfad: Path) -> Markierung | None:
    roh = dateien.lesen_json(pfad)
    if not isinstance(roh, dict) or not isinstance(roh.get("anlage"), str) or not roh["anlage"]:
        return None
    konfiguration = roh.get("konfiguration")
    passend = isinstance(konfiguration, dict) and konfiguration.get("anlage") == roh["anlage"]
    return Markierung(roh["anlage"], konfiguration if passend else None, roh.get("zurueckgesetzt") is True)


def _lesbar(pfad: Path) -> bool:
    """False, wenn die Markierung da ist, aber nicht gelesen werden kann (EIO, EACCES): dann bleibt sie, und nichts
    wird geloescht (S3S3-Marker). Verschwindet sie zwischendurch, entscheidet lesen() wie bisher."""
    try:
        pfad.read_bytes()
    except FileNotFoundError:
        return True
    except OSError as error:
        logger.warning("%s nicht lesbar (%s): Markierung bleibt, nichts wird geloescht", pfad.name,
                       type(error).__name__)
        return False
    return True


def entscheiden(pfad: Path, gespeichert) -> str:
    """Vor dem Start des Geraetekerns: KEINE, VERWERFEN (wieder eingerichtet, Zustand bleibt), ZURUECKSETZEN (Markierung
    bleibt, die Laufzeit setzt zurueck), LOESCHEN (Zustand und Markierung weg) oder BEHALTEN (Markierung unlesbar:
    nichts loeschen)."""
    try:
        da = pfad.exists()  # wirft bei EIO/EACCES im stat (nur fehlende Datei ergibt False)
    except OSError as error:
        logger.warning("%s nicht pruefbar (%s): Markierung bleibt, nichts wird geloescht", pfad.name,
                       type(error).__name__)
        return BEHALTEN
    if not da:
        return KEINE
    if eingerichtet(gespeichert):
        return VERWERFEN
    if not _lesbar(pfad):
        return BEHALTEN
    markierung = lesen(pfad)
    if markierung is None or markierung.konfiguration is None or markierung.zurueckgesetzt:
        return LOESCHEN
    return ZURUECKSETZEN


def laufzeit_konfiguration(gespeichert, pfad: Path) -> tuple[dict, bool]:
    """(Konfiguration fuer die Laufzeit, im Modus abgemeldet starten?). Steht das Zuruecksetzen aus, die alte
    Konfiguration aus der Markierung; sonst die gespeicherte (leer: {})."""
    if not eingerichtet(gespeichert):
        markierung = lesen(pfad)
        if markierung is not None and markierung.konfiguration is not None and not markierung.zurueckgesetzt:
            return dict(markierung.konfiguration), True
    return dict(gespeichert or {}), False


def zurueckgesetzt(pfad: Path, anlage: str) -> bool:
    """Nach gelungenem Zuruecksetzen: True, wenn damit die offene Markierung dieser Anlage erledigt ist."""
    markierung = lesen(pfad)
    if markierung is None or markierung.anlage != anlage or markierung.konfiguration is None \
            or markierung.zurueckgesetzt:
        return False
    dateien.schreiben_json(pfad, {"anlage": markierung.anlage, "konfiguration": markierung.konfiguration,
                                  "zurueckgesetzt": True}, modus=dateien.PRIVAT)
    return True

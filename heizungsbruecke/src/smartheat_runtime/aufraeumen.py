"""Leer heisst abmelden (Audit 5, A5-23; Spec Stufe 3 S3 Abschnitt 4), beide Hosts gleich. Beim Wechsel von eingerichtet
auf leer schreibt der Host die Markierung aufraeumen.json mit der alten Konfiguration und startet neu. Der naechste
Start setzt mit ihr zurueck wie sign_off (Laufzeit im Modus abgemeldet); gelingt das, wird die Markierung
`zurueckgesetzt`, der Prozess startet neu, und erst dann loescht der Host den anlagengebundenen Zustand. So kann kein
Server-Pfad (device release, ersetzen, _fail) die Anlage auf SmartHeat-Werten zuruecklassen. Ohne gespeicherte
Konfiguration (Markierung aus 1.0.0, kaputt) wird geloescht wie bisher.

Bedrohungsmodell (Spec 2): die Markierung schreibt das Geraet selbst aus seiner gespeicherten, schon geprueften
Konfiguration; der Server liefert hier nichts. Nennt die Konfiguration der Markierung eine andere Anlage als die
Markierung, gilt sie als fehlend (loeschen wie bisher), und die Laufzeit prueft sie vor dem Zuruecksetzen ohnehin gegen
die Rastung (sign_off_parts)."""
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from smartheat_core import dateien
from smartheat_runtime.dokument_config import eingerichtet

KEINE, VERWERFEN, ZURUECKSETZEN, LOESCHEN = "keine", "verwerfen", "zuruecksetzen", "loeschen"


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


def entscheiden(pfad: Path, gespeichert) -> str:
    """Vor dem Start des Geraetekerns: KEINE, VERWERFEN (wieder eingerichtet, Zustand bleibt), ZURUECKSETZEN (Markierung
    bleibt, die Laufzeit setzt zurueck) oder LOESCHEN (Zustand und Markierung weg)."""
    if not pfad.exists():
        return KEINE
    if eingerichtet(gespeichert):
        return VERWERFEN
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

"""Testhilfe: Rastungen, wie der Dokumentspeicher sie aus einer Konfiguration schreibt. Nur fuer Tests (nicht im Image).

`rastung_aus` bildet je Kreis des Dokuments einen Eintrag mit dessen Schreibzielen und Soll-Entity; Hebelsatz und
Verteilsystem lassen sich abweichend setzen, damit Tests "dasselbe Dokument, anderes Paar" ausdruecken koennen. Ein
Dokument ohne lesbaren Kreis ergibt einen Eintrag mit einem Platzhalterziel (passt zu keinem Kreis)."""
from collections.abc import Mapping

from smartheat_core import kreisansicht, rastung

PAAR = ("viessmann_vicare", "Heizkoerper")


def rastung_aus(konfiguration, hebelsatz: str | None = None, verteilsystem: str | None = None) -> rastung.Rastung:
    kreise = []
    for index, eintrag in enumerate(kreisansicht.kreise(konfiguration), start=1):
        bindung = eintrag.get("bindung")
        nummer = eintrag.get("kreis")
        kreise.append(rastung.KreisRastung(
            kreis=nummer if isinstance(nummer, int) and not isinstance(nummer, bool) and nummer >= 1 else index,
            hebelsatz=hebelsatz or eintrag.get("hebelsatz"), verteilsystem=verteilsystem or eintrag.get("verteilsystem"),
            ziele=rastung.ziele(bindung) or ("platzhalter",),
            soll_entity=rastung.soll_entity(bindung) if isinstance(bindung, Mapping) else None))
    if not kreise:
        kreise.append(rastung.KreisRastung(1, hebelsatz or PAAR[0], verteilsystem or PAAR[1], ("platzhalter",), None))
    return rastung.Rastung(tuple(kreise))


def fuer(festlegung, konfiguration):
    """Ein Paar (Hebelsatz, Verteilsystem) wird zur Rastung, die genau zu `konfiguration` passt, ausser im Paar; None
    und fertige Rastungen bleiben unveraendert."""
    return rastung_aus(konfiguration, *festlegung) if isinstance(festlegung, tuple) else festlegung


def einzel(hebelsatz: str = PAAR[0], verteilsystem: str = PAAR[1], *, ziele=("treiber=simulation",),
           soll_entity: str | None = None, kreis: int = 1) -> rastung.Rastung:
    """Eine Rastung mit genau einem Kreis aus Rohwerten (fuer Tests der Datei und der Gleichheit)."""
    return rastung.Rastung((rastung.KreisRastung(kreis, hebelsatz, verteilsystem, tuple(ziele), soll_entity),))

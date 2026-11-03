"""Abweichung des Ist vom Ziel ueber die Referenzraeume eines Kreises (Rechnung, ohne I/O).

Jeder Referenzraum hat ein Ziel: das Soll des Kreises plus seinen Versatz, begrenzt auf den Bereich der Raumsolle
(`ZIEL_MIN` bis `ZIEL_MAX`). Das Delta ist das Mittel von Ziel minus Ist ueber die Referenzraeume mit gueltigem Ist;
Kontrollraeume gehen nie ein, der Aufrufer reicht sie nicht herein. Die Entscheidungen fuer Boost und Notfall-Boost
rechnen mit `Soll - Delta` wie mit dem Ist eines einzelnen Raums: ein Referenzraum ohne Versatz ergibt genau das Ist.

Dieselbe Rechnung steht auf dem Server (`heizungsserver.regelung.raum_delta`); der Contract-Check vergleicht beide an
gemeinsamen Vektoren (`tools/contracts/raum_delta.json`)."""
from collections.abc import Mapping

#: Bereich der Raumsolle (Grad), gleich `smartheat_device.wire.RAUM_SOLL_MIN/MAX`; ein Test haelt die Werte gleich.
ZIEL_MIN = 15.0
ZIEL_MAX = 25.0


def ziel(soll: float, versatz: float) -> float:
    """Ziel eines Referenzraums: Soll plus Versatz, begrenzt auf 15 bis 25. Liegt das Soll selbst ausserhalb (Frostschutz,
    Abwesenheit), zieht die Begrenzung das Ziel nie darauf zu: sie reicht nur bis zum Soll, ein Raum ohne Versatz hat
    genau das Soll als Ziel."""
    return min(max(ZIEL_MAX, soll), max(min(ZIEL_MIN, soll), soll + versatz))


def delta(soll: float, ist_je_referenzraum: Mapping[int, float], versatz_je_raum: Mapping[int, float]) -> float | None:
    """Mittel von Ziel minus Ist ueber die Referenzraeume in `ist_je_referenzraum` (Raumnummer -> gueltiges Ist);
    `versatz_je_raum` nennt den Versatz je Raumnummer, ein fehlender gilt als 0. Ohne Referenzraum mit Ist: None."""
    if not ist_je_referenzraum:
        return None
    abweichungen = [ziel(soll, versatz_je_raum.get(raum, 0.0)) - ist for raum, ist in ist_je_referenzraum.items()]
    return sum(abweichungen) / len(abweichungen)


def wirksames_ist(soll: float, ist_je_referenzraum: Mapping[int, float], versatz_je_raum: Mapping[int, float]) -> float | None:
    """Das Ist, mit dem die Regelung rechnet: `Soll - delta(...)`, hier als Mittel von `Ist + (Soll - Ziel)` je Referenzraum
    gerechnet. Mathematisch dasselbe, aber ein Referenzraum ohne Versatz liefert sein Ist unveraendert (kein Rundungsrauschen
    von `Soll - (Soll - Ist)`), das Verhalten bleibt dort bit-gleich zu einem Kreis mit einem Raum. Ohne Referenzraum: None."""
    if not ist_je_referenzraum:
        return None
    werte = [ist + (soll - ziel(soll, versatz_je_raum.get(raum, 0.0))) for raum, ist in ist_je_referenzraum.items()]
    return sum(werte) / len(werte)

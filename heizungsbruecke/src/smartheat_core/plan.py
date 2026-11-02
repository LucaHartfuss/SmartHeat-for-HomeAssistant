"""Soll eines Heizkreises aus Bedienung und Konfiguration, rein: nur Standardbibliothek, keine Uhr, keine Datei.

`soll_effektiv` bekommt die Zeit von aussen (`jetzt` mit Zeitzone) und rechnet in der Zone der Anlage, nie in der des
Hosts. Heute gibt es keinen Plan: das Soll ist der gemerkte Wunsch der Bedienung, `soll_art` ist `wunsch`, einen
naechsten Wechsel gibt es nicht. Steht die Soll-Quelle auf aus oder Frostschutz, gibt es kein Soll (dann wirkt nur der
Frostnaehe-Ausloeser des Geraets). Die Funktion begrenzt den Wunsch nicht: der Bereich gilt am Wunsch-Eingang und in der
Schreibpipeline."""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from smartheat_core.zahlen import ist_endliche_zahl

#: Quelle des geltenden Solls (Telemetrie `soll_art`); weitere Worte kommen mit dem Plan.
SOLL_ART_WUNSCH = "wunsch"


@dataclass(frozen=True)
class SollErgebnis:
    """`soll` None = kein Soll (Soll-Quelle aus oder Frostschutz), dann ist auch `soll_art` None."""

    soll: float | None
    soll_art: str | None
    naechster_wechsel: datetime | None


def soll_effektiv(jetzt: datetime, zone: str, bedienung: Mapping[str, object], konfiguration: Mapping[str, object],
                  *, soll_quelle_aus: bool = False) -> SollErgebnis:
    """Soll des Kreises zu `jetzt`; `bedienung` ist der Eintrag des Kreises (Feld `raum_soll`), `konfiguration` der
    des Kreises. `soll_quelle_aus`: die Soll-Quelle steht auf aus oder Frostschutz."""
    if not isinstance(jetzt, datetime) or jetzt.utcoffset() is None:
        raise ValueError("jetzt braucht eine Zeitzone")
    _zone_pruefen(zone)
    if not isinstance(konfiguration, Mapping):
        raise ValueError("konfiguration ist kein Objekt")
    wunsch = bedienung.get("raum_soll") if isinstance(bedienung, Mapping) else None
    if not isinstance(wunsch, int | float) or not ist_endliche_zahl(wunsch):
        raise ValueError("bedienung hat keinen brauchbaren Wunsch")
    if soll_quelle_aus:
        return SollErgebnis(soll=None, soll_art=None, naechster_wechsel=None)
    return SollErgebnis(soll=float(wunsch), soll_art=SOLL_ART_WUNSCH, naechster_wechsel=None)


def _zone_pruefen(zone: object) -> None:
    if not isinstance(zone, str):
        raise ValueError("zone ist keine bekannte Zeitzone")
    try:
        ZoneInfo(zone)
    except (ValueError, LookupError, OSError):
        raise ValueError("zone ist keine bekannte Zeitzone") from None

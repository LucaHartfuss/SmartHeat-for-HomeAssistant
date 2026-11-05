"""Ablage des Zustands eines Heizkreises unter `<runtime>/kreis-<n>/`: Wiederherstellungspunkt, Failsafe-Zustand,
Abo-Zustand und Raum-Soll. Jeder Kreis hat seinen eigenen Ordner; was die ganze Anlage betrifft (Abmelde-Markierung,
Budget der Anlage), liegt neben den Kreisordnern. Nur Standardbibliothek: beide Hosts und die Migration nutzen sie."""
import re
from pathlib import Path

BACKUP = "backup.json"
FAILSAFE = "failsafe_state.json"
ENTITLEMENT = "entitlement_state.json"
ROOM_TARGET = "room_target.json"
DATEIEN = (BACKUP, FAILSAFE, ENTITLEMENT, ROOM_TARGET)
#: Zaehler der ganzen Anlage, neben den Kreisordnern.
ANLAGE_BUDGET = "anlage_budget.json"

_ORDNER = re.compile(r"kreis-([1-9][0-9]{0,3})")


def kreis_dir(runtime_dir: Path, kreis: int) -> Path:
    """Der Zustandsordner des Kreises; ValueError bei einer Nummer, die kein Kreis sein kann."""
    if isinstance(kreis, bool) or not isinstance(kreis, int) or not 1 <= kreis <= 9999:
        raise ValueError("Kreisnummer ausserhalb des Bereichs")
    return runtime_dir / f"kreis-{kreis}"


def kreisordner(runtime_dir: Path) -> list[Path]:
    """Die vorhandenen Zustandsordner der Kreise (nach Nummer sortiert); ein gleichnamiger Symlink oder eine Datei
    zaehlt nicht."""
    if not runtime_dir.is_dir():
        return []
    gefunden = [pfad for pfad in runtime_dir.iterdir()
                if _ORDNER.fullmatch(pfad.name) and pfad.is_dir() and not pfad.is_symlink()]
    return sorted(gefunden, key=lambda pfad: int(pfad.name.split("-")[1]))


def ohne_wiederherstellungspunkt(runtime_dir: Path) -> list[Path]:
    """Alles unter `runtime_dir`, was das Zuruecksetzen vor den Wiederherstellungspunkten loeschen muss: die Eintraege
    neben den Kreisordnern und der Inhalt der Kreisordner ohne `backup.json`. Wirft OSError, wenn ein Ordner nicht
    lesbar ist."""
    if not runtime_dir.exists():
        return []
    ordner = kreisordner(runtime_dir)
    eintraege: list[Path] = []
    for pfad in sorted(runtime_dir.iterdir()):
        if pfad in ordner:
            eintraege.extend(sorted(inhalt for inhalt in pfad.iterdir() if inhalt.name != BACKUP))
        else:
            eintraege.append(pfad)
    return eintraege


def wiederherstellungspunkte(runtime_dir: Path) -> list[Path]:
    """Die vorhandenen `backup.json` der Kreise: Bindung an Einrichtung und Anlage, die beim Zuruecksetzen zuletzt
    fallen."""
    return [datei for ordner in kreisordner(runtime_dir) if (datei := ordner / BACKUP).exists()]

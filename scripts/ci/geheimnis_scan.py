#!/usr/bin/env python3
"""Geheimnis-Scan (Lieferkette, Sicherheitsgrundsatz S-10, Regel 6): sucht in den versionierten und den noch nicht
versionierten, nicht ignorierten Dateien eines Git-Repos (`git ls-files --cached --others --exclude-standard`) nach
Schluessel- und Token-Mustern: private Schluessel (PEM), minisign-Geheimschluessel, AWS-Zugangskennungen und
GitHub-Token. Ignorierte Dateien (.gitignore) liest der Scan nie. Ein Fund nennt Pfad, Zeile und Art, nie den Wert.

Ausnahmen (Test-Fixtures mit erfundenen Schluesseln) stehen je Zeile als `<pfad>  # <Grund>` in
`geheimnis_ausnahmen.txt` neben diesem Skript (oder per --ausnahmen); eine Ausnahme gilt fuer die ganze Datei, ohne
Grund ist sie ein Fehler, und eine Ausnahme, die nichts mehr deckt, ist veraltet und ebenfalls ein Fehler. Nur
Standardbibliothek; kanonisch in HomeAssistant_Dev_Root/tools/geheimnis_scan.py, identische Kopien in Add-on,
Integration und HomeAssistantPapa unter scripts/ci/geheimnis_scan.py (Root-check.sh Schritt "copies" prueft das; die
Kopie im Server-Repo folgt mit seinem Arbeitspaket).

Aufruf: python3 tools/geheimnis_scan.py [--repo PFAD] [--ausnahmen DATEI]. Exit 0 = keine Funde, 1 = Funde (je Zeile
`FAIL <pfad>:<zeile>: <art>`) oder ein Fehler."""
import argparse
import re
import subprocess
import sys
from pathlib import Path

# Die Muster stehen als Bytes-Ausdruecke, damit auch Binaerdateien (Schluesseldateien, Archive) gelesen werden. Der
# minisign-Kopf steht in zwei Literalen, damit diese Datei sich nicht selbst findet.
MUSTER: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("privater Schluessel", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("minisign-Geheimschluessel", re.compile(b"untrusted comment: minisign " b"encrypted secret key")),
    ("AWS-Zugangskennung", re.compile(rb"AKIA[0-9A-Z]{16}")),
    ("GitHub-Token", re.compile(rb"gh[pousr]_[A-Za-z0-9]{36}")),
    ("GitHub-Token (fein)", re.compile(rb"github_pat_[A-Za-z0-9_]{22,}")),
)
AUSNAHMEN_DATEI = "geheimnis_ausnahmen.txt"


def lies_ausnahmen(text: str) -> dict[str, str]:
    """`<pfad>  # <Grund>` je Zeile; Leer- und Kommentarzeilen sind frei. Ohne Grund oder doppelt: ValueError."""
    ausnahmen: dict[str, str] = {}
    for zeile in text.splitlines():
        if not zeile.strip() or zeile.lstrip().startswith("#"):
            continue
        pfad, _, grund = zeile.partition("#")
        pfad, grund = pfad.strip(), grund.strip()
        if not grund:
            raise ValueError(f"Ausnahme ohne Grund: {pfad} (Zeile '<pfad>  # <Grund>')")
        if pfad in ausnahmen:
            raise ValueError(f"Ausnahme doppelt: {pfad}")
        ausnahmen[pfad] = grund
    return ausnahmen


def _dateien(repo: Path) -> list[str]:
    try:
        ergebnis = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            capture_output=True, check=False)
    except OSError as error:
        raise RuntimeError(f"git nicht ausfuehrbar: {error}") from error
    if ergebnis.returncode != 0:
        raise RuntimeError(f"{repo}: kein Git-Repo oder git ls-files gescheitert")
    namen = ergebnis.stdout.decode("utf-8", "surrogateescape").split("\0")
    return sorted({name for name in namen if name})


def _fund_zeilen(repo: Path, name: str) -> list[tuple[int, str]]:
    pfad = repo / name
    if not pfad.is_file():  # versioniert, aber im Arbeitsbaum geloescht; Ordner (Submodul)
        return []
    try:
        inhalt = pfad.read_bytes()
    except OSError:
        return [(0, "nicht lesbar")]
    treffer = {(inhalt.count(b"\n", 0, m.start()) + 1, art) for art, muster in MUSTER for m in muster.finditer(inhalt)}
    return sorted(treffer)


def _pruefe(repo: Path, ausnahmen: dict[str, str]) -> tuple[list[str], list[str]]:
    """(Funde, Ausnahmen ohne Fund). Eine Ausnahme ohne Grund gilt nicht und ist selbst ein Fund."""
    gueltig = {pfad for pfad, grund in ausnahmen.items() if grund.strip()}
    funde: list[str] = []
    gedeckt: set[str] = set()
    for name in _dateien(repo):
        zeilen = _fund_zeilen(repo, name)
        if zeilen and name in gueltig:
            gedeckt.add(name)
            continue
        funde += [f"{name}:{zeile}: {art}" for zeile, art in zeilen]
    funde += [f"{pfad}: Ausnahme ohne Grund" for pfad in sorted(ausnahmen) if pfad not in gueltig]
    return funde, sorted(gueltig - gedeckt)


def funde(repo: Path, ausnahmen: dict[str, str]) -> list[str]:
    """Alle Funde des Repos als `<pfad>:<zeile>: <art>` (nach Pfad und Zeile sortiert), ohne die Dateien mit Ausnahme."""
    return _pruefe(repo, ausnahmen)[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--ausnahmen", type=Path, default=None)
    args = parser.parse_args(argv)
    ausnahmen_pfad = args.ausnahmen or Path(__file__).with_name(AUSNAHMEN_DATEI)
    try:
        ausnahmen = lies_ausnahmen(ausnahmen_pfad.read_text(encoding="utf-8")) if ausnahmen_pfad.is_file() else {}
        gefunden, veraltet = _pruefe(args.repo, ausnahmen)
    except (ValueError, RuntimeError, OSError) as error:
        print(f"FAIL {error}")
        return 1
    meldungen = [*gefunden, *(f"{pfad}: Ausnahme ohne Fund" for pfad in veraltet)]
    for meldung in meldungen:
        print(f"FAIL {meldung}")
    if not meldungen:
        print("OK   Geheimnis-Scan (Schluessel- und Token-Muster)")
    return 1 if meldungen else 0


if __name__ == "__main__":
    sys.exit(main())

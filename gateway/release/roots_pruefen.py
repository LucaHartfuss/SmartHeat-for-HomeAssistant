#!/usr/bin/env python3
"""Prueft vor einem Gateway-Bau, dass der Schluesselbund der Roots zur gepinnten Liste (host/release-roots.sha256) passt
(Sicherheit, S-10): ein echtes Release nimmt nur Roots aus der Liste, nie eine Test-Root; ein Labor-Bau nimmt nur
Test-Roots, nie eine aus der Liste.

Die Liste hat je Zeile `<64 Hex, SHA-256 ueber die DER-Form des Zertifikats>  <Name>`; Leerzeilen und Zeilen mit # sind
Kommentare. Eine Zeile mit dem Platzhalter-Marker (wie in release-roots.pem) macht die Liste zum Platzhalter. Der Name
in der Liste ist nur eine Lesehilfe; entschieden wird ueber den Fingerabdruck. "Test-Root" heisst: "(Test)" steht im
Subject des Zertifikats (ca.py: TEST_ROOT_CN).

Aufruf: roots_pruefen.py produktion|labor --liste DATEI ROOTS
  produktion: die Liste ist kein Platzhalter und nicht leer; jede Root steht mit ihrem Fingerabdruck in der Liste und hat
              kein "(Test)" im Subject (auch dann nicht, wenn sie in der Liste steht).
  labor:      jede Root hat "(Test)" im Subject und steht nicht in der Liste (eine reine Platzhalter-Liste hat keine Eintraege).
Exit 0 = gueltig, 1 = abgelehnt (je Grund eine Zeile FEHLER: auf stderr), 2 = falscher Aufruf."""
import argparse
import re
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))  # Aufruf als Skript ohne PYTHONPATH
from smartheat_host import kette  # noqa: E402

TEST_MARKE = "(Test)"
ZEILE = re.compile(r"^[0-9a-fA-F]{64}\s+\S.*$")


class ListeFehler(Exception):
    pass


def lies_liste(text: str) -> tuple[set[str], bool]:
    """(Fingerabdruecke in Kleinschreibung, ist Platzhalter). Eine kaputte Zeile ist ein ListeFehler."""
    abdruecke: set[str] = set()
    platzhalter = False
    for nummer, zeile in enumerate(text.splitlines(), start=1):
        if kette.PLACEHOLDER_MARKER in zeile:
            platzhalter = True
            continue
        if not zeile.strip() or zeile.lstrip().startswith("#"):
            continue
        if not ZEILE.match(zeile.strip()):
            raise ListeFehler(f"Liste, Zeile {nummer}: erwartet <64 Hex>  <Name>")
        abdruecke.add(zeile.split()[0].lower())
    return abdruecke, platzhalter


def abdruck(root: x509.Certificate) -> str:
    return root.fingerprint(hashes.SHA256()).hex()


def ist_test_root(root: x509.Certificate) -> bool:
    return TEST_MARKE in root.subject.rfc4514_string()


def pruefe(modus: str, liste: str, roots_pem: str) -> list[str]:
    """Gruende, die Roots abzulehnen; leer = gueltig."""
    abdruecke, platzhalter = lies_liste(liste)
    if modus == "produktion":
        if platzhalter:
            return ["Die Liste der Release-Roots ist ein Platzhalter: erst die echte Root eintragen (AP-04-Root, Runbook 13.2)"]
        if not abdruecke:
            return ["Die Liste der Release-Roots ist leer: erst die echte Root eintragen (AP-04-Root, Runbook 13.2)"]
    gruende = []
    for root in kette.load_roots(roots_pem):
        name, finger = root.subject.rfc4514_string(), abdruck(root)
        if modus == "produktion":
            if ist_test_root(root):
                gruende.append(f"Root {finger} ({name}) ist eine Test-Root ({TEST_MARKE} im Namen), fuer ein Release nicht erlaubt")
            elif finger not in abdruecke:
                gruende.append(f"Root {finger} ({name}) steht nicht in der Liste der Release-Roots")
        elif not ist_test_root(root):
            gruende.append(f"Root {finger} ({name}) hat kein {TEST_MARKE} im Namen, im Labor nicht erlaubt")
        elif finger in abdruecke:
            gruende.append(f"Root {finger} ({name}) steht in der Liste der Release-Roots, im Labor nicht erlaubt")
    return gruende


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("modus", choices=["produktion", "labor"])
    parser.add_argument("--liste", type=Path, required=True, metavar="DATEI")
    parser.add_argument("roots", type=Path, metavar="ROOTS")
    args = parser.parse_args(argv)
    try:
        gruende = pruefe(args.modus, args.liste.read_text(), args.roots.read_text())
    except (OSError, UnicodeDecodeError, ListeFehler, kette.SignatureError, ValueError) as fehler:
        print(f"FEHLER: {fehler}", file=sys.stderr)
        return 1
    for grund in gruende:
        print(f"FEHLER: {grund}", file=sys.stderr)
    return 1 if gruende else 0


if __name__ == "__main__":
    sys.exit(main())

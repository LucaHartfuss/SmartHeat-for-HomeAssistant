#!/usr/bin/env python3
"""Abschnitt "## <Version>" aus einem CHANGELOG fuer die Release-Notizen des Signier-Jobs (Audit 4, A4-36): der Job
nimmt Notizen aus dem eigenen, getaggten Checkout statt aus dem Artefakt des Build-Jobs. Gleiche Regel wie
changelog_section in tools/release_gate.py (Dev-Root), hier ohne Abhaengigkeit auf das Root-Repo.

Aufruf: changelog_section.py CHANGELOG VERSION. Exit 0 = Abschnitt auf stdout, 1 = kein Abschnitt."""
import re
import sys
from pathlib import Path


def section(text: str, version: str) -> str | None:
    """Wie changelog_section in tools/release_gate.py: Ueberschrift "## V", "## [V]" oder "## V <Zusatz>" (z. B. Datum);
    der Abschnitt endet an der naechsten Zeile mit "## "; ein leerer Abschnitt zaehlt als fehlend."""
    heading = re.compile(rf"^## \[?{re.escape(version)}\]?(\s.*)?$")
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if heading.match(line):
            body: list[str] = []
            for following in lines[index + 1:]:
                if following.startswith("## "):
                    break
                body.append(following)
            found = "\n".join(body).strip()
            return found + "\n" if found else None
    return None


def main(argv: list[str]) -> int:
    path, version = argv
    found = section(Path(path).read_text(encoding="utf-8"), version)
    if found is None:
        print(f"::error::{path} hat keinen (oder einen leeren) Abschnitt '## {version}'", file=sys.stderr)
        return 1
    sys.stdout.write(found)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

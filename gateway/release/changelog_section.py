#!/usr/bin/env python3
"""Abschnitt "## <Version>" aus einem CHANGELOG fuer die Release-Notizen des Signier-Jobs (Audit 4, A4-36): der Job
nimmt Notizen aus dem eigenen, getaggten Checkout statt aus dem Artefakt des Build-Jobs. Gleiche Regel wie
changelog_section in tools/release_gate.py (Dev-Root), hier ohne Abhaengigkeit auf das Root-Repo.

Aufruf: changelog_section.py CHANGELOG VERSION. Exit 0 = Abschnitt auf stdout, 1 = kein Abschnitt."""
import re
import sys
from pathlib import Path


def section(text: str, version: str) -> str | None:
    match = re.search(rf"^## {re.escape(version)}[ \t]*\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    return None if match is None else match.group(1).strip() + "\n"


def main(argv: list[str]) -> int:
    path, version = argv
    found = section(Path(path).read_text(encoding="utf-8"), version)
    if found is None:
        print(f"::error::{path} hat keinen Abschnitt '## {version}'", file=sys.stderr)
        return 1
    sys.stdout.write(found)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

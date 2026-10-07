#!/usr/bin/env python3
"""Holt die Geraete-Aufzeichnungen aus tests/response von PyViCare (Quelle der Testmatrix, SHG G4 3). Aufruf:
vendor_vicare_matrix.py <commit> <zielordner>. Kopiert jede *.json, schreibt SOURCE.txt und die Lizenzdatei."""
import json
import sys
import urllib.request
from datetime import date
from pathlib import Path

REPO = "openviess/PyViCare"
API = f"https://api.github.com/repos/{REPO}"


def get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "smartheat-vendor"}), timeout=60) as r:
        return r.read()


def main(commit: str, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    listing = json.loads(get(f"{API}/contents/tests/response?ref={commit}"))
    names = [item["name"] for item in listing if item["name"].endswith(".json")]
    for name in names:
        (target / name).write_bytes(get(f"https://raw.githubusercontent.com/{REPO}/{commit}/tests/response/{name}"))
    (target / "LICENSE-PyViCare").write_bytes(get(f"https://raw.githubusercontent.com/{REPO}/{commit}/LICENSE"))
    (target / "SOURCE.txt").write_text(f"{REPO} {commit}\ntests/response/*.json, geholt am {date.today().isoformat()}\n")
    print(f"{len(names)} Aufzeichnungen nach {target}")


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]))

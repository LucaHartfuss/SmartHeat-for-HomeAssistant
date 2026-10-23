"""Hostneutraler Kern (2.1 Punkt 4): In smartheat_core und smartheat_runtime steht keine
Tenant-ID. Die Importgrenzen stehen in test_modulgrenzen.py."""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
TENANT_ID = re.compile(r"\bclient\d+\b|\bzuhause\b", re.IGNORECASE)


def test_no_tenant_ids_in_core_and_runtime():
    offenders = [
        f"{path.relative_to(SRC)}:{number}: {line.strip()}"
        for package in ("smartheat_core", "smartheat_runtime")
        for path in sorted((SRC / package).rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if TENANT_ID.search(line)
    ]
    assert offenders == []

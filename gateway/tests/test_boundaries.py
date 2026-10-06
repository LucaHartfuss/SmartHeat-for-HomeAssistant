"""Grenzen des Gateways (Spec SHG G2 1, Uebersicht 2.1 Punkt 4)."""
import ast
import re
from pathlib import Path

GW = Path(__file__).resolve().parents[1]
SRC = GW / "src" / "smartheat_gateway"
REPO = GW.parent
TENANT_ID = re.compile(r"\bclient\d+\b|\bzuhause\b", re.IGNORECASE)


def _imports(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append(node.module)
    return found


def test_gateway_never_imports_the_ha_host():
    offenders = [
        f"{path.relative_to(SRC)}: {module}"
        for path in sorted(SRC.rglob("*.py"))
        for module in _imports(path)
        if module.split(".")[0] == "heizungsbruecke"
    ]
    assert offenders == []


def test_no_tenant_ids_in_gateway_sources():
    offenders = [
        f"{path.relative_to(SRC)}:{number}"
        for path in sorted(SRC.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if TENANT_ID.search(line)
    ]
    assert offenders == []


def test_supervisor_does_not_see_the_gateway_and_the_addon_image_ignores_it():
    assert not (GW / "config.yaml").exists()
    dockerfile = (REPO / "heizungsbruecke" / "Dockerfile").read_text()
    assert "gateway" not in dockerfile


def test_version_file_matches_the_code():
    from smartheat_gateway.version import GATEWAY_VERSION
    assert (GW / "VERSION").read_text().strip() == GATEWAY_VERSION
    assert f"## {GATEWAY_VERSION}" in (GW / "CHANGELOG.md").read_text()

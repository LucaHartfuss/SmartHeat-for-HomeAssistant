"""Grenzen des Gateways."""
import re
import tomllib
from pathlib import Path

GW = Path(__file__).resolve().parents[1]
SRC = GW / "src" / "smartheat_gateway"
REPO = GW.parent
TENANT_ID = re.compile(r"\bclient\d+\b|\bzuhause\b", re.IGNORECASE)


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


def test_pyproject_reads_the_version_from_version_py():
    """Eine zweite, statische Versionsnummer in pyproject.toml wuerde unbemerkt von GATEWAY_VERSION abweichen."""
    pyproject = tomllib.loads((GW / "pyproject.toml").read_text())
    assert "version" not in pyproject["project"] and pyproject["project"]["dynamic"] == ["version"]
    assert pyproject["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "smartheat_gateway.version.GATEWAY_VERSION"}

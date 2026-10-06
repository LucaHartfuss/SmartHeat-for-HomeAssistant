"""Gleiche Versionen gemeinsamer Abhaengigkeiten wie das Add-on (Spec SHG G2 7.1)."""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _locked(path: Path) -> dict[str, str]:
    return dict(re.findall(r"^([A-Za-z0-9._-]+)==([^\s\\]+)", path.read_text(), flags=re.MULTILINE))


def test_shared_dependencies_have_the_addon_versions():
    addon = _locked(REPO / "heizungsbruecke" / "requirements.txt")
    gateway = _locked(REPO / "gateway" / "requirements.txt")
    for name in ("paho-mqtt", "requests", "certifi", "urllib3", "idna", "charset-normalizer"):
        assert gateway[name] == addon[name], name
    for name in ("cryptography", "segno"):
        assert name in gateway

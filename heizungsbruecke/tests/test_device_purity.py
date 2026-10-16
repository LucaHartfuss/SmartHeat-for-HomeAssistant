"""Geraetekern (Spec 5b 5.1): wire.py ist reine Standardbibliothek. Ab Teilprojekt 5c ist das HA-Add-on ein Geraet:
heizungsbruecke nutzt den Geraetekern (Geraete-Host geraet_host.py, Spec 5c 1.3). Die uebrigen Importgrenzen
(Drittpakete je Datei, kein Host im Kern) stehen in test_modulgrenzen.py (Audit 5 A5-59)."""
import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
DEVICE = SRC / "smartheat_device"


def _imported_modules(path: Path) -> list[str]:
    modules = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.append(node.module)
    return modules


def test_wire_is_standard_library_only():
    assert all(module.split(".")[0] in sys.stdlib_module_names for module in _imported_modules(DEVICE / "wire.py"))


def test_the_ha_host_uses_the_device_core_from_5c():
    """Bis 5b verboten (kein Einfluss auf client1), ab 5c der Weg des Add-ons: der Geraete-Host baut auf dem Kern."""
    modules = _imported_modules(SRC / "heizungsbruecke" / "geraet_host.py")
    assert any(module.split(".")[0] == "smartheat_device" for module in modules)

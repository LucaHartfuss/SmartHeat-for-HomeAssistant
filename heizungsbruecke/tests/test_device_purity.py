"""Grenzen des Geraetekerns (Spec 5b 5.1): smartheat_device importiert die Standardbibliothek, die eigenen Pakete und
genau drei Drittpakete an genau einer Stelle (cryptography: Identitaet, paho: Link, requests: Bootstrap). wire.py ist
reine Standardbibliothek. Ab Teilprojekt 5c ist das HA-Add-on ein Geraet: heizungsbruecke nutzt den Geraetekern
(Geraete-Host geraet_host.py, Spec 5c 1.3); umgekehrt kennt der Geraetekern keinen Host (erste Pruefung)."""
import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
DEVICE = SRC / "smartheat_device"
OWN_PACKAGES = {"smartheat_device", "smartheat_core", "smartheat_runtime"}
THIRD_PARTY_ALLOWED_IN = {"cryptography": {"identity.py"}, "paho": {"link.py"}, "requests": {"bootstrap.py"}}


def _imported_modules(path: Path) -> list[str]:
    modules = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.append(node.module)
    return modules


def test_device_core_imports_only_what_it_may():
    offenders = []
    for path in sorted(DEVICE.glob("*.py")):
        for module in _imported_modules(path):
            top = module.split(".")[0]
            if top in OWN_PACKAGES or top in sys.stdlib_module_names:
                continue
            if path.name in THIRD_PARTY_ALLOWED_IN.get(top, set()):
                continue
            offenders.append(f"{path.name}: {module}")
    assert offenders == []
    assert (DEVICE / "__init__.py").exists()


def test_wire_is_standard_library_only():
    assert all(module.split(".")[0] in sys.stdlib_module_names for module in _imported_modules(DEVICE / "wire.py"))


def test_the_ha_host_uses_the_device_core_from_5c():
    """Bis 5b verboten (kein Einfluss auf client1), ab 5c der Weg des Add-ons: der Geraete-Host baut auf dem Kern."""
    modules = _imported_modules(SRC / "heizungsbruecke" / "geraet_host.py")
    assert any(module.split(".")[0] == "smartheat_device" for module in modules)

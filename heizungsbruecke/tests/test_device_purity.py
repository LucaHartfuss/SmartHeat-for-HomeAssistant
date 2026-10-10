"""Grenzen des Geraetekerns (Spec 5b 5.1): smartheat_device importiert die Standardbibliothek, die eigenen Pakete und
genau drei Drittpakete an genau einer Stelle (cryptography: Identitaet, paho: Link, requests: Bootstrap). wire.py ist
reine Standardbibliothek. Der HA-Host nutzt den Geraetekern erst ab Teilprojekt 5c (bis dahin kein Einfluss auf
client1, Spec 5b 10)."""
import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
DEVICE = SRC / "smartheat_device"
OWN_PACKAGES = {"smartheat_device", "smartheat_core", "smartheat_runtime", "smartheat_transport"}
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


def test_the_ha_host_does_not_use_the_device_core_before_5c():
    offenders = [
        f"{path.relative_to(SRC)}: {module}"
        for path in sorted((SRC / "heizungsbruecke").rglob("*.py"))
        for module in _imported_modules(path)
        if module.split(".")[0] == "smartheat_device"
    ]
    assert offenders == []

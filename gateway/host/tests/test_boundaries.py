"""Grenzen des Host-Pakets: Standardbibliothek, cryptography und genau diese Gateway-Module."""
import ast
import re
import sys
from pathlib import Path

HOST = Path(__file__).resolve().parents[1]
SRC = HOST.parent / "src" / "smartheat_gateway"
ALLOWED_GATEWAY = {"smartheat_gateway", "smartheat_gateway.paths", "smartheat_gateway.version"}
# Dateien, die install.sh aus gateway/src mit ablegt.
SHIPPED = ["__init__.py", "paths.py", "version.py"]
#: Dateien aus heizungsbruecke/src/smartheat_core, die install.sh mit ablegt (ein Schreibweg)
SHIPPED_CORE = ["__init__.py", "dateien.py"]
CORE = HOST.parent.parent / "heizungsbruecke" / "src" / "smartheat_core"
ALLOWED_CORE = {"smartheat_core", "smartheat_core.dateien"}
TENANT_ID = re.compile(r"\bclient\d+\b|\bzuhause\b", re.IGNORECASE)


# Pakete der erlaubten Gateway-Module: ein "from <Paket> import name" importiert ein Untermodul "<Paket>.name"
# und dieses muss selbst in ALLOWED_GATEWAY bzw. ALLOWED_CORE stehen. Namen aus erlaubten Modulen (paths, ...)
# sind frei.
ALLOWED_PACKAGES = {"smartheat_gateway", "smartheat_core"}


def _resolve(node: ast.ImportFrom, package: str) -> str | None:
    """Absoluter Modulname eines from-Imports (relative Importe gegen das Paket der Datei aufgeloest)."""
    if node.level == 0:
        return node.module
    parts = package.split(".")
    if node.level - 1 >= len(parts):
        return None
    base = parts[: len(parts) - (node.level - 1)]
    return ".".join([*base, node.module] if node.module else base)


def _imported_modules(source: str, package: str) -> set[str]:
    """Alle Module, die die Quelle importiert (bei from-Imports aus Paketen auch die importierten Untermodule)."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            module = _resolve(node, package) or f"<unaufloesbar: Ebene {node.level}>"
            found.add(module)
            if module in ALLOWED_PACKAGES or module.startswith("<"):
                found |= {f"{module}.{alias.name}" for alias in node.names}
    return found


def forbidden_imports(source: str, package: str, allow_host: bool) -> list[str]:
    """Imports ausserhalb von Standardbibliothek, cryptography, (Host-Paket) und exakt erlaubten Gateway-Modulen."""
    offenders = []
    for module in sorted(_imported_modules(source, package)):
        top = module.split(".")[0]
        if top in sys.stdlib_module_names or top == "cryptography" or (allow_host and top == "smartheat_host"):
            continue
        if module not in ALLOWED_GATEWAY and module not in ALLOWED_CORE:
            offenders.append(module)
    return offenders


def _package_of(path: Path) -> str:
    """Dotted-Paket einer ausgelieferten Gateway-Datei aus ihrem Pfad unter gateway/src."""
    relative = path.relative_to(SRC.parent).parent
    return ".".join(relative.parts)


def test_host_imports_only_stdlib_cryptography_and_the_shipped_gateway_modules():
    files = sorted((HOST / "smartheat_host").rglob("*.py"))
    assert files
    for path in files:
        package = ".".join(path.relative_to(HOST).parent.parts)
        assert forbidden_imports(path.read_text(encoding="utf-8"), package, allow_host=True) == [], path.name


def test_shipped_gateway_modules_import_nothing_else():
    for name in SHIPPED:
        path = SRC / name
        assert forbidden_imports(path.read_text(encoding="utf-8"), _package_of(path), allow_host=False) == [], name


def test_checker_rejects_forbidden_gateway_imports():
    cases = [
        ("import smartheat_gateway.agent.mqtt", "smartheat_host", ["smartheat_gateway.agent.mqtt"]),
        ("from smartheat_gateway import runtime", "smartheat_host", ["smartheat_gateway.runtime"]),
        ("from smartheat_gateway.agent import loop", "smartheat_host", ["smartheat_gateway.agent"]),
        ("from smartheat_gateway.runtime_main import main", "smartheat_host", ["smartheat_gateway.runtime_main"]),
        ("from .runtime import x", "smartheat_gateway", ["smartheat_gateway.runtime"]),
        ("from . import runtime", "smartheat_gateway", ["smartheat_gateway.runtime"]),
        ("from .agent import loop", "smartheat_gateway", ["smartheat_gateway.agent"]),
        ("from ..bus import x", "smartheat_gateway.agent", ["smartheat_gateway.bus"]),
        ("from .loop import x", "smartheat_gateway.agent", ["smartheat_gateway.agent.loop"]),
        ("from ... import x", "smartheat_gateway", ["<unaufloesbar: Ebene 3>", "<unaufloesbar: Ebene 3>.x"]),
        ("from smartheat_gateway.agent import wire", "smartheat_host", ["smartheat_gateway.agent"]),
        ("import smartheat_device.wire", "smartheat_host", ["smartheat_device.wire"]),
        ("import requests", "smartheat_host", ["requests"]),
        ("from smartheat_core import runtime", "smartheat_host", ["smartheat_core.runtime"]),
        ("import smartheat_core.safety", "smartheat_host", ["smartheat_core.safety"]),
    ]
    for source, package, expected in cases:
        assert forbidden_imports(source, package, allow_host=True) == expected, source


def test_checker_rejects_host_package_in_shipped_gateway_modules():
    assert forbidden_imports("import smartheat_host.minisign", "smartheat_gateway", allow_host=False) == [
        "smartheat_host.minisign"
    ]


def test_checker_accepts_the_allowed_forms():
    source = "\n".join([
        "import os, json",
        "from cryptography.hazmat.primitives import hashes",
        "import smartheat_gateway",
        "from smartheat_gateway import paths, version",
        "from smartheat_core import dateien",
        "from smartheat_core.dateien import schreiben_json",
        "from smartheat_gateway.paths import Paths",
        "from smartheat_gateway.version import GATEWAY_VERSION",
        "from smartheat_host import minisign",
        "from smartheat_host.minisign import verify",
        "from . import minisign as sibling",
    ])
    assert forbidden_imports(source, "smartheat_host", allow_host=True) == []
    relative = "from .paths import Paths"
    assert forbidden_imports(relative, "smartheat_gateway", allow_host=False) == []


def test_install_ships_exactly_the_listed_gateway_modules():
    text = (HOST / "install.sh").read_text()
    line = next(row for row in text.splitlines() if "for rel in" in row and "paths.py" in row)
    assert line.split("for rel in", 1)[1].split(";", 1)[0].split() == SHIPPED


def test_install_ships_exactly_the_listed_core_modules():
    text = (HOST / "install.sh").read_text()
    line = next(row for row in text.splitlines() if "for rel in" in row and "dateien.py" in row)
    assert line.split("for rel in", 1)[1].split(";", 1)[0].split() == SHIPPED_CORE


def test_shipped_core_modules_are_standard_library_only():
    for name in SHIPPED_CORE:
        source = (CORE / name).read_text(encoding="utf-8")
        assert forbidden_imports(source, "smartheat_core", allow_host=False) == [], name


def test_no_tenant_ids_in_host_files():
    for path in sorted(p for p in HOST.rglob("*") if p.is_file() and "tests" not in p.parts and p.suffix != ".pyc"):
        assert not TENANT_ID.search(path.read_text(encoding="utf-8", errors="ignore")), path

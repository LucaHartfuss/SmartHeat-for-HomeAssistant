"""Grenzen des Host-Pakets (Spec G2b-1 2.1): Standardbibliothek, cryptography und genau diese Gateway-Module."""
import ast
import re
import sys
from pathlib import Path

HOST = Path(__file__).resolve().parents[1]
SRC = HOST.parent / "src" / "smartheat_gateway"
ALLOWED_GATEWAY = {
    "smartheat_gateway", "smartheat_gateway.agent", "smartheat_gateway.agent.wire", "smartheat_gateway.agent.identity",
    "smartheat_gateway.files", "smartheat_gateway.paths", "smartheat_gateway.version",
}
# Dateien, die install.sh aus gateway/src mit ablegt (Plan G2b-1 Praezisierung 10).
SHIPPED = ["__init__.py", "files.py", "paths.py", "version.py", "agent/__init__.py", "agent/wire.py",
           "agent/identity.py"]
TENANT_ID = re.compile(r"\bclient\d+\b|\bzuhause\b", re.IGNORECASE)


# Pakete der erlaubten Gateway-Module: ein "from <Paket> import name" importiert ein Untermodul "<Paket>.name"
# und dieses muss selbst in ALLOWED_GATEWAY stehen. Namen aus erlaubten Modulen (files, paths, ...) sind frei.
ALLOWED_PACKAGES = {"smartheat_gateway", "smartheat_gateway.agent"}


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
        if module not in ALLOWED_GATEWAY:
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
        ("from smartheat_gateway.agent import loop", "smartheat_host", ["smartheat_gateway.agent.loop"]),
        ("from smartheat_gateway.runtime_main import main", "smartheat_host", ["smartheat_gateway.runtime_main"]),
        ("from .runtime import x", "smartheat_gateway", ["smartheat_gateway.runtime"]),
        ("from . import runtime", "smartheat_gateway", ["smartheat_gateway.runtime"]),
        ("from .agent import loop", "smartheat_gateway", ["smartheat_gateway.agent.loop"]),
        ("from ..bus import x", "smartheat_gateway.agent", ["smartheat_gateway.bus"]),
        ("from .loop import x", "smartheat_gateway.agent", ["smartheat_gateway.agent.loop"]),
        ("from ... import x", "smartheat_gateway", ["<unaufloesbar: Ebene 3>", "<unaufloesbar: Ebene 3>.x"]),
        ("import requests", "smartheat_host", ["requests"]),
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
        "import smartheat_gateway.agent.wire",
        "from smartheat_gateway import files, paths, version",
        "from smartheat_gateway.agent import identity, wire",
        "from smartheat_gateway.agent.wire import sign_request",
        "from smartheat_gateway.files import write_json",
        "from smartheat_gateway.paths import Paths",
        "from smartheat_gateway.version import GATEWAY_VERSION",
        "from smartheat_host import minisign",
        "from smartheat_host.minisign import verify",
        "from . import minisign as sibling",
    ])
    assert forbidden_imports(source, "smartheat_host", allow_host=True) == []
    relative = "from .wire import x\nfrom . import wire\nfrom .. import files\nfrom ..paths import Paths"
    assert forbidden_imports(relative, "smartheat_gateway.agent", allow_host=False) == []


def test_no_tenant_ids_in_host_files():
    for path in sorted(p for p in HOST.rglob("*") if p.is_file() and "tests" not in p.parts and p.suffix != ".pyc"):
        assert not TENANT_ID.search(path.read_text(encoding="utf-8", errors="ignore")), path

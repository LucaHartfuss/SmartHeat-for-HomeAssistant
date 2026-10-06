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


def _imports(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
            found |= {f"{node.module}.{alias.name}" for alias in node.names if node.module.startswith("smartheat_")}
    return found


def _top(module: str) -> str:
    return module.split(".")[0]


def test_host_imports_only_stdlib_cryptography_and_the_shipped_gateway_modules():
    files = sorted((HOST / "smartheat_host").rglob("*.py"))
    assert files
    for path in files:
        for module in _imports(path):
            top = _top(module)
            if top in sys.stdlib_module_names or top in ("cryptography", "smartheat_host"):
                continue
            assert module in ALLOWED_GATEWAY or module.rsplit(".", 1)[0] in ALLOWED_GATEWAY, f"{path.name}: {module}"


def test_shipped_gateway_modules_import_nothing_else():
    for name in SHIPPED:
        for module in _imports(SRC / name):
            top = _top(module)
            if top in sys.stdlib_module_names or top == "cryptography":
                continue
            assert module in ALLOWED_GATEWAY or module.rsplit(".", 1)[0] in ALLOWED_GATEWAY, f"{name}: {module}"


def test_no_tenant_ids_in_host_files():
    for path in sorted(p for p in HOST.rglob("*") if p.is_file() and "tests" not in p.parts and p.suffix != ".pyc"):
        assert not TENANT_ID.search(path.read_text(encoding="utf-8", errors="ignore")), path

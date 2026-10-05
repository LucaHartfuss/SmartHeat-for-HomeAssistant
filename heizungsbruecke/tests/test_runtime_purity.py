"""Grenzen des hostneutralen Kerns (Spec SHG 3.4 Punkt 4, 2.1 Punkt 4): smartheat_runtime importiert weder
heizungsbruecke noch gateway noch Home Assistant; requests nur fuer die Accounts-API (entitlement.py). In
smartheat_core und smartheat_runtime steht keine Tenant-ID."""
import ast
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
RUNTIME = SRC / "smartheat_runtime"
OWN_PACKAGES = {"smartheat_runtime", "smartheat_core", "smartheat_transport"}
# Drittpakete nur in genau diesen Dateien.
THIRD_PARTY_ALLOWED_IN = {"requests": {"entitlement.py"}}
TENANT_ID = re.compile(r"\bclient\d+\b|\bzuhause\b", re.IGNORECASE)


def _imported_modules(path: Path) -> list[str]:
    modules = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.append(node.module)
    return modules


def test_runtime_imports_only_stdlib_own_packages_and_requests_for_the_accounts_api():
    offenders = []
    for path in sorted(RUNTIME.glob("*.py")):
        for module in _imported_modules(path):
            top = module.split(".")[0]
            if top in OWN_PACKAGES or top in sys.stdlib_module_names:
                continue
            if path.name in THIRD_PARTY_ALLOWED_IN.get(top, set()):
                continue
            offenders.append(f"{path.name}: {module}")
    assert offenders == []
    assert (RUNTIME / "__init__.py").exists()


def test_no_tenant_ids_in_core_and_runtime():
    offenders = [
        f"{path.relative_to(SRC)}:{number}: {line.strip()}"
        for package in ("smartheat_core", "smartheat_runtime")
        for path in sorted((SRC / package).rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if TENANT_ID.search(line)
    ]
    assert offenders == []

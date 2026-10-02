"""smartheat_core ist rein (Hersteller-Abstraktion, Plan 2 P2-1): nur Standardbibliothek und das Paket selbst."""
import ast
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "src" / "smartheat_core"


def _imported_modules(path: Path) -> list[str]:
    modules = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.append(node.module)
    return modules


def test_core_imports_only_the_standard_library_and_itself():
    offenders = [
        f"{path.name}: {module}"
        for path in sorted(CORE.glob("*.py"))
        for module in _imported_modules(path)
        if module.split(".")[0] != "smartheat_core" and module.split(".")[0] not in sys.stdlib_module_names
    ]
    assert offenders == []
    assert (CORE / "__init__.py").exists()

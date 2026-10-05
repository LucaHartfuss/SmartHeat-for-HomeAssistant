"""smartheat_transport ist HA-frei (Spec AWS-IoT 5.1): nur Standardbibliothek und das Paket selbst;
paho nur im MQTT-Client."""
import ast
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "smartheat_transport"


def _imported_modules(path: Path) -> list[str]:
    modules = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.append(node.module)
    return modules


# paho bekommt connect.py weiter als Objekt; nur der MQTT-Client selbst importiert es (Spec SHG 3.3).
THIRD_PARTY_ALLOWED_IN = {"paho": {"mqtt_client.py"}}


def test_transport_imports_only_the_standard_library_itself_and_paho_in_the_client():
    offenders = [
        f"{path.name}: {module}"
        for path in sorted(PACKAGE.glob("*.py"))
        for module in _imported_modules(path)
        if module.split(".")[0] != "smartheat_transport" and module.split(".")[0] not in sys.stdlib_module_names
        and path.name not in THIRD_PARTY_ALLOWED_IN.get(module.split(".")[0], set())
    ]
    assert offenders == []
    assert (PACKAGE / "__init__.py").exists()
    assert (PACKAGE / "mqtt_client.py").exists()

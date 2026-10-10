"""Plan 5b D1 Task 1: Vertragskopie smartheat_device/wire.py (Spec 5b 2). Die Gleichheit mit dem Server prueft
zusaetzlich der Contract-Check im Dev-Root (Plan 5b-W Teil A)."""
import ast
import hashlib
import os
from pathlib import Path

import pytest

from smartheat_device import wire

DEVICE = "shg-aaaaaaaaaaaaaaaa"
ADDON_REPO = Path(__file__).resolve().parents[2]
DEV_ROOT = Path(os.environ.get("DEV_ROOT") or ADDON_REPO.parent)
SERVER_PROTOCOL = DEV_ROOT / "HeizungssteuerungServer" / "src" / "heizungsserver" / "device_protocol.py"
MARKER = "# --- Server-Regeln (nicht im Vertrag) ---"


def vertragsteil(text: str) -> str:
    """Quelltext ab der ersten Anweisung nach Modul-Docstring und Importen bis vor MARKER bzw. bis zum Ende, ohne
    Leerzeilen am Rand. Dieselbe Regel nutzt tools/contract_check.py (Plan 5b-W Teil A)."""
    body = ast.parse(text).body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    first = next(node for node in body if not isinstance(node, (ast.Import, ast.ImportFrom)))
    lines = text.splitlines()[first.lineno - 1:]
    if MARKER in lines:
        lines = lines[:lines.index(MARKER)]
    return "\n".join(lines).strip() + "\n"


@pytest.mark.skipif(not SERVER_PROTOCOL.exists(), reason="Server-Repo nicht unter DEV_ROOT (Contract-Check prueft es)")
def test_the_copy_is_literal():
    assert vertragsteil(Path(wire.__file__).read_text()) == vertragsteil(SERVER_PROTOCOL.read_text())


def test_the_copy_ends_with_the_command_errors():
    assert vertragsteil(Path(wire.__file__).read_text()).rstrip().endswith(
        "return COMMANDS[kind].errors + COMMON_ERRORS")


@pytest.mark.parametrize("name", [*wire.UP_TOPICS, *wire.DOWN_TOPICS])
def test_topic_roundtrip(name):
    assert wire.parse_topic(wire.topic(DEVICE, name)) == (DEVICE, name)


def test_the_device_subscribes_exactly_the_down_filter():
    assert wire.topic(DEVICE, wire.DEVICE_SUBSCRIPTION) == f"smartheat/{DEVICE}/down/#"


@pytest.mark.parametrize("topic", ["smartheat/x", "other/shg-a/up/hello", "smartheat/+/down/config"])
def test_parse_topic_rejects_foreign_shapes(topic):
    assert wire.parse_topic(topic) is None


def test_every_command_error_list_ends_with_the_common_errors():
    for kind in wire.COMMANDS:
        assert wire.command_errors(kind)[-len(wire.COMMON_ERRORS):] == wire.COMMON_ERRORS


def test_documents_have_their_down_topics():
    assert wire.DOKUMENT_TOPICS == {"konfiguration": "down/config", "bedienung": "down/operation"}
    assert set(wire.DOKUMENT_TOPICS.values()) <= set(wire.DOWN_TOPICS)


def test_signature_string_is_the_g3_scheme():
    digest = hashlib.sha256(b"{}").hexdigest()
    assert wire.canonical_string("POST", "/devices/register", 7, b"{}") == (
        f"SHG1\nPOST\n/devices/register\n7\n{digest}".encode())

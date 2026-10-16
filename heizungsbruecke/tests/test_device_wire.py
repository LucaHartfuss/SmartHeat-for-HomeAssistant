"""Plan 5b D1 Task 1: Vertragskopie smartheat_device/wire.py (Spec 5b 2). Die Gleichheit mit dem Server prueft
zusaetzlich der Contract-Check im Dev-Root (Plan 5b-W Teil A)."""
import ast
import hashlib
import os
import re
from pathlib import Path

import pytest

from smartheat_device import wire

DEVICE = "shg-aaaaaaaaaaaaaaaa"
ADDON_REPO = Path(__file__).resolve().parents[2]
DEV_ROOT = Path(os.environ.get("DEV_ROOT") or ADDON_REPO.parent)
SERVER_REPO = DEV_ROOT / "HeizungssteuerungServer"
SERVER_PROTOCOL = SERVER_REPO / "src" / "heizungsserver" / "geraete" / "device_protocol.py"
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


@pytest.mark.skipif(not SERVER_REPO.is_dir(), reason="Server-Repo nicht unter DEV_ROOT (Contract-Check prueft es)")
def test_the_copy_is_literal():
    # Nur ein fehlendes Server-Repo ueberspringt den Test; fehlt bei vorhandenem Repo die Quelle (z. B. nach einem
    # Umzug der Datei), schlaegt er laut fehl statt still zu entfallen.
    assert SERVER_PROTOCOL.is_file(), f"Quelle des Geraeteprotokolls fehlt im Server-Repo: {SERVER_PROTOCOL}"
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


GERAET_QUELLEN = [ADDON_REPO / "heizungsbruecke" / "src", ADDON_REPO / "gateway" / "src"]
# Der Kern importiert nichts (P4): seine Kopien pinnt dieser Test; nur dessen Pins zaehlen, nicht seine Lesezugriffe.
PIN_TEST = ADDON_REPO / "heizungsbruecke" / "tests" / "test_core_vertrag_pin.py"
_VERTRAG_MODUL = "smartheat_device.wire"
_PIN_PRAEFIX = "_VERTRAG_GEPINNT"
# V1-Konstanten, die nur der Server braucht; jede Zeile mit Grund. Kein Eintrag ohne Grund.
NUR_SERVER: set[str] = {
    "MAX_BODY_BYTES",  # nur der Server begrenzt den Bootstrap-Body; die Bausteine liefern wenige hundert Bytes
    "MAX_CLOCK_SKEW_SECONDS",  # nur der Server prueft das Zeitfenster der Signatur
    "UNAUTHENTICATED_ERROR",  # Text der 401-Antwort; das Geraet wertet nur den Status 401 (Spec 1: fuer alle Faelle gleich)
}
NUR_GEPINNT_GERAET: set[str] = {  # Kopien im Kern (smartheat_core darf nichts importieren), nur gepinnt (P4)
    "ABO",  # config_check.ABO_WERTE
    "BINDUNGSPARAMETER_FIELDS",  # bindungsparameter.FELDER
    "BINDUNG_GATEWAY_FIELDS",  # config_check.GATEWAY_BINDUNG_FELDER
    "BINDUNG_HA_FIELDS",  # config_check.HA_BINDUNG_FELDER
    "BINDUNG_HA_OPTIONAL",  # config_check.HA_BINDUNG_OPTIONAL
    "ENERGIE_EINHEITEN",  # bindungsparameter.EINHEITEN
    "HA_HINWEISE",  # config_check.HA_HINWEISE
    "HA_ROLLEN",  # config_check.HA_ROLLEN
    "KREIS_FIELDS",  # config_check.KREIS_FELDER
    "SCHREIBSCHUTZ_FIELDS",  # bindungsparameter.SCHREIBSCHUTZ_FELDER
    "TREIBER_FIELDS",  # config_check.TREIBER_FELDER
    "ZAEHLER",  # bindungsparameter.ZAEHLER
}
OFFEN: set[str] = {  # Ist-Stand 2026-10-16; die Tasks 2-6 leeren es, Task 7 prueft OFFEN == set()
    "INVENTUR_ANLAGE_FIELDS",  # Task 6
    "INVENTUR_ENTITY_FIELDS",  # Task 6
    "INVENTUR_FEATURE_FIELDS",  # Task 6
    "INVENTUR_FILTER_FIELDS",  # Task 6
    "INVENTUR_GATEWAY_FIELDS",  # Task 6
    "INVENTUR_GERAET_FIELDS",  # Task 6
    "INVENTUR_HA_FIELDS",  # Task 6
    "ZIGBEE_ARTEN",  # Task 6
    "ZIGBEE_DEVICE_FIELDS",  # Task 6
}


def _vertragsnamen() -> set[str]:
    tree = ast.parse(Path(wire.__file__).read_text())
    ende = next(n.lineno for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "command_errors")
    namen: set[str] = set()
    for node in tree.body:
        if node.lineno >= ende:
            break
        ziele = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        for ziel in ziele:
            namen |= {n.id for n in ast.walk(ziel) if isinstance(n, ast.Name) and re.fullmatch(r"[A-Z][A-Z0-9_]+", n.id)}
    return namen


def _funktionsleser() -> dict[str, set[str]]:
    """Je Funktion des Vertragsteils (canonical_string, topic, command_errors, ...) die Konstanten, die sie liest: wer
    die Funktion aufruft, liest sie mit."""
    tree = ast.parse(Path(wire.__file__).read_text())
    namen = _vertragsnamen()
    return {node.name: {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and n.id in namen}
            for node in tree.body if isinstance(node, ast.FunctionDef)}


def _zugriffe(path: Path) -> tuple[set[str], set[str]]:
    """(gelesen, gepinnt) wie der Server-Test: `wire.NAME` (from smartheat_device import wire [as x]), `from
    smartheat_device.wire import NAME`; Aufruf einer Vertragsfunktion liest deren Konstanten; Zuweisungen an
    `_VERTRAG_GEPINNT*` sind gepinnt."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliase: set[str] = set()
    direkt: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "smartheat_device":
            aliase |= {a.asname or a.name for a in node.names if a.name == "wire"}
        elif isinstance(node, ast.ImportFrom) and node.module == _VERTRAG_MODUL:
            direkt |= {a.name for a in node.names}
    funktionen = _funktionsleser()

    def zugriffe(wurzel: ast.AST) -> set[str]:
        found = set()
        for n in ast.walk(wurzel):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in aliase:
                found.add(n.attr)
                found |= funktionen.get(n.attr, set())
        return found

    gelesen = {name for name in direkt} | {k for name in direkt for k in funktionen.get(name, set())}
    gepinnt: set[str] = set()
    for node in tree.body:
        ziele = node.targets if isinstance(node, ast.Assign) else []
        if any(isinstance(z, ast.Name) and z.id.startswith(_PIN_PRAEFIX) for z in ziele):
            gepinnt |= zugriffe(node)
        else:
            gelesen |= zugriffe(node)
    return gelesen, gepinnt


def _leser_im_geraet() -> tuple[set[str], set[str]]:
    gelesen: set[str] = set()
    gepinnt: set[str] = set()
    for quelle in GERAET_QUELLEN:
        for path in quelle.rglob("*.py"):
            if path.name != "wire.py" and "__pycache__" not in path.parts:
                leser, pins = _zugriffe(path)
                gelesen |= leser
                gepinnt |= pins
    gepinnt |= _zugriffe(PIN_TEST)[1]
    return gelesen, gepinnt


def test_jede_vertragskonstante_hat_einen_leser_im_geraet():
    gelesen, gepinnt = _leser_im_geraet()
    ohne = _vertragsnamen() - gelesen
    assert ohne - gepinnt == NUR_SERVER | OFFEN
    assert ohne & gepinnt == NUR_GEPINNT_GERAET


def test_jeder_pin_vergleicht_den_vertrag_mit_einer_kopie_aus_kern_oder_laufzeit():
    """Ein Pin `(wire.X, wire.X)` oder `(wire.X, ("a", "b"))` pinnt nichts: das zweite Element muss ein Attribut eines
    Moduls aus smartheat_core oder smartheat_runtime lesen (auch in tuple(...)) und darf wire nicht erwaehnen."""
    tree = ast.parse(PIN_TEST.read_text(encoding="utf-8"))
    module = {a.asname or a.name for n in tree.body if isinstance(n, ast.ImportFrom)
              and n.module in ("smartheat_core", "smartheat_runtime") for a in n.names}
    pins = [n for n in tree.body if isinstance(n, ast.Assign) and any(
        isinstance(z, ast.Name) and z.id.startswith(_PIN_PRAEFIX) for z in n.targets)]
    assert module and pins
    for pin in pins:
        assert isinstance(pin.value, ast.Tuple) and len(pin.value.elts) == 2, ast.unparse(pin)
        vertrag, kopie = pin.value.elts
        assert isinstance(vertrag, ast.Attribute) and isinstance(vertrag.value, ast.Name) and vertrag.value.id == "wire"
        namen = {n.id for n in ast.walk(kopie) if isinstance(n, ast.Name)}
        assert "wire" not in namen, ast.unparse(pin)
        assert any(isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in module
                   for n in ast.walk(kopie)), ast.unparse(pin)


def test_der_leser_erkennt_alias_direktimport_funktionen_und_pins(tmp_path):
    datei = tmp_path / "m.py"
    datei.write_text("from smartheat_device import wire as w\nfrom smartheat_device.wire import QOS\n"
                     "# wire.KOMMENTAR\nX = w.SCHEMA\nY = w.topic('a', 'b')\n_VERTRAG_GEPINNT = {'A': w.HOSTS}\n")
    gelesen, gepinnt = _zugriffe(datei)
    assert {"QOS", "SCHEMA", "TOPIC_PREFIX"} <= gelesen and gepinnt == {"HOSTS"}

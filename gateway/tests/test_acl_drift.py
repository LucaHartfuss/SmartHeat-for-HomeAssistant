"""Drift-Test ACL <-> Bus-Aufrufe (Plan G2b-2 Task 1, Roadmap-Restpunkt G2b-1): Jede publish/subscribe-Stelle im
Gateway-Code ist einem Dienst zugeordnet (CALLERS), und die ACL aus init.py erlaubt sie diesem Dienst nach den
Mosquitto-Regeln. Eine neue Bus-Stelle ohne Eintrag in CALLERS oder ein Topic ausserhalb der ACL laesst den Test
scheitern."""
import ast
from pathlib import Path

import pytest

from smartheat_gateway import init, topics

SRC = Path(init.__file__).resolve().parent
VARIABLE = "+"  # Laufzeitwert (IEEE-Adresse, Meldungsschluessel): genau eine Topic-Ebene
TRANSPORT = {"bus.py"}  # die Bus-Klasse reicht nur durch
ACCESS = {"publish": ("write", "readwrite"), "subscribe": ("read", "readwrite")}

# (Datei relativ zu smartheat_gateway, umgebende Funktion, publish|subscribe) -> Dienste, die diese Stelle ausfuehren
CALLERS: dict[tuple[str, str, str], tuple[str, ...]] = {
    ("agent/context.py", "start", "subscribe"): ("agent",),
    ("agent/lifecycle.py", "apply_config", "publish"): ("agent",),
    ("agent/lifecycle.py", "set_room_target", "publish"): ("agent",),
    ("agent/lifecycle.py", "sign_off", "publish"): ("agent",),
    ("agent/lifecycle.py", "cleanup_after_sign_off", "publish"): ("agent",),
    ("agent/loop.py", "__init__", "subscribe"): ("agent",),
    ("raum.py", "publish", "publish"): ("runtime",),
    ("runtime_main.py", "attach", "subscribe"): ("runtime",),
    ("runtime_main.py", "attach", "publish"): ("runtime",),
    ("sinks.py", "publish", "publish"): ("runtime",),
    ("sinks.py", "push", "publish"): ("runtime",),
    ("sinks.py", "show", "publish"): ("runtime",),
    ("sinks.py", "withdraw", "publish"): ("runtime",),
    ("triggers.py", "start", "subscribe"): ("runtime",),
    ("zigbee.py", "start", "subscribe"): ("agent", "runtime"),
    ("zigbee.py", "write_setpoint", "publish"): ("runtime",),
    ("zigbee.py", "permit_join", "publish"): ("agent",),
}


def _topic(node: ast.expr) -> str:
    """Topic-Muster eines Ausdrucks: Konstanten aus topics.py und Literale woertlich, alles andere eine Ebene (+)."""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "topics":
        return getattr(topics, node.attr)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _topic(node.left) + _topic(node.right)
    if isinstance(node, ast.JoinedStr):
        return "".join(_topic(part.value) if isinstance(part, ast.FormattedValue) else _topic(part)
                       for part in node.values)
    return VARIABLE


class _Calls(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.stack: list[str] = []
        self.found: list[tuple[str, str, str, str]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ACCESS and node.args:
            where = self.stack[-1] if self.stack else "<modul>"
            self.found.append((self.rel, where, func.attr, _topic(node.args[0])))
        self.generic_visit(node)


def bus_calls() -> list[tuple[str, str, str, str]]:
    """(Datei, Funktion, publish|subscribe, Topic-Muster) fuer jede Bus-Stelle im Gateway-Code."""
    found: list[tuple[str, str, str, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in TRANSPORT:
            continue
        visitor = _Calls(rel)
        visitor.visit(ast.parse(path.read_text()))
        found.extend(visitor.found)
    return found


def covers(pattern: str, topic: str) -> bool:
    """Deckt das ACL-Muster das Topic(-Muster)? + und # nach MQTT; ein # im Topic deckt nur ein # im Muster."""
    want, have = pattern.split("/"), topic.split("/")
    for index, part in enumerate(want):
        if part == "#":
            return True
        if index >= len(have) or have[index] == "#":
            return False
        if part != "+" and part != have[index]:
            return False
    return len(want) == len(have)


def allowed(service: str, method: str, topic: str) -> bool:
    return any(access in ACCESS[method] and covers(pattern, topic) for access, pattern in init.ACL_RULES[service])


def test_every_bus_call_is_assigned_to_a_service():
    calls = {(rel, func, method) for rel, func, method, _ in bus_calls()}
    assert calls == set(CALLERS), "Neue oder entfallene Bus-Stelle: CALLERS und die ACL in init.py nachziehen"


@pytest.mark.parametrize("call", bus_calls(), ids=lambda call: f"{call[0]}:{call[1]}:{call[3]}")
def test_acl_allows_every_bus_call(call):
    rel, func, method, topic = call
    assert topic != VARIABLE, f"{rel}:{func}: Topic nicht statisch bestimmbar"
    for service in CALLERS[(rel, func, method)]:
        assert allowed(service, method, topic), f"ACL von {service} erlaubt {method} {topic} nicht ({rel}:{func})"


def test_covers_follows_mqtt_rules():
    assert covers("shg/notify/#", "shg/notify/+") and covers("zigbee2mqtt/+/set", "zigbee2mqtt/+/set")
    assert covers("shg/cmd/#", "shg/cmd/reload") and not covers("shg/cmd/reload", "shg/cmd/#")
    assert not covers("zigbee2mqtt/+/set", "zigbee2mqtt/bridge/request/permit_join")
    assert not covers("shg/status", "shg/status/x") and not covers("shg/+", "shg/notify/+")


def test_zigbee2mqtt_cannot_reach_the_gateway_topics():
    for method, topic in (("publish", topics.CMD_RELOAD), ("publish", topics.STATUS),
                          ("subscribe", topics.CMD_ROOM_TARGET)):
        assert not allowed("zigbee2mqtt", method, topic)


def test_a_missing_acl_line_is_detected(monkeypatch):
    rules = tuple(rule for rule in init.ACL_RULES["runtime"] if rule != ("write", "shg/raum"))
    monkeypatch.setitem(init.ACL_RULES, "runtime", rules)
    assert not allowed("runtime", "publish", topics.RAUM)

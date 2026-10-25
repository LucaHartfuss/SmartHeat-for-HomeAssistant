"""Testhilfe: die Befehlsarten, die ein `CommandRegister` eingetragen hat (nur Tests lesen sie, der Code nie)."""
from smartheat_device.commands import CommandRegister


def registrierte_befehle(register: CommandRegister) -> frozenset[str]:
    return frozenset(register._handlers)

"""Status- und Meldungs-Sinks des Gateways (Spec SHG G2 3.6) auf den lokalen Bus; der Agent reicht weiter."""
from collections.abc import Callable
from datetime import datetime

from smartheat_gateway import topics
from smartheat_gateway.bus import Bus
from smartheat_runtime.notifier import category


class BusStatusSink:
    def __init__(self, bus: Bus) -> None:
        self._bus = bus

    def publish(self, event: dict) -> None:
        self._bus.publish(topics.STATUS, event, retain=True)


class BusNotifySink:
    def __init__(self, bus: Bus, now: Callable[[], datetime]) -> None:
        self._bus = bus
        self._now = now

    def push(self, key: str, message: str) -> None:
        self._bus.publish(topics.NOTIFY_PUSH, {"key": key, "text": message, "ts": self._now().isoformat()})

    def show(self, key: str, message: str) -> None:
        self._bus.publish(
            topics.NOTIFY_PREFIX + key,
            {"kategorie": category(key), "kritisch": True, "text": message, "ts": self._now().isoformat()},
            retain=True,
        )

    def withdraw(self, key: str) -> None:
        self._bus.publish(topics.NOTIFY_PREFIX + key, None, retain=True)

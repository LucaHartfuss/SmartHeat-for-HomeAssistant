"""Test-Doubles des Gateways. FakeBus ist ein In-Speicher-Broker: mehrere Komponenten teilen eine Instanz."""
from paho.mqtt.client import topic_matches_sub

from smartheat_gateway.bus import decode, encode


class FakeBus:
    def __init__(self) -> None:
        self.subscriptions: list[tuple[str, object]] = []
        self.retained: dict[str, object] = {}
        self.published: list[tuple[str, bytes, bool]] = []
        self.hooks: list = []
        self._connected = True

    @property
    def connected(self) -> bool:
        return self._connected

    def subscribe(self, topic_filter, callback) -> None:
        self.subscriptions.append((topic_filter, callback))
        if self._connected:
            for topic, payload in list(self.retained.items()):
                if topic_matches_sub(topic_filter, topic):
                    callback(topic, encode(payload), True)

    def publish(self, topic, payload, *, retain=False) -> None:
        raw = encode(payload)
        self.published.append((topic, raw, retain))
        if retain:
            if raw:
                self.retained[topic] = decode(raw)
            else:
                self.retained.pop(topic, None)
        if not self._connected:
            return
        for topic_filter, callback in list(self.subscriptions):
            if topic_matches_sub(topic_filter, topic):
                callback(topic, raw, False)

    def deliver(self, topic, payload, *, retain=False) -> None:
        """Stellt eine Nachricht an die Abonnenten zu, ohne sie zu speichern (Broker-Replay: retain=True)."""
        raw = encode(payload)
        for topic_filter, callback in list(self.subscriptions):
            if topic_matches_sub(topic_filter, topic):
                callback(topic, raw, retain)

    def decoded(self) -> list[tuple[str, object]]:
        return [(topic, decode(raw)) for topic, raw, _ in self.published]

    def on_connected(self, hook) -> None:
        self.hooks.append(hook)

    def wait_connected(self, timeout=None) -> bool:
        return self._connected

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def disconnect(self) -> None:
        self._connected = False

    def connect(self) -> None:
        self._connected = True
        for topic_filter, callback in list(self.subscriptions):
            for topic, payload in list(self.retained.items()):
                if topic_matches_sub(topic_filter, topic):
                    callback(topic, encode(payload), True)
        for hook in self.hooks:
            hook()

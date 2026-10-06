from fakes import FakeBus

from smartheat_gateway.bus import LocalBus, decode


def test_decode():
    assert decode(b"") is None
    assert decode(b'{"a": 1}') == {"a": 1}
    assert decode(b"online") == "online"


def test_fake_bus_replays_retained_and_matches_wildcards():
    bus = FakeBus()
    bus.publish("zigbee2mqtt/bridge/state", {"state": "online"}, retain=True)
    seen = []
    bus.subscribe("zigbee2mqtt/#", lambda topic, payload, retain: seen.append((topic, decode(payload), retain)))
    assert seen == [("zigbee2mqtt/bridge/state", {"state": "online"}, True)]
    bus.publish("zigbee2mqtt/0x01", {"temperature": 20.5})
    assert seen[-1] == ("zigbee2mqtt/0x01", {"temperature": 20.5}, False)
    bus.publish("zigbee2mqtt/bridge/state", None, retain=True)  # leere retained Nachricht loescht
    late = []
    bus.subscribe("zigbee2mqtt/bridge/state", lambda *args: late.append(args))
    assert late == []


class _ConnectReason:
    is_failure = False


class _StubPaho:
    """Stub fuer den paho-Client: protokolliert SUBSCRIBE; der erste Aufruf aus _on_connect simuliert ein
    subscribe() eines anderen Threads genau zwischen der Momentaufnahme der Abos und dem Setzen von `connected`."""

    def __init__(self, on_first_subscribe) -> None:
        self.subscribed: list[str] = []
        self._on_first = on_first_subscribe

    def subscribe(self, topic_filter, qos=0):
        self.subscribed.append(topic_filter)
        if self._on_first is not None:
            hook, self._on_first = self._on_first, None
            hook()


def test_subscribe_racing_with_connect_is_not_lost():
    bus = LocalBus("127.0.0.1", 1, "test-bus")
    bus.subscribe("shg/status", lambda *args: None)
    stub = _StubPaho(lambda: bus.subscribe("shg/notify/+", lambda *args: None))
    bus._client = stub
    bus._on_connect(stub, None, None, _ConnectReason(), None)
    assert bus.connected
    assert "shg/status" in stub.subscribed and "shg/notify/+" in stub.subscribed

from fakes import FakeBus

from smartheat_gateway.bus import decode


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

from datetime import UTC, datetime

from fakes import FakeBus

from smartheat_gateway import topics
from smartheat_gateway.bus import decode
from smartheat_gateway.sinks import BusNotifySink, BusStatusSink

NOW = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)


def test_status_is_retained():
    bus = FakeBus()
    BusStatusSink(bus).publish({"schema": 2, "status": "regelt"})
    assert bus.retained[topics.STATUS] == {"schema": 2, "status": "regelt"}


def test_notify_show_withdraw_push():
    bus = FakeBus()
    sink = BusNotifySink(bus, lambda: NOW)
    sink.show("raumfuehler:0x01", "Text A")
    assert bus.retained["shg/notify/raumfuehler:0x01"] == {
        "kategorie": "raumfuehler", "kritisch": True, "text": "Text A", "ts": NOW.isoformat(),
    }
    sink.withdraw("raumfuehler:0x01")
    assert "shg/notify/raumfuehler:0x01" not in bus.retained
    sink.push("batterie:x", "Text B")
    topic, payload, retain = bus.published[-1]
    expected = {"key": "batterie:x", "text": "Text B", "ts": NOW.isoformat()}
    assert (topic, decode(payload), retain) == (topics.NOTIFY_PUSH, expected, False)

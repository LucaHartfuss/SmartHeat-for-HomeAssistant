"""Spec 5b 5.1/5.2: Tick und Telemetrie der Laufzeit ueber den Link des Geraets (RuntimeChannel)."""
from smartheat_device.laufzeit import RuntimeChannel
from smartheat_runtime.runtime import EV_MQTT_CONNECTED, EV_SETPOINTS
from smartheat_runtime.worker import RegulationWorker


class Link:
    def __init__(self):
        self.sent, self.connected, self.failures = [], True, 0

    def publish(self, name, payload):
        self.sent.append((name, payload))
        return len(self.sent)


def _channel(link):
    return RuntimeChannel(link.publish, lambda: link.connected, lambda: link.failures)


def test_snapshot_carries_the_effective_room_target():
    link = Link()
    _channel(link).publish_snapshot({"schema": 4, "seq": "s", "room_target": 21.5, "levers": {}})
    assert link.sent == [("up/snapshot", {"schema": 4, "seq": "s", "room_target": 21.5, "levers": {},
                                          "raum_soll_wirksam": 21.5})]


def test_telemetry_goes_to_the_telemetry_topic():
    link = Link()
    _channel(link).publish_telemetry({"room_actual": 20.0})
    assert link.sent == [("telemetry", {"room_actual": 20.0})]


def test_stop_silences_the_runtime_but_the_link_stays():
    link = Link()
    channel = _channel(link)
    channel.stop()
    channel.publish_snapshot({"room_target": 21.0})
    channel.publish_telemetry({"room_actual": 20.0})
    assert link.sent == [] and not channel.is_connected() and link.connected


def test_connection_state_and_failures_come_from_the_link():
    link = Link()
    channel = _channel(link)
    link.failures = 4
    assert channel.is_connected() and channel.connect_failures == 4
    link.connected = False
    assert not channel.is_connected()


def test_connect_and_setpoints_become_worker_events(clock):
    worker, seen = RegulationWorker(clock=clock), []
    worker.register(EV_MQTT_CONNECTED, seen.append)
    worker.register(EV_SETPOINTS, seen.append)
    link = Link()
    channel = _channel(link).binden(worker)  # schon verbunden: sofort EV_MQTT_CONNECTED
    worker.run_pending()  # Ereignis abholen, sonst verschmilzt das naechste verbunden() damit (post_coalesced)
    channel.setpoints({"seq": "s"})
    channel.verbunden()
    worker.run_pending()
    assert [(event.kind, event.data.get("payload")) for event in seen] == [
        (EV_MQTT_CONNECTED, None), (EV_SETPOINTS, {"seq": "s"}), (EV_MQTT_CONNECTED, None)]


def test_nothing_reaches_the_worker_after_stop(clock):
    worker, seen = RegulationWorker(clock=clock), []
    worker.register(EV_SETPOINTS, seen.append)
    worker.register(EV_MQTT_CONNECTED, seen.append)
    link = Link()
    link.connected = False
    channel = _channel(link).binden(worker)
    channel.stop()
    channel.setpoints({"seq": "s"})
    channel.verbunden()
    worker.run_pending()
    assert seen == []

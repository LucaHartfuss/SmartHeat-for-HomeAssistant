"""Spec 5b 5.2: die Laufzeit im Geraete-Pfad mit dem HA-Testaufbau (Fake-HA): Konfiguration ohne Transport, Abo aus
abo_source (kein Abruf beim Server), Tick und Antwort ueber den RuntimeChannel statt ueber einen eigenen MQTT-Client."""
import dataclasses
import json

from test_runtime import OPTIONS, _failsafe_file, _trigger, env  # noqa: F401 - env ist eine Fixture

from heizungsbruecke.host import HaHost
from smartheat_device.laufzeit import RuntimeChannel
from smartheat_runtime import app, entitlement
from smartheat_runtime.runtime import Runtime


class Sent:
    def __init__(self):
        self.messages, self.connected = [], True

    def publish(self, name, payload):
        self.messages.append((name, json.loads(json.dumps(payload))))
        return len(self.messages)

    def of(self, name):
        return [payload for topic, payload in self.messages if topic == name]


def _start(env, monkeypatch, abo):
    original = HaHost.load

    def load(self):
        loaded = original(self)
        config = dataclasses.replace(loaded.config, descriptor=None, credential=None, installation_token=None,
                                     accounts_api_base_url=None, abo_source=lambda: abo)
        return dataclasses.replace(loaded, config=config)

    monkeypatch.setattr(HaHost, "load", load)
    sent, channels = Sent(), []

    def factory(worker):
        channel = RuntimeChannel(sent.publish, lambda: sent.connected, lambda: 0)
        channels.append(channel)
        return channel.binden(worker)

    bridge = app.start(HaHost(dict(OPTIONS), env.ha), env.clock, mqtt_factory=factory)
    assert isinstance(bridge, Runtime)
    bridge.worker.run_pending()
    return bridge, sent, channels


def test_tick_and_answer_go_over_the_device_link(env, monkeypatch):
    bridge, sent, channels = _start(env, monkeypatch, entitlement.ACTIVE)
    _trigger(env, bridge, "sensor.room_target")
    (snapshot,) = sent.of("up/snapshot")
    assert snapshot["raum_soll_wirksam"] == snapshot["room_target"] == env.ha.states["sensor.room_target"]
    assert sent.of("telemetry") and env.abo["queries"] == 0 and env.mqtt_clients == []
    channels[0].setpoints({"schema": 4, "seq": snapshot["seq"], "ts": "x", "status": "ok", "reason": None,
                           "levers": {"curve": 0.95, "room_setpoint": 23.0, "heat_limit": 16.0},
                           "learned": {"curve": 1.0, "heat_limit": 16.0}})
    bridge.worker.run_pending()
    assert bridge.store.state.delivery.pending is None


def test_inactive_abo_from_the_document_runs_locally_without_the_link(env, monkeypatch):
    bridge, sent, channels = _start(env, monkeypatch, entitlement.INACTIVE)
    assert channels == [] and bridge.mqtt_client is None and sent.messages == []
    assert bridge.store.state.abo_inactive_since is not None and _failsafe_file(env)["failsafe_active"] is True
    assert env.abo["queries"] == 0

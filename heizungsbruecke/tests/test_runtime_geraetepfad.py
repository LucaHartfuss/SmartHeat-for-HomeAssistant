"""Spec 5b 5.2 / 5c 1.3: die Laufzeit im Geraete-Pfad mit dem HA-Testaufbau (Fake-HA): Konfiguration ohne Transport,
Abo aus dem Konfigurationsdokument, Tick und Antwort ueber den Link des Geraets (RuntimeChannel) statt ueber einen
eigenen MQTT-Client."""
from test_runtime import _failsafe_file, _links, _set_room_target, _start_bridge, env  # noqa: F401 - env: Fixture

from smartheat_runtime import entitlement
from smartheat_runtime.runtime import Runtime


def test_tick_and_answer_go_over_the_device_link(env):
    bridge = _start_bridge(env)
    assert isinstance(bridge, Runtime)
    bridge.worker.run_pending()
    _set_room_target(env, bridge, 20.5)
    (snapshot,) = env.geraet.sent.of("up/snapshot")
    assert snapshot["raum_soll_wirksam"] == snapshot["room_target"] == 20.5
    assert env.geraet.sent.of("telemetry")
    env.geraet.channel.setpoints({"schema": 4, "seq": snapshot["seq"], "ts": "x", "status": "ok", "reason": None,
                                  "levers": {"curve": 0.95, "room_setpoint": 23.0, "heat_limit": 16.0},
                                  "learned": {"curve": 1.0, "heat_limit": 16.0}})
    bridge.worker.run_pending()
    assert bridge.store.state.delivery.pending is None


def test_inactive_abo_from_the_document_runs_locally_without_the_link(env):
    env.abo["status"] = entitlement.INACTIVE
    bridge = _start_bridge(env)
    bridge.worker.run_pending()
    assert _links(env) == [] and bridge.mqtt_client is None and env.geraet.sent.messages == []
    assert bridge.store.state.abo_inactive_since is not None and _failsafe_file(env)["failsafe_active"] is True

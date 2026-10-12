"""MQTT-Anbindung (mqtt_link): der Sollwert-Callback stellt nur in den Worker ein. Die HA-Trigger: test_geraet_trigger.py."""
from unittest.mock import MagicMock

import pytest
from fakes import runtime_config

from smartheat_runtime import mqtt_link
from smartheat_runtime.runtime import EV_SETPOINTS
from smartheat_runtime.worker import Event, RegulationWorker
from smartheat_transport.connect import ConnectOptions


@pytest.mark.parametrize("raw,retain", [
    (b'{"seq": "s1"}', True), (b"[]", False), (b'"x"', False), (b"{kaputt", False), (b"", False),
])
def test_setpoints_callback_drops_retained_and_non_object_payloads(clock, raw, retain):
    worker = RegulationWorker(clock=clock)
    seen = []
    worker.register(EV_SETPOINTS, seen.append)
    message = MagicMock()
    message.payload = raw
    message.retain = retain

    mqtt_link.make_setpoints_callback(worker)(None, None, message)  # darf nicht werfen
    worker.run_pending()

    assert seen == []


def test_setpoints_callback_posts_fresh_answer_to_worker(clock):
    worker = RegulationWorker(clock=clock)
    seen = []
    worker.register(EV_SETPOINTS, seen.append)
    message = MagicMock()
    message.payload = b'{"seq": "s1", "status": "ok"}'
    message.retain = False

    mqtt_link.make_setpoints_callback(worker)(None, None, message)
    worker.run_pending()

    assert seen == [Event(EV_SETPOINTS, {"payload": {"seq": "s1", "status": "ok"}})]


def test_create_mqtt_client_only_subscribes_the_answers(monkeypatch, clock):
    created = MagicMock()
    factory = MagicMock(return_value=created)
    monkeypatch.setattr("smartheat_runtime.mqtt_link.BridgeMqttClient", factory)

    mqtt_link.create_mqtt_client(
        runtime_config(tenant_id="t1"),
        RegulationWorker(clock=clock),
    )

    options, tenant = factory.call_args.args
    assert options == ConnectOptions(host="127.0.0.1", port=18830, client_id="", username="u", password="p")
    assert tenant == "t1"
    assert factory.call_args.kwargs["transport_kind"] == "mosquitto_cloudflared"
    assert [name for name, _, _ in created.mock_calls] == ["subscribe_setpoints"]

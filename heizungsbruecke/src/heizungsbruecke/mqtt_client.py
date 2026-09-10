import json

import paho.mqtt.client as mqtt


class BridgeMqttClient:
    def __init__(self, host: str, port: int, tenant_id: str):
        self._tenant_id = tenant_id
        self._client = mqtt.Client()
        self._client.connect(host, port)

    def publish_value(self, role: str, value: float, seq: str) -> None:
        topic = f"smartheat/{self._tenant_id}/up/{role}"
        payload = json.dumps({"v": value, "seq": seq})
        self._client.publish(topic, payload)

    def subscribe_down(self, role: str, on_message) -> None:
        topic = f"smartheat/{self._tenant_id}/down/{role}"
        self._client.message_callback_add(topic, on_message)
        self._client.subscribe(topic)

    def loop_start(self) -> None:
        self._client.loop_start()

    def loop_stop(self) -> None:
        self._client.loop_stop()

import json
import logging

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


class BridgeMqttClient:
    def __init__(self, host: str, port: int, tenant_id: str):
        self._tenant_id = tenant_id
        self._subscriptions = {}  # role -> on_message
        self._client = mqtt.Client()
        self._client.on_connect = self._on_connect
        self._client.connect(host, port)

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if self._subscriptions:
            logger.info("MQTT (re-)verbunden, %d Down-Subscription(s) werden (erneut) angemeldet", len(self._subscriptions))
        for role, on_message in self._subscriptions.items():
            self._subscribe(role=role, on_message=on_message)

    def _subscribe(self, role: str, on_message) -> None:
        topic = f"smartheat/{self._tenant_id}/down/{role}"
        self._client.message_callback_add(topic, on_message)
        self._client.subscribe(topic)

    def publish_value(self, role: str, value: float, seq: str) -> None:
        topic = f"smartheat/{self._tenant_id}/up/{role}"
        payload = json.dumps({"v": value, "seq": seq})
        self._client.publish(topic, payload)

    def subscribe_down(self, role: str, on_message) -> None:
        self._subscriptions[role] = on_message
        self._subscribe(role=role, on_message=on_message)

    def loop_start(self) -> None:
        self._client.loop_start()

    def loop_stop(self) -> None:
        self._client.loop_stop()

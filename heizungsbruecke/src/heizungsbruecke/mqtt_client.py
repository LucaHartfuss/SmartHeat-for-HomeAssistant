import json
import logging

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


class BridgeMqttClient:
    def __init__(self, host: str, port: int, tenant_id: str, username: str, password: str):
        self._tenant_id = tenant_id
        self._subscriptions = {}  # role -> on_message
        self._discovery_configs = {}  # (component, object_id) -> config dict
        self._last_status = {}  # object_id -> payload
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.username_pw_set(username, password)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.will_set(self._availability_topic(), payload="offline", retain=True)
        self._client.connect(host, port)

    def _availability_topic(self) -> str:
        return f"smartheat/{self._tenant_id}/status/availability"

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        logger.info("MQTT verbunden (reason_code=%s)", reason_code)
        if self._subscriptions:
            logger.info("MQTT (re-)verbunden, %d Down-Subscription(s) werden (erneut) angemeldet", len(self._subscriptions))
        for role, on_message in self._subscriptions.items():
            self._subscribe(role=role, on_message=on_message)
        if self._discovery_configs:
            logger.info("MQTT (re-)verbunden, %d Discovery-Config(s) werden (erneut) veroeffentlicht", len(self._discovery_configs))
        for (component, object_id), config in self._discovery_configs.items():
            self._publish_discovery(component=component, object_id=object_id, config=config)
        if self._last_status:
            logger.info("MQTT (re-)verbunden, %d Status-Payload(s) werden (erneut) veroeffentlicht", len(self._last_status))
        for object_id, payload in self._last_status.items():
            self._publish_status(object_id=object_id, payload=payload)
        self._client.publish(self._availability_topic(), "online", retain=True)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        logger.warning("MQTT-Verbindung getrennt (reason_code=%s) - Reconnect laeuft ueber paho automatisch", reason_code)

    def _subscribe(self, role: str, on_message) -> None:
        topic = f"smartheat/{self._tenant_id}/down/{role}"
        self._client.message_callback_add(topic, on_message)
        self._client.subscribe(topic, 1)

    def _publish_discovery(self, component: str, object_id: str, config: dict) -> None:
        topic = f"homeassistant/{component}/heizungsbruecke_{self._tenant_id}/{object_id}/config"
        self._client.publish(topic, json.dumps(config), retain=True)

    def _publish_status(self, object_id: str, payload: str) -> None:
        topic = f"smartheat/{self._tenant_id}/status/{object_id}"
        self._client.publish(topic, payload, retain=True)

    def publish_value(self, role: str, value: float, seq: str) -> None:
        topic = f"smartheat/{self._tenant_id}/up/{role}"
        payload = json.dumps({"v": value, "seq": seq})
        self._client.publish(topic, payload, qos=1)

    def publish_discovery(self, component: str, object_id: str, config: dict) -> None:
        """Publishes a retained MQTT Discovery config so Home Assistant's MQTT
        integration creates the entity automatically -- no configuration.yaml needed
        on the customer side. Stored so it is replayed on every reconnect (see
        _on_connect), the same way down-subscriptions already are.
        """
        self._discovery_configs[(component, object_id)] = config
        self._publish_discovery(component=component, object_id=object_id, config=config)

    def publish_status(self, object_id: str, payload: str) -> None:
        self._last_status[object_id] = payload
        self._publish_status(object_id=object_id, payload=payload)

    def subscribe_down(self, role: str, on_message) -> None:
        self._subscriptions[role] = on_message
        self._subscribe(role=role, on_message=on_message)

    def loop_start(self) -> None:
        self._client.loop_start()

    def loop_stop(self) -> None:
        self._client.loop_stop()

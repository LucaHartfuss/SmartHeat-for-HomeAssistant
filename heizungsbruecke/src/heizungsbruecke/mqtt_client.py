import json
import logging

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

# CONNACK-Reason-Codes, bei denen der Broker die Zugangsdaten abgelehnt hat (MQTT 3.1.1
# rc 4/5 werden von paho 2.x auf 134/135 abgebildet) -- typischer Fall: Credentials beim
# Suspend des Tenants widerrufen, siehe Abo-inaktiv-Modus in abo.py.
_AUTH_REJECTED_REASON_CODES = frozenset({134, 135})

# Payload des Last Will auf dem Availability-Topic; stop() veroeffentlicht ihn selbst,
# weil ein sauberes disconnect() den Last Will nicht ausloest.
_AVAILABILITY_OFFLINE = "offline"

# Obergrenze fuer paho's Reconnect-Backoff: der Broker ist
# nur ueber cloudflared_access_mqtt erreichbar, das beim Booten evtl. noch nicht laeuft --
# das Add-on wartet darauf, statt sich zu beenden.
_RECONNECT_MAX_DELAY_SECONDS = 120


class BridgeMqttClient:
    def __init__(
        self, host: str, port: int, tenant_id: str, username: str, password: str,
        on_auth_rejected=None, on_connected=None,
    ):
        self._tenant_id = tenant_id
        self._host, self._port = host, port
        self._discovery_configs = {}  # (component, object_id) -> config dict
        self._last_status = {}  # object_id -> payload
        self._setpoints_callback = None
        self._on_auth_rejected = on_auth_rejected
        self._on_connected = on_connected
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.username_pw_set(username, password)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_connect_fail = self._on_connect_fail
        self._client.will_set(self._availability_topic(), payload=_AVAILABILITY_OFFLINE, retain=True)
        self._client.reconnect_delay_set(min_delay=1, max_delay=_RECONNECT_MAX_DELAY_SECONDS)
        # Kein blockierender Connect (B10): paho verbindet nach loop_start() selbst und
        # versucht es bei Fehlschlag dauerhaft weiter (loop_forever mit
        # retry_first_connection=True) -- kein Retry-Budget, kein Exit.
        self._client.connect_async(host, port)

    def _availability_topic(self) -> str:
        return f"smartheat/{self._tenant_id}/status/availability"

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        # reason_code ist in Produktion ein paho-ReasonCode; Tests uebergeben auch 0.
        if getattr(reason_code, "is_failure", False):
            logger.error("MQTT-Verbindung vom Broker abgelehnt (reason_code=%s)", reason_code)
            if getattr(reason_code, "value", None) in _AUTH_REJECTED_REASON_CODES and self._on_auth_rejected is not None:
                try:
                    self._on_auth_rejected(self)
                except Exception:
                    logger.exception("Fehler bei der Behandlung der abgelehnten MQTT-Anmeldung")
            return
        logger.info("MQTT verbunden (reason_code=%s)", reason_code)
        if self._setpoints_callback is not None:
            self._subscribe_setpoints()
        if self._discovery_configs:
            logger.info("MQTT (re-)verbunden, %d Discovery-Config(s) werden (erneut) veroeffentlicht", len(self._discovery_configs))
        for (component, object_id), config in self._discovery_configs.items():
            self._publish_discovery(component=component, object_id=object_id, config=config)
        if self._last_status:
            logger.info("MQTT (re-)verbunden, %d Status-Payload(s) werden (erneut) veroeffentlicht", len(self._last_status))
        for object_id, payload in self._last_status.items():
            self._publish_status(object_id=object_id, payload=payload)
        self._client.publish(self._availability_topic(), "online", retain=True)
        if self._on_connected is not None:
            try:
                self._on_connected(self)
            except Exception:
                logger.exception("Fehler bei der Behandlung der MQTT-Verbindung")

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        logger.warning("MQTT-Verbindung getrennt (reason_code=%s) - Reconnect laeuft ueber paho automatisch", reason_code)

    def _on_connect_fail(self, client, userdata) -> None:
        logger.error(
            "MQTT-Verbindung zu %s:%s fehlgeschlagen - laeuft das Add-on 'cloudflared_access_mqtt' "
            "und lauscht es auf Port %s? paho versucht es automatisch weiter.",
            self._host, self._port, self._port,
        )

    def _setpoints_topic(self) -> str:
        return f"smartheat/{self._tenant_id}/down/setpoints"

    def _subscribe_setpoints(self) -> None:
        topic = self._setpoints_topic()
        self._client.message_callback_add(topic, self._setpoints_callback)
        self._client.subscribe(topic, 1)

    def _publish_discovery(self, component: str, object_id: str, config: dict) -> None:
        topic = f"homeassistant/{component}/heizungsbruecke_{self._tenant_id}/{object_id}/config"
        self._client.publish(topic, json.dumps(config), retain=True)

    def _publish_status(self, object_id: str, payload: str) -> None:
        topic = f"smartheat/{self._tenant_id}/status/{object_id}"
        self._client.publish(topic, payload, retain=True)

    def publish_telemetry(self, payload: dict) -> None:
        topic = f"smartheat/{self._tenant_id}/telemetry"
        self._client.publish(topic, json.dumps(payload), qos=1)

    def publish_snapshot(self, payload: dict) -> None:
        self._client.publish(f"smartheat/{self._tenant_id}/up/snapshot", json.dumps(payload), qos=1)

    def is_connected(self) -> bool:
        return self._client.is_connected()

    def publish_discovery(self, component: str, object_id: str, config: dict) -> None:
        """Publishes a retained MQTT Discovery config so Home Assistant's MQTT
        integration creates the entity automatically -- no configuration.yaml needed
        on the customer side. Stored so it is replayed on every reconnect (see
        _on_connect), the same way the setpoints subscription already is.
        """
        self._discovery_configs[(component, object_id)] = config
        self._publish_discovery(component=component, object_id=object_id, config=config)

    def publish_status(self, object_id: str, payload: str) -> None:
        self._last_status[object_id] = payload
        self._publish_status(object_id=object_id, payload=payload)

    def subscribe_setpoints(self, on_message) -> None:
        self._setpoints_callback = on_message
        self._subscribe_setpoints()

    def loop_start(self) -> None:
        self._client.loop_start()

    def loop_stop(self) -> None:
        self._client.loop_stop()

    def stop(self) -> None:
        """Beendet die Verbindung dauerhaft (kein Auto-Reconnect mehr). Auch aus dem
        paho-Netzwerk-Thread selbst aufrufbar: loop_stop() joint dann nicht.
        Veroeffentlicht vorher best effort den Last-Will-Payload, da ein sauberes
        disconnect() den LWT nicht ausloest und HA die Entities sonst weiter als
        "online" anzeigen wuerde."""
        try:
            self._client.publish(self._availability_topic(), _AVAILABILITY_OFFLINE, retain=True)
        except Exception:
            logger.warning("Availability 'offline' konnte vor dem Trennen nicht veroeffentlicht werden")
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()

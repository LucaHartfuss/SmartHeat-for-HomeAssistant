import json
import logging

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

# CONNACK-Reason-Codes, bei denen der Broker die Zugangsdaten abgelehnt hat (MQTT 3.1.1
# rc 4/5 werden von paho 2.x auf 134/135 abgebildet) -- typischer Fall: Credentials beim
# Suspend des Tenants widerrufen, siehe Abo-inaktiv-Modus in abo.py.
_AUTH_REJECTED_REASON_CODES = frozenset({134, 135})

# Obergrenze fuer paho's Reconnect-Backoff: der Broker ist
# nur ueber cloudflared_access_mqtt erreichbar, das beim Booten evtl. noch nicht laeuft --
# das Add-on wartet darauf, statt sich zu beenden.
_RECONNECT_MAX_DELAY_SECONDS = 120


class BridgeMqttClient:
    """Snapshots und Telemetrie hoch, Antworten runter -- sonst nichts. Kein Last Will und keine
    Nachricht unter smartheat/<tenant>/status/ oder homeassistant/: der Status fuer den Kunden
    laeuft seit 0.20.0 lokal ueber die SmartHeat-Integration (Spec TP7 1.4). Mosquitto selbst
    lehnt einen Connect mit einem von der ACL nicht erlaubten Will-Topic NICHT ab (widerlegte
    Annahme aus Praezisierung 1, siehe tests/test_mosquitto_will_acl.sh) -- Add-ons < 0.20.0
    behalten ihre Verbindung deshalb auch nach `refresh-acl`."""

    def __init__(
        self, host: str, port: int, tenant_id: str, username: str, password: str,
        on_auth_rejected=None, on_connected=None,
    ):
        self._tenant_id = tenant_id
        self._host, self._port = host, port
        self._setpoints_callback = None
        self._on_auth_rejected = on_auth_rejected
        self._on_connected = on_connected
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.username_pw_set(username, password)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_connect_fail = self._on_connect_fail
        self._client.reconnect_delay_set(min_delay=1, max_delay=_RECONNECT_MAX_DELAY_SECONDS)
        # Kein blockierender Connect (B10): paho verbindet nach loop_start() selbst und
        # versucht es bei Fehlschlag dauerhaft weiter (loop_forever mit
        # retry_first_connection=True) -- kein Retry-Budget, kein Exit.
        self._client.connect_async(host, port)

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

    def publish_telemetry(self, payload: dict) -> None:
        topic = f"smartheat/{self._tenant_id}/telemetry"
        self._client.publish(topic, json.dumps(payload), qos=1)

    def publish_snapshot(self, payload: dict) -> None:
        self._client.publish(f"smartheat/{self._tenant_id}/up/snapshot", json.dumps(payload), qos=1)

    def is_connected(self) -> bool:
        return self._client.is_connected()

    def subscribe_setpoints(self, on_message) -> None:
        self._setpoints_callback = on_message
        self._subscribe_setpoints()

    def loop_start(self) -> None:
        self._client.loop_start()

    def loop_stop(self) -> None:
        self._client.loop_stop()

    def stop(self) -> None:
        """Beendet die Verbindung dauerhaft (kein Auto-Reconnect mehr). Auch aus dem
        paho-Netzwerk-Thread selbst aufrufbar: loop_stop() joint dann nicht."""
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()

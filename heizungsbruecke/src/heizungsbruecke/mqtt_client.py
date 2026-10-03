import json
import logging
import sys
from collections.abc import Callable

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from smartheat_transport.connect import (
    CONNECT_ERROR_NETWORK,
    CONNECT_ERROR_TLS,
    ConnectOptions,
    apply,
    classify_connect_error,
)
from smartheat_transport.descriptor import KIND_MOSQUITTO

logger = logging.getLogger(__name__)

# CONNACK-Reason-Codes, bei denen der Broker die Zugangsdaten abgelehnt hat (MQTT 3.1.1
# rc 4/5 werden von paho 2.x auf 134/135 abgebildet) -- typischer Fall: Credentials beim
# Suspend des Tenants widerrufen, siehe Abo-inaktiv-Modus in abo.py.
_AUTH_REJECTED_REASON_CODES = frozenset({134, 135})

# Obergrenze fuer paho's Reconnect-Backoff: der Broker bzw. der lokale Tunnel ist beim Booten evtl. noch
# nicht erreichbar -- das Add-on wartet darauf, statt sich zu beenden.
_RECONNECT_MAX_DELAY_SECONDS = 120

_CONNECT_ERROR_TEXT = {
    CONNECT_ERROR_TLS: "TLS-Fehler (Zertifikat abgelehnt oder Handshake abgebrochen)",
    CONNECT_ERROR_NETWORK: "Netzwerkfehler (Broker nicht erreichbar)",
}


class BridgeMqttClient:
    """Snapshots und Telemetrie hoch, Antworten runter -- sonst nichts. Kein Last Will und keine
    Nachricht unter smartheat/<tenant>/status/ oder homeassistant/: der Status fuer den Kunden
    laeuft seit 0.20.0 lokal ueber die SmartHeat-Integration (Spec TP7 1.4). Mosquitto selbst
    lehnt einen Connect mit einem von der ACL nicht erlaubten Will-Topic NICHT ab (siehe
    tests/test_mosquitto_will_acl.sh) -- Add-ons < 0.20.0 behalten ihre Verbindung deshalb
    auch nach `refresh-acl`."""

    def __init__(
        self, options: ConnectOptions, tenant_id: str, *, transport_kind: str,
        on_auth_rejected: Callable[["BridgeMqttClient"], None] | None = None,
        on_connected: Callable[["BridgeMqttClient"], None] | None = None,
    ):
        self._tenant_id = tenant_id
        self._host, self._port = options.host, options.port
        self._transport_kind = transport_kind
        self._setpoints_callback = None
        self._on_auth_rejected = on_auth_rejected
        self._on_connected = on_connected
        # Gescheiterte Verbindungsversuche in Folge (paho-Thread schreibt, Worker liest; ein int ist
        # unter dem GIL atomar). Basis der transportneutralen Abo-Erkennung (abo.handle_connection_failing).
        self.connect_failures = 0
        self._client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=options.client_id)
        apply(self._client, options)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_connect_fail = self._on_connect_fail
        self._client.reconnect_delay_set(min_delay=1, max_delay=_RECONNECT_MAX_DELAY_SECONDS)
        # Kein blockierender Connect (B10): paho verbindet nach loop_start() selbst und
        # versucht es bei Fehlschlag dauerhaft weiter (loop_forever mit
        # retry_first_connection=True) -- kein Retry-Budget, kein Exit.
        self._client.connect_async(options.host, options.port)

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        # reason_code ist in Produktion ein paho-ReasonCode; Tests uebergeben auch 0.
        if getattr(reason_code, "is_failure", False):
            logger.error("MQTT-Anmeldung vom Broker abgelehnt (reason_code=%s)", reason_code)
            if getattr(reason_code, "value", None) in _AUTH_REJECTED_REASON_CODES and self._on_auth_rejected is not None:
                try:
                    self._on_auth_rejected(self)
                except Exception:
                    logger.exception("Fehler bei der Behandlung der abgelehnten MQTT-Anmeldung")
            return
        self.connect_failures = 0
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
        # paho ruft das im except-Block von loop_forever auf: sys.exception() ist der Verbindungsfehler.
        self.connect_failures += 1
        kind = classify_connect_error(sys.exception())
        hint = ""
        if self._transport_kind == KIND_MOSQUITTO and kind == CONNECT_ERROR_NETWORK:
            hint = f" - laeuft das Add-on 'cloudflared_access_mqtt' und lauscht es auf Port {self._port}?"
        logger.error(
            "MQTT-Verbindung zu %s:%s fehlgeschlagen: %s, %d. Versuch in Folge%s - paho versucht es automatisch weiter.",
            self._host, self._port, _CONNECT_ERROR_TEXT.get(kind, "unbekannter Fehler"), self.connect_failures, hint,
        )

    def _setpoints_topic(self) -> str:
        return f"smartheat/{self._tenant_id}/down/setpoints"

    def _subscribe_setpoints(self) -> None:
        topic = self._setpoints_topic()
        callback = self._setpoints_callback
        # Nur aus _on_connect aufgerufen, nachdem dort auf "is not None" geprueft wurde; einmal
        # gesetzt (subscribe_setpoints()) wird das Feld nie wieder auf None zurueckgesetzt.
        assert callback is not None
        self._client.message_callback_add(topic, callback)
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

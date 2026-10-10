"""Die eine MQTT-Verbindung des Geraets (Spec 5b 5.1, Weichenstellung 2): MQTT 5, Client-ID = Geraete-ID (= Thing),
persistente Session (clean_start=False, Ablauf 3600 s, AN-9), TLS mit dem Geraete-Zertifikat aus dem Bootstrap ueber
smartheat_transport.connect (derselbe TLS-Weg wie im Add-on). Nach jedem Verbinden abonniert der Link genau den Filter
down/# (AN-6) und meldet "verbunden" (das Geraet schickt dann hello). Nachrichten auf fremden Topics und retained
Nachrichten werden verworfen. Gesendet wird nur bei bestehender Verbindung (sonst None: paho staute QoS-1-Nachrichten
unbegrenzt, AU-035); was ausfaellt, gleicht hello aus.

Anmeldefehler: TLS-Fehler, ein abgelehntes CONNACK und eine Trennung ohne CONNACK (IoT Core bricht bei einem
gesperrten Zertifikat den TLS-Aufbau ab, AN-1) zaehlen; nach AUTH_FAILURES_FOR_RESCUE in Folge meldet der Link
"anmeldung_scheitert" (Rettungsweg im Geraet). Netzfehler zaehlen dafuer nicht; nach PORT_FALLBACK_FAILURES Netzfehlern
in Folge wechselt der Link zwischen dem Port des Bootstraps und 443 mit ALPN, wenn der Broker ALPN anbietet (AN-3).
Alle paho-Rueckrufe melden nur Ereignisse; den Wechsel fuehrt das Geraet in seinem Thread aus."""
import json
import logging
import sys
from collections.abc import Callable
from typing import Any

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from smartheat_device import wire
from smartheat_device.bootstrap import Zugang
from smartheat_transport.connect import (
    CONNECT_ERROR_NETWORK,
    CONNECT_ERROR_TLS,
    apply,
    classify_connect_error,
    connect_options,
)
from smartheat_transport.descriptor import CREDENTIAL_CERTIFICATE, KIND_IOT_CORE, Credential, Descriptor

logger = logging.getLogger(__name__)

AUTH_FAILURES_FOR_RESCUE = 3
PORT_FALLBACK_FAILURES = 3
KEEPALIVE_SECONDS = 60
RECONNECT_MAX_DELAY_SECONDS = 120
PORT_443 = 443
VERBUNDEN = "verbunden"
GETRENNT = "getrennt"
ANMELDUNG_SCHEITERT = "anmeldung_scheitert"
NACHRICHT = "nachricht"
GESENDET = "gesendet"


def _paho_client(client_id: str) -> Any:
    return mqtt.Client(CallbackAPIVersion.VERSION2, client_id=client_id, protocol=mqtt.MQTTv5)


class Link:
    def __init__(self, device_id: str, zugang: Zugang, private_key_pem: str, ereignis: Callable[..., None], *,
                 client_factory: Callable[[str], Any] = _paho_client) -> None:
        self.device_id = device_id
        self._zugang, self._key = zugang, private_key_pem
        self._ereignis, self._factory = ereignis, client_factory
        self.port = zugang.endpoint.port
        self.connect_failures = 0  # Laufzeit: Verbindungswaechter (app._on_connection_check)
        self._auth_failures = 0
        self._net_failures = 0
        self._connack = False
        self._abgelehnt = False  # abgelehntes CONNACK schon gezaehlt; paho ruft danach on_disconnect
        self._wechsel = False
        self._client: Any = None

    @property
    def connected(self) -> bool:
        client = self._client
        return client is not None and client.is_connected()

    def start(self) -> None:
        """Wirft TransportConfigError, wenn Zertifikat, Schluessel oder CA nicht zusammenpassen."""
        self._connack = self._abgelehnt = False
        self._client = self._neuer_client()
        self._client.loop_start()

    def stop(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.disconnect()
        finally:
            client.loop_stop()

    def publish(self, name: str, payload: dict) -> int | None:
        if name not in wire.UP_TOPICS:
            raise ValueError(f"{name} ist kein Topic des Geraets")
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        if len(raw) > wire.MAX_MESSAGE_BYTES:
            raise ValueError(f"{name}: {len(raw)} Bytes, erlaubt {wire.MAX_MESSAGE_BYTES}")
        client = self._client
        if client is None or not client.is_connected():
            return None
        info = client.publish(wire.topic(self.device_id, name), raw, qos=wire.QOS, retain=False)
        return info.mid if info.rc == mqtt.MQTT_ERR_SUCCESS else None

    def port_wechseln_falls_noetig(self) -> None:
        """Im Geraete-Thread: nach PORT_FALLBACK_FAILURES Netzfehlern neuer Client auf dem anderen Port."""
        if not self._wechsel:
            return
        self.stop()
        self._wechsel = False
        self.port = PORT_443 if self.port != PORT_443 else self._zugang.endpoint.port
        logger.warning("MQTT: Broker nicht erreichbar, Wechsel auf Port %d", self.port)
        self.start()

    # --- intern ---

    def _descriptor(self) -> Descriptor:
        endpoint = self._zugang.endpoint
        alpn = endpoint.alpn if self.port == PORT_443 else None
        return Descriptor(KIND_IOT_CORE, endpoint.host, self.port, alpn, endpoint.ca_pem, self.device_id)

    def _neuer_client(self) -> Any:
        options = connect_options(self._descriptor(), Credential(
            CREDENTIAL_CERTIFICATE, certificate_pem=self._zugang.certificate_pem, private_key_pem=self._key))
        client = self._factory(self.device_id)
        apply(client, options)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_connect_fail = self._on_connect_fail
        client.on_message = self._on_message
        client.on_publish = self._on_publish
        client.reconnect_delay_set(min_delay=1, max_delay=RECONNECT_MAX_DELAY_SECONDS)
        properties = Properties(PacketTypes.CONNECT)
        properties.SessionExpiryInterval = wire.SESSION_EXPIRY_SECONDS
        client.connect_async(options.host, options.port, keepalive=KEEPALIVE_SECONDS, clean_start=False,
                             properties=properties)
        return client

    def _fallback_moeglich(self) -> bool:
        endpoint = self._zugang.endpoint
        return endpoint.alpn is not None and endpoint.port != PORT_443

    def _melden(self, *event) -> None:
        """Ereignis ans Geraet; ein Fehler dort darf den paho-Thread nicht beenden (paho wirft Rueckruf-Fehler weiter)."""
        try:
            self._ereignis(*event)
        except Exception as error:
            logger.error("MQTT: Ereignis %s wurde im Geraet nicht verarbeitet (%s)", event[0], type(error).__name__)

    def _anmeldefehler(self) -> None:
        self._auth_failures += 1
        if self._auth_failures >= AUTH_FAILURES_FOR_RESCUE:
            self._auth_failures = 0
            self._melden(ANMELDUNG_SCHEITERT)

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if getattr(reason_code, "is_failure", False):
            logger.error("MQTT-Anmeldung abgelehnt (reason_code=%s)", reason_code)
            self.connect_failures += 1
            self._abgelehnt = True
            self._anmeldefehler()
            return
        self._connack = True
        self._abgelehnt = False
        self._wechsel = False  # Verbindung steht wieder: ein noch nicht ausgefuehrter Portwechsel ist hinfaellig
        self.connect_failures = self._auth_failures = self._net_failures = 0
        client.subscribe(wire.topic(self.device_id, wire.DEVICE_SUBSCRIPTION), wire.QOS)
        logger.info("MQTT verbunden (Port %d)", self.port)
        self._melden(VERBUNDEN)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        if self._connack:
            self._connack = False
            logger.warning("MQTT getrennt (reason_code=%s), paho verbindet neu", reason_code)
            self._melden(GETRENNT)
            return
        if self._abgelehnt:  # paho trennt nach einem abgelehnten CONNACK; der Versuch ist schon gezaehlt
            self._abgelehnt = False
            return
        self.connect_failures += 1
        self._anmeldefehler()

    def _on_connect_fail(self, client, userdata) -> None:
        self._abgelehnt = False  # neuer Versuch
        self.connect_failures += 1
        kind = classify_connect_error(sys.exception())
        if kind == CONNECT_ERROR_TLS:
            logger.error("MQTT: TLS-Fehler beim Verbinden (Zertifikat abgelehnt oder Handshake abgebrochen)")
            self._anmeldefehler()
        elif kind == CONNECT_ERROR_NETWORK:
            self._net_failures += 1
            logger.warning("MQTT: Broker auf Port %d nicht erreichbar, %d. Versuch in Folge", self.port,
                           self._net_failures)
            if self._net_failures >= PORT_FALLBACK_FAILURES and self._fallback_moeglich():
                self._net_failures = 0
                self._wechsel = True
        else:
            logger.warning("MQTT: Verbindung gescheitert (unbekannter Fehler)")

    def _on_message(self, client, userdata, message) -> None:
        if message.retain:
            return
        parsed = wire.parse_topic(message.topic)
        if parsed is None or parsed[0] != self.device_id or not parsed[1].startswith("down/"):
            return
        self._melden(NACHRICHT, parsed[1], bytes(message.payload))

    def _on_publish(self, client, userdata, mid, reason_code, properties) -> None:
        self._melden(GESENDET, mid)

"""Lokaler Bus (Spec SHG G2 6.1): kleine paho-Huelle fuer Agent und Laufzeit, getrennt von der MQTT-Verbindung zum
Server (smartheat_transport). Callbacks laufen im paho-Thread und duerfen nur Daten ablegen bzw. Ereignisse in den
Worker stellen. Abos werden bei jedem (Wieder-)Verbinden erneuert."""
import json
import logging
import os
import threading
from collections.abc import Callable
from typing import Protocol

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

logger = logging.getLogger(__name__)

Callback = Callable[[str, bytes, bool], None]


def decode(payload: bytes) -> object | None:
    if not payload:
        return None
    text = payload.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def encode(payload) -> bytes:
    if payload is None:
        return b""
    if isinstance(payload, str):
        return payload.encode()
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()


class Bus(Protocol):
    @property
    def connected(self) -> bool: ...
    def subscribe(self, topic_filter: str, callback: Callback) -> None: ...
    def publish(self, topic: str, payload, *, retain: bool = False) -> None: ...
    def on_connected(self, hook: Callable[[], None]) -> None: ...
    def wait_connected(self, timeout: float | None = None) -> bool: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...


def credentials_from_env() -> tuple[str, str] | None:
    """Zugangsdaten des Dienstes am lokalen Bus (Plan G2b-1 Task 7): Datei aus SHG_BUS_CREDENTIALS, geschrieben vom
    Init-Schritt. Ohne Variable anonym (Tests). Gesetzt, aber unlesbar: Fehler, damit Compose den Dienst neu startet
    statt ihn anonym (und damit abgewiesen) laufen zu lassen."""
    path = os.environ.get("SHG_BUS_CREDENTIALS")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return str(data["username"]), str(data["password"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RuntimeError(f"Zugangsdaten fuer den lokalen Bus unlesbar ({type(error).__name__})") from None


class LocalBus:
    def __init__(self, host: str, port: int, client_id: str, credentials: tuple[str, str] | None = None) -> None:
        self._host, self._port = host, port
        self._client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=client_id)
        if credentials is not None:
            self._client.username_pw_set(*credentials)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._subscriptions: list[tuple[str, Callback]] = []
        self._hooks: list[Callable[[], None]] = []
        self._connected = threading.Event()
        self._lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def subscribe(self, topic_filter: str, callback: Callback) -> None:
        # Anhaengen und `connected` lesen unter derselben Sperre wie Momentaufnahme+set() in _on_connect: das Abo
        # steht entweder in der Momentaufnahme oder wird hier gesendet (doppeltes SUBSCRIBE ist harmlos).
        with self._lock:
            self._subscriptions.append((topic_filter, callback))
            connected = self._connected.is_set()
        if connected:
            self._client.subscribe(topic_filter, qos=1)

    def publish(self, topic: str, payload, *, retain: bool = False) -> None:
        self._client.publish(topic, encode(payload), qos=1, retain=retain)

    def on_connected(self, hook: Callable[[], None]) -> None:
        self._hooks.append(hook)

    def wait_connected(self, timeout: float | None = None) -> bool:
        return self._connected.wait(timeout)

    def start(self) -> None:
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.connect_async(self._host, self._port, keepalive=30)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            logger.warning("Lokaler Bus lehnt die Verbindung ab: %s", reason_code)
            return
        with self._lock:
            filters = [topic_filter for topic_filter, _ in self._subscriptions]
            self._connected.set()
        for topic_filter in filters:
            client.subscribe(topic_filter, qos=1)
        for hook in self._hooks:
            try:
                hook()
            except Exception:
                logger.exception("Fehler im Verbindungs-Hook des lokalen Busses")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        self._connected.clear()
        logger.warning("Lokaler Bus getrennt (%s), paho verbindet neu", reason_code)

    def _on_message(self, client, userdata, message) -> None:
        with self._lock:
            matching = [
                cb for topic_filter, cb in self._subscriptions if mqtt.topic_matches_sub(topic_filter, message.topic)
            ]
        for callback in matching:
            try:
                callback(message.topic, message.payload, bool(message.retain))
            except Exception:
                logger.exception("Fehler beim Verarbeiten von %s", message.topic)

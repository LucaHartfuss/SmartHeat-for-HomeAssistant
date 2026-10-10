"""MQTT-Anbindung der Laufzeit (Spec SHG 2: die Laufzeit haelt als Einzige die MQTT-Verbindung). Der paho-Thread
stellt nur Ereignisse in den Regel-Worker ein und aendert nie selbst Zustand."""
import json
import logging

from smartheat_runtime.runtime import EV_AUTH_REJECTED, EV_MQTT_CONNECTED, EV_SETPOINTS
from smartheat_runtime.runtime_config import RuntimeConfig
from smartheat_runtime.worker import Event, RegulationWorker
from smartheat_transport.connect import connect_options
from smartheat_transport.mqtt_client import BridgeMqttClient

logger = logging.getLogger(__name__)


def make_setpoints_callback(worker: RegulationWorker):
    """paho-Callback fuer down/setpoints: parst nur und stellt die Antwort ein. Retained
    Nachrichten (Broker-Replay beim (Re-)Subscribe) und Nicht-Objekte werden verworfen."""
    def _callback(client, userdata, message):
        try:
            if message.retain:
                logger.info("Retained Setpoints-Nachricht beim (Re-)Subscribe uebersprungen (Broker-Replay)")
                return
            payload = json.loads(message.payload)
            if not isinstance(payload, dict):
                logger.warning("Setpoints-Nachricht ist kein JSON-Objekt, verworfen: %r", payload)
                return
            worker.post(Event(EV_SETPOINTS, {"payload": payload}))
        except Exception:
            logger.exception("Setpoints-Nachricht nicht lesbar, verworfen")
    return _callback


def create_mqtt_client(config: RuntimeConfig, worker: RegulationWorker) -> BridgeMqttClient:
    """Verbindet asynchron, ohne Retry-Budget und ohne Exit, ueber den Transport der Konfiguration (beim
    Start schon von config.resolve_transport geprueft). Die Subscription wird bei jedem (Re-)Connect
    erneuert; die Auth-Ablehnung aus dem paho-Thread wird gebuendelt eingestellt (paho meldet sie im
    Backoff mehrfach)."""
    if config.descriptor is None or config.credential is None:
        raise ValueError("Laufzeit ohne Transport-Deskriptor: der Host muss eine mqtt_factory uebergeben "
                         "(Geraete-Pfad, Spec 5b 5.2)")
    client = BridgeMqttClient(
        connect_options(config.descriptor, config.credential), config.tenant_id, transport_kind=config.descriptor.kind,
        on_auth_rejected=lambda _client: worker.post_coalesced(EV_AUTH_REJECTED),
        on_connected=lambda _client: worker.post_coalesced(EV_MQTT_CONNECTED),
    )
    client.subscribe_setpoints(on_message=make_setpoints_callback(worker))
    return client

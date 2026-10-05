"""Anbindung an Home Assistant (WebSocket-Trigger) und an den Broker (MQTT).

WS- und paho-Thread stellen nur Ereignisse in den Regel-Worker ein und aendern nie selbst
Zustand."""
import json
import logging

from heizungsbruecke.ha_trigger_client import HaTriggerClient
from heizungsbruecke.mqtt_client import BridgeMqttClient
from heizungsbruecke.runtime import (
    EV_AUTH_REJECTED,
    EV_HA_CONNECTED,
    EV_LOCAL_CHECK,
    EV_MQTT_CONNECTED,
    EV_SETPOINTS,
)
from smartheat_runtime.runtime_config import RuntimeConfig
from smartheat_runtime.worker import Event, RegulationWorker
from smartheat_transport.connect import connect_options

logger = logging.getLogger(__name__)

# Stabilitaetsfenster, bevor eine room_target-Aenderung als final gilt: Boost und Tick sollen
# nicht auf einen Zwischenwert reagieren, waehrend jemand das Thermostat verstellt.
ROOM_TARGET_DEBOUNCE_SECONDS = 10


def _strip_attribute_suffix(entity_id: str) -> str:
    """`climate.wz::temperature` -> `climate.wz`: ein Trigger braucht die echte Entity-ID,
    `::attribut` ist eine Konvention nur dieses Add-ons (siehe ha_api.get_state)."""
    real_entity_id, _, _ = entity_id.partition("::")
    return real_entity_id


def _extract_attribute_suffix(entity_id: str) -> str | None:
    _, _, attribute = entity_id.partition("::")
    return attribute or None


def _state_trigger(raw_entity_id: str) -> dict:
    trigger = {"platform": "state", "entity_id": _strip_attribute_suffix(raw_entity_id)}
    attribute = _extract_attribute_suffix(raw_entity_id)
    if attribute:
        trigger["attribute"] = attribute
    return trigger


def make_trigger_event_callback(manifest, worker: RegulationWorker):
    """Stellt pro Trigger nur einen (koaleszierten) lokalen Check ein. Den entprellten
    room_target-Trigger erkennt der Callback an entity_id UND attribute: room_actual und
    room_target koennen dieselbe Entity sein (Thermostat mit current_temperature/temperature)."""
    room_target_entity_id = None
    room_target_attribute = None
    if "room_target" in manifest.entity_ids:
        room_target_entity_id = _strip_attribute_suffix(manifest.entity_ids["room_target"])
        room_target_attribute = _extract_attribute_suffix(manifest.entity_ids["room_target"])

    def _on_trigger_event(trigger: dict) -> None:
        room_target_fired = (
            room_target_entity_id is not None
            and trigger.get("platform") == "state"
            and trigger.get("entity_id") == room_target_entity_id
            and trigger.get("attribute") == room_target_attribute
        )
        worker.post_coalesced(EV_LOCAL_CHECK, room_target_fired=room_target_fired)
    return _on_trigger_event


def build_ha_trigger_client(manifest, options: dict, ha_api, worker: RegulationWorker) -> HaTriggerClient:
    trigger_list = []
    if "room_target" in manifest.entity_ids:
        trigger_list.append({
            **_state_trigger(manifest.entity_ids["room_target"]),
            "for": {"seconds": ROOM_TARGET_DEBOUNCE_SECONDS},
        })
    if "room_actual" in manifest.entity_ids:
        trigger_list.append(_state_trigger(manifest.entity_ids["room_actual"]))
    daily_trigger_time = options.get("daily_trigger_time")
    if daily_trigger_time:
        trigger_list.append({"platform": "time", "at": daily_trigger_time})

    def _on_connected() -> None:
        # Bei jeder (Re-)Verbindung Cache frisch lesen und pruefen (eine Sollwertaenderung waehrend
        # der Trennung wird sofort verarbeitet) und den vollen Status per Event neu senden (nach
        # einem HA-Neustart hat die Integration ihn nicht mehr).
        worker.post_coalesced(EV_LOCAL_CHECK, room_target_fired=True)
        worker.post_coalesced(EV_HA_CONNECTED)

    return HaTriggerClient(
        ws_url=ha_api.websocket_url(), token=ha_api.token, triggers=trigger_list,
        on_trigger_event=make_trigger_event_callback(manifest, worker),
        on_connected=_on_connected,
    )


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
    client = BridgeMqttClient(
        connect_options(config.descriptor, config.credential), config.tenant_id, transport_kind=config.descriptor.kind,
        on_auth_rejected=lambda _client: worker.post_coalesced(EV_AUTH_REJECTED),
        on_connected=lambda _client: worker.post_coalesced(EV_MQTT_CONNECTED),
    )
    client.subscribe_setpoints(on_message=make_setpoints_callback(worker))
    return client

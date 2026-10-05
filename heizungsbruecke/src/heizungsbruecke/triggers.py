"""Anbindung an Home Assistant (WebSocket-Trigger).

Der WS-Thread stellt nur Ereignisse in den Regel-Worker ein und aendert nie selbst Zustand."""
from heizungsbruecke.ha_trigger_client import HaTriggerClient
from smartheat_runtime.runtime import EV_LOCAL_CHECK, EV_SOURCE_CONNECTED
from smartheat_runtime.worker import RegulationWorker

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
        worker.post_coalesced(EV_SOURCE_CONNECTED)

    return HaTriggerClient(
        ws_url=ha_api.websocket_url(), token=ha_api.token, triggers=trigger_list,
        on_trigger_event=make_trigger_event_callback(manifest, worker),
        on_connected=_on_connected,
    )

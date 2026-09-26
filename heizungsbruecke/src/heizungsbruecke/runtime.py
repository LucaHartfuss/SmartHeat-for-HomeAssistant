"""Laufzeit-Kontext der Bruecke und Ereignisarten des Regel-Workers.

Importiert zur Laufzeit nichts aus dem Paket, damit regulation/ticks/abo/triggers ihn ohne
Zyklus nutzen koennen."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from heizungsbruecke.ha_api import HomeAssistantApi
    from heizungsbruecke.ha_trigger_client import HaTriggerClient
    from heizungsbruecke.manifest import ChannelManifest
    from heizungsbruecke.mqtt_client import BridgeMqttClient
    from heizungsbruecke.override import Override
    from heizungsbruecke.state import StateStore
    from heizungsbruecke.worker import RegulationWorker

EV_LOCAL_CHECK = "local_check"
EV_SETPOINTS = "setpoints"
EV_AUTH_REJECTED = "auth_rejected"
EV_ACK_TIMEOUT = "ack_timeout"
EV_RETRY_DUE = "retry_due"
EV_WATCHDOG = "watchdog"
EV_TELEMETRY = "telemetry"
EV_DAYNIGHT = "daynight"
EV_GRACE_CHECK = "grace_check"
EV_MQTT_CONNECTED = "mqtt_connected"


@dataclass
class Runtime:
    """Wird nur im Worker-Thread benutzt. Zustand liegt ausschliesslich in `store`, Schreiben
    auf die Anlage ausschliesslich ueber `override`."""
    manifest: ChannelManifest
    ha_api: HomeAssistantApi
    options: dict
    derived_entity_ids: dict
    worker: RegulationWorker
    store: StateStore
    override: Override
    mqtt_client: BridgeMqttClient | None = None
    trigger_client: HaTriggerClient | None = None
